import os
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

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
# গঠন: { "movie/song নাম": { "quality": file_id, ... } }

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "স্বাগতম!\nমুভি বা গানের নাম লিখে সার্চ করো।"
    )

# ---------- অ্যাডমিন: কনটেন্ট যোগ করা ----------
# ভিডিও/অডিও পাঠানোর সময় ক্যাপশনে লিখতে হবে: টাইটেল | কোয়ালিটি
# উদাহরণ ক্যাপশন: Amar Video | 720
async def add_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return  # অ্যাডমিন ছাড়া কেউ যোগ করতে পারবে না

    caption = update.message.caption
    if not caption or "|" not in caption:
        await update.message.reply_text(
            "ক্যাপশন ফরম্যাট ভুল। এভাবে লিখো: টাইটেল | কোয়ালিটি\nউদাহরণ: Amar Video | 720"
        )
        return

    title, quality = [x.strip() for x in caption.split("|", 1)]

    if update.message.video:
        file_id = update.message.video.file_id
    elif update.message.audio:
        file_id = update.message.audio.file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("ভিডিও/অডিও/ফাইল পাঠাও।")
        return

    db.setdefault(title, {})[quality] = file_id
    save_db(db)
    await update.message.reply_text(f"যোগ হয়েছে ✅\nটাইটেল: {title}\nকোয়ালিটি: {quality}")

# ---------- সার্চ (ইউজার টেক্সট পাঠালে) ----------
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    if not query:
        return

    matches = [title for title in db.keys() if query in title.lower()]

    if not matches:
        await update.message.reply_text("কিছু পাওয়া যায়নি।")
        return

    buttons = [
        [InlineKeyboardButton(title, callback_data=f"title::{title}")]
        for title in matches[:15]
    ]
    await update.message.reply_text(
        "রেজাল্ট:", reply_markup=InlineKeyboardMarkup(buttons)
    )

# ---------- টাইটেল সিলেক্ট করলে কোয়ালিটি দেখানো ----------
async def show_qualities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    title = query.data.split("::", 1)[1]

    qualities = db.get(title, {})
    if not qualities:
        await query.edit_message_text("এই কনটেন্ট আর পাওয়া যাচ্ছে না।")
        return

    buttons = [
        [InlineKeyboardButton(q, callback_data=f"get::{title}::{q}")]
        for q in qualities.keys()
    ]
    await query.edit_message_text(
        f"{title}\nকোয়ালিটি সিলেক্ট করো:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ---------- কোয়ালিটি সিলেক্ট করলে ফাইল পাঠানো ----------
async def send_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, title, quality = query.data.split("::", 2)

    file_id = db.get(title, {}).get(quality)
    if not file_id:
        await query.message.reply_text("ফাইল পাওয়া যায়নি।")
        return

    await context.bot.send_document(chat_id=query.message.chat_id, document=file_id)

# ---------- মেইন ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        (filters.VIDEO | filters.AUDIO | filters.Document.ALL) & filters.CAPTION,
        add_content
    ))
    app.add_handler(CallbackQueryHandler(show_qualities, pattern=r"^title::"))
    app.add_handler(CallbackQueryHandler(send_file, pattern=r"^get::"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    port = os.environ.get("PORT")

    if render_url and port:
        # Render (ও অন্য যেকোনো ওয়েব-সার্ভিস হোস্টিং)-এর জন্য webhook মোড
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=BOT_TOKEN,
            webhook_url=f"{render_url}/{BOT_TOKEN}",
        )
    else:
        # সাধারণ polling মোড (VPS/লোকাল রানের জন্য)
        app.run_polling()

if __name__ == "__main__":
    main()
