import os
import json
import logging
import asyncio

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

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # তোমার নিজের টেলিগ্রাম ইউজার আইডি
DB_FILE = "data.json"

# ---------- ডাটা লোড/সেভ ----------
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

db = load_db()
# গঠন: { "movie/song নাম": { "quality": "google drive link" } }

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Type a movie or song name to search.\n"
        "स्वागत है! मूवी या गाने का नाम लिखकर खोजें।\n"
        "স্বাগতম! মুভি বা গানের নাম লিখে সার্চ করো।"
    )

# ---------- অ্যাডমিন: লিংক যোগ করা ----------
# ব্যবহার: /add টাইটেল | কোয়ালিটি | লিংক
# উদাহরণ: /add Amar Movie | 720 | https://drive.google.com/xyz
async def add_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return  # অ্যাডমিন ছাড়া কেউ যোগ করতে পারবে না

    text = update.message.text or ""
    parts = text.split(" ", 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await update.message.reply_text(
            "ফরম্যাট ভুল। এভাবে লিখো:\n"
            "/add টাইটেল | কোয়ালিটি | লিংক\n"
            "উদাহরণ: /add Amar Movie | 720 | https://drive.google.com/xyz"
        )
        return

    payload = parts[1]
    segments = [s.strip() for s in payload.split("|")]
    if len(segments) != 3:
        await update.message.reply_text(
            "ফরম্যাট ভুল। এভাবে লিখো:\n"
            "/add টাইটেল | কোয়ালিটি | লিংক\n"
            "উদাহরণ: /add Amar Movie | 720 | https://drive.google.com/xyz"
        )
        return

    title, quality, link = segments
    if not title or not quality or not link:
        await update.message.reply_text("টাইটেল, কোয়ালিটি ও লিংক তিনটেই দিতে হবে।")
        return

    db.setdefault(title, {})[quality] = link
    save_db(db)
    await update.message.reply_text(f"যোগ হয়েছে ✅\nটাইটেল: {title}\nকোয়ালিটি: {quality}")

# ---------- অ্যাডমিন: কনটেন্ট ডিলিট করা ----------
# ব্যবহার: /remove টাইটেল
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
        save_db(db)
        await update.message.reply_text(f"ডিলিট হয়েছে ✅: {title}")
    else:
        await update.message.reply_text("এই টাইটেল পাওয়া যায়নি।")

# ---------- সার্চ (ইউজার টেক্সট পাঠালে) ----------
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    if not query:
        return

    matches = [title for title in db.keys() if query in title.lower()]

    if not matches:
        await update.message.reply_text(
            "Not found.\nकुछ नहीं मिला।\nকিছু পাওয়া যায়নি।"
        )
        return

    buttons = [
        [InlineKeyboardButton(title, callback_data=f"title::{title}")]
        for title in matches[:15]
    ]
    await update.message.reply_text(
        "Results / परिणाम / রেজাল্ট:", reply_markup=InlineKeyboardMarkup(buttons)
    )

# ---------- টাইটেল সিলেক্ট করলে কোয়ালিটি দেখানো ----------
async def show_qualities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    title = query.data.split("::", 1)[1]

    qualities = db.get(title, {})
    if not qualities:
        await query.edit_message_text(
            "This content is no longer available.\n"
            "यह सामग्री अब उपलब्ध नहीं है।\n"
            "এই কনটেন্ট আর পাওয়া যাচ্ছে না।"
        )
        return

    buttons = [
        [InlineKeyboardButton(q, callback_data=f"get::{title}::{q}")]
        for q in qualities.keys()
    ]
    await query.edit_message_text(
        f"{title}\nSelect quality / क्वालिटी चुनें / কোয়ালিটি সিলেক্ট করো:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ---------- কোয়ালিটি সিলেক্ট করলে ডাউনলোড লিংক পাঠানো ----------
async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, title, quality = query.data.split("::", 2)

    link = db.get(title, {}).get(quality)
    if not link:
        await query.message.reply_text(
            "Link not found.\nलिंक नहीं मिला।\nলিংক পাওয়া যায়নি।"
        )
        return

    await query.message.reply_text(
        f"{title} ({quality})\n"
        f"Download link / डाउनलोड लिंक / ডাউনলোড লিংক:\n{link}"
    )

# ---------- webhook মোড (Render-এর জন্য, starlette+uvicorn দিয়ে) ----------
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
    application.add_handler(CommandHandler("remove", remove_content))
    application.add_handler(CallbackQueryHandler(show_qualities, pattern=r"^title::"))
    application.add_handler(CallbackQueryHandler(send_file, pattern=r"^get::"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
    return application

def main():
    application = build_application()

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    port = os.environ.get("PORT")

    if render_url and port:
        # Render (বা অন্য যেকোনো ওয়েব-সার্ভিস হোস্টিং)-এর জন্য webhook মোড
        asyncio.run(run_webhook_server(application, render_url, int(port)))
    else:
        # সাধারণ polling মোড (VPS/লোকাল রানের জন্য)
        application.run_polling()

if __name__ == "__main__":
    main()
