"""Cine Chaska bot.

python bot.py draft    -> news chuno, poster + caption banao, Telegram pe approval ke liye bhejo
python bot.py approve  -> Telegram buttons check karo, Approve hua to Facebook page pe post karo
"""
import os, sys, io, json, glob, hashlib, time, datetime as dt
import requests, feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (compatible; CineChaskaBot/1.0)"
D = "data"
W, H = 1080, 1350  # 4:5

TG = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FB_PAGE = os.environ.get("FB_PAGE_ID", "")
FB_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
GRAPH = os.environ.get("GRAPH_VER", "v23.0")
FORCE = os.environ.get("FORCE") == "1"  # window ke bahar test karne ke liye

MAX_PENDING = 6        # itne drafts approval ke wait me ho to naya nahi banega
EXPIRE_HOURS = 4       # itne ghante baad draft purana maana jayega
RECO_AFTER_HOUR = 20   # roz 8 baje ke baad ek "aaj kya dekhein" post

# Ye feeds test karke dekh lena; jo kaam na kare wo skip ho jata hai
FEEDS = [
    "https://www.hindustantimes.com/feeds/rss/entertainment/bollywood/rssfeed.xml",
    "https://indianexpress.com/section/entertainment/bollywood/feed/",
    "https://feeds.feedburner.com/ndtvmovies-bollywood",
    "https://www.koimoi.com/feed/",
    "https://www.bollywoodhungama.com/rss/news.xml",
    "https://www.pinkvilla.com/feed",
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
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
        },
        timeout=120,
    )
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])

STYLE = (
    "Tum 'Cine Chaska' naam ke Bollywood news Facebook page ke editor ho. "
    "Tone: masaledar, chatpati, youth wali, lekin sirf diye gaye facts use karo, koi naya fact ya quote mat banao. "
    "Caption Hinglish (Roman Hindi) me, 4-6 chhoti lines, kuch emojis, aakhir me ek sawal jisse comments aaye."
)

def pick_news(cands, recent):
    lst = "\n".join(f"{i}. {c['title']} | {c['summary']}" for i, c in enumerate(cands))
    rec = "\n".join(recent[-30:]) or "(koi nahi)"
    prompt = (
        STYLE
        + "\n\nNeeche khabrein hain. Sabse clickable ek chuno (movie release, OTT, gossip, celeb life, outfit)."
        + " Agar koi khabar 'recent titles' jaisi hi ho to usse mat chuno. Agar sab bekaar hain to index -1 do."
        + "\n\nKHABREIN:\n" + lst + "\n\nRECENT TITLES:\n" + rec
        + '\n\nSirf JSON do: {"index": int, "headline": "Hindi (Devanagari) hook headline, max 12 shabd", '
        '"caption": "Hinglish caption", "hashtags": ["#..", "#.."] (6-8 hashtags)}'
    )
    return gemini(prompt)

def regen_caption(ctx):
    prompt = (
        STYLE + f"\n\nKhabar: {ctx['title']} | {ctx['summary']}\nPehle se alag angle ka naya caption likho."
        + '\nSirf JSON: {"caption": "...", "hashtags": ["#..", ...]}'
    )
    return gemini(prompt)

def reco_json(ctx_titles):
    prompt = (
        STYLE + "\n\nAaj ke liye 5 movie/web-series recommend karo jo Netflix, Prime Video, JioHotstar, ZEE5 ya SonyLIV pe dekhi ja sakti hon. "
        "Sirf wo titles do jinke bare me tumhe pakka pata ho. Recent entertainment headlines context ke liye:\n"
        + "\n".join(ctx_titles[:15])
        + '\n\nSirf JSON: {"items": [{"title": "...", "platform": "...", "line": "ek line kyun dekhein (Hindi, chhoti)"}] (5 items), '
        '"caption": "Hinglish caption jisme 5 ki list ho", "hashtags": ["#..", ...]}'
    )
    return gemini(prompt)

# ---------------------------------------------------------------- news
def entry_image(e):
    for k in ("media_content", "media_thumbnail"):
        for m in e.get(k) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []):
        if l.get("type", "").startswith("image"):
            return l.get("href")
    return None

def og_image(url):
    try:
        h = requests.get(url, headers={"User-Agent": UA}, timeout=20).text
        m = BeautifulSoup(h, "html.parser").find("meta", property="og:image")
        return m["content"] if m and m.get("content") else None
    except Exception:
        return None

def candidates(seen):
    out = []
    for f in FEEDS:
        try:
            d = feedparser.parse(f, agent=UA)
        except Exception:
            continue
        for e in d.entries[:15]:
            u = e.get("link")
            if not u or u in seen:
                continue
            out.append({
                "title": e.get("title", "").strip(),
                "url": u,
                "summary": BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")[:350],
                "img": entry_image(e),
                "ts": time.mktime(e["published_parsed"]) if e.get("published_parsed") else 0,
            })
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out[:10]

def download(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")

# ---------------------------------------------------------------- poster
def find_font():
    if os.path.exists("assets/font.ttf"):
        return "assets/font.ttf"
    fs = glob.glob("/usr/share/fonts/**/NotoSansDevanagari*.ttf", recursive=True)
    fs.sort(key=lambda p: ("Bold" not in p, len(p)))
    if fs:
        return fs[0]
    fs = glob.glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True)
    return fs[0] if fs else None

FONT = find_font()

def font(sz):
    try:
        return ImageFont.truetype(FONT, sz, layout_engine=ImageFont.Layout.RAQM)
    except Exception:
        return ImageFont.truetype(FONT, sz)

def wrap(d, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def cover(img, w, h):
    r = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1))
    x = (img.width - w) // 2
    y = (img.height - h) // 3  # thoda upar se crop, chehre bachte hain
    return img.crop((x, y, x + w, y + h))

def add_logo(img):
    if os.path.exists("assets/logo.png"):
        lg = Image.open("assets/logo.png").convert("RGBA")
        lg.thumbnail((220, 220))
        img.paste(lg, (W - lg.width - 40, 40), lg)

def to_jpeg(img):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=92)
    return b.getvalue()

def make_poster(photo, headline):
    base = cover(photo, W, H).convert("RGBA")
    d = ImageDraw.Draw(base, "RGBA")
    f = font(64)
    lines = wrap(d, headline, f, W - 170)[:4]
    lh = 96
    box_h = len(lines) * lh + 70
    top = H - box_h - 70
    d.rectangle([40, top, W - 40, top + box_h], fill=(0, 0, 0, 190))
    d.rectangle([40, top, 54, top + box_h], fill=(255, 196, 0, 255))
    y = top + 30
    for l in lines:
        d.text((85, y), l, font=f, fill="white")
        y += lh
    base = base.convert("RGB")
    add_logo(base)
    return to_jpeg(base)

def make_reco_poster(items):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for yy in range(H):
        c = int(60 * yy / H)
        d.line([(0, yy), (W, yy)], fill=(25 + c, 8, 55 + c))
    d.text((70, 90), "आज क्या देखें?", font=font(96), fill=(255, 196, 0))
    y = 270
    for i, it in enumerate(items[:5], 1):
        d.text((70, y), f"{i}. {it['title']}", font=font(54), fill="white")
        sub = f"{it['platform']}  •  {it['line']}"
        sub_lines = wrap(d, sub, font(34), W - 140)
        d.text((100, y + 72), sub_lines[0], font=font(34), fill=(215, 205, 235))
        y += 200
    add_logo(img)
    return to_jpeg(img)

# ---------------------------------------------------------------- telegram
def tg(method, **kw):
    r = requests.post(f"https://api.telegram.org/bot{TG}/{method}", timeout=90, **kw)
    r.raise_for_status()
    return r.json()["result"]

def keyboard(did):
    return json.dumps({"inline_keyboard": [
        [{"text": "✅ Approve", "callback_data": f"ok:{did}"},
         {"text": "❌ Reject", "callback_data": f"no:{did}"}],
        [{"text": "🔁 Caption dobara", "callback_data": f"re:{did}"}],
    ]})

def send_draft(did, jpeg, caption, label):
    res = tg("sendPhoto", data={"chat_id": CHAT}, files={"photo": ("p.jpg", jpeg)})
    file_id = res["photo"][-1]["file_id"]
    tg("sendMessage", data={
        "chat_id": CHAT,
        "text": f"{label}\n\n{caption}"[:4000],
        "reply_markup": keyboard(did),
    })
    return file_id

def full_caption(g):
    return g["caption"].strip() + "\n\n" + " ".join(g.get("hashtags", []))

# ---------------------------------------------------------------- modes
def in_window(now):
    return 6 <= now.hour < 23 or (now.hour == 23 and now.minute < 20)

def mode_draft():
    now = dt.datetime.now(IST)
    if not FORCE and not in_window(now):
        print("window ke bahar")
        return
    pending = load("pending", {})
    pending = {k: v for k, v in pending.items() if time.time() - v["created"] < EXPIRE_HOURS * 3600}
    if len(pending) >= MAX_PENDING:
        print("bahut drafts pending hain")
        save("pending", pending)
        return

    state = load("state", {})
    seen = load("seen", [])
    recent = load("recent", [])
    today = str(now.date())

    # roz ek recommendation post
    if now.hour >= RECO_AFTER_HOUR and state.get("reco") != today:
        cands = candidates(set(seen))
        g = reco_json([c["title"] for c in cands])
        jpeg = make_reco_poster(g["items"])
        did = "r" + hashlib.md5(today.encode()).hexdigest()[:7]
        cap = full_caption(g)
        fid = send_draft(did, jpeg, cap, "🎬 Aaj ke 5 recommendations (titles/platform check kar lena)")
        pending[did] = {"id": did, "caption": cap, "file_id": fid, "created": time.time(), "kind": "reco",
                        "ctx": {"title": "Aaj ke 5 recommendations", "summary": json.dumps(g["items"], ensure_ascii=False)}}
        state["reco"] = today
        save("state", state)
        save("pending", pending)
        return

    cands = candidates(set(seen))
    if not cands:
        print("nayi khabar nahi")
        save("pending", pending)
        return
    g = pick_news(cands, recent)
    idx = g.get("index", -1)
    if idx < 0 or idx >= len(cands):
        print("kuch kaam ka nahi mila")
        return
    c = cands[idx]
    seen.append(c["url"])
    save("seen", seen[-2000:])
    img_url = c["img"] or og_image(c["url"])
    if not img_url:
        print("image nahi mili, skip")
        return
    try:
        photo = download(img_url)
    except Exception as e:
        print("image download fail:", e)
        return
    jpeg = make_poster(photo, g["headline"])
    did = hashlib.md5(c["url"].encode()).hexdigest()[:8]
    cap = full_caption(g)
    fid = send_draft(did, jpeg, cap, f"📰 {c['title']}\n{c['url']}")
    pending[did] = {"id": did, "caption": cap, "file_id": fid, "created": time.time(), "kind": "news", "ctx": c}
    recent.append(c["title"])
    save("recent", recent[-40:])
    save("pending", pending)

def post_fb(dr):
    f = tg("getFile", data={"file_id": dr["file_id"]})
    img = requests.get(f"https://api.telegram.org/file/bot{TG}/{f['file_path']}", timeout=60).content
    r = requests.post(
        f"https://graph.facebook.com/{GRAPH}/{FB_PAGE}/photos",
        data={"caption": dr["caption"], "access_token": FB_TOKEN},
        files={"source": ("p.jpg", img)},
        timeout=120,
    )
    if not r.ok:
        raise RuntimeError(r.text[:300])
    return r.json()

def clear_buttons(mid):
    tg("editMessageReplyMarkup", data={"chat_id": CHAT, "message_id": mid,
                                       "reply_markup": json.dumps({"inline_keyboard": []})})

def say(text):
    tg("sendMessage", data={"chat_id": CHAT, "text": text[:4000]})

def mode_approve():
    state = load("state", {})
    pending = load("pending", {})
    ups = tg("getUpdates", data={"offset": state.get("offset", 0), "timeout": 0,
                                 "allowed_updates": json.dumps(["callback_query"])})
    for u in ups:
        state["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if not cq or str(cq["message"]["chat"]["id"]) != str(CHAT):
            continue
        try:
            tg("answerCallbackQuery", data={"callback_query_id": cq["id"]})
        except Exception:
            pass
        act, _, did = cq["data"].partition(":")
        mid = cq["message"]["message_id"]
        dr = pending.get(did)
        if not dr:
            try:
                clear_buttons(mid)
            except Exception:
                pass
            continue
        try:
            if act == "no":
                pending.pop(did)
                clear_buttons(mid)
                say("❌ Reject kar diya")
            elif act == "ok":
                post_fb(dr)
                pending.pop(did)
                clear_buttons(mid)
                say("✅ Page pe post ho gaya")
            elif act == "re":
                g = regen_caption(dr["ctx"])
                dr["caption"] = full_caption(g)
                tg("editMessageText", data={"chat_id": CHAT, "message_id": mid,
                                            "text": f"🔁 Naya caption\n\n{dr['caption']}"[:4000],
                                            "reply_markup": keyboard(did)})
        except Exception as e:
            say(f"⚠️ Error ({act}): {str(e)[:300]}")
    state_keep = load("state", {})
    state_keep["offset"] = state.get("offset", 0)
    save("state", state_keep)
    save("pending", pending)

if __name__ == "__main__":
    {"draft": mode_draft, "approve": mode_approve}[sys.argv[1]]()
