import os
import json
import logging
import asyncio
import difflib
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse
from starlette.routing import Route
import uvicorn

from pymongo import MongoClient
import certifi

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
MONGO_URI = os.environ.get("MONGO_URI")
DB_FILE = "data.json"
LANG_FILE = "lang.json"
META_FILE = "meta.json"
USERS_FILE = "users.json"

PAGE_SIZE = 10

# ---------- ডাটা লোড/সেভ (MongoDB থাকলে সেটা ব্যবহার হবে, নাহলে লোকাল ফাইল) ----------
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where()) if MONGO_URI else None
mongo_state = mongo_client["telegram_bot"]["state"] if mongo_client else None

def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_state(key, path, default):
    if mongo_state is not None:
        doc = mongo_state.find_one({"_id": key})
        return doc["data"] if doc else default
    return load_json(path)

def save_state(key, path, data):
    if mongo_state is not None:
        mongo_state.update_one({"_id": key}, {"$set": {"data": data}}, upsert=True)
    else:
        save_json(path, data)

db = load_state("content", DB_FILE, {})            # { "title": { "quality": "link" } }
user_lang = load_state("languages", LANG_FILE, {})  # { "user_id": "lang_code" }
title_meta = load_state("meta", META_FILE, {})      # { "title": {"added_at": iso_string} }
known_users = set(load_state("users", USERS_FILE, []))

# শুধু এই সেশনে চালু থাকা, রিস্টার্টে মুছে যাওয়া অস্থায়ী ডাটা
last_search_results = {}   # user_id -> [title, ...]  (পেজিনেশনের জন্য)
pending_request = {}       # user_id -> query text     (রিকোয়েস্ট বাটনের জন্য)
browse_state = {}          # user_id -> {"title":..., "path":[...], "children":[...]}  (সিজন/এপিসোড নেভিগেশনের জন্য)

def register_user(user_id: int):
    if user_id not in known_users:
        known_users.add(user_id)
        save_state("users", USERS_FILE, list(known_users))

# ---------- ভাষা ----------
LANGUAGES = {
    "en": "English",
    "hi": "हिंदी",
    "bn": "বাংলা",
    "ta": "தமிழ்",
    "te": "తెలుగు",
    "mr": "मराठी",
    "gu": "ગુજરાતી",
}

TEXTS = {
    "en": {
        "choose_language": "Select your language:",
        "language_set": "Language set to English.",
        "search_prompt": "Now type a movie or song name to search.",
        "results": "Results:",
        "not_found": "Not found.",
        "select_quality": "Select quality:",
        "content_unavailable": "This content is no longer available.",
        "link_not_found": "Link not found.",
        "download_link": "Download link:",
        "request_button": "Request this title",
        "request_sent": "Your request has been sent to the admin.",
        "thank_you": "Thank you for using the bot! Enjoy watching.",
        "select_option": "Select:",
    },
    "hi": {
        "choose_language": "अपनी भाषा चुनें:",
        "language_set": "भाषा हिंदी में सेट कर दी गई है।",
        "search_prompt": "अब खोजने के लिए मूवी या गाने का नाम लिखें।",
        "results": "परिणाम:",
        "not_found": "कुछ नहीं मिला।",
        "select_quality": "क्वालिटी चुनें:",
        "content_unavailable": "यह सामग्री अब उपलब्ध नहीं है।",
        "link_not_found": "लिंक नहीं मिला।",
        "download_link": "डाउनलोड लिंक:",
        "request_button": "यह टाइटल रिक्वेस्ट करें",
        "request_sent": "आपका अनुरोध एडमिन को भेज दिया गया है।",
        "thank_you": "बॉट इस्तेमाल करने के लिए धन्यवाद! देखने का आनंद लें।",
        "select_option": "चुनें:",
    },
    "bn": {
        "choose_language": "আপনার ভাষা নির্বাচন করুন:",
        "language_set": "ভাষা বাংলায় সেট করা হয়েছে।",
        "search_prompt": "এখন মুভি বা গানের নাম লিখে সার্চ করুন।",
        "results": "রেজাল্ট:",
        "not_found": "কিছু পাওয়া যায়নি।",
        "select_quality": "কোয়ালিটি সিলেক্ট করো:",
        "content_unavailable": "এই কনটেন্ট আর পাওয়া যাচ্ছে না।",
        "link_not_found": "লিংক পাওয়া যায়নি।",
        "download_link": "ডাউনলোড লিংক:",
        "request_button": "এই টাইটেল রিকোয়েস্ট করো",
        "request_sent": "তোমার রিকোয়েস্ট অ্যাডমিনের কাছে পাঠানো হয়েছে।",
        "thank_you": "বট ব্যবহার করার জন্য ধন্যবাদ! উপভোগ করো।",
        "select_option": "সিলেক্ট করো:",
    },
    "ta": {
        "choose_language": "உங்கள் மொழியைத் தேர்ந்தெடுக்கவும்:",
        "language_set": "மொழி தமிழாக அமைக்கப்பட்டது.",
        "search_prompt": "இப்போது தேட திரைப்படம் அல்லது பாடலின் பெயரை தட்டச்சு செய்யவும்.",
        "results": "முடிவுகள்:",
        "not_found": "எதுவும் கிடைக்கவில்லை.",
        "select_quality": "தரத்தைத் தேர்ந்தெடுக்கவும்:",
        "content_unavailable": "இந்த உள்ளடக்கம் இனி கிடைக்கவில்லை.",
        "link_not_found": "இணைப்பு கிடைக்கவில்லை.",
        "download_link": "பதிவிறக்க இணைப்பு:",
        "request_button": "இந்த தலைப்பை கோரவும்",
        "request_sent": "உங்கள் கோரிக்கை நிர்வாகிக்கு அனுப்பப்பட்டது.",
        "thank_you": "பாட்டைப் பயன்படுத்தியதற்கு நன்றி! பார்த்து மகிழுங்கள்.",
        "select_option": "தேர்ந்தெடுக்கவும்:",
    },
    "te": {
        "choose_language": "మీ భాషను ఎంచుకోండి:",
        "language_set": "భాష తెలుగుగా సెట్ చేయబడింది.",
        "search_prompt": "ఇప్పుడు శోధించడానికి సినిమా లేదా పాట పేరు టైప్ చేయండి.",
        "results": "ఫలితాలు:",
        "not_found": "ఏమీ కనుగొనబడలేదు.",
        "select_quality": "క్వాలిటీని ఎంచుకోండి:",
        "content_unavailable": "ఈ కంటెంట్ ఇకపై అందుబాటులో లేదు.",
        "link_not_found": "లింక్ కనుగొనబడలేదు.",
        "download_link": "డౌన్‌లోడ్ లింక్:",
        "request_button": "ఈ టైటిల్ అభ్యర్థించండి",
        "request_sent": "మీ అభ్యర్థన అడ్మిన్‌కు పంపబడింది.",
        "thank_you": "బాట్ ఉపయోగించినందుకు ధన్యవాదాలు! ఆనందించండి.",
        "select_option": "ఎంచుకోండి:",
    },
    "mr": {
        "choose_language": "तुमची भाषा निवडा:",
        "language_set": "भाषा मराठी सेट केली आहे.",
        "search_prompt": "आता शोधण्यासाठी चित्रपट किंवा गाण्याचे नाव टाइप करा.",
        "results": "निकाल:",
        "not_found": "काही सापडले नाही.",
        "select_quality": "गुणवत्ता निवडा:",
        "content_unavailable": "ही सामग्री यापुढे उपलब्ध नाही.",
        "link_not_found": "लिंक सापडली नाही.",
        "download_link": "डाउनलोड लिंक:",
        "request_button": "हे शीर्षक विनंती करा",
        "request_sent": "तुमची विनंती अॅडमिनला पाठवली आहे.",
        "thank_you": "बॉट वापरल्याबद्दल धन्यवाद! आनंद घ्या.",
        "select_option": "निवडा:",
    },
    "gu": {
        "choose_language": "તમારી ભાષા પસંદ કરો:",
        "language_set": "ભાષા ગુજરાતી સેટ કરવામાં આવી છે.",
        "search_prompt": "હવે શોધવા માટે મૂવી અથવા ગીતનું નામ ટાઈપ કરો.",
        "results": "પરિણામો:",
        "not_found": "કંઈ મળ્યું નથી.",
        "select_quality": "ગુણવત્તા પસંદ કરો:",
        "content_unavailable": "આ સામગ્રી હવે ઉપલબ્ધ નથી.",
        "link_not_found": "લિંક મળી નથી.",
        "download_link": "ડાઉનલોડ લિંક:",
        "request_button": "આ શીર્ષક વિનંતી કરો",
        "request_sent": "તમારી વિનંતી એડમિનને મોકલવામાં આવી છે.",
        "thank_you": "બોટ વાપરવા બદલ આભાર! માણો.",
        "select_option": "પસંદ કરો:",
    },
}

def get_lang(user_id: int) -> str:
    return user_lang.get(str(user_id), "en")

def t(user_id: int, key: str) -> str:
    lang = get_lang(user_id)
    return TEXTS.get(lang, TEXTS["en"])[key]

def find_existing_title(title: str) -> str:
    normalized = title.strip().lower()
    for existing in db.keys():
        if existing.strip().lower() == normalized:
            return existing
    return title

def is_leaf_level(node: dict) -> bool:
    """node-এর ভ্যালুগুলো স্ট্রিং (লিংক) হলে এটাই শেষ ধাপ (কোয়ালিটি লেভেল);
    ভ্যালুগুলো dict হলে আরও গভীরে যেতে হবে (যেমন সিজন -> এপিসোড)।"""
    if not node:
        return True
    return isinstance(next(iter(node.values())), str)

def fuzzy_search(query: str, titles, limit: int = 200):
    query = query.strip().lower()
    if not query:
        return []

    exact = [t for t in titles if query in t.lower()]
    if exact:
        return exact[:limit]

    scored = []
    for title in titles:
        title_lower = title.lower()
        best_ratio = difflib.SequenceMatcher(None, query, title_lower).ratio()
        for word in title_lower.split():
            best_ratio = max(best_ratio, difflib.SequenceMatcher(None, query, word).ratio())
        if best_ratio >= 0.6:
            scored.append((best_ratio, title))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [title for _, title in scored[:limit]]

def build_results_keyboard(titles, page: int = 0):
    start = page * PAGE_SIZE
    page_titles = titles[start:start + PAGE_SIZE]
    buttons = [[InlineKeyboardButton(title, callback_data=f"title::{title}")] for title in page_titles]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"page::{page - 1}"))
    if start + PAGE_SIZE < len(titles):
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"page::{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    return InlineKeyboardMarkup(buttons)

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id)
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"lang::{code}")]
        for code, name in LANGUAGES.items()
    ]
    await update.message.reply_text(
        "Welcome! / स्वागत है! / স্বাগতম!\n"
        "Select your language / अपनी भाषा चुनें / আপনার ভাষা নির্বাচন করুন:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang_code = query.data.split("::", 1)[1]
    user_lang[str(query.from_user.id)] = lang_code
    save_state("languages", LANG_FILE, user_lang)

    texts = TEXTS[lang_code]
    await query.edit_message_text(f"{texts['language_set']}\n{texts['search_prompt']}")

# ---------- অ্যাডমিন: লিংক যোগ করা ----------
async def add_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            "ফরম্যাট ভুল। এভাবে লিখো:\n"
            "/add টাইটেল | কোয়ালিটি | লিংক\n"
            "উদাহরণ: /add Amar Movie | 720 | https://terabox.com/xyz"
        )
        return

    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 3 or not all(segments):
        await update.message.reply_text("ফরম্যাট ভুল। এভাবে লিখো:\n/add টাইটেল | কোয়ালিটি | লিংক")
        return

    title, quality, link = segments
    existing_title = find_existing_title(title)
    db.setdefault(existing_title, {})[quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)

    await update.message.reply_text(f"যোগ হয়েছে ✅\nটাইটেল: {existing_title}\nকোয়ালিটি: {quality}")

async def add_series_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            "ফরম্যাট ভুল। এভাবে লিখো:\n"
            "/addseries টাইটেল | সিজন | এপিসোড | কোয়ালিটি | লিংক\n"
            "উদাহরণ: /addseries Money Heist | Season 1 | Episode 1 | 720 | https://drive.google.com/xyz"
        )
        return

    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 5 or not all(segments):
        await update.message.reply_text(
            "ফরম্যাট ভুল। এভাবে লিখো:\n"
            "/addseries টাইটেল | সিজন | এপিসোড | কোয়ালিটি | লিংক"
        )
        return

    title, season, episode, quality, link = segments
    existing_title = find_existing_title(title)
    db.setdefault(existing_title, {})
    db[existing_title].setdefault(season, {})
    db[existing_title][season].setdefault(episode, {})
    db[existing_title][season][episode][quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)

    await update.message.reply_text(
        f"যোগ হয়েছে ✅\nটাইটেল: {existing_title}\nসিজন: {season}\nএপিসোড: {episode}\nকোয়ালিটি: {quality}"
    )

async def remove_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("এভাবে লিখো:\n/remove টাইটেল")
        return

    title = parts[1].strip()
    if title in db:
        del db[title]
        save_state("content", DB_FILE, db)
        if title in title_meta:
            del title_meta[title]
            save_state("meta", META_FILE, title_meta)
        await update.message.reply_text(f"ডিলিট হয়েছে ✅: {title}")
    else:
        await update.message.reply_text("এই টাইটেল পাওয়া যায়নি।")

async def list_titles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    titles = list(db.keys())
    if not titles:
        await update.message.reply_text("এখনো কোনো টাইটেল যোগ করা হয়নি।")
        return
    shown = titles[:100]
    text = f"মোট টাইটেল: {len(titles)}\n\n" + "\n".join(f"• {x}" for x in shown)
    if len(titles) > 100:
        text += f"\n... আরও {len(titles) - 100}টা আছে"
    await update.message.reply_text(text)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total_titles = len(db)
    total_links = sum(len(q) for q in db.values())
    total_users = len(known_users)
    await update.message.reply_text(
        f"📊 পরিসংখ্যান\nটাইটেল: {total_titles}\nমোট লিংক: {total_links}\nইউজার: {total_users}"
    )

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("এভাবে লিখো:\n/broadcast তোমার মেসেজ")
        return
    message = parts[1].strip()
    sent, failed = 0, 0
    for user_id in list(known_users):
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"পাঠানো হয়েছে ✅\nসফল: {sent}\nব্যর্থ: {failed}")

async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid)
    titles_sorted = sorted(
        db.keys(),
        key=lambda x: title_meta.get(x, {}).get("added_at", ""),
        reverse=True
    )[:10]
    if not titles_sorted:
        await update.message.reply_text(t(uid, "not_found"))
        return
    last_search_results[uid] = titles_sorted
    await update.message.reply_text(t(uid, "results"), reply_markup=build_results_keyboard(titles_sorted, 0))

# ---------- সার্চ ----------
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_query = update.message.text.strip()
    if not raw_query:
        return
    uid = update.effective_user.id
    register_user(uid)

    matches = fuzzy_search(raw_query, db.keys())

    if not matches:
        pending_request[uid] = raw_query
        buttons = [[InlineKeyboardButton(t(uid, "request_button"), callback_data="request")]]
        await update.message.reply_text(t(uid, "not_found"), reply_markup=InlineKeyboardMarkup(buttons))
        return

    last_search_results[uid] = matches
    await update.message.reply_text(t(uid, "results"), reply_markup=build_results_keyboard(matches, 0))

async def paginate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    page = int(query.data.split("::", 1)[1])
    titles = last_search_results.get(uid, [])
    if not titles:
        return
    await query.edit_message_reply_markup(reply_markup=build_results_keyboard(titles, page))

async def request_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    text = pending_request.get(uid)
    if text and ADMIN_ID:
        username = query.from_user.username or query.from_user.first_name or str(uid)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🔔 নতুন রিকোয়েস্ট @{username} (id: {uid}) থেকে:\n{text}"
            )
        except Exception:
            pass
    await query.edit_message_text(t(uid, "request_sent"))

# ---------- টাইটেল সিলেক্ট করলে (মুভি হলে কোয়ালিটি, সিরিজ হলে সিজন দেখানো) ----------
async def show_qualities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    title = query.data.split("::", 1)[1]

    node = db.get(title, {})
    if not node:
        await query.edit_message_text(t(uid, "content_unavailable"))
        return

    if is_leaf_level(node):
        # সাধারণ মুভি/গান — সরাসরি কোয়ালিটি দেখাও
        buttons = [
            [InlineKeyboardButton(q, callback_data=f"get::{title}::{q}")]
            for q in node.keys()
        ]
        await query.edit_message_text(
            f"{title}\n{t(uid, 'select_quality')}", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        # সিরিজ — সিজন (বা পরের ধাপ) দেখাও
        browse_state[uid] = {"title": title, "path": [], "children": list(node.keys())}
        buttons = [
            [InlineKeyboardButton(k, callback_data=f"nav::{i}")]
            for i, k in enumerate(node.keys())
        ]
        await query.edit_message_text(
            f"{title}\n{t(uid, 'select_option')}", reply_markup=InlineKeyboardMarkup(buttons)
        )

async def navigate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    state = browse_state.get(uid)
    if not state:
        return

    idx = int(query.data.split("::", 1)[1])
    if idx >= len(state["children"]):
        return
    chosen_key = state["children"][idx]
    state["path"] = state["path"] + [chosen_key]

    node = db.get(state["title"], {})
    for key in state["path"]:
        node = node.get(key, {})

    if not node:
        await query.edit_message_text(t(uid, "content_unavailable"))
        return

    label = f"{state['title']} - {' / '.join(state['path'])}"

    if is_leaf_level(node):
        state["children"] = list(node.keys())
        buttons = [
            [InlineKeyboardButton(q, callback_data=f"getnav::{i}")]
            for i, q in enumerate(node.keys())
        ]
        await query.edit_message_text(
            f"{label}\n{t(uid, 'select_quality')}", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        state["children"] = list(node.keys())
        buttons = [
            [InlineKeyboardButton(k, callback_data=f"nav::{i}")]
            for i, k in enumerate(node.keys())
        ]
        await query.edit_message_text(
            f"{label}\n{t(uid, 'select_option')}", reply_markup=InlineKeyboardMarkup(buttons)
        )

async def send_file_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    state = browse_state.get(uid)
    if not state:
        return

    idx = int(query.data.split("::", 1)[1])
    if idx >= len(state["children"]):
        return
    quality = state["children"][idx]

    node = db.get(state["title"], {})
    for key in state["path"]:
        node = node.get(key, {})
    link = node.get(quality)

    if not link:
        await query.message.reply_text(t(uid, "link_not_found"))
        return

    label = f"{state['title']} - {' / '.join(state['path'])} ({quality})"
    await query.message.reply_text(
        f"{label}\n{t(uid, 'download_link')}\n{link}\n\n{t(uid, 'thank_you')}"
    )

# ---------- কোয়ালিটি সিলেক্ট করলে ডাউনলোড লিংক পাঠানো (সাধারণ মুভি/গান) ----------
async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    _, title, quality = query.data.split("::", 2)

    link = db.get(title, {}).get(quality)
    if not link:
        await query.message.reply_text(t(uid, "link_not_found"))
        return

    await query.message.reply_text(
        f"{title} ({quality})\n{t(uid, 'download_link')}\n{link}\n\n{t(uid, 'thank_you')}"
    )

# ---------- webhook মোড (Render-এর জন্য) ----------
async def run_webhook_server(application: Application, base_url: str, port: int):
    await application.bot.set_webhook(url=f"{base_url}/{BOT_TOKEN}")

    async def telegram_webhook(request: Request) -> Response:
        data = await request.json()
        update = Update.de_json(data=data, bot=application.bot)
        await application.update_queue.put(update)
        return Response()

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    starlette_app = Starlette(routes=[
        Route("/", health, methods=["GET"]),
        Route(f"/{BOT_TOKEN}", telegram_webhook, methods=["POST"]),
    ])

    server = uvicorn.Server(
        config=uvicorn.Config(app=starlette_app, host="0.0.0.0", port=port, log_level="info")
    )

    async with application:
        await application.start()
        await server.serve()
        await application.stop()

# ---------- মেইন ----------
def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_content))
    application.add_handler(CommandHandler("addseries", add_series_content))
    application.add_handler(CommandHandler("remove", remove_content))
    application.add_handler(CommandHandler("list", list_titles))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("latest", latest))
    application.add_handler(CallbackQueryHandler(set_language, pattern=r"^lang::"))
    application.add_handler(CallbackQueryHandler(show_qualities, pattern=r"^title::"))
    application.add_handler(CallbackQueryHandler(send_file, pattern=r"^get::"))
    application.add_handler(CallbackQueryHandler(navigate, pattern=r"^nav::"))
    application.add_handler(CallbackQueryHandler(send_file_nav, pattern=r"^getnav::"))
    application.add_handler(CallbackQueryHandler(paginate, pattern=r"^page::"))
    application.add_handler(CallbackQueryHandler(request_title, pattern=r"^request$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
    return application

def main():
    application = build_application()

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    port = os.environ.get("PORT")

    if render_url and port:
        asyncio.run(run_webhook_server(application, render_url, int(port)))
    else:
        application.run_polling()

if __name__ == "__main__":
    main()
