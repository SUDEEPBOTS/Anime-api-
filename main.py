import os
import time
import requests
from fastapi import FastAPI, Request, HTTPException, Depends, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from groq import Groq
from bson import ObjectId

# 1. SETUP & CONFIG
load_dotenv()
app = FastAPI(title="Anime Discovery API")
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")

# DB Connection
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = client.anime_db
collection = db.links

# AI Setup (Groq)
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# --- SECURITY (Admin Login) ---
def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = os.getenv("ADMIN_USER")
    correct_pass = os.getenv("ADMIN_PASS")
    if credentials.username != correct_user or credentials.password != correct_pass:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- HELPER FUNCTIONS ---

def get_hd_anime_info(anime_name):
    """Jikan API se HD Photo aur Synopsis layega"""
    try:
        # Jikan Free API
        url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=1"
        res = requests.get(url).json()
        data = res['data'][0]
        return {
            "title": data.get('title_english', data['title']),
            "synopsis": data.get('synopsis', 'No description available.')[:300] + "...",
            "image": data['images']['jpg']['large_image_url']
        }
    except Exception as e:
        print(f"Jikan Error: {e}")
        return None

def google_search_api(query):
    """Google Custom Search API se Link layega"""
    api_key = os.getenv("GOOGLE_API_KEY")
    cx = os.getenv("GOOGLE_CX_ID")
    
    # Query optimized for Telegram Channels
    search_query = f"{query} hindi dubbed telegram channel t.me"
    
    url = f"https://www.googleapis.com/customsearch/v1?key={api_key}&cx={cx}&q={search_query}"
    
    try:
        data = requests.get(url).json()
        if 'items' in data:
            # Top result return karega
            return data['items'][0]['link']
        return "https://t.me/" # Fallback
    except Exception as e:
        print(f"Google API Error: {e}")
        return "https://t.me/"

# --- API ROUTES (PUBLIC) ---

@app.get("/")
def home():
    return {"message": "Anime API is Online. Use /api/search?query=AnimeName"}

@app.get("/api/search")
async def search_anime(query: str):
    start = time.time()
    clean_query = query.lower().strip()

    # STEP 1: Check Database (Cache)
    cached = await collection.find_one({"search_term": clean_query})
    
    if cached:
        return {
            "status": "success",
            "source": "database",
            "data": {
                "title": cached['title'],
                "link": cached['generated_url'],
                "views": cached.get('views', 0)
            },
            "response_time": f"{time.time() - start:.2f}s"
        }

    # STEP 2: Not in DB -> Use AI to Clean Name
    try:
        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": f"Extract the official anime name only from this query: '{query}'. Output ONLY the name, nothing else."}],
            model="llama3-8b-8192",
        )
        ai_name = chat.choices[0].message.content.strip()
    except:
        ai_name = query # Fallback if AI fails

    # STEP 3: Fetch Info (Jikan) & Link (Google)
    info = get_hd_anime_info(ai_name)
    
    if not info:
        return {"status": "error", "message": "Anime details not found."}
    
    tg_link = google_search_api(info['title'])

    # STEP 4: Save to Database
    slug = clean_query.replace(" ", "-")
    view_url = f"{os.getenv('BASE_URL')}/view/{slug}"
    
    new_data = {
        "search_term": clean_query,
        "title": info['title'],
        "synopsis": info['synopsis'],
        "thumbnail": info['image'],
        "telegram_link": tg_link,
        "generated_url": view_url,
        "views": 0, "likes": 0, "dislikes": 0, "reports": 0
    }
    
    await collection.insert_one(new_data)

    return {
        "status": "success", 
        "source": "fetched_new", 
        "data": {"title": info['title'], "link": view_url},
        "response_time": f"{time.time() - start:.2f}s"
    }

# --- WEBSITE ROUTES (VIEW PAGE) ---

@app.get("/view/{slug}", response_class=HTMLResponse)
async def view_page(slug: str, request: Request):
    search_term = slug.replace("-", " ")
    anime = await collection.find_one({"search_term": search_term})
    
    if not anime:
        return templates.TemplateResponse("404.html", {"request": request}) # 404 page bana lena optionally

    # Increment Views
    await collection.update_one({"_id": anime["_id"]}, {"$inc": {"views": 1}})

    return templates.TemplateResponse("view.html", {"request": request, "anime": anime})

# Action Route (Like/Dislike/Report)
@app.post("/api/action/{slug}/{action}")
async def user_action(slug: str, action: str):
    search_term = slug.replace("-", " ")
    valid_actions = ["likes", "dislikes", "reports"]
    
    if action in valid_actions:
        await collection.update_one({"search_term": search_term}, {"$inc": {action: 1}})
        return {"status": "ok", "action": action}
    return {"status": "error", "message": "Invalid action"}

# --- ADMIN PANEL ROUTES ---

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, username: str = Depends(get_current_username)):
    # Fetch all animes sorted by newest first
    animes = await collection.find().sort("_id", -1).to_list(200)
    return templates.TemplateResponse("admin.html", {
        "request": request, 
        "animes": animes, 
        "user": username
    })

# Manual Add / Update Route
@app.post("/admin/add")
async def add_anime_manual(
    search_keyword: str = Form(...),
    title: str = Form(...),
    thumbnail: str = Form(...),
    telegram_link: str = Form(...),
    synopsis: str = Form("No description added."),
    username: str = Depends(get_current_username)
):
    clean_query = search_keyword.lower().strip()
    slug = clean_query.replace(" ", "-")
    view_url = f"{os.getenv('BASE_URL')}/view/{slug}"

    # Check if exists (Update vs Insert)
    existing = await collection.find_one({"search_term": clean_query})
    
    data_payload = {
        "search_term": clean_query,
        "title": title,
        "thumbnail": thumbnail,
        "telegram_link": telegram_link,
        "synopsis": synopsis,
        "generated_url": view_url
    }

    if existing:
        # Update existing
        await collection.update_one({"search_term": clean_query}, {"$set": data_payload})
    else:
        # Create new default fields
        data_payload.update({"views": 0, "likes": 0, "dislikes": 0, "reports": 0})
        await collection.insert_one(data_payload)

    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/delete/{anime_id}")
async def delete_anime(anime_id: str, username: str = Depends(get_current_username)):
    try:
        await collection.delete_one({"_id": ObjectId(anime_id)})
    except:
        pass # Handle invalid ID error
    return RedirectResponse(url="/admin", status_code=303)
