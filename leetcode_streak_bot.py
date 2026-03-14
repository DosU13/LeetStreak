"""
LeetCode Streak Reminder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Install:
    pip install python-telegram-bot apscheduler aiohttp aiosqlite

Run:
    BOT_TOKEN=your_token python leetcode_streak_bot.py

Commands:
    /start           — welcome
    /register        — set your LeetCode username
    /status          — check if you solved today
    /addreminder     — add a new reminder
    /reminders       — list your reminders
    /deletereminder  — remove a reminder

Reminder types:
    once  — one message at the set time
    nag   — message every minute until you submit, or day ends

Note on phone calls:
    Telegram Bot API does not support initiating calls.
    "Nag" mode (1-min repeat) is the most aggressive option available.

LeetCode day boundary: midnight UTC = 06:00 Bishkek (UTC+6)
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH   = "streakbot.db"
BISHKEK   = ZoneInfo("Asia/Bishkek")
UTC       = timezone.utc

# Conversation states
ASK_USERNAME = 1
ASK_TIME     = 2
ASK_TYPE     = 3

# Global bot app reference (set in main)
_app = None
scheduler = AsyncIOScheduler(timezone=BISHKEK)

# ── Database ──────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id  INTEGER PRIMARY KEY,
                username TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id  INTEGER NOT NULL,
                hhmm     TEXT    NOT NULL,
                type     TEXT    NOT NULL CHECK(type IN ('once','nag'))
            );
        """)
        await db.commit()

async def db_get_username(chat_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username FROM users WHERE chat_id=?", (chat_id,)) as c:
            row = await c.fetchone()
            return row[0] if row else None

async def db_set_username(chat_id: int, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (chat_id, username))
        await db.commit()

async def db_add_reminder(chat_id: int, hhmm: str, rtype: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO reminders (chat_id,hhmm,type) VALUES (?,?,?)", (chat_id, hhmm, rtype)
        )
        await db.commit()
        return cur.lastrowid

async def db_get_reminders(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id,hhmm,type FROM reminders WHERE chat_id=? ORDER BY hhmm", (chat_id,)
        ) as c:
            return await c.fetchall()

async def db_delete_reminder(rid: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE id=? AND chat_id=?", (rid, chat_id))
        await db.commit()

async def db_all_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT r.id, r.chat_id, r.hhmm, r.type, u.username
            FROM reminders r JOIN users u USING(chat_id)
        """) as c:
            return await c.fetchall()

# ── LeetCode API ──────────────────────────────────────────────────────────────

_LC_URL     = "https://leetcode.com/graphql"
_LC_HEADERS = {"Content-Type": "application/json", "Referer": "https://leetcode.com"}

async def lc_solved_today(username: str) -> bool | None:
    """Return True if username submitted AC today (UTC day). None on error."""
    query = """
    query($user:String!,$limit:Int!){
      recentAcSubmissionList(username:$user,limit:$limit){ timestamp }
    }"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _LC_URL,
                json={"query": query, "variables": {"user": username, "limit": 10}},
                headers=_LC_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
        subs = data.get("data", {}).get("recentAcSubmissionList") or []
        # LeetCode streak day = UTC calendar day (midnight UTC = 06:00 Bishkek)
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return any(
            datetime.fromtimestamp(int(s["timestamp"]), UTC) >= day_start
            for s in subs
        )
    except Exception as e:
        logger.warning(f"lc_solved_today error: {e}")
        return None

async def lc_profile(username: str) -> dict | None:
    """Return streak, total solved, total active days. None on error."""
    year = datetime.now(BISHKEK).year
    query = f"""
    query($user:String!){{
      matchedUser(username:$user){{
        username
        submitStatsGlobal{{ acSubmissionNum{{ difficulty count }} }}
        userCalendar(year:{year}){{ streak totalActiveDays }}
      }}
    }}"""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                _LC_URL,
                json={"query": query, "variables": {"user": username}},
                headers=_LC_HEADERS,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
        u = (data.get("data") or {}).get("matchedUser")
        if not u:
            return None
        cal   = u.get("userCalendar") or {}
        stats = u.get("submitStatsGlobal", {}).get("acSubmissionNum", [])
        total = next((x["count"] for x in stats if x["difficulty"] == "All"), 0)
        return {
            "username":     u["username"],
            "streak":       cal.get("streak", 0),
            "active_days":  cal.get("totalActiveDays", 0),
            "total_solved": total,
        }
    except Exception as e:
        logger.warning(f"lc_profile error: {e}")
        return None

async def lc_user_exists(username: str) -> bool:
    p = await lc_profile(username)
    return p is not None

# ── Scheduler helpers ─────────────────────────────────────────────────────────

def job_id_once(rid: int) -> str:
    return f"once_{rid}"

def job_id_nag_cron(rid: int) -> str:
    return f"nag_cron_{rid}"

def job_id_nag_interval(rid: int) -> str:
    return f"nag_interval_{rid}"

async def send_once(chat_id: int, username: str):
    """Fire once: check status and send appropriate message."""
    solved = await lc_solved_today(username)
    now_bk = datetime.now(BISHKEK).strftime("%H:%M")

    if solved is None:
        msg = (
            f"⚠️ Couldn't reach LeetCode at {now_bk}.\n"
            "Please check your streak manually!"
        )
    elif solved:
        profile = await lc_profile(username)
        streak = profile["streak"] if profile else "?"
        msg = (
            f"✅ You already solved today! Streak safe 🔥\n"
            f"Current streak: *{streak} days*"
        )
    else:
        msg = (
            f"🚨 *Streak alert!* ({now_bk} Bishkek)\n\n"
            f"You haven't submitted anything on LeetCode today, @{username}!\n"
            f"Don't break your streak — go solve something 💪\n\n"
            f"👉 https://leetcode.com/problemset/"
        )
    try:
        await _app.bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"send_once error for {chat_id}: {e}")

async def nag_tick(chat_id: int, username: str, rid: int):
    """Called every minute in nag mode. Stops when solved or day ends."""
    # Stop if LeetCode day already ended (past 23:59 UTC = past 05:59 next day Bishkek)
    now_utc = datetime.now(UTC)
    if now_utc.hour == 0 and now_utc.minute == 0:
        # Just rolled over midnight UTC — day ended, remove interval job
        job = scheduler.get_job(job_id_nag_interval(rid))
        if job:
            job.remove()
        logger.info(f"Nag {rid} stopped: day ended")
        return

    solved = await lc_solved_today(username)

    if solved is True:
        job = scheduler.get_job(job_id_nag_interval(rid))
        if job:
            job.remove()
        now_bk = datetime.now(BISHKEK).strftime("%H:%M")
        try:
            await _app.bot.send_message(
                chat_id,
                f"✅ Submission detected at {now_bk}! Streak saved 🔥 Nagging stopped.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(e)
        return

    now_bk = datetime.now(BISHKEK).strftime("%H:%M")
    if solved is False:
        msg = (
            f"⏰ *{now_bk} — Still no submission today!*\n"
            f"LeetCode streak at risk, @{username}!\n"
            f"👉 https://leetcode.com/problemset/"
        )
    else:
        msg = f"⚠️ {now_bk} — Couldn't check LeetCode. Please verify your streak manually!"

    try:
        await _app.bot.send_message(chat_id, msg, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"nag_tick error for {chat_id}: {e}")

def schedule_reminder(rid: int, chat_id: int, hhmm: str, rtype: str, username: str):
    """Register APScheduler jobs for a reminder."""
    hour, minute = map(int, hhmm.split(":"))

    if rtype == "once":
        scheduler.add_job(
            send_once,
            CronTrigger(hour=hour, minute=minute, timezone=BISHKEK),
            args=[chat_id, username],
            id=job_id_once(rid),
            replace_existing=True,
            misfire_grace_time=60,
        )

    elif rtype == "nag":
        # At the set time, start a per-minute interval job
        async def start_nagging(c=chat_id, u=username, r=rid):
            # Remove old interval if any
            old = scheduler.get_job(job_id_nag_interval(r))
            if old:
                old.remove()
            # Check immediately
            await nag_tick(c, u, r)
            # Then every 60 seconds
            scheduler.add_job(
                nag_tick,
                "interval",
                seconds=60,
                args=[c, u, r],
                id=job_id_nag_interval(r),
                replace_existing=True,
            )

        scheduler.add_job(
            start_nagging,
            CronTrigger(hour=hour, minute=minute, timezone=BISHKEK),
            id=job_id_nag_cron(rid),
            replace_existing=True,
            misfire_grace_time=60,
        )

def unschedule_reminder(rid: int):
    for jid in [job_id_once(rid), job_id_nag_cron(rid), job_id_nag_interval(rid)]:
        j = scheduler.get_job(jid)
        if j:
            j.remove()

# ── Bot handlers ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *LeetCode Streak Bot*\n\n"
        "I'll make sure you never lose your streak!\n\n"
        "Commands:\n"
        "• /register — link your LeetCode username\n"
        "• /status — check today's progress\n"
        "• /addreminder — add a reminder\n"
        "• /reminders — view your reminders\n"
        "• /deletereminder — remove a reminder",
        parse_mode="Markdown",
    )

# ── /register conversation ────────────────────────────────────────────────────

async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = await db_get_username(update.effective_chat.id)
    hint = f"\nCurrent: `{current}`" if current else ""
    await update.message.reply_text(
        f"Send me your LeetCode username:{hint}",
        parse_mode="Markdown",
    )
    return ASK_USERNAME

async def register_got_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Checking username…")

    exists = await lc_user_exists(username)
    if not exists:
        await msg.edit_text(
            f"❌ Couldn't find LeetCode user `{username}`.\nCheck the username and try again.",
            parse_mode="Markdown",
        )
        return ASK_USERNAME

    await db_set_username(update.effective_chat.id, username)

    # Reschedule any existing reminders with new username
    for rid, hhmm, rtype in await db_get_reminders(update.effective_chat.id):
        schedule_reminder(rid, update.effective_chat.id, hhmm, rtype, username)

    profile = await lc_profile(username)
    streak = profile["streak"] if profile else "?"
    solved = profile["total_solved"] if profile else "?"
    await msg.edit_text(
        f"✅ Registered as `{username}`!\n"
        f"🔥 Current streak: *{streak} days*\n"
        f"✔️ Total solved: *{solved}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = await db_get_username(chat_id)
    if not username:
        await update.message.reply_text("You haven't registered yet. Use /register first.")
        return

    msg = await update.message.reply_text("🔍 Checking LeetCode…")
    profile = await lc_profile(username)
    solved  = await lc_solved_today(username)

    now_bk = datetime.now(BISHKEK).strftime("%d %b %Y %H:%M")

    if profile is None:
        await msg.edit_text("⚠️ Couldn't reach LeetCode right now. Try again later.")
        return

    streak_emoji = "🔥" if solved else "❄️"
    today_line   = "✅ Solved today!" if solved else "❌ Not solved yet today"

    await msg.edit_text(
        f"*{username}* — {now_bk} (Bishkek)\n\n"
        f"{streak_emoji} Streak: *{profile['streak']} days*\n"
        f"📅 Active days this year: *{profile['active_days']}*\n"
        f"✔️ Total solved: *{profile['total_solved']}*\n\n"
        f"{today_line}\n\n"
        f"_LeetCode day resets at 06:00 Bishkek (midnight UTC)_",
        parse_mode="Markdown",
    )

# ── /addreminder conversation ─────────────────────────────────────────────────

async def cmd_addreminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    username = await db_get_username(update.effective_chat.id)
    if not username:
        await update.message.reply_text("Register first with /register.")
        return ConversationHandler.END

    await update.message.reply_text(
        "⏰ What time should I remind you? (Bishkek UTC+6)\n"
        "Send in *HH:MM* format, e.g. `22:00` or `05:00`",
        parse_mode="Markdown",
    )
    return ASK_TIME

async def addreminder_got_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        hour, minute = map(int, text.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        await update.message.reply_text("Invalid format. Send HH:MM, e.g. `22:00`", parse_mode="Markdown")
        return ASK_TIME

    ctx.user_data["reminder_time"] = f"{hour:02d}:{minute:02d}"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 Once", callback_data="rtype:once"),
            InlineKeyboardButton("🚨 Nag (every min)", callback_data="rtype:nag"),
        ]
    ])
    await update.message.reply_text(
        f"Time set to *{ctx.user_data['reminder_time']}* (Bishkek)\n\n"
        "Choose reminder type:\n\n"
        "• *Once* — one message at the set time\n"
        "• *Nag* — message every minute until you submit (stops at midnight UTC)",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ASK_TYPE

async def addreminder_got_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    rtype    = query.data.split(":")[1]
    hhmm     = ctx.user_data.get("reminder_time", "22:00")
    chat_id  = query.message.chat_id
    username = await db_get_username(chat_id)

    rid = await db_add_reminder(chat_id, hhmm, rtype)
    schedule_reminder(rid, chat_id, hhmm, rtype, username)

    type_label = "once" if rtype == "once" else "every-minute nag"
    await query.edit_message_text(
        f"✅ Reminder #{rid} added!\n"
        f"⏰ Time: *{hhmm}* (Bishkek)\n"
        f"📣 Type: *{type_label}*",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

# ── /reminders ────────────────────────────────────────────────────────────────

async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await db_get_reminders(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no reminders. Use /addreminder to add one.")
        return

    lines = []
    for rid, hhmm, rtype in rows:
        icon = "🔔" if rtype == "once" else "🚨"
        lines.append(f"#{rid} — {icon} *{hhmm}* Bishkek ({rtype})")

    await update.message.reply_text(
        "Your reminders:\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )

# ── /deletereminder ───────────────────────────────────────────────────────────

async def cmd_deletereminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await db_get_reminders(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You have no reminders to delete.")
        return

    buttons = [
        [InlineKeyboardButton(
            f"#{rid} — {'🔔' if rtype=='once' else '🚨'} {hhmm} ({rtype})",
            callback_data=f"delrem:{rid}"
        )]
        for rid, hhmm, rtype in rows
    ]
    await update.message.reply_text(
        "Choose a reminder to delete:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def delete_reminder_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rid = int(query.data.split(":")[1])
    await db_delete_reminder(rid, query.message.chat_id)
    unschedule_reminder(rid)
    await query.edit_message_text(f"✅ Reminder #{rid} deleted.")

# ── Startup: reload reminders from DB ────────────────────────────────────────

async def reload_reminders():
    rows = await db_all_reminders()
    for rid, chat_id, hhmm, rtype, username in rows:
        schedule_reminder(rid, chat_id, hhmm, rtype, username)
    logger.info(f"Reloaded {len(rows)} reminder(s) from DB")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _app

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("Set the BOT_TOKEN environment variable")

    _app = ApplicationBuilder().token(token).build()

    # Register conversation — /register
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_got_username)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Addreminder conversation
    addreminder_conv = ConversationHandler(
        entry_points=[CommandHandler("addreminder", cmd_addreminder)],
        states={
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addreminder_got_time)],
            ASK_TYPE: [CallbackQueryHandler(addreminder_got_type, pattern=r"^rtype:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(CommandHandler("status", cmd_status))
    _app.add_handler(CommandHandler("reminders", cmd_reminders))
    _app.add_handler(CommandHandler("deletereminder", cmd_deletereminder))
    _app.add_handler(CallbackQueryHandler(delete_reminder_callback, pattern=r"^delrem:"))
    _app.add_handler(register_conv)
    _app.add_handler(addreminder_conv)

    async def on_startup(app):
        await init_db()
        await reload_reminders()
        scheduler.start()
        logger.info("Bot started.")

    async def on_shutdown(app):
        scheduler.shutdown(wait=False)

    _app.post_init    = on_startup
    _app.post_shutdown = on_shutdown

    async def run():
        await init_db()
        await reload_reminders()
        scheduler.start()

        # Minimal HTTP server so Render's health check passes
        from aiohttp import web

        async def health(_):
            return web.Response(text="ok")

        site = web.Application()
        site.router.add_get("/", health)
        runner = web.AppRunner(site)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        logger.info(f"Health server on port {port}")

        # Run bot (blocks until stopped)
        async with _app:
            await _app.start()
            await _app.updater.start_polling()
            logger.info("Bot polling…")
            # Keep running forever
            await asyncio.Event().wait()

    asyncio.run(run())

if __name__ == "__main__":
    main()
