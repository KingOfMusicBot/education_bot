# bot.py
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient

# ========================= CONFIG =========================
BOT_TOKEN = "7572890989:AAGizMW3AO9mA-PONpEFAL4NBO6jldL-fNk"           # <-- replace
MONGO_URI = "mongodb+srv://parice819:fOJsdMBDj7xMKVFW@cluster0.str54m7.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"            # <-- replace

# Provided by you
ADMIN_IDS = [8142003954, 6722991035]
SHORT_API = "be0a750eaa503966539bb811a849dd99ced62f24"

LIMIT_FREE = 10
DB_NAME = "lecture_bot"

# ========================= INIT ===========================
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

mongo = MongoClient(MONGO_URI)
db = mongo[DB_NAME]

users_col = db.users
lectures_col = db.lectures
chapters_col = db.chapters

# ========================= UTIL ===========================
def is_admin(uid):
    return uid in ADMIN_IDS

def get_user(uid: int):
    u = users_col.find_one({"_id": uid})
    if not u:
        users_col.insert_one({
            "_id": uid,
            "premium": False,
            "expiry": None,
            "pending": None
        })
        return {"premium": False, "expiry": None, "pending": None}
    return u

# ========================= ADMIN ==========================
@dp.message_handler(commands=["add_chapter"])
async def add_chapter(message: types.Message):
    """
    Usage:
    /add_chapter batch subject chapter_id "Chapter Name"
    Example:
    /add_chapter Arjuna_jee_2026 physics ch01 "Kinematics Basics"
    """
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Not admin.")
    try:
        parts = message.text.split()
        if len(parts) < 5:
            raise ValueError("bad")
        _, batch, subject, chapter_id = parts[:4]
        rest = message.text.split(chapter_id, 1)[1].strip()
        chapter_name = rest.strip().strip('"').strip()
        chapters_col.insert_one({
            "batch": batch,
            "subject": subject,
            "chapter_id": chapter_id,
            "chapter_name": chapter_name,
            "created_at": datetime.utcnow()
        })
        await message.reply(f"✔ Chapter added: {batch}/{subject}/{chapter_id} — {chapter_name}")
    except Exception as e:
        logging.exception(e)
        await message.reply(
            "Usage:\n/add_chapter batch subject chapter_id \"Chapter Name\"\n\n"
            "Example:\n/add_chapter Arjuna_jee_2026 physics ch01 \"Kinematics Basics\""
        )

@dp.message_handler(commands=["add_lecture"])
async def add_lecture(message: types.Message):
    """
    Usage:
    /add_lecture batch subject chapter_id lecture_no channel_id message_id
    """
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Not admin.")
    try:
        _, batch, subject, chapter_id, lec_no, channel, msgid = message.text.split()
        lectures_col.insert_one({
            "batch": batch,
            "subject": subject,
            "chapter": chapter_id,
            "lec_no": int(lec_no),
            "channel_id": int(channel),
            "message_id": int(msgid),
            "created_at": datetime.utcnow()
        })
        await message.reply(f"✔ Lecture added: {batch}/{subject}/{chapter_id}/L{lec_no}")
    except Exception as e:
        logging.exception(e)
        await message.reply(
            "Usage:\n/add_lecture batch subject chapter_id lecture_no channel_id message_id\n"
            "Example:\n/add_lecture Arjuna_jee_2026 physics ch01 1 -100123456789 45"
        )

@dp.message_handler(commands=["set_premium"])
async def set_premium(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid, days = message.text.split()
        uid = int(uid); days = int(days)
        users_col.update_one(
            {"_id": uid},
            {"$set": {"premium": True, "expiry": datetime.utcnow() + timedelta(days=days)}},
            upsert=True
        )
        await message.reply(f"⭐ Premium activated for {uid} ({days} days)")
    except Exception as e:
        logging.exception(e)
        await message.reply("Usage:\n/set_premium user_id days")

@dp.message_handler(commands=["revoke"])
async def revoke(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid = message.text.split()
        uid = int(uid)
        users_col.update_one({"_id": uid}, {"$set": {"premium": False}})
        await message.reply("❌ Premium Removed")
    except Exception as e:
        logging.exception(e)
        await message.reply("Usage:\n/revoke user_id")

# ========================= START / MENU ===================
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    # deep-link unlock handling (token)
    if "token_" in (message.text or ""):
        return await unlock_start(message)

    kb = InlineKeyboardMarkup()
    batches = lectures_col.distinct("batch")
    if not batches:
        return await message.answer("No batches configured yet.")
    for batch in batches:
        kb.add(InlineKeyboardButton(batch, callback_data=f"batch|{batch}"))
    await message.answer("📚 Select Batch", reply_markup=kb)

# =================== SELECT SUBJECT ======================
@dp.callback_query_handler(lambda c: c.data.startswith("batch|"))
async def select_subject(c: types.CallbackQuery):
    _, batch = c.data.split("|", 1)
    subjects_set = set()
    for s in lectures_col.find({"batch": batch}).distinct("subject"):
        subjects_set.add(s)
    for s in chapters_col.find({"batch": batch}).distinct("subject"):
        subjects_set.add(s)
    if not subjects_set:
        return await c.message.edit_text("No subjects found for this batch.")
    kb = InlineKeyboardMarkup()
    for sub in sorted(subjects_set):
        kb.add(InlineKeyboardButton(sub, callback_data=f"sub|{batch}|{sub}"))
    await c.message.edit_text(f"📖 {batch}\nChoose Subject", reply_markup=kb)

# =================== SELECT CHAPTER ======================
@dp.callback_query_handler(lambda c: c.data.startswith("sub|"))
async def select_chapter(c: types.CallbackQuery):
    _, batch, sub = c.data.split("|", 2)
    chapters = list(chapters_col.find({"batch": batch, "subject": sub}).sort("created_at", 1))
    kb = InlineKeyboardMarkup()
    if chapters:
        for ch in chapters:
            cid = ch["chapter_id"]
            cname = ch.get("chapter_name") or cid
            kb.add(InlineKeyboardButton(f"{cname}", callback_data=f"chap|{batch}|{sub}|{cid}"))
    else:
        # fallback: group lectures by chapter field (may be empty)
        for ch in lectures_col.find({"batch": batch, "subject": sub}).distinct("chapter"):
            cid = ch or "default"
            kb.add(InlineKeyboardButton(f"{cid}", callback_data=f"chap|{batch}|{sub}|{cid}"))
    await c.message.edit_text(f"📚 {batch} / {sub}\nSelect Chapter", reply_markup=kb)

# =================== SELECT LECTURE ======================
@dp.callback_query_handler(lambda c: c.data.startswith("chap|"))
async def select_lecture(c: types.CallbackQuery):
    _, batch, sub, chapter_id = c.data.split("|", 3)
    kb = InlineKeyboardMarkup(row_width=5)
    cursor = lectures_col.find({"batch": batch, "subject": sub, "chapter": chapter_id}).sort("lec_no", 1)
    found = False
    for lec in cursor:
        found = True
        n = lec["lec_no"]
        kb.insert(InlineKeyboardButton(str(n), callback_data=f"lec|{batch}|{sub}|{chapter_id}|{n}"))
    if not found:
        return await c.message.edit_text("No lectures found in this chapter.")
    await c.message.edit_text(f"🎬 {batch}/{sub}/{chapter_id}\nSelect Lecture", reply_markup=kb)

# ------------- TOKEN-BASED VERIFICATION FLOW -------------
@dp.callback_query_handler(lambda c: c.data.startswith("lec|"))
async def lecture_request(c: types.CallbackQuery):
    uid = c.from_user.id
    _, batch, sub, chapter_id, lec = c.data.split("|", 4)
    lec = int(lec)

    u = get_user(uid)
    premium = bool(u.get("premium")) and u.get("expiry") and u["expiry"] > datetime.utcnow()

    # Premium check: if above free limit and not premium -> ask to buy
    if lec > LIMIT_FREE and not premium:
        return await c.message.answer("🔒 Premium required for this lecture.")

    # If premium -> forward directly
    if premium:
        lec_doc = lectures_col.find_one({"batch": batch, "subject": sub, "chapter": chapter_id, "lec_no": lec})
        if not lec_doc:
            return await c.message.answer("Lecture not found.")
        await bot.forward_message(uid, lec_doc["channel_id"], lec_doc["message_id"])
        return await c.answer("▶ Sent")

    # FREE lecture -> create single-use token & save pending mapping
    token = secrets.token_urlsafe(12)
    users_col.update_one(
        {"_id": uid},
        {"$set": {"pending": {
            "token": token,
            "batch": batch,
            "subject": sub,
            "chapter": chapter_id,
            "lec": lec,
            "created_at": datetime.utcnow()
        }}},
        upsert=True
    )

    me = await bot.get_me()
    long_link = f"https://t.me/{me.username}?start=token_{token}"

    # Shorten link with params safely
    try:
        api_url = "https://arolinks.com/api"
        params = {"api": SHORT_API, "url": long_link}
        resp = requests.get(api_url, params=params, timeout=10)
        data = resp.json()
        short_url = data.get("shortenedUrl") or long_link
    except Exception as e:
        logging.exception(e)
        short_url = long_link

    text = (
        "🔐 Verification needed (cannot be skipped).\n\n"
        "1) Open the link below\n"
        "2) Complete the shortner flow\n"
        "3) When redirected back to bot, lecture will unlock automatically.\n\n"
        f"{short_url}\n\n"
        "⚠ Lecture will unlock only if you return via this link."
    )
    await c.message.answer(text)
    await c.answer()

# ==================== UNLOCK HANDLER (TOKEN) =====================
async def unlock_start(m: types.Message):
    """
    Handles deep-link like: /start token_<token>
    """
    try:
        text = m.text or ""
        if "token_" not in text:
            return await m.answer("❌ No verification token found in start command.")

        token = None
        try:
            token = text.split("token_", 1)[1].strip()
        except:
            decoded = urllib.parse.unquote_plus(text)
            if "token_" in decoded:
                token = decoded.split("token_", 1)[1].strip()

        if not token:
            return await m.answer("❌ Invalid verification token.")

        # Find which user has this pending token
        # We expect the user returning to be the same whose pending token exists.
        uid = m.from_user.id
        u = get_user(uid)
        pending = u.get("pending")
        if not pending or pending.get("token") != token:
            return await m.answer("❌ Verification mismatch or expired. Open the short link again.")

        # All good: forward lecture and clear pending
        batch = pending["batch"]
        sub = pending["subject"]
        chapter = pending["chapter"]
        lec = int(pending["lec"])

        lec_doc = lectures_col.find_one({"batch": batch, "subject": sub, "chapter": chapter, "lec_no": lec})
        if not lec_doc:
            return await m.answer("Lecture not found (contact admin).")

        users_col.update_one({"_id": uid}, {"$unset": {"pending": ""}})

        await bot.forward_message(uid, lec_doc["channel_id"], lec_doc["message_id"])
        return await m.answer("🎉 Verified — Lecture Unlocked!")
    except Exception as e:
        logging.exception(e)
        return await m.answer("Verification failed — try again.")

# ==========================================================
if __name__ == "__main__":
    logging.info("Bot starting with token-based verification and chapters...")
    executor.start_polling(dp, skip_updates=True)
