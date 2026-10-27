"""Cine Chaska bot.

python bot.py draft    -> news chuno, poster + caption banao, Telegram pe approval ke liye bhejo
python bot.py approve  -> Telegram buttons check karo, Approve hua to Facebook page pe post karo
"""
import os, sys, io, re, json, glob, hashlib, time, subprocess, datetime as dt
import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (compatible; CineChaskaBot/1.0)"
D = "data"
W, H = 1080, 1350  # 4:5

TG = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
# pehla model na chale (404/429/503) to agla try hota hai
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.5-flash", "gemini-3.5-flash-lite",
                      "gemini-3.1-flash-lite", "gemini-3-flash-preview"] if m]
FB_PAGE = os.environ.get("FB_PAGE_ID", "")
FB_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
GRAPH = os.environ.get("GRAPH_VER", "v23.0")
IG_ID = os.environ.get("IG_USER_ID", "")  # khali ho to Instagram skip
GROUP = os.environ.get("GROUP_CHAT_ID", "")      # Telegram group ki id (khali ho to group me nahi bhejega)
GROUP_WHEN = os.environ.get("GROUP_WHEN", "approve")  # "approve" = post hone ke baad, "draft" = draft bante hi
FORCE = os.environ.get("FORCE") == "1"  # window ke bahar test karne ke liye

MAX_PENDING = 6        # itne drafts approval ke wait me ho to naya nahi banega
EXPIRE_HOURS = 4       # itne ghante baad draft purana maana jayega
RECO_AFTER_HOUR = 20   # roz 8 baje ke baad ek "aaj kya dekhein" post

# Ye feeds test karke dekh lena; jo kaam na kare wo skip ho jata hai
FEEDS = [
    # General Bollywood news
    "https://www.hindustantimes.com/feeds/rss/entertainment/bollywood/rssfeed.xml",
    "https://indianexpress.com/section/entertainment/bollywood/feed/",
    "https://feeds.feedburner.com/ndtvmovies-bollywood",
    "https://www.koimoi.com/feed/",
    "https://www.bollywoodhungama.com/rss/news.xml",
    "https://www.pinkvilla.com/feed",
    # Actress fashion, outfits, photoshoots and viral looks (Google News RSS)
    "https://news.google.com/rss/search?q=Bollywood+actress+black+outfit&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Bollywood+actress+fashion+photoshoot&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Bollywood+actress+viral+photos&hl=en-IN&gl=IN&ceid=IN:en",
    "https://news.google.com/rss/search?q=Bollywood+actress+red+carpet+look&hl=en-IN&gl=IN&ceid=IN:en",
]

# ---------------------------------------------------------------- state
def load(name, default):
    p = f"{D}/{name}.json"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default

def save(name, val):
    os.makedirs(D, exist_ok=True)
    with open(f"{D}/{name}.json", "w", encoding="utf-8") as f:
        json.dump(val, f, ensure_ascii=False, indent=1)

# ---------------------------------------------------------------- gemini
def gemini(prompt):
    last = None
    for m in MODELS:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
            },
            timeout=120,
        )
        if r.status_code in (404, 429, 503):
            last = f"{m}: {r.status_code} {r.text[:150]}"
            print("model skip ->", last)
            continue
        r.raise_for_status()
        print("gemini model:", m)
        return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    raise RuntimeError("Koi Gemini model nahi chala. Last: " + str(last))

STYLE = (
    "Tum 'Cine Chaska' naam ke Bollywood news Facebook page ke editor ho. "
    "Tone: masaledar, chatpati, youth wali, lekin sirf diye gaye facts use karo, koi naya fact ya quote mat banao. "
    "Celeb fashion/look news me kapde, event aur fan reaction tabhi likho jab source me confirm ho. "
    "Poora text (headline aur caption) shuddh Hindi me, Devanagari lipi me, bol-chal wali chatpati bhasha. English ke aam shabd (OTT, KBC, trailer, ad) Roman me chalenge. ZARURI: ek hi shabd me Hindi aur English akshar kabhi mix mat karo. Ya to poora Devanagari (जैसे बिलियनेयर, सीरीज़) ya poora Roman (Billionaire). Series/film ka naam English ho to poora Roman me likho. Caption 4-6 chhoti lines, kuch emojis, aakhir me ek sawal jisse comments aaye."
)

def pick_news(cands, recent):
    lst = "\n".join(f"{i}. {c['title']} | {c['summary'][:300]}" for i, c in enumerate(cands))
    rec = "\n".join(recent[-30:]) or "(koi nahi)"
    prompt = (
        STYLE
