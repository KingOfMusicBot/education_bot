# bot.py
import logging
import secrets
import urllib.parse
import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
import requests
from pymongo import MongoClient
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import pytz

# ================= CONFIG - REPLACE THESE =================
BOT_TOKEN = "7572890989:AAFQizQJs0y48AFEpU4r_-iypYxtD7mLu2U"
MONGO_URI = "mongodb+srv://parice819:fOJsdMBDj7xMKVFW@cluster0.str54m7.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
SHORT_API = "be0a750eaa503966539bb811a849dd99ced62f24"

# Admins - numeric Telegram IDs (replace with your real admin IDs)
ADMIN_IDS = [8142003954, 6722991035]

# Channels user must join (public username or -100id). Keep empty [] if not enforcing.
REQUIRED_CHANNELS = ["@YourAnnounceChannel", ]  # example

# ADMIN BYPASS flag: if True, admins will NOT require shortener verification
ADMIN_BYPASS = True

# Anti-abuse config
TOKEN_EXPIRY_SECONDS = 10 * 60        # token valid for 10 minutes
LECTURE_COOLDOWN_SECONDS = 5 * 60     # cooldown per lecture
DAILY_UNLOCK_LIMIT = 30               # max free unlocks per day
LIMIT_FREE = 10                       # free lecture count per subject (1..10)

DB_NAME = "lecture_bot"

# ----------------- Auto-update (git) config -----------------
REPO_PATH = "/root/education_bot"   # <-- set to your repo folder (absolute)
GIT_BRANCH = "main"                 # <-- branch to pull from
AUTO_INSTALL_REQUIRES = False        # <-- set True if you want pip install -r requirements.txt automatically

# ================= INIT =================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("edu_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

mongo = MongoClient(MONGO_URI)
db = mongo[DB_NAME]

users_col = db.users
lectures_col = db.lectures
chapters_col = db.chapters
tokens_col = db.tokens
analytics_col = db.analytics
payments_col = db.payments if 'payments' in db.list_collection_names() else db.payments

# In-memory helper for admin forwarded capture
LAST_FORWARDED = {}

# ================= UTIL =================
def is_admin(uid): return uid in ADMIN_IDS
def today_str(): return datetime.utcnow().strftime("%Y-%m-%d")

def get_user(uid: int):
    u = users_col.find_one({"_id": uid})
    if not u:
        doc = {
            "_id": uid,
            "premium": False,
            "expiry": None,
            "pending": None,
            "daily_unlocks": {"date": today_str(), "count": 0},
            "cooldowns": {},
            "last_unlocked": None
        }
        users_col.insert_one(doc)
        return doc
    return u

async def check_subscriptions(uid):
    # returns (True, None) or (False, missing_channel)
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=uid)
            if member.status in ("left", "kicked"):
                return False, ch
        except Exception as e:
            # Could be bot not admin or channel private
            logger.debug("check_subscriptions exception for %s: %s", ch, e)
            return False, ch
    return True, None

# ---------------- helper: shell-quote ----------------
def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"

# ================= ADMIN: capture forwarded original post (reliable) ==============
@dp.message_handler(lambda m: m.forward_from_chat is not None and m.from_user.id in ADMIN_IDS, content_types=types.ContentTypes.ANY)
async def capture_forwarded(message: types.Message):
    try:
        fid = message.from_user.id
        fchat = message.forward_from_chat
        fmsgid = message.forward_from_message_id
        if not fchat or not fmsgid:
            return await message.reply("Forwarded message missing original chat metadata. Forward the original channel post (not copy).")
        LAST_FORWARDED[fid] = {"channel_id": fchat.id, "message_id": fmsgid}
        await message.reply(f"Captured forward: channel_id={fchat.id} message_id={fmsgid}\nNow run:\n/save_forward <batch> <subject> <chapter> <lec_no>")
    except Exception as e:
        logger.exception(e)
        await message.reply("Capture failed.")

@dp.message_handler(commands=['save_forward'])
async def save_forward_cmd(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.reply("Not admin.")
    try:
        parts = message.text.split()
        if len(parts) != 5:
            return await message.reply("Usage:\n/save_forward batch subject chapter lec_no")
        _, batch, subject, chapter, lec_no = parts
        lec_no = int(lec_no)
        info = LAST_FORWARDED.get(message.from_user.id)
        if not info:
            return await message.reply("No forwarded message captured. Forward a channel message to bot first.")
        lectures_col.insert_one({
            "batch": batch,
            "subject": subject,
            "chapter": chapter,
            "lec_no": lec_no,
            "channel_id": int(info['channel_id']),
            "message_id": int(info['message_id']),
            "created_at": datetime.utcnow()
        })
        LAST_FORWARDED.pop(message.from_user.id, None)
        await message.reply(f"Saved lecture {batch}/{subject}/{chapter}/L{lec_no}")
    except Exception as e:
        logger.exception(e)
        await message.reply("Failed to save. Usage:\n/save_forward batch subject chapter lec_no")

# ================= ADMIN: add chapter/lecture (manual) =================
@dp.message_handler(commands=["add_chapter"])
async def add_chapter(message: types.Message):
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
        logger.exception(e)
        await message.reply("Usage:\n/add_chapter batch subject chapter_id \"Chapter Name\"")

@dp.message_handler(commands=["add_lecture"])
async def add_lecture(message: types.Message):
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
        logger.exception(e)
        await message.reply("Usage:\n/add_lecture batch subject chapter_id lecture_no channel_id message_id")

# ================= START / MENU =================
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    if "token_" in (message.text or ""):
        return await unlock_start(message)

    kb = InlineKeyboardMarkup()
    batches = lectures_col.distinct("batch")
    if not batches:
        return await message.answer("No batches configured yet.")
    for batch in batches:
        kb.add(InlineKeyboardButton(batch, callback_data=f"batch|{batch}"))
    await message.answer("📚 Select Batch", reply_markup=kb)

# ================= SELECT FLOW =================
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

@dp.callback_query_handler(lambda c: c.data.startswith("sub|"))
async def select_chapter(c: types.CallbackQuery):
    _, batch, sub = c.data.split("|", 2)
    chapters = list(chapters_col.find({"batch": batch, "subject": sub}).sort("created_at", 1))
    kb = InlineKeyboardMarkup()
    if chapters:
        for ch in chapters:
            cid = ch["chapter_id"]; cname = ch.get("chapter_name") or cid
            kb.add(InlineKeyboardButton(f"{cname}", callback_data=f"chap|{batch}|{sub}|{cid}"))
    else:
        for ch in lectures_col.find({"batch": batch, "subject": sub}).distinct("chapter"):
            cid = ch or "default"
            kb.add(InlineKeyboardButton(f"{cid}", callback_data=f"chap|{batch}|{sub}|{cid}"))
    await c.message.edit_text(f"📚 {batch} / {sub}\nSelect Chapter", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("chap|"))
async def select_lecture(c: types.CallbackQuery):
    _, batch, sub, chapter_id = c.data.split("|", 3)
    kb = InlineKeyboardMarkup(row_width=5)
    cursor = lectures_col.find({"batch": batch, "subject": sub, "chapter": chapter_id}).sort("lec_no", 1)
    found = False
    for lec in cursor:
        found = True; n = lec["lec_no"]
        kb.insert(InlineKeyboardButton(str(n), callback_data=f"lec|{batch}|{sub}|{chapter_id}|{n}"))
    if not found:
        return await c.message.edit_text("No lectures found in this chapter.")
    await c.message.edit_text(f"🎬 {batch}/{sub}/{chapter_id}\nSelect Lecture", reply_markup=kb)

# ================= LECTURE REQUEST (token + anti-abuse + subscribe) =================
@dp.callback_query_handler(lambda c: c.data.startswith("lec|"))
async def lecture_request(c: types.CallbackQuery):
    uid = c.from_user.id
    _, batch, sub, chapter_id, lec = c.data.split("|", 4)
    lec = int(lec)

    # ADMIN BYPASS: if enabled and user is admin -> send directly
    if ADMIN_BYPASS and is_admin(uid):
        lec_doc = lectures_col.find_one({"batch": batch, "subject": sub, "chapter": chapter_id, "lec_no": lec})
        if not lec_doc:
            return await c.message.answer("Lecture not found.")
        try:
            await bot.forward_message(uid, lec_doc["channel_id"], lec_doc["message_id"])
            return await c.answer("▶ Sent (admin bypass)")
        except Exception as e:
            logger.exception("admin forward failed: %s", e)
            return await c.message.answer("Error forwarding lecture — contact admin.")

    u = get_user(uid)
    premium = bool(u.get("premium")) and u.get("expiry") and u["expiry"] > datetime.utcnow()

    # premium condition for > free limit
    #if lec > LIMIT_FREE and not premium:
        #return await c.message.answer("🔒 Premium required for this lecture.")

    # auto-subscribe check
    if REQUIRED_CHANNELS:
        ok, missing = await check_subscriptions(uid)
        if not ok:
            kb = InlineKeyboardMarkup()
            ch = missing
            url = f"https://t.me/{str(ch).lstrip('@')}"
            kb.add(InlineKeyboardButton("Join Channel", url=url))
            return await c.message.answer("🔔 You must join our channel to access lectures.", reply_markup=kb)

    # cooldown check
    cooldown_key = f"{batch}|{sub}|{chapter_id}|{lec}"
    cooldowns = u.get("cooldowns") or {}
    next_allowed = cooldowns.get(cooldown_key)
    if next_allowed and next_allowed > datetime.utcnow():
        wait = int((next_allowed - datetime.utcnow()).total_seconds())
        return await c.answer(f"⏳ Wait {wait//60}m {wait%60}s before retrying this lecture.")

    # daily limit
    du = u.get("daily_unlocks") or {}
    if du.get("date") == today_str() and du.get("count", 0) >= DAILY_UNLOCK_LIMIT and not premium:
        return await c.message.answer("⚠ Daily unlock limit reached. Try tomorrow or buy premium.")

    # premium direct access
    if premium:
        lec_doc = lectures_col.find_one({"batch": batch, "subject": sub, "chapter": chapter_id, "lec_no": lec})
        if not lec_doc:
            return await c.message.answer("Lecture not found.")
        try:
            await bot.forward_message(uid, lec_doc["channel_id"], lec_doc["message_id"])
        except Exception as e:
            logger.exception("forward failed: %s", e)
            return await c.message.answer("Error forwarding lecture — contact admin.")
        return await c.answer("▶ Sent")

    # FREE lecture -> create single-use token & save pending mapping + token doc with expiry
    token = secrets.token_urlsafe(12)
    now = datetime.utcnow()
    users_col.update_one({"_id": uid}, {"$set": {"pending": {"token": token, "batch": batch, "subject": sub, "chapter": chapter_id, "lec": lec, "created_at": now}}}, upsert=True)
    tokens_col.insert_one({"token": token, "uid": uid, "created_at": now, "expires_at": now + timedelta(seconds=TOKEN_EXPIRY_SECONDS), "used": False})

    me = await bot.get_me()
    long_link = f"https://t.me/{me.username}?start=token_{token}"

    # shorten link
    try:
        api_url = "https://arolinks.com/api"
        params = {"api": SHORT_API, "url": long_link}
        resp = requests.get(api_url, params=params, timeout=10)
        data = resp.json()
        short_url = data.get("shortenedUrl") or long_link
    except Exception as e:
        logger.exception(e)
        short_url = long_link

    text = ("🔐 Verification needed (cannot be skipped).\n\n"
            "1) Open the link below\n2) Complete the shortner flow\n3) When redirected back to bot, lecture will unlock automatically.\n\n"
            f"{short_url}\n\n⚠ Lecture will unlock only if you return via this link.")
    await c.message.answer(text)
    await c.answer()

# ================= UNLOCK HANDLER (token) =================
async def unlock_start(m: types.Message):
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

        tok = tokens_col.find_one({"token": token})
        if not tok:
            return await m.answer("❌ Token invalid or expired. Open the short link again.")
        if tok.get("used"): return await m.answer("❌ Token already used.")
        if tok.get("expires_at") and tok["expires_at"] < datetime.utcnow():
            return await m.answer("❌ Token expired. Try again.")

        uid = m.from_user.id
        u = get_user(uid)
        pending = u.get("pending")
        if not pending or pending.get("token") != token:
            return await m.answer("❌ Verification mismatch or expired. Open the short link again.")

        # forward lecture
        batch = pending["batch"]; sub = pending["subject"]; chapter = pending["chapter"]; lec = int(pending["lec"])
        lec_doc = lectures_col.find_one({"batch": batch, "subject": sub, "chapter": chapter, "lec_no": lec})
        if not lec_doc:
            return await m.answer("Lecture not found (contact admin).")

        try:
            await bot.forward_message(uid, lec_doc["channel_id"], lec_doc["message_id"])
        except Exception as e:
            logger.exception("forward failed: %s", e)
            return await m.answer("Error forwarding lecture — contact admin.")

        # mark token used & clear pending
        tokens_col.update_one({"token": token}, {"$set": {"used": True, "used_at": datetime.utcnow()}})
        users_col.update_one({"_id": uid}, {"$unset": {"pending": ""}})

        # update cooldown & daily counters & analytics
        cooldown_key = f"{batch}|{sub}|{chapter}|{lec}"
        next_time = datetime.utcnow() + timedelta(seconds=LECTURE_COOLDOWN_SECONDS)
        users_col.update_one({"_id": uid}, {"$set": {f"cooldowns.{cooldown_key}": next_time, "last_unlocked": datetime.utcnow()}})
        du = users_col.find_one({"_id": uid}).get("daily_unlocks") or {}
        if du.get("date") == today_str():
            users_col.update_one({"_id": uid}, {"$inc": {"daily_unlocks.count": 1}})
        else:
            users_col.update_one({"_id": uid}, {"$set": {"daily_unlocks": {"date": today_str(), "count": 1}}})

        analytics_col.insert_one({"ts": datetime.utcnow(), "user": uid, "batch": batch, "subject": sub, "chapter": chapter, "lec": lec, "success": True})
        return await m.answer("🎉 Verified — Lecture Unlocked!")
    except Exception as e:
        logger.exception(e)
        return await m.answer("Verification failed — try again.")

# ================= AUTO SYNC: channel_post handler for #meta caption ================
@dp.channel_post_handler(content_types=types.ContentTypes.ANY)
async def channel_post_handler(message: types.Message):
    try:
        caption = (message.caption or "") + " " + (message.text or "")
        if "#meta" not in caption:
            return
        start = caption.index("#meta")
        metastr = caption[start:]
        pairs = {}
        for part in metastr.replace("#meta", "").strip().split():
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k.strip()] = v.strip()
        required = ("batch" in pairs and "subject" in pairs and "lec" in pairs)
        if not required:
            logger.info("meta missing required fields: %s", pairs)
            return
        batch = pairs["batch"]; subject = pairs["subject"]; chapter = pairs.get("chapter", "default"); lec = int(pairs["lec"])
        lectures_col.insert_one({
            "batch": batch,
            "subject": subject,
            "chapter": chapter,
            "lec_no": lec,
            "channel_id": message.chat.id,
            "message_id": message.message_id,
            "created_at": datetime.utcnow()
        })
        note = f"Auto-synced lecture: {batch}/{subject}/{chapter}/L{lec} from channel {message.chat.id}"
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, note)
            except Exception:
                pass
    except Exception as e:
        logger.exception(e)

# ================= ADMIN: analytics panel =================
@dp.message_handler(commands=["stats"])
async def stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.reply("Not admin.")
    users_count = users_col.count_documents({})
    total_lectures = lectures_col.count_documents({})
    premium_count = users_col.count_documents({"premium": True})
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_unlocks = analytics_col.count_documents({"ts": {"$gte": today_start}})
    txt = (f"📊 Stats\nUsers: {users_count}\nPremium users: {premium_count}\n"
           f"Lectures total: {total_lectures}\nToday's unlocks: {today_unlocks}")
    await message.reply(txt)

@dp.message_handler(commands=["top_lectures"])
async def top_lectures(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.reply("Not admin.")
    parts = message.text.split()
    n = int(parts[1]) if len(parts) > 1 else 10
    pipeline = [
        {"$group": {"_id": {"batch":"$batch","subject":"$subject","chapter":"$chapter","lec":"$lec"}, "count":{"$sum":1}}},
        {"$sort": {"count": -1}},
        {"$limit": n}
    ]
    res = analytics_col.aggregate(pipeline)
    txt = "Top lectures:\n"
    for r in res:
        key = r["_id"]
        txt += f"{key['batch']}/{key['subject']}/{key['chapter']}/L{key['lec']} — {r['count']}\n"
    await message.reply(txt)

@dp.message_handler(commands=["pending_tokens"])
async def pending_tokens(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.reply("Not admin.")
    rows = tokens_col.find({"used": False}).sort("created_at", -1).limit(20)
    txt = "Recent tokens (unused):\n"
    for r in rows:
        txt += f"{r.get('token')} | uid:{r.get('uid')} | created:{r.get('created_at')}\n"
    await message.reply(txt)

# ---------------- Admin: Git pull + restart (admin-only) ----------------
@dp.message_handler(commands=["update_repo"])
async def update_repo(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Not authorised.")

    args = message.text.split()
    do_install = AUTO_INSTALL_REQUIRES
    if len(args) > 1 and args[1].lower() in ("no-install", "noinstall"):
        do_install = False
    if len(args) > 1 and args[1].lower() in ("install", "pip"):
        do_install = True

    repo = Path(REPO_PATH)
    if not repo.exists():
        return await message.reply(f"❌ REPO_PATH does not exist: {REPO_PATH}")

    info_msg = await message.reply(f"🔄 Pulling from branch `{GIT_BRANCH}` at `{REPO_PATH}`...\nThis may take a few seconds.")
    try:
        cmd = f"cd {sh_quote(str(repo))} && git fetch --all --prune && git reset --hard origin/{sh_quote(GIT_BRANCH)} && git pull origin {sh_quote(GIT_BRANCH)}"
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        stdout = out.decode(errors="ignore").strip()
        stderr = err.decode(errors="ignore").strip()

        reply = f"📥 Git pull finished.\nExit: {proc.returncode}\n\n"
        if stdout:
            reply += f"--- stdout ---\n{stdout}\n\n"
        if stderr:
            reply += f"--- stderr ---\n{stderr}\n\n"

        req_changed = False
        if "requirements.txt" in stdout or "requirements.txt" in stderr:
            req_changed = True

        await info_msg.edit_text(reply + ("\nProceeding to install requirements..." if (do_install and req_changed) else ""))
    except Exception as e:
        logger.exception("git pull error")
        return await info_msg.edit_text(f"❌ Git pull failed: {e}")

    if do_install and req_changed:
        try:
            await info_msg.edit_text((info_msg.text or "") + "\n📦 Installing requirements (pip)...")
            pip_cmd = f"{sh_quote(sys.executable)} -m pip install -r {sh_quote(str(repo / 'requirements.txt'))}"
            proc = await asyncio.create_subprocess_shell(pip_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            stdout = out.decode(errors="ignore").strip()
            stderr = err.decode(errors="ignore").strip()
            out_text = f"📦 pip finished (exit {proc.returncode}).\n\n"
            if stdout: out_text += f"--- stdout ---\n{stdout}\n\n"
            if stderr: out_text += f"--- stderr ---\n{stderr}\n\n"
            await info_msg.edit_text((info_msg.text or "") + "\n" + out_text)
        except Exception as e:
            logger.exception("pip install error")
            await info_msg.edit_text((info_msg.text or "") + f"\n❌ pip install failed: {e}")

    try:
        await info_msg.edit_text((info_msg.text or "") + "\n🔁 Restarting bot process now...")
        try:
            await bot.close()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.exception("restart failed")
        await message.reply(f"❌ Restart failed: {e}")

# ================= HELP COMMAND =================
@dp.message_handler(commands=["help"])
async def help_cmd(message: types.Message):
    """Show available commands with short examples. Admins see admin-only commands."""
    isadm = is_admin(message.from_user.id)
    user_cmds = [
        ("/start", "Open bot menu (select batch → subject → chapter → lecture)"),
        ("Select buttons", "Use on-screen buttons to navigate batches/subjects/chapters/lectures"),
        ("/save_forward", "Admin helper — see admin commands"),
    ]
    admin_cmds = [
        ("/add_chapter <batch> <subject> <chapter_id> \"Chapter Name\"", "Add chapter. Example: /add_chapter Arjuna_jee_2026 physics ch01 \"Kinematics Basics\""),
        ("/add_lecture <batch> <subject> <chapter_id> <lec_no> <channel_id> <message_id>", "Add lecture manually. Example: /add_lecture Arjuna_jee_2026 physics ch01 1 -100123456789 45"),
        ("Forward channel post to bot + /save_forward <batch> <subject> <chapter> <lec_no>", "Reliable way to save channel lecture without copying ids."),
        ("/stats", "Show basic analytics"),
        ("/top_lectures [N]", "Top N lectures by unlocks. Example: /top_lectures 10"),
        ("/pending_tokens", "Show recent unused tokens"),
        ("/update_repo [no-install|install]", "Pull latest repo and restart bot. Example: /update_repo no-install"),
    ]
    text = "📘 Available commands":

"
    # user commands
    text += "User / Student:
"
    text += f" - /start → Open menu and pick batch/subject/chapter/lecture
"
    text += f" - After selecting lecture, follow on-screen verification link (shortener) to unlock free lectures.

"
    if isadm:
        text += "Admin commands:
"
        for cmd, desc in admin_cmds:
            text += f" - {cmd}
   ↳ {desc}
"
    else:
        text += "If you face issues, contact admins.
"
    text += "
Examples:
"
    text += " - Admin flow: forward a channel post to the bot → then run:
   /save_forward Arjuna_jee_2026 physics ch01 1
"
    text += " - Student flow: /start → Arjuna_jee_2026 → physics → Kinematics Basics → Lecture 1 → open short link → return to bot → lecture delivered.
"
    await message.reply(text)

# ================= RUN =================
if __name__ == "__main__":
    logger.info("Bot starting with ADMIN_BYPASS=%s ...", ADMIN_BYPASS)
    executor.start_polling(dp, skip_updates=True)
