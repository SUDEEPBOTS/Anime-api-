import os
import time
import requests
from fastapi import FastAPI, Request, HTTPException, Depends, status, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from groq import Groq
from bson import ObjectId

# 1. SETUP
load_dotenv()
app = FastAPI()
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")

# DB & AI
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = client.anime_db
collection = db.links
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# --- HELPER: SMALL CAPS CONVERTER ---
def to_small_caps(text):
    mapping = {
        'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ꜰ', 'g': 'ɢ', 'h': 'ʜ', 'i': 'ɪ',
        'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ', 'p': 'ᴘ', 'q': 'ǫ', 'r': 'ʀ',
        's': 's', 't': 'ᴛ', 'u': 'ᴜ', 'v': 'ᴠ', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ',
        '0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄', '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉'
    }
    return "".join(mapping.get(char, char) for char in text.lower())

# --- HELPER: TELEGRAM NOTIFICATION (UPDATED) ---
def send_telegram_log(title, thumbnail, synopsis, view_link):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_LOGGER_ID")
    
    if not token or not chat_id: return

    # Convert Synopsis to Small Caps
    sc_synopsis = to_small_caps(synopsis[:250] + "...")
    
    # HTML Caption: 
    # 1. Title is the Blue Link
    # 2. Story in Small Caps
    caption = (
        f"<b><a href='{view_link}'>{title.upper()}</a></b>\n\n"
        f"📖 {sc_synopsis}"
    )

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": thumbnail,
        "caption": caption,
        "parse_mode": "HTML",
        "has_spoiler": True  # <--- Photo Spoiler (Blur) kar dega
    }
    try: requests.post(url, json=payload)
    except Exception as e: print(f"Telegram Log Error: {e}")

# --- HELPER: GET USER IP ---
def get_client_ip(request: Request):
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded: return x_forwarded.split(",")[0]
    return request.client.host

# --- HELPER: GOOGLE SEARCH (STRICT TELEGRAM ONLY) ---
def google_search_api(query):
    api_key = os.getenv("GOOGLE_API_KEY")
    cx = os.getenv("GOOGLE_CX_ID")
    
    # "site:t.me" forces Google to return only Telegram links
    search_query = f"{query} hindi dubbed site:t.me"
    
    url = f"https://www.googleapis.com/customsearch/v1?key={api_key}&cx={cx}&q={search_query}"
    valid_links = []
    
    try:
        data = requests.get(url).json()
        if 'items' in data:
            # Check top 10 results to find 4 valid ones
            for item in data['items']:
                link = item['link']
                
                # Strict Python Filter: Must contain t.me and NOT contain facebook/instagram
                if "t.me/" in link and "facebook.com" not in link and "instagram.com" not in link:
                    valid_links.append(link)
                
                # Stop once we have 4 good links
                if len(valid_links) >= 4:
                    break
    except Exception as e:
        print(f"Google Error: {e}")
    
    # Fallback
    if not valid_links: valid_links.append("https://t.me/")
    return valid_links

def get_hd_anime_info(anime_name):
    try:
        url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=1"
        res = requests.get(url).json()
        data = res['data'][0]
        return {
            "title": data.get('title_english', data['title']),
            "synopsis": data.get('synopsis', 'No desc')[:300] + "...",
            "image": data['images']['jpg']['large_image_url']
        }
    except: return None

# --- SECURITY ---
def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = os.getenv("ADMIN_USER")
    correct_pass = os.getenv("ADMIN_PASS")
    if credentials.username != correct_user or credentials.password != correct_pass:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

# --- ROUTES ---

# UPTIME FIX: Allow both HEAD and GET requests
@app.head("/")
@app.get("/")
def home(): 
    return {"message": "Anime API is Online"}

@app.get("/api/search")
async def search_anime(query: str):
    start = time.time()
    clean_query = query.lower().strip()

    # 1. Check DB
    cached = await collection.find_one({"search_term": clean_query})
    if cached:
        links = cached.get('telegram_links', [cached.get('telegram_link', '#')])
        return {
            "status": "success", 
            "source": "database",
            "data": {
                "title": cached['title'], 
                "links": links,
                "website_link": cached['generated_url']
            },
            "response_time": f"{time.time() - start:.2f}s"
        }

    # 2. AI Clean Name
    try:
        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": f"Extract official anime name from '{query}'. Output ONLY name."}],
            model="llama-3.3-70b-versatile",
        )
        ai_name = chat.choices[0].message.content.strip()
    except: ai_name = query

    # 3. Fetch Data
    info = get_hd_anime_info(ai_name)
    if not info: return {"status": "error", "message": "Not Found"}
    
    # 4. Get Strict Telegram Links
    tg_links = google_search_api(info['title'])
    
    slug = clean_query.replace(" ", "-")
    view_url = f"{os.getenv('BASE_URL')}/view/{slug}"
    
    new_data = {
        "search_term": clean_query,
        "title": info['title'],
        "synopsis": info['synopsis'],
        "thumbnail": info['image'],
        "telegram_links": tg_links,
        "generated_url": view_url,
        "views": 0, "likes": 0, "dislikes": 0, "reports": 0,
        "liked_ips": [], "disliked_ips": []
    }
    
    await collection.insert_one(new_data)
    
    # 5. SEND TELEGRAM LOG (SPOILER + LINKED TITLE)
    send_telegram_log(info['title'], info['image'], info['synopsis'], view_url)

    return {
        "status": "success", 
        "source": "fetched_new", 
        "data": {
            "title": info['title'], 
            "links": tg_links,
            "website_link": view_url
        },
        "response_time": f"{time.time() - start:.2f}s"
    }

@app.get("/view/{slug}", response_class=HTMLResponse)
async def view_page(slug: str, request: Request):
    search_term = slug.replace("-", " ")
    anime = await collection.find_one({"search_term": search_term})
    if not anime: return templates.TemplateResponse("404.html", {"request": request})

    if "telegram_links" not in anime:
        anime["telegram_links"] = [anime.get("telegram_link", "#")]

    cookie_name = f"viewed_{slug}"
    response = templates.TemplateResponse("view.html", {"request": request, "anime": anime})
    
    if not request.cookies.get(cookie_name):
        await collection.update_one({"_id": anime["_id"]}, {"$inc": {"views": 1}})
        response.set_cookie(key=cookie_name, value="true", max_age=86400)
        
    return response

# Action Route
@app.post("/api/action/{slug}/{action}")
async def user_action(slug: str, action: str, request: Request):
    search_term = slug.replace("-", " ")
    user_ip = get_client_ip(request)
    anime = await collection.find_one({"search_term": search_term})
    if not anime: return {"status": "error"}

    liked_ips = anime.get("liked_ips", [])
    disliked_ips = anime.get("disliked_ips", [])

    if action == "likes":
        if user_ip in liked_ips:
            await collection.update_one({"search_term": search_term}, {"$pull": {"liked_ips": user_ip}, "$inc": {"likes": -1}})
            return {"status": "removed_like"}
        else:
            update = {"$addToSet": {"liked_ips": user_ip}, "$inc": {"likes": 1}}
            if user_ip in disliked_ips:
                update["$pull"] = {"disliked_ips": user_ip}
                update["$inc"]["dislikes"] = -1
            await collection.update_one({"search_term": search_term}, update)
            return {"status": "liked"}

    elif action == "dislikes":
        if user_ip in disliked_ips:
            await collection.update_one({"search_term": search_term}, {"$pull": {"disliked_ips": user_ip}, "$inc": {"dislikes": -1}})
            return {"status": "removed_dislike"}
        else:
            update = {"$addToSet": {"disliked_ips": user_ip}, "$inc": {"dislikes": 1}}
            if user_ip in liked_ips:
                update["$pull"] = {"liked_ips": user_ip}
                update["$inc"]["likes"] = -1
            await collection.update_one({"search_term": search_term}, update)
            return {"status": "disliked"}

    elif action == "reports":
        await collection.update_one({"search_term": search_term}, {"$inc": {"reports": 1}})
        return {"status": "reported"}
    
    return {"status": "ok"}

# --- ADMIN ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, username: str = Depends(get_current_username)):
    animes = await collection.find().sort("_id", -1).to_list(200)
    return templates.TemplateResponse("admin.html", {"request": request, "animes": animes, "user": username})

@app.post("/admin/add")
async def add_anime_manual(
    search_keyword: str = Form(...),
    title: str = Form(...),
    thumbnail: str = Form(...),
    telegram_link: str = Form(...),
    synopsis: str = Form("Manual Add"),
    username: str = Depends(get_current_username)
):
    clean_query = search_keyword.lower().strip()
    slug = clean_query.replace(" ", "-")
    view_url = f"{os.getenv('BASE_URL')}/view/{slug}"
    
    links_list = [telegram_link] 

    existing = await collection.find_one({"search_term": clean_query})
    data = {
        "search_term": clean_query,
        "title": title,
        "thumbnail": thumbnail,
        "telegram_links": links_list,
        "synopsis": synopsis,
        "generated_url": view_url
    }

    if existing:
        await collection.update_one({"search_term": clean_query}, {"$set": data})
    else:
        data.update({"views": 0, "likes": 0, "dislikes": 0, "reports": 0, "liked_ips": [], "disliked_ips": []})
        await collection.insert_one(data)
    
    # Log manually added anime too
    send_telegram_log(title, thumbnail, synopsis, view_url)
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/delete/{anime_id}")
async def delete_anime(anime_id: str, username: str = Depends(get_current_username)):
    try: await collection.delete_one({"_id": ObjectId(anime_id)})
    except: pass 
    return RedirectResponse(url="/admin", status_code=303)
