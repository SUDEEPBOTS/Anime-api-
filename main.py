import os
import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from groq import Groq
from googlesearch import search
import requests

# 1. LOAD CONFIG
load_dotenv()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 2. DATABASE SETUP
client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db = client.anime_db
collection = db.links

# 3. AI & TOOLS SETUP
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# --- HELPER FUNCTIONS ---

def get_hd_anime_info(anime_name):
    """Jikan API se HD Photo aur Title layega"""
    try:
        url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=1"
        response = requests.get(url).json()
        data = response['data'][0]
        return {
            "title": data['title_english'] if data.get('title_english') else data['title'],
            "synopsis": data['synopsis'][:150] + "...", # Thodi si story
            "image": data['images']['jpg']['large_image_url']
        }
    except:
        return None

def find_telegram_link(anime_name):
    """Google se Telegram link dhundega"""
    query = f"{anime_name} hindi dubbed telegram channel t.me"
    try:
        # Google search se top 5 result me se pehla t.me link uthayega
        for url in search(query, num_results=5):
            if "t.me/" in url:
                return url
        return "https://t.me/" # Fallback agar nahi mila
    except:
        return "https://t.me/"

# --- API ROUTES ---

@app.get("/")
def home():
    return {"message": "Anime API is Running. Use /api/search?query=Naruto"}

@app.get("/api/search")
async def search_anime(query: str, request: Request):
    start = time.time()
    clean_query = query.lower().strip()

    # 1. DATABASE CHECK (Cache)
    cached = await collection.find_one({"search_term": clean_query})
    
    if cached:
        return {
            "status": "success",
            "source": "database",
            "data": {
                "title": cached['title'],
                "link": cached['generated_url'],
                "views": cached.get('views', 0)
            }
        }

    # 2. AGAR DB MEIN NAHI HAI -> PROCESS START
    
    # A. AI se Query fix karwate hain (Optional step for accuracy)
    chat_completion = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": "Extract only the main Anime name from the user input. Output ONLY the name."},
            {"role": "user", "content": query}
        ],
        model="llama3-8b-8192",
    )
    ai_anime_name = chat_completion.choices[0].message.content.strip()

    # B. HD Info Fetch (Image & Story)
    anime_info = get_hd_anime_info(ai_anime_name)
    if not anime_info:
        return {"status": "error", "message": "Anime not found"}

    # C. Telegram Link Fetch
    tg_link = find_telegram_link(anime_info['title'])

    # D. Save to DB
    slug = clean_query.replace(" ", "-")
    view_url = f"{os.getenv('BASE_URL')}/view/{slug}"
    
    new_entry = {
        "search_term": clean_query,
        "title": anime_info['title'],
        "synopsis": anime_info['synopsis'],
        "thumbnail": anime_info['image'],
        "telegram_link": tg_link,
        "generated_url": view_url,
        "views": 0,
        "likes": 0,
        "dislikes": 0
    }
    
    await collection.insert_one(new_entry)

    return {
        "status": "success",
        "source": "new_fetched",
        "data": {
            "title": new_entry['title'],
            "link": view_url
        },
        "time": f"{time.time() - start:.2f}s"
    }

# --- WEBSITE INTERFACE ---

@app.get("/view/{slug}", response_class=HTMLResponse)
async def view_page(slug: str, request: Request):
    search_term = slug.replace("-", " ")
    anime = await collection.find_one({"search_term": search_term})
    
    if not anime:
        return "Anime Not Found"

    # View Count Update
    await collection.update_one({"_id": anime["_id"]}, {"$inc": {"views": 1}})

    return templates.TemplateResponse("view.html", {"request": request, "anime": anime})

# Like/Dislike/Report Logic (Shortened for brevity)
@app.post("/api/action/{slug}/{action}")
async def user_action(slug: str, action: str):
    search_term = slug.replace("-", " ")
    if action in ["likes", "dislikes", "reports"]:
        await collection.update_one({"search_term": search_term}, {"$inc": {action: 1}})
        return {"status": "ok"}
    return {"status": "error"}
  
