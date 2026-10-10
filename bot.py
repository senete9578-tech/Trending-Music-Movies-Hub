import os
import io
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

_SINGLE_ROW_KEEP = ("catnav::", "navback", "catback", "begin", "request", "page::", "reset_")

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
    parts = txt.split(" - ")
    while len(parts) > 1 and len(" - ".join(parts)) > limit:
        parts.pop(0)   # লম্বা নামে আগের অংশ বাদ দিয়ে শেষের অংশ (Season 1 ইত্যাদি) রাখে
    txt = " - ".join(parts)
    if len(txt) <= limit:
        return InlineKeyboardButton(txt, callback_data=btn.callback_data, url=btn.url)
    return InlineKeyboardButton(txt[:limit - 1].rstrip() + "…", callback_data=btn.callback_data, url=btn.url)

class InlineKeyboardMarkup(_OrigMarkup):
    def __init__(self, inline_keyboard, *args, **kwargs):
        rows = _regroup_rows(inline_keyboard)
        rows = [[_fit(b) for b in r] if len(r) == 2 else ([_fit(b, 12) for b in r] if len(r) == 3 else r) for r in rows]
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
        "admin_already": "Already added - nothing changed ✅",
        "admin_updated": "Updated with the new link ✅",
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
        "admin_already": "पहले से जोड़ा हुआ है, कोई बदलाव नहीं ✅",
        "admin_updated": "नए लिंक से अपडेट किया गया ✅",
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
        "admin_already": "আগে থেকেই যোগ করা আছে, কিছু বদলায়নি ✅",
        "admin_updated": "নতুন লিংক দিয়ে আপডেট হয়েছে ✅",
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
        "admin_already": "ஏற்கனவே சேர்க்கப்பட்டுள்ளது, மாற்றம் இல்லை ✅",
        "admin_updated": "புதிய இணைப்புடன் புதுப்பிக்கப்பட்டது ✅",
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
        "admin_already": "ఇప్పటికే జోడించబడింది, మార్పు లేదు ✅",
        "admin_updated": "కొత్త లింక్‌తో నవీకరించబడింది ✅",
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
        "admin_already": "आधीच जोडलेले आहे, बदल नाही ✅",
        "admin_updated": "नवीन लिंकने अपडेट केले ✅",
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
        "admin_already": "પહેલેથી ઉમેરેલું છે, કોઈ ફેરફાર નથી ✅",
        "admin_updated": "નવી લિંક સાથે અપડેટ કર્યું ✅",
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
    return TEXTS.get(lang, TEXTS["en"]).get(key) or TEXTS["en"][key]

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
    old = db.get(existing_title, {}).get(quality) if isinstance(db.get(existing_title), dict) else None
    if old == link:
        await update.message.reply_text(f"{t(uid, 'admin_already')}\n{t(uid, 'title_label')} {existing_title}\n{t(uid, 'quality_label')} {quality}")
        return
    status_key = "admin_updated" if old else "admin_added"
    db.setdefault(existing_title, {})[quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)
    await notify_fulfilled_requests(context, existing_title)

    await update.message.reply_text(
        f"{t(uid, status_key)}\n{t(uid, 'title_label')} {existing_title}\n{t(uid, 'quality_label')} {quality}"
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
    try:
        old = db[existing_title][season][episode][quality]
    except (KeyError, TypeError):
        old = None
    if old == link:
        await update.message.reply_text(
            f"{t(uid, 'admin_already')}\n{t(uid, 'title_label')} {existing_title}\n"
            f"{t(uid, 'season_label')} {season}\n{t(uid, 'episode_label')} {episode}\n{t(uid, 'quality_label')} {quality}")
        return
    status_key = "admin_updated" if old else "admin_added"
    db.setdefault(existing_title, {})
    db[existing_title].setdefault(season, {})
    db[existing_title][season].setdefault(episode, {})
    db[existing_title][season][episode][quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)
    await notify_fulfilled_requests(context, existing_title)

    await update.message.reply_text(
        f"{t(uid, status_key)}\n"
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
    old = db[existing_title][label].get(quality)
    if old == link:
        await update.message.reply_text(f"{t(uid, 'admin_already')}\n{t(uid, 'title_label')} {existing_title}\n{label}\n{t(uid, 'quality_label')} {quality}")
        return
    status_key = "admin_updated" if old else "admin_added"
    db[existing_title][label][quality] = link
    save_state("content", DB_FILE, db)

    title_meta.setdefault(existing_title, {})["added_at"] = datetime.now(timezone.utc).isoformat()
    save_state("meta", META_FILE, title_meta)
    await notify_fulfilled_requests(context, existing_title)

    await update.message.reply_text(
        f"{t(uid, status_key)}\n"
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


# ---------- পোস্টারে বটের লোগো (ওয়াটারমার্ক) বসানো ----------
try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None

import base64 as _b64
_LOGO_B64 = "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"

def apply_watermark(img_bytes: bytes) -> bytes:
    """পোস্টারের উপরে-ডানে বটের গোল লোগো বসিয়ে নতুন JPEG বাইটস ফেরত দেয়। Pillow না থাকলে আসল ছবিই ফেরত দেয়।"""
    if _PILImage is None:
        return img_bytes
    try:
        base = _PILImage.open(io.BytesIO(img_bytes)).convert("RGBA")
        w, h = base.size
        logo = _PILImage.open(io.BytesIO(_b64.b64decode(_LOGO_B64))).convert("RGBA")
        size = max(48, int(min(w, h) * 0.17))
        logo = logo.resize((size, size), _PILImage.LANCZOS)
        alpha = logo.getchannel("A").point(lambda a: int(a * 0.92))
        logo.putalpha(alpha)
        margin = int(min(w, h) * 0.03)
        base.alpha_composite(logo, (w - size - margin, margin))
        out = io.BytesIO()
        base.convert("RGB").save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception:
        return img_bytes

async def store_watermarked_poster(context, chat_id, key, img_bytes):
    """লোগোসহ ছবিটা অ্যাডমিনের চ্যাটে পাঠায়, আর টেলিগ্রামের দেওয়া নতুন file_id পোস্টার হিসেবে সেভ করে।"""
    marked = await asyncio.get_running_loop().run_in_executor(None, apply_watermark, img_bytes)
    msg = await context.bot.send_photo(chat_id=chat_id, photo=marked, caption="Poster saved with logo")
    title_posters[key] = msg.photo[-1].file_id
    save_state("posters", POSTERS_FILE, title_posters)

def poster_key(title, path):
    return title if not path else title + "::" + " / ".join(path)

def find_poster(title, path):
    """সবচেয়ে নির্দিষ্ট পোস্টার আগে (যেমন Part 2), না থাকলে উপরের ধাপের, শেষে মূল টাইটেলের।"""
    for i in range(len(path), -1, -1):
        p = title_posters.get(poster_key(title, path[:i]))
        if p:
            return p
    return None

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
    segments = [x.strip() for x in raw.split("|") if x.strip()]
    url = None
    if len(segments) > 1 and segments[-1].lower().startswith(("http://", "https://")):
        url = segments.pop()
    res = resolve_title_and_path(segments) if segments else None
    if res is None or res[0]:
        await update.message.reply_text(t(uid, "admin_not_found"))
        return
    _, matched, nested = res
    key = poster_key(matched, nested)
    shown = matched if not nested else f"{matched} - {' / '.join(nested)}"
    if url:
        try:
            import urllib.request
            def _fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read()
            raw_img = await asyncio.get_running_loop().run_in_executor(None, _fetch)
            await store_watermarked_poster(context, update.effective_chat.id, key, raw_img)
        except Exception:
            title_posters[key] = url
            save_state("posters", POSTERS_FILE, title_posters)
        await update.message.reply_text(f"{t(uid, 'admin_added')}\n{shown}")
        return
    awaiting_poster[uid] = key
    await update.message.reply_text(f"Now send the poster photo for:\n{shown}")

async def receive_poster_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID or uid not in awaiting_poster:
        return
    title = awaiting_poster.pop(uid)
    photo = update.message.photo[-1]
    try:
        tg_file = await photo.get_file()
        raw_img = bytes(await tg_file.download_as_bytearray())
        await store_watermarked_poster(context, update.effective_chat.id, title, raw_img)
    except Exception:
        title_posters[title] = photo.file_id   # সমস্যা হলে লোগো ছাড়া আসল ছবিই সেভ হয়
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
    notified = set()   # একই ইউজার একই টাইটেলের জন্য মাত্র একবার নোটিফিকেশন পাবে
    new_sq = _squash(new_title)
    for query_key, user_ids in list(pending_requests.items()):
        qsq = _squash(query_key)
        words = [w for w in re.split(r"[^0-9a-z]+", new_title.lower()) if w]
        strict = bool(qsq) and (qsq in new_sq or any(
            difflib.SequenceMatcher(None, query_key.lower(), w).ratio() >= 0.8 for w in words))
        if strict:
            for req_uid in user_ids:
                if req_uid in notified:
                    continue
                notified.add(req_uid)
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

def pyramid_rows(flat):
    """সারি ১,২,৩,৩… বাটন (পিরামিড)। ছোট নাম ভাগ হয়ে বসে, বড় নাম পুরো সারি নেয়।"""
    def cap(b):
        n = len(b.text)
        return 3 if n <= 11 else (2 if n <= 22 else 1)
    rows, i, r = [], 0, 0
    while i < len(flat):
        s = min(r + 1, 3)
        while s > 1 and (i + s > len(flat) or any(cap(b) < s for b in flat[i:i + s])):
            s -= 1
        rows.append(flat[i:i + s])
        i += s
        r += 1
    return rows

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
        label = ti.get("display", ti["title"])
        if ti.get("path") and label.startswith(ti["title"] + " - "):
            label = " - ".join(ti["path"])   # ফোল্ডারের ভেতরে শুধু Season 1 / Part-1 দেখাবে, পুরো নাম নয়
        flat.append(InlineKeyboardButton(label, callback_data=f"catnav::{len(children) - 1}"))

    category_browse_state[uid] = {"path": path, "children": children}

    buttons = pyramid_rows(flat)
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

    display = matched_title if not nested_path else " - ".join(nested_path)
    entry = {"title": matched_title, "path": nested_path, "display": display}

    node = category_tree
    for seg in folder_path:
        node = node.setdefault(seg, {})
    node.setdefault("_titles", [])
    already = any(
        isinstance(e, dict) and e.get("title") == matched_title and e.get("path", []) == nested_path
        for e in node["_titles"]
    )
    if already:
        await update.message.reply_text(f"{t(uid, 'admin_already')}\n{' / '.join(folder_path)} - {display}")
        return

    # একই টাইটেল (একই সিজন/পার্টসহ) অন্য ফোল্ডারে থাকলে সেখান থেকে সরিয়ে নতুন জায়গায় আনা হবে
    moved_from = []
    def _purge(tree, trail):
        for key, val in list(tree.items()):
            if key == "_titles":
                keep = []
                for e in val:
                    if isinstance(e, dict) and e.get("title") == matched_title and e.get("path", []) == nested_path and trail != folder_path:
                        moved_from.append(" / ".join(trail))
                    else:
                        keep.append(e)
                tree["_titles"] = keep
            elif isinstance(val, dict):
                _purge(val, trail + [key])
    _purge(category_tree, [])

    node = category_tree
    for seg in folder_path:
        node = node.setdefault(seg, {})
    node.setdefault("_titles", [])
    node["_titles"].append(entry)
    save_state("categories", CATEGORY_FILE, category_tree)
    if moved_from:
        await update.message.reply_text(
            f"{t(uid, 'admin_updated')}\n{display}\n{' | '.join(moved_from)}  ->  {' / '.join(folder_path)}")
    else:
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

async def wrap_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wrapcategory ফোল্ডার | সাব-ফোল্ডার | নতুন-নাম — ফোল্ডারের ভেতরের সবকিছু একটা নতুন সাব-ফোল্ডারে ঢুকিয়ে দেয়।"""
    if update.effective_user.id != ADMIN_ID:
        return
    uid = update.effective_user.id
    parts = (update.message.text or "").split(" ", 1)
    segments = [x.strip() for x in parts[1].split("|")] if len(parts) > 1 else []
    if len(segments) < 2 or not all(segments):
        await update.message.reply_text(
            f"{t(uid, 'admin_format_error')}\n/wrapcategory Folder | ... | New Subfolder\n"
            "Example: /wrapcategory Hollywood | Marvel | Main Universe")
        return
    *folder_path, new_name = segments
    node = category_tree
    real_path = []
    for seg in folder_path:
        key = find_key_ci(node, seg)
        if key is None or not isinstance(node.get(key), dict):
            await update.message.reply_text(t(uid, "admin_not_found"))
            return
        real_path.append(key)
        node = node[key]
    if find_key_ci(node, new_name) is not None:
        await update.message.reply_text(f"{t(uid, 'admin_already')}\n{' / '.join(real_path)} / {new_name}")
        return
    moved = dict(node)
    node.clear()
    node[new_name] = moved
    save_state("categories", CATEGORY_FILE, category_tree)
    await update.message.reply_text(f"{t(uid, 'admin_updated')}\n{' / '.join(real_path)} / {new_name}")

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
    poster = find_poster(state["title"], state["path"])
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
    application.add_handler(CommandHandler("wrapcategory", wrap_category))
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
