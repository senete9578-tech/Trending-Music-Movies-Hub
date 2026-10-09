import os
import json
import logging
import asyncio
import difflib
import re
import random
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup as _OrigMarkup, BotCommand, ForceReply
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, TypeHandler, ApplicationHandlerStop
)

_SINGLE_ROW_KEEP = ("navback", "catback", "begin", "request", "page::", "reset_")

def _regroup_rows(rows):
    """পাশাপাশি থাকা একক-বাটনের সারিগুলোকে দুটো করে এক সারিতে বসায় (ব্যাক/পেজ বাটন আলাদা থাকে)।"""
    out, run = [], []
    def flush():
        for i in range(0, len(run), 2):
            out.append(list(run[i:i + 2]))
        run.clear()
    for row in rows:
        row = list(row)
        cb = getattr(row[0], "callback_data", None) if len(row) == 1 else None
        if len(row) == 1 and isinstance(cb, str) and not cb.startswith(_SINGLE_ROW_KEEP):
            run.append(row[0])
        else:
            flush()
            out.append(row)
    flush()
    return out

def _fit(btn, limit=22):
    txt = btn.text
    if len(txt) <= limit:
        return btn
    return InlineKeyboardButton(txt[:limit - 1].rstrip() + "…", callback_data=btn.callback_data, url=btn.url)

class InlineKeyboardMarkup(_OrigMarkup):
    def __init__(self, inline_keyboard, *args, **kwargs):
        rows = _regroup_rows(inline_keyboard)
        rows = [[_fit(b) for b in r] if len(r) == 2 else r for r in rows]
        super().__init__(rows, *args, **kwargs)

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
SETTINGS_FILE = "settings.json"
REQUESTS_FILE = "requests.json"
LATEST_FILE = "latest.json"
USER_NAMES_FILE = "user_names.json"

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
bot_settings = load_state("settings", SETTINGS_FILE, {})   # { "loading_animations": [{"file_id":..., "type": "sticker"|"animation"}, ...] }
pending_requests = load_state("requests", REQUESTS_FILE, {})   # normalized query text -> [user_id, ...] (যারা এটা খুঁজে না পেয়ে রিকোয়েস্ট করেছে)
latest_titles = load_state("latest", LATEST_FILE, {})   # title -> মার্ক করার সময় (অ্যাডমিন নিজে হাতে "Latest"-এ যোগ করা টাইটেল)
user_names = load_state("user_names", USER_NAMES_FILE, {})   # str(user_id) -> display name (username/full name)
SEARCH_HISTORY_FILE = "search_history.json"
POSTERS_FILE = "posters.json"
DOWNLOAD_HISTORY_FILE = "download_history.json"
user_search_history = load_state("search_history", SEARCH_HISTORY_FILE, {})   # str(user_id) -> [query, ...] (সর্বশেষ কয়েকটা)
title_posters = load_state("posters", POSTERS_FILE, {})   # title -> ছবির file_id অথবা URL
CATEGORY_FILE = "categories.json"
category_tree = load_state("categories", CATEGORY_FILE, {})   # নেস্টেড ফোল্ডার, প্রতিটা নোডে "_titles" কী-তে আসল টাইটেলের লিস্ট থাকে
category_browse_state = {}   # user_id -> {"path": [...], "children": [("folder"|"title", name), ...]}
category_return_path = {}    # user_id -> category path to return to when Back is pressed from an opened title
awaiting_poster = {}   # admin এখন কোন টাইটেলের জন্য ছবি পাঠাবে (ট্রানজিয়েন্ট)
user_download_history = load_state("download_history", DOWNLOAD_HISTORY_FILE, {})   # str(user_id) -> [title, ...] (সর্বশেষ কয়েকটা)
HISTORY_LIMIT = 15

# ---------- লোডিং/প্রসেসিং টেক্সট-অ্যানিমেশন (একটা মেসেজ নিজেই বদলে বদলে দেখায়, ChatGPT/DeepSeek-স্টাইল) ----------
LOADING_FRAME_SETS = [
    ["⚡", "⚡ ⚡", "⚡ ⚡ ⚡", "🔥 ⚡ 🔥"],
    ["🔍", "🔍 ✨", "✨ 🔍 ✨", "🎬 ✨ 🎬"],
    ["⏳", "⏳ ⏳", "⏳ ⏳ ⏳", "✅"],
    ["🌀", "🌀 🌀", "🌀 🌀 🌀", "🎯"],
]

# শুধু এই সেশনে চালু থাকা, রিস্টার্টে মুছে যাওয়া অস্থায়ী ডাটা
last_search_results = {}   # user_id -> [title, ...]  (পেজিনেশনের জন্য)
pending_request = {}       # user_id -> query text     (রিকোয়েস্ট বাটনের জন্য)
pending_feedback = {}      # user_id -> "feedback" | "support"  (পরের টেক্সট মেসেজ অ্যাডমিনের কাছে যাবে)
typing_tasks = {}          # chat_id -> asyncio.Task    (উত্তর না আসা পর্যন্ত টানা টাইপিং অ্যানিমেশন)
browse_state = {}          # user_id -> {"title":..., "path":[...], "children":[...]}  (সিজন/এপিসোড নেভিগেশনের জন্য)

def register_user(user_id: int, tg_user=None):
    if user_id not in known_users:
        known_users.add(user_id)
        save_state("users", USERS_FILE, list(known_users))
    if tg_user is not None:
        name = f"@{tg_user.username}" if tg_user.username else (tg_user.full_name or str(user_id))
        key = str(user_id)
        if user_names.get(key) != name:
            user_names[key] = name
            save_state("user_names", USER_NAMES_FILE, user_names)

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
        "admin_added": "Added ✅",
        "admin_deleted": "Deleted ✅",
        "admin_migrated": "Migrated ✅",
        "admin_fixed": "Fixed ✅",
        "admin_format_error": "Wrong format. Use:",
        "admin_not_found": "Not found. Check with /list.",
        "admin_already_series": "This is already in series format — no need to migrate.",
        "admin_conflict": "There's a conflicting entry here — fix that first.",
        "title_label": "Title:",
        "season_label": "Season:",
        "episode_label": "Episode:",
        "quality_label": "Quality:",
        "admin_no_titles": "No titles added yet.",
        "admin_total_titles": "Total titles:",
        "admin_more": "...and {n} more",
        "admin_stats_header": "📊 Stats",
        "admin_stats_titles": "Titles:",
        "admin_stats_links": "Total links:",
        "admin_stats_users": "Users:",
        "admin_broadcast_sent": "Sent ✅",
        "admin_broadcast_success": "Success:",
        "admin_broadcast_failed": "Failed:",
        "admin_new_request": "🔔 New request from",
        "back_button": "◀️ Back",
        "admin_loading_set": "Loading animation set ✅",
        "admin_loading_removed": "Loading animation removed ✅",
        "request_fulfilled": "The title you requested is now available:",
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
        "admin_added": "जोड़ दिया गया ✅",
        "admin_deleted": "हटा दिया गया ✅",
        "admin_migrated": "माइग्रेट हो गया ✅",
        "admin_fixed": "ठीक कर दिया गया ✅",
        "admin_format_error": "फॉर्मेट गलत है। इस तरह लिखें:",
        "admin_not_found": "नहीं मिला। /list से जांचें।",
        "admin_already_series": "यह पहले से ही सीरीज़ फॉर्मेट में है — माइग्रेट करने की ज़रूरत नहीं।",
        "admin_conflict": "यहाँ पहले से एक टकराव वाली एंट्री है — पहले उसे ठीक करें।",
        "title_label": "टाइटल:",
        "season_label": "सीज़न:",
        "episode_label": "एपिसोड:",
        "quality_label": "क्वालिटी:",
        "admin_no_titles": "अभी तक कोई टाइटल नहीं जोड़ा गया।",
        "admin_total_titles": "कुल टाइटल:",
        "admin_more": "...और {n} और हैं",
        "admin_stats_header": "📊 आँकड़े",
        "admin_stats_titles": "टाइटल:",
        "admin_stats_links": "कुल लिंक:",
        "admin_stats_users": "यूज़र:",
        "admin_broadcast_sent": "भेज दिया गया ✅",
        "admin_broadcast_success": "सफल:",
        "admin_broadcast_failed": "असफल:",
        "admin_new_request": "🔔 नया अनुरोध",
        "back_button": "◀️ पीछे",
        "admin_loading_set": "लोडिंग एनिमेशन सेट हो गया ✅",
        "admin_loading_removed": "लोडिंग एनिमेशन हटा दिया गया ✅",
        "request_fulfilled": "आपने जो टाइटल रिक्वेस्ट किया था वह अब उपलब्ध है:",
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
        "admin_added": "যোগ হয়েছে ✅",
        "admin_deleted": "ডিলিট হয়েছে ✅",
        "admin_migrated": "মাইগ্রেট হয়েছে ✅",
        "admin_fixed": "ঠিক করা হয়েছে ✅",
        "admin_format_error": "ফরম্যাট ভুল। এভাবে লিখো:",
        "admin_not_found": "পাওয়া যায়নি। /list দিয়ে চেক করো।",
        "admin_already_series": "এটা এমনিতেই সিরিজ ফরম্যাটে আছে — মাইগ্রেট করার দরকার নেই।",
        "admin_conflict": "এখানে আগে থেকেই একটা সাংঘর্ষিক এন্ট্রি আছে — আগে সেটা ঠিক করো।",
        "title_label": "টাইটেল:",
        "season_label": "সিজন:",
        "episode_label": "এপিসোড:",
        "quality_label": "কোয়ালিটি:",
        "admin_no_titles": "এখনো কোনো টাইটেল যোগ করা হয়নি।",
        "admin_total_titles": "মোট টাইটেল:",
        "admin_more": "... আরও {n}টা আছে",
        "admin_stats_header": "📊 পরিসংখ্যান",
        "admin_stats_titles": "টাইটেল:",
        "admin_stats_links": "মোট লিংক:",
        "admin_stats_users": "ইউজার:",
        "admin_broadcast_sent": "পাঠানো হয়েছে ✅",
        "admin_broadcast_success": "সফল:",
        "admin_broadcast_failed": "ব্যর্থ:",
        "admin_new_request": "🔔 নতুন রিকোয়েস্ট",
        "back_button": "◀️ পেছনে",
        "admin_loading_set": "লোডিং অ্যানিমেশন সেট হয়েছে ✅",
        "admin_loading_removed": "লোডিং অ্যানিমেশন সরানো হয়েছে ✅",
        "request_fulfilled": "তুমি যেটা রিকোয়েস্ট করেছিলে সেটা এখন পাওয়া যাচ্ছে:",
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
        "admin_added": "சேர்க்கப்பட்டது ✅",
        "admin_deleted": "நீக்கப்பட்டது ✅",
        "admin_migrated": "மாற்றப்பட்டது ✅",
        "admin_fixed": "சரி செய்யப்பட்டது ✅",
        "admin_format_error": "தவறான வடிவம். இப்படி எழுதவும்:",
        "admin_not_found": "கிடைக்கவில்லை. /list மூலம் சரிபார்க்கவும்.",
        "admin_already_series": "இது ஏற்கனவே சீரிஸ் வடிவத்தில் உள்ளது — மாற்ற வேண்டியதில்லை.",
        "admin_conflict": "இங்கே ஏற்கனவே முரண்பாடான உள்ளீடு உள்ளது — முதலில் அதைச் சரிசெய்யவும்.",
        "title_label": "தலைப்பு:",
        "season_label": "சீசன்:",
        "episode_label": "எபிசோட்:",
        "quality_label": "தரம்:",
        "admin_no_titles": "இதுவரை எந்த தலைப்பும் சேர்க்கப்படவில்லை.",
        "admin_total_titles": "மொத்த தலைப்புகள்:",
        "admin_more": "...மேலும் {n}",
        "admin_stats_header": "📊 புள்ளிவிவரங்கள்",
        "admin_stats_titles": "தலைப்புகள்:",
        "admin_stats_links": "மொத்த இணைப்புகள்:",
        "admin_stats_users": "பயனர்கள்:",
        "admin_broadcast_sent": "அனுப்பப்பட்டது ✅",
        "admin_broadcast_success": "வெற்றி:",
        "admin_broadcast_failed": "தோல்வி:",
        "admin_new_request": "🔔 புதிய கோரிக்கை",
        "back_button": "◀️ பின்",
        "admin_loading_set": "ஏற்றல் அனிமேஷன் அமைக்கப்பட்டது ✅",
        "admin_loading_removed": "ஏற்றல் அனிமேஷன் அகற்றப்பட்டது ✅",
        "request_fulfilled": "நீங்கள் கோரிய தலைப்பு இப்போது கிடைக்கிறது:",
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
        "admin_added": "జోడించబడింది ✅",
        "admin_deleted": "తొలగించబడింది ✅",
        "admin_migrated": "మైగ్రేట్ చేయబడింది ✅",
        "admin_fixed": "సరిచేయబడింది ✅",
        "admin_format_error": "ఫార్మాట్ తప్పు. ఇలా టైప్ చేయండి:",
        "admin_not_found": "కనుగొనబడలేదు. /list తో చెక్ చేయండి.",
        "admin_already_series": "ఇది ఇప్పటికే సిరీస్ ఫార్మాట్‌లో ఉంది — మైగ్రేట్ చేయాల్సిన అవసరం లేదు.",
        "admin_conflict": "ఇక్కడ ఇప్పటికే ఘర్షణ ఉన్న ఎంట్రీ ఉంది — ముందు దాన్ని సరిచేయండి.",
        "title_label": "టైటిల్:",
        "season_label": "సీజన్:",
        "episode_label": "ఎపిసోడ్:",
        "quality_label": "క్వాలిటీ:",
        "admin_no_titles": "ఇంకా ఏ టైటిల్ జోడించబడలేదు.",
        "admin_total_titles": "మొత్తం టైటిల్స్:",
        "admin_more": "...మరో {n}",
        "admin_stats_header": "📊 గణాంకాలు",
        "admin_stats_titles": "టైటిల్స్:",
        "admin_stats_links": "మొత్తం లింక్‌లు:",
        "admin_stats_users": "యూజర్లు:",
        "admin_broadcast_sent": "పంపబడింది ✅",
        "admin_broadcast_success": "విజయవంతం:",
        "admin_broadcast_failed": "విఫలం:",
        "admin_new_request": "🔔 కొత్త అభ్యర్థన",
        "back_button": "◀️ వెనక్కి",
        "admin_loading_set": "లోడింగ్ యానిమేషన్ సెట్ చేయబడింది ✅",
        "admin_loading_removed": "లోడింగ్ యానిమేషన్ తీసివేయబడింది ✅",
        "request_fulfilled": "మీరు అభ్యర్థించిన టైటిల్ ఇప్పుడు అందుబాటులో ఉంది:",
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
        "admin_added": "जोडले गेले ✅",
        "admin_deleted": "हटवले गेले ✅",
        "admin_migrated": "माइग्रेट केले गेले ✅",
        "admin_fixed": "दुरुस्त केले गेले ✅",
        "admin_format_error": "फॉरमॅट चुकीचा आहे. असे लिहा:",
        "admin_not_found": "सापडले नाही. /list ने तपासा.",
        "admin_already_series": "हे आधीच सीरिज फॉरमॅटमध्ये आहे — माइग्रेट करण्याची गरज नाही.",
        "admin_conflict": "इथे आधीच एक विरोधाभासी नोंद आहे — आधी ती दुरुस्त करा.",
        "title_label": "शीर्षक:",
        "season_label": "सीझन:",
        "episode_label": "भाग:",
        "quality_label": "गुणवत्ता:",
        "admin_no_titles": "अजून कोणतेही शीर्षक जोडलेले नाही.",
        "admin_total_titles": "एकूण शीर्षके:",
        "admin_more": "...आणखी {n}",
        "admin_stats_header": "📊 आकडेवारी",
        "admin_stats_titles": "शीर्षके:",
        "admin_stats_links": "एकूण लिंक्स:",
        "admin_stats_users": "युजर्स:",
        "admin_broadcast_sent": "पाठवले गेले ✅",
        "admin_broadcast_success": "यशस्वी:",
        "admin_broadcast_failed": "अयशस्वी:",
        "admin_new_request": "🔔 नवीन विनंती",
        "back_button": "◀️ मागे",
        "admin_loading_set": "लोडिंग अॅनिमेशन सेट केले ✅",
        "admin_loading_removed": "लोडिंग अॅनिमेशन काढले ✅",
        "request_fulfilled": "तुम्ही विनंती केलेले शीर्षक आता उपलब्ध आहे:",
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
        "admin_added": "ઉમેરવામાં આવ્યું ✅",
        "admin_deleted": "ડિલીટ કરવામાં આવ્યું ✅",
        "admin_migrated": "માઇગ્રેટ કરવામાં આવ્યું ✅",
        "admin_fixed": "ઠીક કરવામાં આવ્યું ✅",
        "admin_format_error": "ફોર્મેટ ખોટું છે. આ રીતે લખો:",
        "admin_not_found": "મળ્યું નથી. /list થી ચેક કરો.",
        "admin_already_series": "આ પહેલેથી જ સિરીઝ ફોર્મેટમાં છે — માઇગ્રેટ કરવાની જરૂર નથી.",
        "admin_conflict": "અહીં પહેલેથી જ વિરોધાભાસી એન્ટ્રી છે — પહેલા તેને ઠીક કરો.",
        "title_label": "ટાઈટલ:",
        "season_label": "સીઝન:",
        "episode_label": "એપિસોડ:",
        "quality_label": "ક્વોલિટી:",
        "admin_no_titles": "હજુ સુધી કોઈ ટાઈટલ ઉમેરવામાં આવ્યું નથી.",
        "admin_total_titles": "કુલ ટાઈટલ:",
        "admin_more": "...બીજા {n}",
        "admin_stats_header": "📊 આંકડા",
        "admin_stats_titles": "ટાઈટલ:",
        "admin_stats_links": "કુલ લિંક:",
        "admin_stats_users": "યુઝર્સ:",
        "admin_broadcast_sent": "મોકલવામાં આવ્યું ✅",
        "admin_broadcast_success": "સફળ:",
        "admin_broadcast_failed": "નિષ્ફળ:",
        "admin_new_request": "🔔 નવી વિનંતી",
        "back_button": "◀️ પાછળ",
        "admin_loading_set": "લોડિંગ એનિમેશન સેટ થયું ✅",
        "admin_loading_removed": "લોડિંગ એનિમેશન દૂર કરવામાં આવ્યું ✅",
        "request_fulfilled": "તમે વિનંતી કરેલું ટાઈટલ હવે ઉપલબ્ધ છે:",
    },
}

def get_lang(user_id: int) -> str:
    return user_lang.get(str(user_id), "en")

def t(user_id: int, key: str) -> str:
    lang = get_lang(user_id)
    return TEXTS.get(lang, TEXTS["en"])[key]

def _normalize_label(s):
    return re.sub(r"[\s\-_]+", " ", s.strip().lower()).strip()

def find_key_ci(node, key):
    if not isinstance(node, dict):
        return None
    normalized = _normalize_label(key)
    for k in node.keys():
        if _normalize_label(k) == normalized:
            return k
    return None

def resolve_title_and_path(segments):
    """segments-এর ভেতর কোথায় একটা আসল টাইটেল শুরু হচ্ছে খুঁজে বের করে, তারপর বাকিটুকু
    সেই টাইটেলের ভেতরের নেস্টেড পাথ (যেমন Part 2) হিসেবে মেলানোর চেষ্টা করে।
    রিটার্ন করে (folder_path, matched_title, nested_path) অথবা None।"""
    for i, seg in enumerate(segments):
        matched = find_existing_title(seg)
        if matched in db:
            node = db[matched]
            resolved_path = []
            ok = True
            for label in segments[i + 1:]:
                key = find_key_ci(node, label)
                if key is None:
                    ok = False
                    break
                resolved_path.append(key)
                node = node[key]
            if ok:
                return list(segments[:i]), matched, resolved_path
    return None

def find_existing_title(title: str) -> str:
    normalized = title.strip().lower()
    for existing in db.keys():
        if existing.strip().lower() == normalized:
            return existing
    # হাইফেন/আন্ডারস্কোর/এক্সট্রা স্পেসের পার্থক্য উপেক্ষা করে আরেকবার চেষ্টা
    loose = re.sub(r"[\s\-_]+", " ", normalized)
    for existing in db.keys():
        if re.sub(r"[\s\-_]+", " ", existing.strip().lower()) == loose:
            return existing
    return title

def is_leaf_level(node: dict) -> bool:
    """node-এর ভ্যালুগুলো স্ট্রিং (লিংক) হলে এটাই শেষ ধাপ (কোয়ালিটি লেভেল);
    ভ্যালুগুলো dict হলে আরও গভীরে যেতে হবে (যেমন সিজন -> এপিসোড)।"""
    if not node:
        return True
    return isinstance(next(iter(node.values())), str)

def natural_sort_key(text: str):
    return [int(chunk) if chunk.isdigit() else chunk.lower() for chunk in re.split(r"(\d+)", text)]

def _squash(s):
    return re.sub(r"[^0-9a-z\u0980-\u09ff\u0900-\u097f]+", "", s.lower())

def fuzzy_search(query: str, titles, limit: int = 200):
    query = query.strip().lower()
    titles = list(titles)
    if not query or not titles:
        return []

    sq = _squash(query)
    exact = [t for t in titles if query in t.lower() or (sq and sq in _squash(t))]
    if exact:
        exact.sort(key=lambda x: (0 if x.lower().startswith(query) else 1, natural_sort_key(x)))
        return exact[:limit]

    scored = []
    for title in titles:
        tl = title.lower()
        best = difflib.SequenceMatcher(None, query, tl).ratio()
        for word in re.split(r"[^0-9a-z\u0980-\u09ff\u0900-\u097f]+", tl):
            if word:
                best = max(best, difflib.SequenceMatcher(None, query, word).ratio())
                if word.startswith(query[:2]) and len(query) >= 2:
                    best += 0.1
        scored.append((best, title))
    scored.sort(key=lambda x: x[0], reverse=True)
    good = [t for r, t in scored if r >= 0.5]
    if good:
        return good[:limit]
    return [t for _, t in scored[:6]]   # কিছু না মিললেও সবচেয়ে কাছের কয়েকটা দেখাবে

def grid_rows(flat_buttons, per_row=2):
    """বাটনগুলোকে একটা করে সারির বদলে দুটো করে সারিতে (গ্রিড) সাজায়।"""
    return [flat_buttons[i:i + per_row] for i in range(0, len(flat_buttons), per_row)]

def build_results_keyboard(titles, page: int = 0):
    start = page * PAGE_SIZE
    page_titles = titles[start:start + PAGE_SIZE]
    flat = [InlineKeyboardButton(title, callback_data=f"title::{title}") for title in page_titles]
    buttons = grid_rows(flat, 2)
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
    register_user(update.effective_user.id, update.effective_user)
    buttons = [[InlineKeyboardButton("Follow & Start", callback_data="begin")]]
    await update.message.reply_text(
        "Welcome!",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def begin_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"lang::{code}")]
        for code, name in LANGUAGES.items()
    ]
    await query.edit_message_text(
        "Select your language:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"lang::{code}")]
        for code, name in LANGUAGES.items()
    ]
    await update.message.reply_text(
        "Select your language:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lang_code = query.data.split("::", 1)[1]
    uid = query.from_user.id
    user_lang[str(uid)] = lang_code
    save_state("languages", LANG_FILE, user_lang)

    texts = TEXTS[lang_code]
    await query.edit_message_text(f"{texts['language_set']}\n{texts['search_prompt']}")

    cat_text, cat_markup = render_category_level(uid, [])
    if cat_text and cat_markup.inline_keyboard:
        await context.bot.send_message(chat_id=uid, text=cat_text, reply_markup=cat_markup)

# ---------- সাধারণ ইউজার কমান্ড ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)
    await update.message.reply_text(
        "Just type a movie or song name to search.\n\n"
        "/search <name> - same as typing the name directly\n"
        "/language - change language\n"
        "/reset - reset your language & search state here\n"
        "/latest - see the latest additions\n"
        "/share - share this bot\n"
        "/subscribe - get notified about new releases\n"
        "/feedback <message> - send feedback\n"
        "/support <message> - contact support"
    )

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        uid = update.effective_user.id
        register_user(uid, update.effective_user)
        await update.message.reply_text(t(uid, "search_prompt"))
        return
    await perform_search(update, context, parts[1].strip())

async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)
    me = await context.bot.get_me()
    await update.message.reply_text(f"Share this bot: https://t.me/{me.username}")

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)
    await update.message.reply_text("You're subscribed - you'll get a message here whenever new titles are added.")

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        pending_feedback[uid] = "feedback"
        await update.message.reply_text(
            "Write your feedback or suggestion:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type your feedback here")
        )
        return
    await deliver_feedback(update, context, "feedback", parts[1].strip())

async def deliver_feedback(update, context, kind, text):
    uid = update.effective_user.id
    user = update.effective_user
    name = f"@{user.username}" if user.username else (user.full_name or str(uid))
    label = "Feedback" if kind == "feedback" else "Support request"
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"{label} from {name} (id: {uid}):\n{text}")
    except Exception:
        pass
    await update.message.reply_text("Thanks, your feedback has been sent." if kind == "feedback" else "Thanks, your message has been sent to support.")

async def capture_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/feedback বা /support চাপার পর ইউজারের পরের টেক্সট মেসেজটা অ্যাডমিনের কাছে যায় (সার্চ হিসেবে নয়)।"""
    uid = update.effective_user.id
    kind = pending_feedback.pop(uid, None)
    if kind and update.message and update.message.text:
        await deliver_feedback(update, context, kind, update.message.text.strip())
        await stop_typing(update, context)
        raise ApplicationHandlerStop

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        pending_feedback[uid] = "support"
        await update.message.reply_text(
            "Describe your problem:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type your message here")
        )
        return
    await deliver_feedback(update, context, "support", parts[1].strip())

# ---------- রিসেট (ইউজারের ভাষা/সার্চ স্টেট রিসেট — চ্যাটের মেসেজ ডিলিট করে না, বট কখনো ইউজারের নিজের পাঠানো মেসেজ মুছতে পারে না) ----------
async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [[
        InlineKeyboardButton("Yes", callback_data="reset_yes"),
        InlineKeyboardButton("No", callback_data="reset_no"),
    ]]
    await update.message.reply_text(
        "Reset your language & search state here? Continue?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "reset_yes":
        user_lang.pop(str(uid), None)
        save_state("languages", LANG_FILE, user_lang)
        last_search_results.pop(uid, None)
        pending_request.pop(uid, None)
        browse_state.pop(uid, None)
        pending_feedback.pop(uid, None)
        chat_id = query.message.chat_id
        top_id = query.message.message_id
        # চ্যাটের সব মেসেজ (ইউজারের পাঠানো + বটের) মুছে ফেলা — প্রাইভেট চ্যাটে ৪৮ ঘণ্টার মধ্যের মেসেজ মোছা যায়
        low = max(1, top_id - 3000)
        ids = list(range(top_id, low - 1, -1))
        for i in range(0, len(ids), 100):
            try:
                await context.bot.delete_messages(chat_id=chat_id, message_ids=ids[i:i + 100])
            except Exception:
                pass
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"lang::{code}")]
            for code, name in LANGUAGES.items()
        ]
        await context.bot.send_message(chat_id=chat_id, text="Select your language:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await query.edit_message_text("Cancelled.")

# ---------- অ্যাডমিন: লিংক যোগ করা ----------
async def add_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    uid = update.effective_user.id
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/add Title | Quality | Link\n"
            "Example: /add Amar Movie | 720 | https://terabox.com/xyz"
        )
        return

    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 3 or not all(segments):
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/add Title | Quality | Link")
        return

    title, quality, link = segments
    existing_title = find_existing_title(title)
    db.setdefault(existing_title, {})[quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)
    await notify_fulfilled_requests(context, existing_title)

    await update.message.reply_text(
        f"{t(uid, 'admin_added')}\n{t(uid, 'title_label')} {existing_title}\n{t(uid, 'quality_label')} {quality}"
    )

async def add_series_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    uid = update.effective_user.id
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/addseries Title | Season | Episode | Quality | Link\n"
            "Example: /addseries Money Heist | Season 1 | Episode 1 | 720 | https://drive.google.com/xyz"
        )
        return

    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 5 or not all(segments):
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/addseries Title | Season | Episode | Quality | Link"
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
    await notify_fulfilled_requests(context, existing_title)

    await update.message.reply_text(
        f"{t(uid, 'admin_added')}\n"
        f"{t(uid, 'title_label')} {existing_title}\n"
        f"{t(uid, 'season_label')} {season}\n"
        f"{t(uid, 'episode_label')} {episode}\n"
        f"{t(uid, 'quality_label')} {quality}"
    )

async def add_movie_in_series(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """সিরিজ টাইটেলের নিচে সিজনের পাশাপাশি একটা 'একক' এন্ট্রি (যেমন মূল মুভি) যোগ করে,
    যেটাতে ক্লিক করলে সরাসরি কোয়ালিটি দেখাবে, কোনো এপিসোড ধাপ ছাড়াই।"""
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    uid = update.effective_user.id
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/addmovie Title | Label | Quality | Link\n"
            "Example: /addmovie Daredevil Hindi | Daredevil | 720p | https://drive.google.com/xyz"
        )
        return

    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 4 or not all(segments):
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/addmovie Title | Label | Quality | Link"
        )
        return

    title, label, quality, link = segments
    existing_title = find_existing_title(title)
    db.setdefault(existing_title, {})
    db[existing_title].setdefault(label, {})
    if db[existing_title][label] and not is_leaf_level(db[existing_title][label]):
        await update.message.reply_text(t(uid, "admin_conflict"))
        return
    db[existing_title][label][quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)
    await notify_fulfilled_requests(context, existing_title)

    await update.message.reply_text(
        f"{t(uid, 'admin_added')}\n"
        f"{t(uid, 'title_label')} {existing_title}\n"
        f"{label}\n"
        f"{t(uid, 'quality_label')} {quality}"
    )

async def rename_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """টাইটেলের নাম বা টাইটেলের ভেতরের কোনো লেবেল/সিজনের নাম বদলে দেয়, ডেটা ঠিক একই জায়গায় রেখে।"""
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    text = update.message.text or ""
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/rename Title | New Title\n"
            "or (to rename a label/season inside a title):\n"
            "/rename Title | Old Label | New Label"
        )
        return

    segments = [s.strip() for s in parts[1].split("|")]

    if len(segments) == 2 and all(segments):
        title, new_title = segments
        matched = find_existing_title(title)
        if matched not in db:
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        clashing = find_existing_title(new_title)
        if clashing in db and clashing != matched:
            await update.message.reply_text(t(uid, "admin_conflict"))
            return

        node = db.pop(matched)
        db[new_title] = node
        save_state("content", DB_FILE, db)

        if matched in title_meta:
            meta = title_meta.pop(matched)
            title_meta[new_title] = meta
            save_state("meta", META_FILE, title_meta)

        await update.message.reply_text(f"{t(uid, 'admin_fixed')}\n{matched} → {new_title}")

    elif len(segments) == 3 and all(segments):
        title, old_label, new_label = segments
        matched = find_existing_title(title)
        if matched not in db or old_label not in db[matched]:
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        if new_label in db[matched] and new_label != old_label:
            await update.message.reply_text(t(uid, "admin_conflict"))
            return

        node_data = db[matched].pop(old_label)
        db[matched][new_label] = node_data
        save_state("content", DB_FILE, db)

        await update.message.reply_text(f"{t(uid, 'admin_fixed')}\n{matched} / {old_label} → {new_label}")

    else:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/rename Title | New Title\n"
            "or:\n"
            "/rename Title | Old Label | New Label"
        )

async def migrate_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text or ""
    uid = update.effective_user.id
    parts = text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Paste the old title exactly as you used in /add:\n"
            "/migrate Daredevil Hindi SO2 EO1"
        )
        return

    raw = parts[1].strip()

    if "|" in raw:
        # ম্যানুয়াল মোড — টাইটেলে SO/EO প্যাটার্ন না থাকলে এটা ব্যবহার করো
        segments = [s.strip() for s in raw.split("|")]
        if len(segments) != 4 or not all(segments):
            await update.message.reply_text(
                f"{t(uid, 'admin_format_error')}\n"
                "/migrate Old Title | New Title | Season | Episode"
            )
            return
        old_title, new_title, season_label, episode_label = segments
    else:
        # অটো মোড — টাইটেল থেকে SO<নম্বর> আর EO<নম্বর> নিজে খুঁজে বের করবে
        old_title = raw
        season_match = re.search(r"\bSO\s*(\d+)\b", old_title, re.IGNORECASE)
        episode_match = re.search(r"\bEO\s*(\d+)\b", old_title, re.IGNORECASE)
        if not season_match or not episode_match:
            await update.message.reply_text(
                "Couldn't find an SO/EO pattern (e.g. SO2, EO1) in that title.\n"
                "To do it manually, use:\n"
                "/migrate Old Title | New Title | Season | Episode"
            )
            return
        season_label = f"Season {season_match.group(1)}"
        episode_label = f"Episode {episode_match.group(1)}"
        cutoff = min(season_match.start(), episode_match.start())
        new_title = old_title[:cutoff].strip() or old_title

    matched_old = find_existing_title(old_title)
    if matched_old not in db:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return

    old_node = db[matched_old]
    if not is_leaf_level(old_node):
        await update.message.reply_text(t(uid, "admin_already_series"))
        return

    new_existing_title = find_existing_title(new_title)
    existing_new_node = db.get(new_existing_title, {})
    if existing_new_node and is_leaf_level(existing_new_node):
        await update.message.reply_text(t(uid, "admin_conflict"))
        return

    db.setdefault(new_existing_title, {})
    db[new_existing_title].setdefault(season_label, {})
    if db[new_existing_title][season_label] and is_leaf_level(db[new_existing_title][season_label]):
        await update.message.reply_text(t(uid, "admin_conflict"))
        return
    db[new_existing_title][season_label][episode_label] = old_node

    if matched_old != new_existing_title:
        del db[matched_old]
    save_state("content", DB_FILE, db)

    if matched_old in title_meta and matched_old != new_existing_title:
        del title_meta[matched_old]
    title_meta.setdefault(new_existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)

    await update.message.reply_text(
        f"{t(uid, 'admin_migrated')}\n{matched_old} → {new_existing_title} / {season_label} / {episode_label}"
    )

async def move_episode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n/moveepisode Title | Old Season | Old Episode | New Season | New Episode"
        )
        return
    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 5 or not all(segments):
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n/moveepisode Title | Old Season | Old Episode | New Season | New Episode"
        )
        return
    title, old_season, old_episode, new_season, new_episode = segments
    matched_title = find_existing_title(title)
    if matched_title not in db or old_season not in db[matched_title] or old_episode not in db[matched_title][old_season]:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return

    node = db[matched_title]
    episode_data = node[old_season].pop(old_episode)
    if not node[old_season]:
        del node[old_season]
    node.setdefault(new_season, {})
    node[new_season][new_episode] = episode_data
    save_state("content", DB_FILE, db)

    await update.message.reply_text(
        f"{t(uid, 'admin_fixed')}\n{matched_title} / {old_season} / {old_episode}  →  {matched_title} / {new_season} / {new_episode}"
    )

async def remove_episode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/removeepisode Title | Season | Episode")
        return
    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) != 3 or not all(segments):
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/removeepisode Title | Season | Episode")
        return
    title, season, episode = segments
    matched_title = find_existing_title(title)
    if matched_title not in db or season not in db[matched_title] or episode not in db[matched_title][season]:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return

    del db[matched_title][season][episode]
    if not db[matched_title][season]:
        del db[matched_title][season]
    save_state("content", DB_FILE, db)

    await update.message.reply_text(f"{t(uid, 'admin_deleted')}: {matched_title} / {season} / {episode}")

async def remove_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id

    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/remove Title")
        return

    title = parts[1].strip()
    if title in db:
        del db[title]
        save_state("content", DB_FILE, db)
        if title in title_meta:
            del title_meta[title]
            save_state("meta", META_FILE, title_meta)
        await update.message.reply_text(f"{t(uid, 'admin_deleted')}: {title}")
    else:
        await update.message.reply_text(t(uid, "admin_not_found"))

def format_node_lines(node: dict, path_prefix: str):
    """প্রতিটা লিফ (আসল লিংক) পর্যন্ত পুরো পথ জুড়ে একটাই লাইনে সম্পূর্ণ নাম দেখায়
    (টাইটেল - সিজন - এপিসোড - কোয়ালিটি), আলাদা করে ইনডেন্ট করা ট্রি না।"""
    lines = []
    for key, value in node.items():
        full_name = f"{path_prefix} - {key}"
        if isinstance(value, dict) and value:
            if is_leaf_level(value):
                for q in value.keys():
                    lines.append(f"{full_name} - {q}")
            else:
                lines.extend(format_node_lines(value, full_name))
        else:
            lines.append(full_name)
    return lines

def count_links(node: dict) -> int:
    """একটা টাইটেলের ভেতরের সবগুলো লিংক (কোয়ালিটি) মোট কতগুলো, রিকার্সিভভাবে গোনে।"""
    if not node:
        return 0
    if is_leaf_level(node):
        return len(node)
    return sum(count_links(v) for v in node.values())

async def list_titles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    titles = list(db.keys())
    if not titles:
        await update.message.reply_text(t(uid, "admin_no_titles"))
        return

    total_links = sum(count_links(db[title]) for title in titles)
    all_lines = [
        f"{t(uid, 'admin_total_titles')} {len(titles)}",
        f"{t(uid, 'admin_stats_links')} {total_links}",
        "",
    ]
    for title in titles:
        node = db[title]
        if is_leaf_level(node):
            for q in node.keys():
                all_lines.append(f"{title} - {q}")
        else:
            all_lines.extend(format_node_lines(node, title))
        all_lines.append("")

    # টেলিগ্রামের আসল লিমিট ৪০৯৬ — তাই যতটা সম্ভব একটা মেসেজেই রাখা হয়, শুধু সত্যিকার দরকার হলেই ভাগ হবে
    chunk = ""
    for line in all_lines:
        if len(chunk) + len(line) + 1 > 3900:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk)

from datetime import timedelta
BD_OFFSET = timedelta(hours=5, minutes=30)   # ভারতীয় সময় (IST, UTC+5:30) — স্ট্যাটসে দেখানোর সময় এতে কনভার্ট করা হয়

def to_local_time(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        local = dt + BD_OFFSET
        return local.strftime("%d %b, %I:%M %p")
    except Exception:
        return iso_str[:16].replace("T", " ")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    total_titles = len(db)
    total_links = sum(count_links(node) for node in db.values())
    total_users = len(known_users)

    all_lines = [
        t(uid, "admin_stats_header"),
        f"{t(uid, 'admin_stats_titles')} {total_titles}",
        f"{t(uid, 'admin_stats_links')} {total_links}",
        f"{t(uid, 'admin_stats_users')} {total_users}",
        "",
    ]
    names_changed = False
    for u in known_users:
        name = user_names.get(str(u))
        if not name:
            try:
                chat = await context.bot.get_chat(u)
                name = f"@{chat.username}" if chat.username else (chat.full_name or str(u))
                user_names[str(u)] = name
                names_changed = True
            except Exception:
                name = str(u)
        searched = user_search_history.get(str(u), [])
        downloaded = user_download_history.get(str(u), [])
        line = f"- {name}"
        if searched:
            line += "\n    Searched:"
            for s in searched:
                if isinstance(s, dict):
                    when = to_local_time(s.get("t", ""))
                    line += f"\n      {s.get('q','')} ({when})" if when else f"\n      {s.get('q','')}"
                else:
                    line += f"\n      {s}"
        if downloaded:
            line += "\n    Downloaded:"
            for d in downloaded:
                if isinstance(d, dict):
                    when = to_local_time(d.get("t", ""))
                    line += f"\n      {d.get('title','')} ({when})" if when else f"\n      {d.get('title','')}"
                else:
                    line += f"\n      {d}"
        all_lines.append(line)
    if names_changed:
        save_state("user_names", USER_NAMES_FILE, user_names)

    chunk = ""
    for line in all_lines:
        if len(chunk) + len(line) + 1 > 3900:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/broadcast Your message")
        return
    message = parts[1].strip()
    sent, failed = 0, 0
    for user_id in list(known_users):
        try:
            await context.bot.send_message(chat_id=user_id, text=message)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"{t(uid, 'admin_broadcast_sent')}\n"
        f"{t(uid, 'admin_broadcast_success')} {sent}\n"
        f"{t(uid, 'admin_broadcast_failed')} {failed}"
    )

async def set_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/setposter Title   (then send the poster photo next)\n"
            "or: /setposter Title | image URL"
        )
        return

    raw = parts[1].strip()
    if "|" in raw:
        title, url = [s.strip() for s in raw.split("|", 1)]
        matched = find_existing_title(title)
        if matched not in db:
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        title_posters[matched] = url
        save_state("posters", POSTERS_FILE, title_posters)
        await update.message.reply_text(f"{t(uid, 'admin_added')}\n{matched}")
        return

    matched = find_existing_title(raw)
    if matched not in db:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    awaiting_poster[uid] = matched
    await update.message.reply_text(f"Now send the poster photo for:\n{matched}")

async def receive_poster_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID or uid not in awaiting_poster:
        return
    title = awaiting_poster.pop(uid)
    photo = update.message.photo[-1]
    title_posters[title] = photo.file_id
    save_state("posters", POSTERS_FILE, title_posters)
    await update.message.reply_text(f"{t(uid, 'admin_added')}\n{title}")

async def set_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/setlatest Title\nor: /setlatest Title | Label (e.g. Part 2)")
        return

    segments = [s.strip() for s in parts[1].split("|")]
    title = segments[0]
    matched = find_existing_title(title)
    if matched not in db:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return

    node = db[matched]
    path = []
    display = matched
    for label in segments[1:]:
        if not isinstance(node, dict) or label not in node:
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        path.append(label)
        node = node[label]
        display += f" - {label}"

    key = matched if not path else f"{matched}::{'::'.join(path)}"
    latest_titles[key] = {
        "title": matched,
        "path": path,
        "display": display,
        "marked_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state("latest", LATEST_FILE, latest_titles)
    await update.message.reply_text(f"{t(uid, 'admin_added')}\n{display}")

async def remove_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/removelatest Title\nor: /removelatest Title | Label")
        return

    segments = [s.strip() for s in parts[1].split("|")]
    title = segments[0]
    matched = find_existing_title(title)
    key = matched if len(segments) == 1 else f"{matched}::{'::'.join(segments[1:])}"

    if key not in latest_titles:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    display = latest_titles[key].get("display", key) if isinstance(latest_titles[key], dict) else key
    del latest_titles[key]
    save_state("latest", LATEST_FILE, latest_titles)
    await update.message.reply_text(f"{t(uid, 'admin_deleted')}\n{display}")

latest_entries_cache = {}   # user_id -> [{"title":..., "path":[...], "display":...}, ...] (/latest বাটনগুলোর রেফারেন্সের জন্য)

def get_latest_entries(limit=10):
    normalized = []
    for key, value in latest_titles.items():
        if isinstance(value, dict):
            if value.get("title") in db:
                normalized.append(value)
        elif key in db:
            # পুরনো ফরম্যাট (শুধু টাইমস্ট্যাম্প স্ট্রিং) — টপ-লেভেল এন্ট্রি হিসেবে ধরা হচ্ছে
            normalized.append({"title": key, "path": [], "display": key, "marked_at": value})
    normalized.sort(key=lambda v: v.get("marked_at", ""), reverse=True)
    return normalized[:limit]

async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)

    normalized = get_latest_entries(10)

    if not normalized:
        await update.message.reply_text(t(uid, "not_found"))
        return

    latest_entries_cache[uid] = normalized
    buttons = [
        [InlineKeyboardButton(v["display"], callback_data=f"latestopen::{i}")]
        for i, v in enumerate(normalized)
    ]
    await update.message.reply_text(t(uid, "results"), reply_markup=InlineKeyboardMarkup(buttons))

async def open_nested_entry(query, uid, title, path, origin="latest"):
    """টাইটেলের ভেতরের একটা নির্দিষ্ট পাথ (যেমন Part 2) সরাসরি খুলে দেখায় —
    /latest আর ক্যাটাগরি, দুটোতেই ব্যবহার হয়।"""
    path = list(path or [])
    node = db.get(title, {})
    for key in path:
        node = node.get(key, {})
    if not node:
        await query.edit_message_text(t(uid, "content_unavailable"))
        return

    label = title if not path else f"{title} - {' / '.join(path)}"
    browse_state[uid] = {"title": title, "path": path, "children": list(node.keys()), "floor": len(path), "origin": origin}

    if is_leaf_level(node):
        buttons = [[InlineKeyboardButton(q, callback_data=f"getnav::{i}")] for i, q in enumerate(node.keys())]
        option_key = "select_quality"
    else:
        buttons = [[InlineKeyboardButton(k, callback_data=f"nav::{i}")] for i, k in enumerate(node.keys())]
        option_key = "select_option"
    buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="navback")])

    await query.edit_message_text(f"{label}\n{t(uid, option_key)}", reply_markup=InlineKeyboardMarkup(buttons))

async def open_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    idx = int(query.data.split("::", 1)[1])
    entries = latest_entries_cache.get(uid)
    if not entries or idx >= len(entries):
        return
    info = entries[idx]
    await open_nested_entry(query, uid, info["title"], info.get("path", []), origin="latest")

# ---------- সার্চ ----------
async def capture_loading_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """অ্যাডমিন যদি কোনো স্টিকার বা GIF/অ্যানিমেশন সরাসরি বটকে পাঠায়,
    সেটা 'লোডিং' অ্যানিমেশনের লিস্টে যোগ হয়ে যায় — একাধিক পাঠালে প্রতিবার সার্চে
    এলোমেলোভাবে একটা বেছে দেখানো হবে, যাতে সবসময় একই অ্যানিমেশন না দেখায়।"""
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    msg = update.message

    if msg.sticker:
        entry = {"file_id": msg.sticker.file_id, "type": "sticker"}
    elif msg.animation:
        entry = {"file_id": msg.animation.file_id, "type": "animation"}
    else:
        return

    bot_settings.setdefault("loading_animations", []).append(entry)
    save_state("settings", SETTINGS_FILE, bot_settings)
    count = len(bot_settings["loading_animations"])
    await update.message.reply_text(f"{t(uid, 'admin_loading_set')} ({count})")

async def remove_loading_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    bot_settings["loading_animations"] = []
    bot_settings.pop("loading_file_id", None)
    bot_settings.pop("loading_type", None)
    save_state("settings", SETTINGS_FILE, bot_settings)
    await update.message.reply_text(t(uid, "admin_loading_removed"))

async def notify_fulfilled_requests(context: ContextTypes.DEFAULT_TYPE, new_title: str):
    """নতুন কোনো টাইটেল/সিরিজ যোগ হলে, আগে যারা এই নামে খুঁজে না পেয়ে রিকোয়েস্ট করেছিল
    তাদের সবাইকে নোটিফাই করে, তারপর সেই রিকোয়েস্টগুলো তালিকা থেকে সরিয়ে দেয়।"""
    fulfilled_keys = []
    for query_key, user_ids in list(pending_requests.items()):
        if fuzzy_search(query_key, [new_title]):
            for req_uid in user_ids:
                try:
                    await context.bot.send_message(
                        chat_id=req_uid,
                        text=f"{t(req_uid, 'request_fulfilled')}\n{new_title}"
                    )
                except Exception:
                    pass
            fulfilled_keys.append(query_key)
    if fulfilled_keys:
        for k in fulfilled_keys:
            pending_requests.pop(k, None)
        save_state("requests", REQUESTS_FILE, pending_requests)

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_query: str):
    raw_query = raw_query.strip()
    if not raw_query:
        return
    uid = update.effective_user.id
    register_user(uid, update.effective_user)

    hist = user_search_history.setdefault(str(uid), [])
    hist.append({"q": raw_query, "t": datetime.now(timezone.utc).isoformat()})
    del hist[:-HISTORY_LIMIT]
    save_state("search_history", SEARCH_HISTORY_FILE, user_search_history)

    matches = fuzzy_search(raw_query, db.keys())

    if not matches:
        pending_request[uid] = raw_query
        key = raw_query.strip().lower()
        waiting = pending_requests.setdefault(key, [])
        if uid not in waiting:
            waiting.append(uid)
            save_state("requests", REQUESTS_FILE, pending_requests)
        user = update.effective_user
        name = f"@{user.username}" if user.username else (user.full_name or str(uid))
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"{t(ADMIN_ID, 'admin_new_request')} {name} (id: {uid}):\n{raw_query}"
            )
        except Exception:
            pass
        cat_text, cat_markup = render_category_level(uid, [])
        reply_markup = cat_markup if (cat_text and cat_markup.inline_keyboard) else None
        await update.message.reply_text(t(uid, "not_found"), reply_markup=reply_markup)
        return

    last_search_results[uid] = matches
    await update.message.reply_text(t(uid, "results"), reply_markup=build_results_keyboard(matches, 0))

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await perform_search(update, context, update.message.text or "")

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
                text=f"{t(ADMIN_ID, 'admin_new_request')} @{username} (id: {uid}):\n{text}"
            )
        except Exception:
            pass
    await query.edit_message_text(t(uid, "request_sent"))

# ---------- টাইটেল সিলেক্ট করলে (মুভি হলে কোয়ালিটি, সিরিজ হলে সিজন দেখানো) ----------
async def open_title_flow(query, uid, title):
    """একটা টাইটেল সিলেক্ট হলে (সার্চ রেজাল্ট থেকে বা ক্যাটাগরি থেকে) কোয়ালিটি/সিজন দেখায়।"""
    node = db.get(title, {})
    if not node:
        await query.edit_message_text(t(uid, "content_unavailable"))
        return

    if is_leaf_level(node):
        # সাধারণ মুভি/গান — সরাসরি কোয়ালিটি দেখাও
        # (টাইটেল সরাসরি callback_data-তে বসানো হয় না — লম্বা/বাংলা টাইটেলে Telegram-এর ৬৪-বাইট
        # লিমিট ছাড়িয়ে গিয়ে বাটন নিঃশব্দে ভেঙে যেতে পারে, তাই browse_state + ইনডেক্স ব্যবহার করা হচ্ছে)
        browse_state[uid] = {"title": title, "path": [], "children": list(node.keys()), "floor": 0, "origin": "search"}
        buttons = [
            [InlineKeyboardButton(q, callback_data=f"getnav::{i}")]
            for i, q in enumerate(node.keys())
        ]
        buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="navback")])
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
        buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="navback")])
        await query.edit_message_text(
            f"{title}\n{t(uid, 'select_option')}", reply_markup=InlineKeyboardMarkup(buttons)
        )

async def show_qualities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    title = query.data.split("::", 1)[1]
    await open_title_flow(query, uid, title)

# ---------- ক্যাটাগরি/ফোল্ডার ব্রাউজিং ----------
def get_category_node(path):
    node = category_tree
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    return node

def render_category_level(uid, path):
    node = get_category_node(path)
    if node is None:
        return None, None

    subfolders = [k for k in node.keys() if k != "_titles"]
    raw_titles = node.get("_titles", [])
    titles = []
    for rt in raw_titles:
        if isinstance(rt, dict):
            if rt.get("title") in db:
                titles.append(rt)
        elif rt in db:
            titles.append({"title": rt, "path": [], "display": rt})

    children = []
    flat = []
    for sf in subfolders:
        children.append(("folder", sf, None))
        flat.append(InlineKeyboardButton(sf, callback_data=f"catnav::{len(children) - 1}"))
    for ti in titles:
        children.append(("title", ti["title"], ti.get("path", [])))
        flat.append(InlineKeyboardButton(ti.get("display", ti["title"]), callback_data=f"catnav::{len(children) - 1}"))

    category_browse_state[uid] = {"path": path, "children": children}

    buttons = grid_rows(flat, 2)
    if path:
        buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="catback")])

    label = " / ".join(path) if path else "Categories"
    text = f"{label}\n{t(uid, 'select_option')}"
    return text, InlineKeyboardMarkup(buttons)

async def categories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid, update.effective_user)
    text, markup = render_category_level(uid, [])
    if not text or not markup.inline_keyboard:
        await update.message.reply_text(t(uid, "not_found"))
        return
    await update.message.reply_text(text, reply_markup=markup)

async def category_navigate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    idx = int(query.data.split("::", 1)[1])
    state = category_browse_state.get(uid)
    if not state or idx >= len(state["children"]):
        return
    kind, value, extra_path = state["children"][idx]

    if kind == "folder":
        new_path = state["path"] + [value]
        text, markup = render_category_level(uid, new_path)
        if not text:
            await query.edit_message_text(t(uid, "content_unavailable"))
            return
        await query.edit_message_text(text, reply_markup=markup)
    else:
        category_return_path[uid] = state["path"]
        await open_nested_entry(query, uid, value, extra_path, origin="category")

async def category_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    state = category_browse_state.get(uid)
    if not state:
        return
    new_path = state["path"][:-1]
    text, markup = render_category_level(uid, new_path)
    if not text:
        await query.edit_message_text(t(uid, "content_unavailable"))
        return
    await query.edit_message_text(text, reply_markup=markup)

# ---------- ক্যাটাগরি/ফোল্ডার — অ্যাডমিন কমান্ড ----------
async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/addcategory Avengers | MCU")
        return
    segments = [s.strip() for s in parts[1].split("|")]
    if not all(segments):
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/addcategory Avengers | MCU")
        return
    node = category_tree
    for seg in segments:
        node = node.setdefault(seg, {})
    save_state("categories", CATEGORY_FILE, category_tree)
    await update.message.reply_text(f"{t(uid, 'admin_added')}\n{' / '.join(segments)}")

async def add_title_to_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n"
            "/addtitletocategory Folder | ... | Title\n"
            "or (for a part inside a title, e.g. Part 2):\n"
            "/addtitletocategory Folder | ... | Title | Part 2"
        )
        return
    segments = [s.strip() for s in parts[1].split("|")]
    if len(segments) < 2 or not all(segments):
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/addtitletocategory Folder | ... | Title")
        return

    result = resolve_title_and_path(segments)
    if result is None:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    folder_path, matched_title, nested_path = result
    if not folder_path:
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\nNeed at least one folder name before the title.")
        return

    display = matched_title if not nested_path else f"{matched_title} - {' / '.join(nested_path)}"
    entry = {"title": matched_title, "path": nested_path, "display": display}

    node = category_tree
    for seg in folder_path:
        node = node.setdefault(seg, {})
    node.setdefault("_titles", [])
    already = any(
        isinstance(e, dict) and e.get("title") == matched_title and e.get("path", []) == nested_path
        for e in node["_titles"]
    )
    if not already:
        node["_titles"].append(entry)
    save_state("categories", CATEGORY_FILE, category_tree)
    await update.message.reply_text(f"{t(uid, 'admin_added')}\n{' / '.join(folder_path)} - {display}")

# ---------- এক কমান্ডে পুরো ক্যাটাগরি ট্রি মুছে ফেলা / নতুন করে বানানো ----------
# ফরম্যাট: (ফোল্ডারের পথ, [(দেখানোর নাম বা None, [টাইটেল, পার্ট/সিজন, ...]), ...])
# টাইটেলগুলো /list-এ যেভাবে দেখায় ঠিক সেই নামে লেখা (কোয়ালিটি বাদে)।
DEFAULT_CATEGORY_PLAN = [
    (["Hollywood", "Marvel", "Deadpool"], [
        (None, ["Deadpool.2016.Hindi"]),
        (None, ["Deadpool.2.2018.Hindi"]),
        (None, ["Deadpool.&.Wolverine.2024.Hindi"]),
    ]),
    (["Hollywood", "Marvel", "Superhero"], [
        (None, ["Captain.Marvel.2019.Hindi"]),
        (None, ["Captain.America-B.N.W.2025.Hindi"]),
        (None, ["Black.Widow.2021.Hindi"]),
    ]),
    (["Hollywood", "John Wick Series"], [
        (None, ["John Wick Hindi", "Part-1", "John Wick 2014 Hindi"]),
        (None, ["John Wick Hindi", "Part-2", "John Wick Chapter 2 2017 Hindi"]),
        (None, ["John Wick Hindi", "Part-3", "John Wick Chapter 3 Parabellum 2019 Hindi"]),
        (None, ["John Wick Hindi", "Part-4", "John Wick Chapter 4 2023 Hindi"]),
    ]),
    (["Hollywood", "Kingsman Series"], [
        (None, ["Kingsman The Secret Service Hindi"]),
        (None, ["Kingsman The Golden Circle Hindi"]),
    ]),
    (["Hollywood", "Maze Runner Series"], [
        (None, ["The Maze Runner", "Part-1", "The Maze Runner 2014 Hindi"]),
        (None, ["The Maze Runner", "Part-2", "Maze Runner The Scorch Trials 2015 Hindi"]),
        (None, ["The Maze Runner", "Part-3", "Maze Runner The Death Cure 2018 Hindi"]),
    ]),
    (["Hollywood", "Sci-Fi"], [
        (None, ["Ready Player One Hindi"]),
        (None, ["Dial.1975 2026 Hindi"]),
    ]),
    (["Hollywood", "Horror"], [
        (None, ["The Cabin in the Woods Hindi"]),
    ]),
    (["Bollywood", "Spy Movies"], [
        (None, ["Pathaan 2023 Hindi"]),
        (None, ["War.2 2025 Hindi"]),
        (None, ["Alpha 2026 Hindi"]),
    ]),
    (["Bollywood", "Dhoom Series"], [
        (None, ["Dhoom Hindi", "Part-1", "Dhoom Hindi"]),
        (None, ["Dhoom Hindi", "Part-2", "Dhoom 2 Hindi"]),
        (None, ["Dhoom Hindi", "Part-3", "Dhoom 3 Hindi"]),
        (None, ["Dhoom Hindi", "Part-5", "Dhoom 5 Hindi"]),
    ]),
    (["Bollywood", "Dhurandhar Series"], [
        (None, ["Dhurandhar Hindi", "Part 1", "Dhurandhar 2025 Hindi"]),
        (None, ["Dhurandhar Hindi", "Part 2", "Dhurandhar-The Revenge 2026 Hindi"]),
    ]),
    (["Bollywood", "Action"], [
        (None, ["Jawan 2023 Hindi"]),
        (None, ["Ra.One 2011 Hindi"]),
        (None, ["O-Romeo 2026 Hindi"]),
        (None, ["Ghamasaan 2026 Hindi"]),
    ]),
    (["Bollywood", "Thriller"], [
        (None, ["Drishyam 2015 Hindi"]),
        (None, ["Tumbbad 2018 Hindi"]),
    ]),
    (["South Movies", "Pushpa Series"], [
        (None, ["Pushpa Hindi", "Part-1", "Pushpa-The Rise 2021Hindi"]),
        (None, ["Pushpa Hindi", "Part-2", "Pushpa 2 The Rule Reloaded 2024 Hindi"]),
    ]),
    (["South Movies", "Kantara Series"], [
        (None, ["Kantara Hindi", "Part-1", "Kantara 2022 Hindi"]),
        (None, ["Kantara Hindi", "Part-2", "Kantara Chapter 1 2025 Hindi"]),
    ]),
    (["South Movies", "Sci-Fi & Fantasy"], [
        (None, ["Kalki 2024 Hindi"]),
        (None, ["Karthikeya 2. 2022 Hindi"]),
    ]),
    (["South Movies", "Action"], [
        (None, ["RRR 2022 Hindi"]),
        (None, ["Salaar 2023 Hindi"]),
        (None, ["Marco 2024 Hindi"]),
        (None, ["TOXiC 2026 Hindi"]),
        (None, ["Red 2021 Hindi"]),
    ]),
    (["Web Series", "Daredevil"], [
        ("Daredevil - Season 1", ["Daredevil Hindi", "Season 1"]),
        ("Daredevil - Season 2", ["Daredevil Hindi", "Season 2"]),
        ("Daredevil - Season 3", ["Daredevil Hindi", "Season 3"]),
        ("Daredevil Final", ["Daredevil Hindi", "Daredevil Final Hindi"]),
    ]),
    (["Web Series", "The Punisher"], [
        (None, ["The Punisher Hindi"]),
    ]),
    (["Web Series", "Doctor Stranger"], [
        ("Doctor Stranger - Season 1", ["Doctor Stranger Hindi", "Season 1"]),
    ]),
    (["Songs", "Hindi Songs"], [
        ("Apna Bana Le", ["Apna Bana Le"]),
        ("Tum Se Hi - Jab We Met", ["Tum Se Hi"]),
        ("Aaj Ki Raat - Stree 2", ["Aaj Ki Raat"]),
    ]),
    (["Songs", "Bengali Songs"], [
        ("Tomake - Parineeta", ["Tomake"]),
    ]),
]

def match_plan_entry(segments):
    """segments-এর সাথে db-র আসল টাইটেল/পথ মেলায়। পুরোটা না মিললে শেষ ধাপগুলো বাদ দিয়ে যতটুকু মেলে ততটুকু।
    রিটার্ন: (টাইটেল, নেস্টেড পথ, পুরোপুরি মিলেছে কিনা) অথবা None।"""
    for k in range(len(segments), 0, -1):
        segs = segments[:k]
        res = resolve_title_and_path(segs)
        if res and not res[0]:
            return res[1], res[2], k == len(segments)
        first = _normalize_label(segs[0])
        candidates = [ti for ti in db if first and first in _normalize_label(ti)]
        if candidates:
            ti = candidates[0]
            node = db[ti]
            path = []
            ok = True
            for label in segs[1:]:
                key = find_key_ci(node, label)
                if key is None:
                    ok = False
                    break
                path.append(key)
                node = node[key]
            if ok:
                return ti, path, k == len(segments)
    return None

async def clear_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    category_tree.clear()
    category_browse_state.clear()
    save_state("categories", CATEGORY_FILE, category_tree)
    await update.message.reply_text(f"{t(uid, 'admin_deleted')}\nAll category folders cleared (your titles/links are untouched).")

async def build_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id

    category_tree.clear()
    category_browse_state.clear()
    added = 0
    partial_lines = []
    missing_lines = []

    for folder_path, entries in DEFAULT_CATEGORY_PLAN:
        node = category_tree
        for seg in folder_path:
            node = node.setdefault(seg, {})
        node.setdefault("_titles", [])
        for display, segments in entries:
            m = match_plan_entry(segments)
            where = " / ".join(folder_path)
            if m is None:
                missing_lines.append(f"{where}: {' - '.join(segments)}")
                continue
            title, path, full = m
            shown = display or (path[-1] if path else title)
            node["_titles"].append({"title": title, "path": path, "display": shown})
            added += 1
            if not full:
                partial_lines.append(f"{where}: {' - '.join(segments)}  ->  only reached: {title}" + (f" / {' / '.join(path)}" if path else ""))

    save_state("categories", CATEGORY_FILE, category_tree)

    lines = [f"{t(uid, 'admin_added')}", f"Folders built. Titles placed: {added}"]
    if partial_lines:
        lines.append("")
        lines.append("Partly matched (opens one level higher than intended):")
        lines.extend(partial_lines)
    if missing_lines:
        lines.append("")
        lines.append("Not found in your library (check the name with /list):")
        lines.extend(missing_lines)
    if not partial_lines and not missing_lines:
        lines.append("Everything matched.")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3900:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk)

async def remove_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/removecategory Avengers | MCU")
        return
    segments = [s.strip() for s in parts[1].split("|")]
    node = category_tree
    for seg in segments[:-1]:
        if not isinstance(node, dict) or seg not in node:
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        node = node[seg]
    if not isinstance(node, dict) or segments[-1] not in node:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    del node[segments[-1]]
    save_state("categories", CATEGORY_FILE, category_tree)
    await update.message.reply_text(f"{t(uid, 'admin_deleted')}\n{' / '.join(segments)}")

async def remove_title_from_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(f"{t(uid, 'admin_format_error')}\n/removetitlefromcategory Folder | ... | Title")
        return
    segments = [s.strip() for s in parts[1].split("|")]

    result = resolve_title_and_path(segments)
    if result is None:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    folder_path, matched_title, nested_path = result

    node = category_tree
    for seg in folder_path:
        if not isinstance(node, dict) or seg not in node:
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        node = node[seg]

    titles_list = node.get("_titles", [])
    match_idx = None
    for i, e in enumerate(titles_list):
        if isinstance(e, dict) and e.get("title") == matched_title and e.get("path", []) == nested_path:
            match_idx = i
            break
        if not isinstance(e, dict) and e == matched_title and not nested_path:
            match_idx = i
            break
    if match_idx is None:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    display = titles_list[match_idx]["display"] if isinstance(titles_list[match_idx], dict) else titles_list[match_idx]
    del titles_list[match_idx]
    save_state("categories", CATEGORY_FILE, category_tree)
    await update.message.reply_text(f"{t(uid, 'admin_deleted')}\n{' / '.join(folder_path)} - {display}")

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
        buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="navback")])
        await query.edit_message_text(
            f"{label}\n{t(uid, 'select_quality')}", reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        state["children"] = list(node.keys())
        buttons = [
            [InlineKeyboardButton(k, callback_data=f"nav::{i}")]
            for i, k in enumerate(node.keys())
        ]
        buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="navback")])
        await query.edit_message_text(
            f"{label}\n{t(uid, 'select_option')}", reply_markup=InlineKeyboardMarkup(buttons)
        )

async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    state = browse_state.get(uid)
    floor = state.get("floor", 0) if state else 0

    if state and len(state["path"]) > floor:
        state["path"] = state["path"][:-1]
        node = db.get(state["title"], {})
        for key in state["path"]:
            node = node.get(key, {})

        if not node:
            await query.edit_message_text(t(uid, "content_unavailable"))
            return

        label = state["title"] if not state["path"] else f"{state['title']} - {' / '.join(state['path'])}"
        state["children"] = list(node.keys())

        if is_leaf_level(node):
            buttons = [[InlineKeyboardButton(q, callback_data=f"getnav::{i}")] for i, q in enumerate(node.keys())]
            option_key = "select_quality"
        else:
            buttons = [[InlineKeyboardButton(k, callback_data=f"nav::{i}")] for i, k in enumerate(node.keys())]
            option_key = "select_option"
        buttons.append([InlineKeyboardButton(t(uid, "back_button"), callback_data="navback")])

        await query.edit_message_text(
            f"{label}\n{t(uid, option_key)}", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # ফ্লোরে পৌঁছে গেছে — যেখান থেকে শুরু হয়েছিল সেখানেই ফিরে যাও
    if state and state.get("origin") == "latest":
        browse_state.pop(uid, None)
        entries = latest_entries_cache.get(uid, [])
        if entries:
            buttons = [
                [InlineKeyboardButton(v["display"], callback_data=f"latestopen::{i}")]
                for i, v in enumerate(entries)
            ]
            await query.edit_message_text(t(uid, "results"), reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await query.edit_message_text(t(uid, "not_found"))
        return

    if state and state.get("origin") == "category":
        browse_state.pop(uid, None)
        cat_path = category_return_path.get(uid, [])
        text, markup = render_category_level(uid, cat_path)
        if text:
            await query.edit_message_text(text, reply_markup=markup)
        else:
            await query.edit_message_text(t(uid, "not_found"))
        return

    # path খালি (বা কোনো state নেই) — সার্চ রেজাল্টে ফিরে যাও
    browse_state.pop(uid, None)
    titles = last_search_results.get(uid, [])
    if titles:
        await query.edit_message_text(t(uid, "results"), reply_markup=build_results_keyboard(titles, 0))
    else:
        await query.edit_message_text(t(uid, "not_found"))

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

    full_name = state["title"] if not state["path"] else f"{state['title']} - {' / '.join(state['path'])}"
    label = f"{full_name} ({quality})"

    hist = user_download_history.setdefault(str(uid), [])
    hist.append({"title": full_name, "t": datetime.now(timezone.utc).isoformat()})
    del hist[:-HISTORY_LIMIT]
    save_state("download_history", DOWNLOAD_HISTORY_FILE, user_download_history)

    caption = f"{label}\n{t(uid, 'download_link')}\n{link}\n\n{t(uid, 'thank_you')}"
    poster = title_posters.get(state["title"])
    if poster:
        try:
            await query.message.reply_photo(photo=poster, caption=caption)
            return
        except Exception:
            pass
    await query.message.reply_text(caption)

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

    hist = user_download_history.setdefault(str(uid), [])
    hist.append({"title": title, "t": datetime.now(timezone.utc).isoformat()})
    del hist[:-HISTORY_LIMIT]
    save_state("download_history", DOWNLOAD_HISTORY_FILE, user_download_history)

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
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("search", "search for movies or songs"),
        BotCommand("help", "get help"),
        BotCommand("reset", "reset your language & search state"),
        BotCommand("language", "set the language"),
        BotCommand("latest", "show trending movies and songs"),
        BotCommand("share", "share the bot with your friends"),
        BotCommand("subscribe", "subscribe for new updates and releases"),
        BotCommand("feedback", "send your feedback or suggestions"),
        BotCommand("support", "contact support for any help"),
    ])

async def show_typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """প্রতিটা মেসেজ/কমান্ডের শুরুতে 'টাইপিং...' ইন্ডিকেটর দেখায় — ভাষা-নিরপেক্ষ,
    কোল্ড-স্টার্ট বা ধীরগতির সময় ইউজারকে বুঝিয়ে রাখে যে কিছু একটা হচ্ছে।"""
    chat = update.effective_chat
    if not chat:
        return
    old = typing_tasks.pop(chat.id, None)
    if old:
        old.cancel()

    async def _loop(chat_id):
        # টেলিগ্রামের টাইপিং ৫ সেকেন্ডে নিভে যায়, তাই ৪ সেকেন্ড পরপর আবার পাঠাই — সর্বোচ্চ ২ মিনিট
        try:
            for _ in range(30):
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    typing_tasks[chat.id] = asyncio.create_task(_loop(chat.id))

async def stop_typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """হ্যান্ডলার শেষ হলে (বট উত্তর দেওয়ার পর) অ্যানিমেশন থামায়।"""
    chat = update.effective_chat
    if chat:
        task = typing_tasks.pop(chat.id, None)
        if task:
            task.cancel()

def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(TypeHandler(Update, show_typing), group=-3)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, capture_pending_text), group=-2)
    application.add_handler(TypeHandler(Update, stop_typing), group=5)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_content))
    application.add_handler(CommandHandler("addseries", add_series_content))
    application.add_handler(CommandHandler("addmovie", add_movie_in_series))
    application.add_handler(CommandHandler("rename", rename_content))
    application.add_handler(CommandHandler("migrate", migrate_content))
    application.add_handler(CommandHandler("moveepisode", move_episode))
    application.add_handler(CommandHandler("removeepisode", remove_episode))
    application.add_handler(CommandHandler("remove", remove_content))
    application.add_handler(CommandHandler("list", list_titles))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("latest", latest))
    application.add_handler(CommandHandler("setlatest", set_latest))
    application.add_handler(CommandHandler("setposter", set_poster))
    application.add_handler(CommandHandler("categories", categories_command))
    application.add_handler(CommandHandler("addcategory", add_category))
    application.add_handler(CommandHandler("addtitletocategory", add_title_to_category))
    application.add_handler(CommandHandler("removecategory", remove_category))
    application.add_handler(CommandHandler("clearcategories", clear_categories))
    application.add_handler(CommandHandler("buildcategories", build_categories))
    application.add_handler(CommandHandler("removetitlefromcategory", remove_title_from_category))
    application.add_handler(CommandHandler("removelatest", remove_latest))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("share", share_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("removeloading", remove_loading_animation))
    application.add_handler(CallbackQueryHandler(reset_confirm, pattern=r"^reset_"))
    application.add_handler(CallbackQueryHandler(begin_flow, pattern=r"^begin$"))
    application.add_handler(CallbackQueryHandler(set_language, pattern=r"^lang::"))
    application.add_handler(CallbackQueryHandler(show_qualities, pattern=r"^title::"))
    application.add_handler(CallbackQueryHandler(category_navigate, pattern=r"^catnav::"))
    application.add_handler(CallbackQueryHandler(category_back, pattern=r"^catback$"))
    application.add_handler(CallbackQueryHandler(send_file, pattern=r"^get::"))
    application.add_handler(CallbackQueryHandler(navigate, pattern=r"^nav::"))
    application.add_handler(CallbackQueryHandler(open_latest, pattern=r"^latestopen::"))
    application.add_handler(CallbackQueryHandler(go_back, pattern=r"^navback$"))
    application.add_handler(CallbackQueryHandler(send_file_nav, pattern=r"^getnav::"))
    application.add_handler(CallbackQueryHandler(paginate, pattern=r"^page::"))
    application.add_handler(CallbackQueryHandler(request_title, pattern=r"^request$"))
    application.add_handler(MessageHandler(filters.PHOTO, receive_poster_photo))
    application.add_handler(MessageHandler(filters.Sticker.ALL | filters.ANIMATION, capture_loading_animation))
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
