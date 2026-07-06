import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, date

import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ============================================================
#                       تنظیمات اصلی
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "10"))  # تعداد دانلود مجاز روزانه هر کاربر
DOWNLOAD_DIR = "downloads"
DB_PATH = "bot_database.db"
MAX_FILE_SIZE_MB = 1950  # تلگرام حداکثر ۲ گیگ (کمی کمتر برای اطمینان می‌گیریم)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

URL_REGEX = re.compile(r"https?://\S+")

# در حافظه: اطلاعات موقت هر لینک تا وقتی کاربر کیفیت رو انتخاب کنه
pending_links: dict[str, dict] = {}


# ============================================================
#                       دیتابیس
# ============================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT,
            total_downloads INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id INTEGER,
            usage_date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        )
        """
    )
    conn.commit()
    conn.close()


def register_user(user_id: int, username: str):
    conn = db_connect()
    existing = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO users (user_id, username, first_seen, total_downloads) VALUES (?, ?, ?, 0)",
            (user_id, username, datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


def get_today_usage(user_id: int) -> int:
    today = date.today().isoformat()
    conn = db_connect()
    row = conn.execute(
        "SELECT count FROM daily_usage WHERE user_id=? AND usage_date=?", (user_id, today)
    ).fetchone()
    conn.close()
    return row["count"] if row else 0


def increment_usage(user_id: int):
    today = date.today().isoformat()
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO daily_usage (user_id, usage_date, count) VALUES (?, ?, 1)
        ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1
        """,
        (user_id, today),
    )
    conn.execute(
        "UPDATE users SET total_downloads = total_downloads + 1 WHERE user_id=?", (user_id,)
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = db_connect()
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total_downloads = conn.execute("SELECT SUM(total_downloads) c FROM users").fetchone()["c"] or 0
    today = date.today().isoformat()
    today_downloads = conn.execute(
        "SELECT SUM(count) c FROM daily_usage WHERE usage_date=?", (today,)
    ).fetchone()["c"] or 0
    conn.close()
    return {
        "total_users": total_users,
        "total_downloads": total_downloads,
        "today_downloads": today_downloads,
    }


# ============================================================
#                   کیبورد‌های شیشه‌ای (Inline)
# ============================================================
def quality_keyboard(link_id: str) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="🎬 کیفیت بالا (HD)", callback_data=f"q:high:{link_id}"),
        ],
        [
            InlineKeyboardButton(text="📺 کیفیت متوسط", callback_data=f"q:medium:{link_id}"),
        ],
        [
            InlineKeyboardButton(text="📱 کیفیت پایین (سریع)", callback_data=f"q:low:{link_id}"),
        ],
        [
            InlineKeyboardButton(text="🎵 فقط صدا (MP3)", callback_data=f"q:audio:{link_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_selection_map():
    return {
        "high": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "medium": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
        "low": "bestvideo[height<=240]+bestaudio/best[height<=240]/best",
        "audio": "bestaudio/best",
    }


# ============================================================
#                    دانلود با yt-dlp
# ============================================================
def _run_download(url: str, mode: str, out_path_template: str) -> dict:
    """این تابع بلاک‌کننده است و باید در thread جدا اجرا شود."""
    fmt_map = format_selection_map()
    ydl_opts = {
        "format": fmt_map[mode],
        "outtmpl": out_path_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4" if mode != "audio" else None,
    }

    if mode == "audio":
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if mode == "audio":
            base, _ = os.path.splitext(filename)
            filename = base + ".mp3"
        return {"filename": filename, "title": info.get("title", "video"), "info": info}


async def download_media(url: str, mode: str, user_id: int) -> dict:
    ts = int(time.time())
    out_template = os.path.join(DOWNLOAD_DIR, f"{user_id}_{ts}_%(title).60s.%(ext)s")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_download, url, mode, out_template)
    return result


# ============================================================
#                        هندلرها
# ============================================================
WELCOME_TEXT = """
👋 سلام {name} عزیز!

به <b>ربات دانلودر همه‌کاره</b> خوش اومدی 🚀

📥 فقط کافیه لینک ویدیو رو از یکی از این پلتفرم‌ها برام بفرستی:

🔴 یوتیوب (YouTube)
📸 اینستاگرام (Instagram)
🎵 تیک‌تاک (TikTok)
🐦 توییتر / X

بعد از ارسال لینک، کیفیت مورد نظرت رو انتخاب کن و منتظر بمون تا فایل برات آماده بشه ✅

📊 محدودیت روزانه شما: <b>{limit} دانلود</b>

برای دیدن راهنما: /help
"""

HELP_TEXT = """
📖 <b>راهنمای استفاده از ربات</b>

1️⃣ لینک ویدیوی مورد نظر رو کپی کن
2️⃣ همینجا برام ارسالش کن
3️⃣ از بین گزینه‌ها کیفیت دلخواه رو انتخاب کن
4️⃣ منتظر بمون تا دانلود و ارسال بشه 🎉

<b>پلتفرم‌های پشتیبانی شده:</b>
• یوتیوب
• اینستاگرام (پست، ریلز، IGTV)
• تیک‌تاک
• توییتر/X
• و ده‌ها سایت دیگر

⚠️ توجه: هر کاربر روزانه محدودیت دانلود دارد.

دستورات:
/start - شروع مجدد
/help - راهنما
/stats - آمار استفاده شما
"""


@router.message(CommandStart())
async def cmd_start(message: Message):
    register_user(message.from_user.id, message.from_user.username or "")
    await message.answer(
        WELCOME_TEXT.format(name=message.from_user.first_name, limit=DAILY_LIMIT)
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@router.message(Command("stats"))
async def cmd_user_stats(message: Message):
    used = get_today_usage(message.from_user.id)
    remaining = max(DAILY_LIMIT - used, 0)
    await message.answer(
        f"📊 <b>آمار امروز شما</b>\n\n"
        f"✅ استفاده شده: {used}\n"
        f"🔄 باقی‌مانده: {remaining}\n"
        f"📅 محدودیت روزانه: {DAILY_LIMIT}"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔️ شما دسترسی ادمین ندارید.")
        return
    stats = get_stats()
    await message.answer(
        "🛠 <b>پنل ادمین</b>\n\n"
        f"👥 تعداد کل کاربران: <b>{stats['total_users']}</b>\n"
        f"📥 مجموع دانلودها: <b>{stats['total_downloads']}</b>\n"
        f"📅 دانلود امروز: <b>{stats['today_downloads']}</b>"
    )


@router.message(F.text.regexp(URL_REGEX))
async def handle_link(message: Message):
    register_user(message.from_user.id, message.from_user.username or "")

    used = get_today_usage(message.from_user.id)
    if used >= DAILY_LIMIT:
        await message.answer(
            f"⛔️ متأسفانه محدودیت روزانه شما ({DAILY_LIMIT} دانلود) تمام شده.\n"
            "فردا دوباره امتحان کنید 🙏"
        )
        return

    match = URL_REGEX.search(message.text)
    url = match.group(0)

    link_id = f"{message.from_user.id}_{int(time.time())}"
    pending_links[link_id] = {"url": url, "chat_id": message.chat.id}

    await message.answer(
        "🔗 لینک دریافت شد!\n\n"
        "لطفاً کیفیت مورد نظر رو انتخاب کن 👇",
        reply_markup=quality_keyboard(link_id),
    )


@router.callback_query(F.data.startswith("q:"))
async def handle_quality_choice(callback: CallbackQuery):
    _, mode, link_id = callback.data.split(":", 2)

    data = pending_links.get(link_id)
    if not data:
        await callback.answer("⛔️ این لینک منقضی شده، لطفاً دوباره ارسال کنید.", show_alert=True)
        return

    user_id = callback.from_user.id
    used = get_today_usage(user_id)
    if used >= DAILY_LIMIT:
        await callback.message.edit_text(f"⛔️ محدودیت روزانه شما ({DAILY_LIMIT}) تمام شده.")
        return

    await callback.answer("⏳ در حال دانلود...")
    await callback.message.edit_text("⏳ در حال پردازش و دانلود، لطفاً صبر کنید...\n(ممکن است چند ثانیه تا چند دقیقه طول بکشد)")

    url = data["url"]
    try:
        result = await download_media(url, mode, user_id)
        filepath = result["filename"]
        title = result["title"]

        if not os.path.exists(filepath):
            raise FileNotFoundError("فایل خروجی ساخته نشد.")

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await callback.message.edit_text(
                f"⚠️ فایل خیلی بزرگه ({size_mb:.0f} مگابایت) و تلگرام اجازه ارسال نمی‌ده.\n"
                "لطفاً کیفیت پایین‌تری رو امتحان کن."
            )
            os.remove(filepath)
            return

        caption = f"✅ <b>{title}</b>\n\n🤖 دانلود شده توسط ربات"
        file_input = FSInputFile(filepath)

        if mode == "audio":
            await bot.send_audio(chat_id=callback.message.chat.id, audio=file_input, caption=caption)
        else:
            await bot.send_video(chat_id=callback.message.chat.id, video=file_input, caption=caption, supports_streaming=True)

        increment_usage(user_id)
        remaining = DAILY_LIMIT - get_today_usage(user_id)
        await callback.message.edit_text(f"✅ ارسال شد!\n📊 دانلود باقی‌مانده امروز: {remaining}")

        os.remove(filepath)

    except Exception as e:
        logger.exception("Download failed")
        error_msg = str(e)[:200]
        await callback.message.edit_text(
            f"❌ متأسفانه در دانلود مشکلی پیش اومد.\n\n<code>{error_msg}</code>\n\n"
            "لطفاً لینک رو بررسی کن یا کیفیت دیگه‌ای رو امتحان کن."
        )
    finally:
        pending_links.pop(link_id, None)


@router.message()
async def handle_other(message: Message):
    await message.answer(
        "🤔 متوجه نشدم!\n\n"
        "لطفاً یک لینک معتبر از یوتیوب، اینستاگرام، تیک‌تاک یا توییتر ارسال کن.\n"
        "برای راهنما /help رو بزن."
    )


# ============================================================
#                          اجرا
# ============================================================
async def main():
    init_db()
    logger.info("Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
