"""
NETPRIME4U MOVIES FINDER — Telegram Bot
Complete production-ready bot · aiogram 3.x

Channel architecture:
  SOURCE_CHANNEL_ID       — private channel where admin posts content; bot indexes it
  FORCE_JOIN_CHANNEL_ID   — channel users must join before using the bot
  FORCE_JOIN_CHANNEL_URL  — public invite/join link shown on the force-join prompt
  JOIN_CHANNEL_URL        — join button attached under every delivered content post
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
# ─── CONFIG ──────────────────────────────────────────────────────────────────
# ⚠️  DEMO CONFIG — Replace BOT_TOKEN with new token from BotFather before going live

BOT_TOKEN    = "7885531234:AAFlf-cb-NJaJhm7Ll5WxPgLS59uXDEUoFs"
ADMIN_ID     = 1001673282594
BOT_USERNAME = "NetprimeMovieSearch_bot"
DB_PATH      = "netprime4u.db"
TIMEZONE     = "Asia/Kolkata"
TZ           = ZoneInfo(TIMEZONE)

# ── Channel config ────────────────────────────────────────────────────────────
# Private channel jahan tu movies post karta hai — bot yahan se index karta hai
SOURCE_CHANNEL_ID: Optional[int] = -1003901935437

# Channel jisko users join karna padega bot use karne se pehle
FORCE_JOIN_CHANNEL_ID: Optional[int] = -1003096447341
FORCE_JOIN_CHANNEL_URL: str          = "https://t.me/Netprime4u"

# "📢 Join Channel" button jo delivered content ke neeche aata hai
JOIN_CHANNEL_URL: str = "https://t.me/Netprime4u"

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("np4u")

# Warn loudly at startup if critical env vars are missing
def _warn_config():
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is not set!")
    if not ADMIN_ID:
        log.warning("ADMIN_ID is not set!")
    if SOURCE_CHANNEL_ID is None:
        log.warning("SOURCE_CHANNEL_ID is not set or invalid — bot will not index any channel posts.")
    if FORCE_JOIN_CHANNEL_ID is None:
        log.warning("FORCE_JOIN_CHANNEL_ID is not set — force-join check is DISABLED.")
    if not FORCE_JOIN_CHANNEL_URL:
        log.warning("FORCE_JOIN_CHANNEL_URL is not set — join button will have no URL.")
    if not JOIN_CHANNEL_URL:
        log.warning("JOIN_CHANNEL_URL is not set — content delivery button will have no URL.")

# ─── FSM STATES ──────────────────────────────────────────────────────────────
class UserState(StatesGroup):
    searching          = State()
    requesting         = State()
    broadcast_msg      = State()
    admin_search_user  = State()
    admin_add_channel  = State()
    admin_add_bonus    = State()

# ─── DATABASE ────────────────────────────────────────────────────────────────
async def db_connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT,
            first_name    TEXT,
            joined_at     TEXT,
            last_active   TEXT,
            referred_by   INTEGER,
            is_banned     INTEGER DEFAULT 0,
            is_premium    INTEGER DEFAULT 0,
            premium_until TEXT,
            search_bonus  INTEGER DEFAULT 0,
            request_bonus INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS telegram_posts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id  INTEGER NOT NULL,
            source_msg_id   INTEGER NOT NULL,
            title           TEXT,
            searchable_text TEXT,
            content_type    TEXT,
            indexed_at      TEXT,
            is_available    INTEGER DEFAULT 1,
            UNIQUE(source_chat_id, source_msg_id)
        );

        CREATE TABLE IF NOT EXISTS search_usage (
            user_id  INTEGER,
            date_str TEXT,
            count    INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date_str)
        );

        CREATE TABLE IF NOT EXISTS request_usage (
            user_id  INTEGER,
            date_str TEXT,
            count    INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date_str)
        );

        CREATE TABLE IF NOT EXISTS requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            username     TEXT,
            request_text TEXT,
            created_at   TEXT,
            status       TEXT DEFAULT 'Pending'
        );

        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referee_id  INTEGER,
            created_at  TEXT,
            UNIQUE(referee_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_deletions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            chat_id    INTEGER,
            message_id INTEGER,
            delete_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS admin_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id   INTEGER,
            action     TEXT,
            target     TEXT,
            created_at TEXT
        );
        """)

        # Default settings (DB values can be overridden via admin panel)
        defaults = {
            "daily_search_limit":    "3",
            "daily_request_limit":   "2",
            "referral_search_bonus":  "1",
            "referral_request_bonus": "1",
            "maintenance":           "0",
            "delete_delay":          "120",
            "delete_warning_too":    "0",
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        await db.commit()

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def now_str() -> str:
    return datetime.now(TZ).isoformat()

def today_str() -> str:
    return datetime.now(TZ).date().isoformat()

def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

async def get_setting(db, key: str, default: str = "") -> str:
    row = await db.execute_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default

async def set_setting(db, key: str, value: str):
    await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    await db.commit()

async def get_user(db, user_id: int):
    return await db.execute_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))

async def register_user(db, user, referred_by: Optional[int] = None):
    now = now_str()
    await db.execute(
        """INSERT OR IGNORE INTO users
               (user_id, username, first_name, joined_at, last_active, referred_by)
           VALUES (?,?,?,?,?,?)""",
        (user.id, user.username, user.first_name, now, now, referred_by),
    )
    await db.execute(
        "UPDATE users SET last_active=?, username=?, first_name=? WHERE user_id=?",
        (now, user.username, user.first_name, user.id),
    )
    await db.commit()

async def is_new_user(db, user_id: int) -> bool:
    row = await db.execute_fetchone("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    return row is None

# ── Usage helpers ─────────────────────────────────────────────────────────────
async def daily_search_used(db, user_id: int) -> int:
    row = await db.execute_fetchone(
        "SELECT count FROM search_usage WHERE user_id=? AND date_str=?",
        (user_id, today_str()),
    )
    return row["count"] if row else 0

async def daily_search_limit(db, user_id: int) -> int:
    base  = int(await get_setting(db, "daily_search_limit", "3"))
    user  = await get_user(db, user_id)
    if user and user["is_premium"]:
        return 999
    bonus = int(user["search_bonus"]) if user else 0
    return base + bonus

async def inc_search_usage(db, user_id: int):
    await db.execute(
        """INSERT INTO search_usage (user_id, date_str, count) VALUES (?,?,1)
           ON CONFLICT(user_id,date_str) DO UPDATE SET count=count+1""",
        (user_id, today_str()),
    )
    await db.commit()

async def daily_request_used(db, user_id: int) -> int:
    row = await db.execute_fetchone(
        "SELECT count FROM request_usage WHERE user_id=? AND date_str=?",
        (user_id, today_str()),
    )
    return row["count"] if row else 0

async def daily_request_limit(db, user_id: int) -> int:
    base  = int(await get_setting(db, "daily_request_limit", "2"))
    user  = await get_user(db, user_id)
    if user and user["is_premium"]:
        return 999
    bonus = int(user["request_bonus"]) if user else 0
    return base + bonus

async def inc_request_usage(db, user_id: int):
    await db.execute(
        """INSERT INTO request_usage (user_id, date_str, count) VALUES (?,?,1)
           ON CONFLICT(user_id,date_str) DO UPDATE SET count=count+1""",
        (user_id, today_str()),
    )
    await db.commit()

# ─── FORCE-JOIN ──────────────────────────────────────────────────────────────
async def check_force_join(bot: "Bot", user_id: int) -> bool:
    """
    Returns True if the user has joined FORCE_JOIN_CHANNEL_ID (or if force-join
    is not configured at all).  Handles all Telegram API errors gracefully.
    """
    if FORCE_JOIN_CHANNEL_ID is None:
        return True  # not configured → no restriction
    try:
        member = await bot.get_chat_member(
            chat_id=FORCE_JOIN_CHANNEL_ID, user_id=user_id
        )
        return member.status not in (
            ChatMemberStatus.LEFT,
            ChatMemberStatus.KICKED,
            ChatMemberStatus.BANNED,
        )
    except TelegramBadRequest as e:
        log.warning("Force-join check failed (bad request): %s", e)
        # If bot can't reach the channel it's likely misconfigured — let user through
        return True
    except TelegramForbiddenError as e:
        log.warning("Force-join check forbidden (bot not in channel?): %s", e)
        return True
    except Exception as e:
        log.error("Force-join check unexpected error: %s", e)
        return True

def force_join_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown when the user hasn't joined the required channel."""
    buttons = []
    join_url = FORCE_JOIN_CHANNEL_URL or "https://t.me"
    buttons.append([InlineKeyboardButton(text="📢 Join Channel", url=join_url)])
    buttons.append([InlineKeyboardButton(text="✅ I've Joined", callback_data="verify_join")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def content_delivery_keyboard() -> InlineKeyboardMarkup:
    """
    Keyboard attached under every delivered content post.
    Uses JOIN_CHANNEL_URL from .env — separate from force-join channel.
    """
    url = JOIN_CHANNEL_URL or FORCE_JOIN_CHANNEL_URL or "https://t.me"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📢 Join Channel", url=url)
    ]])

# ─── KEYBOARDS ───────────────────────────────────────────────────────────────
def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="🔍 Search Movies",    callback_data="search"),
            InlineKeyboardButton(text="🎬 Available Movies", callback_data="available"),
        ],
        [
            InlineKeyboardButton(text="📥 Request Movie",    callback_data="request"),
            InlineKeyboardButton(text="👤 My Profile",       callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="📊 My Limits",        callback_data="limits"),
            InlineKeyboardButton(text="🎁 Share & Earn",     callback_data="share"),
        ],
        [
            InlineKeyboardButton(text="📢 Updates",          callback_data="updates"),
            InlineKeyboardButton(text="❓ Help",             callback_data="help"),
        ],
        [InlineKeyboardButton(text="⚙️ Settings",            callback_data="settings")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🛠️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_home_kb(back_cb: str = "start") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back",      callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="start"),
    ]])

# ─── AUTO-DELETE HELPERS ──────────────────────────────────────────────────────
def format_countdown(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    return f"{m}:{s:02d}"

async def safe_delete(bot: "Bot", chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        log.warning("Delete msg failed chat=%s msg=%s: %s", chat_id, message_id, e)
    except Exception as e:
        log.error("Delete unexpected error: %s", e)

async def _run_deletion(bot: "Bot", chat_id: int, message_id: int,
                        user_id: int, rec_id: int, delay: int):
    await asyncio.sleep(delay)
    await safe_delete(bot, chat_id, message_id)
    async with await db_connect() as db:
        await db.execute("DELETE FROM pending_deletions WHERE id=?", (rec_id,))
        await db.commit()

async def schedule_deletion(bot: "Bot", chat_id: int, message_id: int,
                            user_id: int, delay: int):
    """Persist the deletion job and start an asyncio task."""
    delete_at = (datetime.now(TZ) + timedelta(seconds=delay)).isoformat()
    async with await db_connect() as db:
        await db.execute(
            "INSERT INTO pending_deletions (user_id,chat_id,message_id,delete_at) VALUES (?,?,?,?)",
            (user_id, chat_id, message_id, delete_at),
        )
        await db.commit()
        row = await db.execute_fetchone(
            "SELECT id FROM pending_deletions WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        rec_id = row["id"] if row else 0

    asyncio.create_task(
        _run_deletion(bot, chat_id, message_id, user_id, rec_id, delay)
    )

async def process_pending_deletions(bot: "Bot"):
    """On startup: delete overdue messages and reschedule future ones."""
    async with await db_connect() as db:
        rows = await db.execute_fetchall("SELECT * FROM pending_deletions")

    now = datetime.now(TZ)
    for row in rows:
        delete_at = datetime.fromisoformat(row["delete_at"])
        if delete_at.tzinfo is None:
            delete_at = delete_at.replace(tzinfo=TZ)
        remaining = (delete_at - now).total_seconds()
        if remaining <= 0:
            await safe_delete(bot, row["chat_id"], row["message_id"])
            async with await db_connect() as db:
                await db.execute("DELETE FROM pending_deletions WHERE id=?", (row["id"],))
                await db.commit()
        else:
            asyncio.create_task(
                _run_deletion(
                    bot, row["chat_id"], row["message_id"],
                    row["user_id"], row["id"], int(remaining)
                )
            )

# ─── CONTENT DELIVERY ────────────────────────────────────────────────────────
async def deliver_content(bot: "Bot", user_id: int, post, delay: int) -> Optional[int]:
    """
    Copy the original source-channel post to the user.
    Attaches JOIN_CHANNEL_URL button.  Returns sent message_id or None on failure.
    Never downloads or re-uploads media — uses copy_message only.
    """
    try:
        sent = await bot.copy_message(
            chat_id=user_id,
            from_chat_id=post["source_chat_id"],
            message_id=post["source_msg_id"],
            reply_markup=content_delivery_keyboard(),
        )
        return sent.message_id
    except TelegramBadRequest as e:
        log.warning("copy_message failed (source deleted?): %s", e)
        return None
    except TelegramForbiddenError:
        log.warning("User %s has blocked the bot.", user_id)
        return None
    except Exception as e:
        log.error("Unexpected delivery error: %s", e)
        return None

async def send_countdown_warning(bot: "Bot", user_id: int, delay: int) -> Optional[int]:
    """Send the ⏳ warning message and schedule countdown edits."""
    text = (
        f"⏳ <b>IMPORTANT</b>\n\n"
        f"This post will automatically disappear after <b>2 minutes</b>.\n\n"
        f"💾 <b>Save this post</b>  or\n"
        f"📤 <b>Share it</b> with your Saved Messages / friend\n"
        f"before the timer ends.\n\n"
        f"⏰ Time remaining: <b>{format_countdown(delay)}</b>"
    )
    try:
        msg = await bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("Could not send warning to %s: %s", user_id, e)
        return None

    async def _countdown(warn_id: int, uid: int, total: int):
        for remaining in sorted(
            [t for t in [total - 30, total - 60, total - 90] if t > 0]
        ):
            await asyncio.sleep(total - remaining)
            try:
                await bot.edit_message_text(
                    chat_id=uid,
                    message_id=warn_id,
                    text=(
                        f"⏳ <b>IMPORTANT</b>\n\n"
                        f"This post disappears in <b>{format_countdown(remaining)}</b>!\n\n"
                        f"💾 <b>Save</b> or 📤 <b>Share</b> it now!"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                break

    asyncio.create_task(_countdown(msg.message_id, user_id, delay))
    return msg.message_id

# ─── BOT + DISPATCHER ────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# ─── GLOBAL MIDDLEWARE ───────────────────────────────────────────────────────
@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def global_middleware(handler, event, data):
    user = data.get("event_from_user")
    if not user:
        return await handler(event, data)

    async with await db_connect() as db:
        maintenance = await get_setting(db, "maintenance", "0")

    if maintenance == "1" and user.id != ADMIN_ID:
        notice = "🔧 <b>NETPRIME4U MOVIES FINDER</b>\n\nBot is under maintenance. Please try again later."
        if isinstance(event, Message):
            await event.answer(notice, parse_mode=ParseMode.HTML)
        else:
            await event.answer("🔧 Bot under maintenance.", show_alert=True)
        return

    async with await db_connect() as db:
        row = await db.execute_fetchone(
            "SELECT is_banned FROM users WHERE user_id=?", (user.id,)
        )
    if row and row["is_banned"]:
        if isinstance(event, Message):
            await event.answer("🚫 You have been banned from this bot.")
        else:
            await event.answer("🚫 You are banned.", show_alert=True)
        return

    return await handler(event, data)

# ─── /start ───────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    args = message.text.split()
    referred_by: Optional[int] = None
    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != message.from_user.id:
                referred_by = ref_id
        except ValueError:
            pass

    async with await db_connect() as db:
        is_new = await is_new_user(db, message.from_user.id)
        await register_user(db, message.from_user, referred_by)

        if is_new and referred_by:
            existing = await db.execute_fetchone(
                "SELECT 1 FROM referrals WHERE referee_id=?", (message.from_user.id,)
            )
            if not existing:
                await db.execute(
                    "INSERT OR IGNORE INTO referrals (referrer_id,referee_id,created_at) VALUES (?,?,?)",
                    (referred_by, message.from_user.id, now_str()),
                )
                s_bon = int(await get_setting(db, "referral_search_bonus", "1"))
                r_bon = int(await get_setting(db, "referral_request_bonus", "1"))
                await db.execute(
                    "UPDATE users SET search_bonus=search_bonus+?, request_bonus=request_bonus+? WHERE user_id=?",
                    (s_bon, r_bon, referred_by),
                )
                await db.commit()
                try:
                    await bot.send_message(
                        referred_by,
                        f"🎁 <b>Referral Bonus!</b>\n\nA new user joined with your link.\n"
                        f"+{s_bon} Search · +{r_bon} Request bonus added!",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

    # Force-join check
    joined = await check_force_join(bot, message.from_user.id)
    if not joined:
        await message.answer(
            "🔐 <b>JOIN REQUIRED</b>\n\n"
            "Please join our required channel to use\n"
            "<b>NETPRIME4U MOVIES FINDER</b>.",
            reply_markup=force_join_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    await _show_main_menu(message)

@router.callback_query(F.data == "verify_join")
async def verify_join(call: CallbackQuery):
    joined = await check_force_join(bot, call.from_user.id)
    if not joined:
        await call.answer(
            "❌ You haven't joined the channel yet. Please join first!",
            show_alert=True,
        )
        return
    await call.answer("✅ Membership Verified!")
    await _show_main_menu(call.message, edit=True, user=call.from_user)

async def _show_main_menu(target, edit: bool = False, user=None):
    if user is None and hasattr(target, "from_user"):
        user = target.from_user
    name     = user.first_name if user else "there"
    is_admin = (user.id == ADMIN_ID) if user else False
    text = (
        f"🎬 <b>NETPRIME4U MOVIES FINDER</b>\n\n"
        f"Hello, {name}! 👋\n\n"
        f"🍿 Find movies & shows instantly\n"
        f"🔍 Search our collection\n"
        f"📥 Request missing content\n"
        f"🎁 Share the bot and increase your limits"
    )
    kb = main_menu_kb(is_admin)
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "start")
async def cb_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_main_menu(call.message, edit=True, user=call.from_user)
    await call.answer()

# ─── GUARD HELPER ────────────────────────────────────────────────────────────
async def _guard(call: CallbackQuery) -> bool:
    """Return True if the user passes the force-join check; otherwise show prompt."""
    joined = await check_force_join(bot, call.from_user.id)
    if not joined:
        try:
            await call.message.edit_text(
                "🔐 <b>JOIN REQUIRED</b>\n\nPlease join our required channel first.",
                reply_markup=force_join_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await call.message.answer(
                "🔐 Please join the required channel first.",
                reply_markup=force_join_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        await call.answer()
        return False
    return True

# ─── SEARCH ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    async with await db_connect() as db:
        used  = await daily_search_used(db, call.from_user.id)
        limit = await daily_search_limit(db, call.from_user.id)
    if used >= limit:
        await call.message.edit_text(
            f"📊 <b>SEARCH LIMIT REACHED</b>\n\n"
            f"You've used all {limit} searches for today.\n\n"
            f"🎁 Earn more by referring friends!\n"
            f"Resets at midnight (IST).",
            reply_markup=back_home_kb(),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    await state.set_state(UserState.searching)
    await call.message.edit_text(
        f"🔎 <b>SEARCH MOVIES</b>\n\n"
        f"Send the movie or series name you want to find.\n\n"
        f"📊 Searches remaining today: <b>{limit - used}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="start"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

@router.message(UserState.searching)
async def handle_search(message: Message, state: FSMContext):
    query = message.text.strip()
    await state.clear()

    anim = await message.answer("🔎 Searching...")
    for step in ["🔍 Searching NETPRIME4U...", "🎬 Finding matching content...", "🍿 Preparing results..."]:
        await asyncio.sleep(0.4)
        try:
            await anim.edit_text(step)
        except Exception:
            pass

    async with await db_connect() as db:
        used  = await daily_search_used(db, message.from_user.id)
        limit = await daily_search_limit(db, message.from_user.id)
        if used >= limit:
            await anim.edit_text(
                f"📊 <b>SEARCH LIMIT REACHED</b>\n\nYou've used all {limit} searches today.",
                reply_markup=back_home_kb(),
                parse_mode=ParseMode.HTML,
            )
            return

        await inc_search_usage(db, message.from_user.id)
        words = normalize(query).split()

        if not words:
            await anim.edit_text(
                "❌ Please enter a valid search term.",
                reply_markup=back_home_kb("search"),
                parse_mode=ParseMode.HTML,
            )
            return

        like_clauses = " AND ".join(["searchable_text LIKE ?" for _ in words])
        rows = await db.execute_fetchall(
            f"SELECT id, title, source_chat_id, source_msg_id "
            f"FROM telegram_posts WHERE is_available=1 AND {like_clauses} "
            f"ORDER BY indexed_at DESC LIMIT 50",
            [f"%{w}%" for w in words],
        )

    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Search Again",  callback_data="search")],
            [InlineKeyboardButton(text="📥 Request Movie", callback_data="request")],
            [InlineKeyboardButton(text="🏠 Main Menu",     callback_data="start")],
        ])
        await anim.edit_text(
            f"❌ <b>NO RESULT FOUND</b>\n\nWe couldn't find:\n"
            f"<code>{query}</code>\n\nTry another spelling or request the content.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    results = [dict(r) for r in rows]
    await state.update_data(search_results=results, search_page=0, search_query=query)
    await _show_search_results(anim, results, 0, query)

async def _show_search_results(msg: Message, results: list, page: int, query: str):
    per_page = 5
    total    = len(results)
    pages    = max(1, (total + per_page - 1) // per_page)
    chunk    = results[page * per_page:(page + 1) * per_page]

    buttons = [
        [InlineKeyboardButton(text=f"🎬 {r['title'][:50] or 'Untitled'}", callback_data=f"pick_{r['id']}")]
        for r in chunk
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"srpage_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"srpage_{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(text="🔍 New Search", callback_data="search"),
        InlineKeyboardButton(text="🏠 Main Menu",  callback_data="start"),
    ])

    text = (
        f"🔎 <b>SEARCH RESULTS</b>\n\n"
        f"Found <b>{total}</b> matching result(s) for: <code>{query}</code>"
    )
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)
    except Exception:
        await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("srpage_"))
async def cb_srpage(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split("_")[1])
    data = await state.get_data()
    results = data.get("search_results", [])
    query   = data.get("search_query", "")
    await state.update_data(search_page=page)
    await _show_search_results(call.message, results, page, query)
    await call.answer()

@router.callback_query(F.data.startswith("pick_"))
async def cb_pick(call: CallbackQuery):
    post_id   = int(call.data.split("_")[1])
    fetch_msg = await call.message.answer("🔄 Fetching content...\n🎬 Preparing your post...")

    async with await db_connect() as db:
        post  = await db.execute_fetchone("SELECT * FROM telegram_posts WHERE id=?", (post_id,))
        delay = int(await get_setting(db, "delete_delay", "120"))

    if not post or not post["is_available"]:
        await fetch_msg.edit_text(
            "⚠️ <b>CONTENT UNAVAILABLE</b>\n\nThis content is currently unavailable. You can request it.",
            reply_markup=back_home_kb("request"),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    sent_id = await deliver_content(bot, call.from_user.id, post, delay)

    if sent_id is None:
        # Source post is gone — mark unavailable
        async with await db_connect() as db:
            await db.execute("UPDATE telegram_posts SET is_available=0 WHERE id=?", (post_id,))
            await db.commit()
        await fetch_msg.edit_text(
            "⚠️ <b>CONTENT UNAVAILABLE</b>\n\nThe source post was deleted. You can request it.",
            reply_markup=back_home_kb(),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    # Schedule 2-minute deletion of the delivered message
    await schedule_deletion(bot, call.from_user.id, sent_id, call.from_user.id, delay)

    # Send countdown warning
    await send_countdown_warning(bot, call.from_user.id, delay)

    title = post["title"] or "Content"
    await fetch_msg.edit_text(
        f"✅ <b>CONTENT FOUND</b>\n\n"
        f"🎬 <b>{title}</b>\n\n"
        f"⏳ This post will automatically disappear after 2 minutes.\n"
        f"💾 Save or 📤 Share it before it disappears.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔍 Search Again", callback_data="search"),
            InlineKeyboardButton(text="🏠 Main Menu",    callback_data="start"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

# ─── AVAILABLE MOVIES ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "available")
async def cb_available(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    await state.update_data(avail_page=0)
    await _show_available(call.message, 0)
    await call.answer()

async def _show_available(msg: Message, page: int):
    per_page = 8
    offset   = page * per_page
    async with await db_connect() as db:
        rows = await db.execute_fetchall(
            "SELECT id, title FROM telegram_posts WHERE is_available=1 "
            "ORDER BY indexed_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        total_row = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM telegram_posts WHERE is_available=1"
        )
    total = total_row["c"] if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if not rows:
        await msg.edit_text(
            "🎬 <b>AVAILABLE MOVIES</b>\n\nNo content indexed yet.",
            reply_markup=back_home_kb(),
            parse_mode=ParseMode.HTML,
        )
        return

    buttons = [
        [InlineKeyboardButton(text=f"🎬 {r['title'][:50] or 'Untitled'}", callback_data=f"pick_{r['id']}")]
        for r in rows
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"avpage_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"avpage_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="🔍 Search Movies", callback_data="search"),
        InlineKeyboardButton(text="🏠 Main Menu",     callback_data="start"),
    ])

    await msg.edit_text(
        f"🎬 <b>AVAILABLE MOVIES</b>\n\n🆕 Latest Added ({total} total) · Page {page+1}/{pages}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.HTML,
    )

@router.callback_query(F.data.startswith("avpage_"))
async def cb_avpage(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split("_")[1])
    await state.update_data(avail_page=page)
    await _show_available(call.message, page)
    await call.answer()

# ─── REQUEST MOVIE ────────────────────────────────────────────────────────────
@router.callback_query(F.data == "request")
async def cb_request(call: CallbackQuery, state: FSMContext):
    if not await _guard(call):
        return
    async with await db_connect() as db:
        used  = await daily_request_used(db, call.from_user.id)
        limit = await daily_request_limit(db, call.from_user.id)
    if used >= limit:
        await call.message.edit_text(
            f"📊 <b>REQUEST LIMIT REACHED</b>\n\n"
            f"You've used all {limit} requests today. Resets at midnight (IST).",
            reply_markup=back_home_kb(),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    await state.set_state(UserState.requesting)
    await call.message.edit_text(
        f"📥 <b>REQUEST MOVIE</b>\n\n"
        f"Send the movie or series name you want.\n\n"
        f"📊 Requests remaining today: <b>{limit - used}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="start"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

@router.message(UserState.requesting)
async def handle_request(message: Message, state: FSMContext):
    req_text = message.text.strip()
    await state.clear()

    async with await db_connect() as db:
        used  = await daily_request_used(db, message.from_user.id)
        limit = await daily_request_limit(db, message.from_user.id)
        if used >= limit:
            await message.answer("📊 Request limit reached. Try tomorrow!")
            return
        await inc_request_usage(db, message.from_user.id)
        await db.execute(
            "INSERT INTO requests (user_id,username,request_text,created_at,status) VALUES (?,?,?,?,?)",
            (message.from_user.id, message.from_user.username, req_text, now_str(), "Pending"),
        )
        await db.commit()
        row = await db.execute_fetchone("SELECT last_insert_rowid() as id")
        req_id = row["id"] if row else "?"

    await message.answer(
        f"✅ <b>REQUEST SUBMITTED</b>\n\n"
        f"🆔 Request: #{req_id}\n"
        f"🎬 {req_text}\n\n"
        f"We'll notify you when it's ready!",
        reply_markup=back_home_kb(),
        parse_mode=ParseMode.HTML,
    )

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Complete", callback_data=f"req_complete_{req_id}"),
        InlineKeyboardButton(text="❌ Reject",   callback_data=f"req_reject_{req_id}"),
    ]])
    uname = f"@{message.from_user.username}" if message.from_user.username else "No username"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📥 <b>NEW MOVIE REQUEST</b>\n\n"
            f"🆔 Request: #{req_id}\n\n"
            f"👤 User: {uname}\n"
            f"🆔 User ID: {message.from_user.id}\n\n"
            f"🎬 Request:\n{req_text}",
            reply_markup=admin_kb,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("req_complete_"))
async def req_complete(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫 Unauthorized", show_alert=True)
        return
    req_id = int(call.data.split("_")[2])
    async with await db_connect() as db:
        await db.execute("UPDATE requests SET status='Completed' WHERE id=?", (req_id,))
        await db.commit()
        row = await db.execute_fetchone("SELECT * FROM requests WHERE id=?", (req_id,))
    if row:
        try:
            await bot.send_message(
                row["user_id"],
                "🎉 <b>REQUEST COMPLETED</b>\n\n"
                "Your requested content has been processed.\n"
                "🔍 Search the bot to find it!",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    await call.answer("✅ Marked complete")
    try:
        await call.message.edit_text(
            call.message.text + "\n\n✅ <b>Completed</b>", parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("req_reject_"))
async def req_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫 Unauthorized", show_alert=True)
        return
    req_id = int(call.data.split("_")[2])
    async with await db_connect() as db:
        await db.execute("UPDATE requests SET status='Rejected' WHERE id=?", (req_id,))
        await db.commit()
        row = await db.execute_fetchone("SELECT * FROM requests WHERE id=?", (req_id,))
    if row:
        try:
            await bot.send_message(
                row["user_id"],
                "❌ <b>REQUEST REJECTED</b>\n\n"
                "Your request could not be fulfilled at this time.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    await call.answer("❌ Rejected")
    try:
        await call.message.edit_text(
            call.message.text + "\n\n❌ <b>Rejected</b>", parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# ─── MY REQUESTS ──────────────────────────────────────────────────────────────
@router.callback_query(F.data == "my_requests")
async def cb_my_requests(call: CallbackQuery):
    async with await db_connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (call.from_user.id,),
        )
    if not rows:
        await call.message.edit_text(
            "📥 <b>MY REQUESTS</b>\n\nNo requests yet.",
            reply_markup=back_home_kb(),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    status_emoji = {"Pending": "⏳", "Completed": "✅", "Rejected": "❌"}
    lines = ["📥 <b>MY REQUESTS</b>\n"]
    for r in rows:
        lines.append(f"#{r['id']}\n🎬 {r['request_text']}\n{status_emoji.get(r['status'], '❓')} {r['status']}\n")

    await call.message.edit_text(
        "\n".join(lines), reply_markup=back_home_kb(), parse_mode=ParseMode.HTML
    )
    await call.answer()

# ─── PROFILE ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    async with await db_connect() as db:
        user = await get_user(db, call.from_user.id)
        if not user:
            await register_user(db, call.from_user)
            user = await get_user(db, call.from_user.id)
        s_used  = await daily_search_used(db, call.from_user.id)
        s_limit = await daily_search_limit(db, call.from_user.id)
        r_used  = await daily_request_used(db, call.from_user.id)
        r_limit = await daily_request_limit(db, call.from_user.id)
        ref_row = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (call.from_user.id,)
        )
    refs   = ref_row["c"] if ref_row else 0
    joined = user["joined_at"][:10] if user else "N/A"
    uname  = f"@{call.from_user.username}" if call.from_user.username else "None"
    prem   = "✅ Active" if user and user["is_premium"] else "❌ Inactive"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Usage",      callback_data="limits"),
            InlineKeyboardButton(text="🎁 Referrals",  callback_data="share"),
        ],
        [InlineKeyboardButton(text="📥 My Requests",   callback_data="my_requests")],
        [
            InlineKeyboardButton(text="🔙 Back",       callback_data="start"),
            InlineKeyboardButton(text="🏠 Main Menu",  callback_data="start"),
        ],
    ])
    await call.message.edit_text(
        f"👤 <b>MY PROFILE</b>\n\n"
        f"🆔 User ID: <code>{call.from_user.id}</code>\n"
        f"👤 Username: {uname}\n"
        f"📅 Joined: {joined}\n\n"
        f"🔍 Searches Today: {s_used}/{s_limit}\n"
        f"📥 Requests Today: {r_used}/{r_limit}\n\n"
        f"👥 Referrals: {refs}\n"
        f"🎁 Search Bonus: +{user['search_bonus'] if user else 0}\n"
        f"🎁 Request Bonus: +{user['request_bonus'] if user else 0}\n\n"
        f"⭐ Premium: {prem}",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ─── LIMITS ───────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "limits")
async def cb_limits(call: CallbackQuery):
    async with await db_connect() as db:
        user    = await get_user(db, call.from_user.id)
        s_used  = await daily_search_used(db, call.from_user.id)
        s_limit = await daily_search_limit(db, call.from_user.id)
        r_used  = await daily_request_used(db, call.from_user.id)
        r_limit = await daily_request_limit(db, call.from_user.id)
    s_bonus = user["search_bonus"]  if user else 0
    r_bonus = user["request_bonus"] if user else 0
    prem    = "✅ Active" if user and user["is_premium"] else "❌ Inactive"
    await call.message.edit_text(
        f"📊 <b>TODAY'S LIMITS</b>\n\n"
        f"🔍 <b>Search</b>\nUsed: {s_used}/{s_limit} · Remaining: {max(0, s_limit-s_used)}\n\n"
        f"📥 <b>Request</b>\nUsed: {r_used}/{r_limit} · Remaining: {max(0, r_limit-r_used)}\n\n"
        f"🎁 <b>Referral Bonus</b>\nSearch: +{s_bonus} · Request: +{r_bonus}\n\n"
        f"⭐ <b>Premium</b>: {prem}",
        reply_markup=back_home_kb(),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ─── SHARE & EARN ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "share")
async def cb_share(call: CallbackQuery):
    async with await db_connect() as db:
        user    = await get_user(db, call.from_user.id)
        ref_row = await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (call.from_user.id,)
        )
        sb_per = await get_setting(db, "referral_search_bonus",  "1")
        rb_per = await get_setting(db, "referral_request_bonus", "1")
    refs    = ref_row["c"] if ref_row else 0
    s_bonus = user["search_bonus"]  if user else 0
    r_bonus = user["request_bonus"] if user else 0
    ref_link = f"https://t.me/{BOT_USERNAME}?start={call.from_user.id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📤 Share Bot",
            url=f"https://t.me/share/url?url={ref_link}&text=Find%20movies%20instantly!",
        )],
        [InlineKeyboardButton(text="📋 Copy Referral Link", switch_inline_query=ref_link)],
        [InlineKeyboardButton(text="👥 My Referrals",        callback_data="my_referrals")],
        [
            InlineKeyboardButton(text="🔙 Back",       callback_data="start"),
            InlineKeyboardButton(text="🏠 Main Menu",  callback_data="start"),
        ],
    ])
    await call.message.edit_text(
        f"🎁 <b>SHARE & EARN</b>\n\n"
        f"Invite friends to <b>NETPRIME4U MOVIES FINDER</b>.\n\n"
        f"👥 Referrals: <b>{refs}</b>\n"
        f"🔍 Search bonus per referral: +{sb_per}\n"
        f"📥 Request bonus per referral: +{rb_per}\n\n"
        f"🎁 Your total bonuses:\nSearch: +{s_bonus} · Request: +{r_bonus}\n\n"
        f"🔗 Your referral link:\n<code>{ref_link}</code>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

@router.callback_query(F.data == "my_referrals")
async def cb_my_referrals(call: CallbackQuery):
    async with await db_connect() as db:
        rows = await db.execute_fetchall(
            "SELECT r.referee_id, u.username, u.first_name, r.created_at "
            "FROM referrals r LEFT JOIN users u ON u.user_id=r.referee_id "
            "WHERE r.referrer_id=? ORDER BY r.id DESC LIMIT 10",
            (call.from_user.id,),
        )
    if not rows:
        await call.message.edit_text(
            "👥 <b>MY REFERRALS</b>\n\nNo referrals yet. Share your link to earn bonuses!",
            reply_markup=back_home_kb("share"),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    lines = ["👥 <b>MY REFERRALS</b>\n"]
    for r in rows:
        name  = r["first_name"] or "Unknown"
        uname = f"(@{r['username']})" if r["username"] else ""
        lines.append(f"• {name} {uname} — {r['created_at'][:10]}")

    await call.message.edit_text(
        "\n".join(lines), reply_markup=back_home_kb("share"), parse_mode=ParseMode.HTML
    )
    await call.answer()

# ─── HELP ─────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await call.message.edit_text(
        "❓ <b>HOW TO USE NETPRIME4U MOVIES FINDER</b>\n\n"
        "1️⃣ Join the required channel.\n"
        "2️⃣ Click 🔍 Search Movies.\n"
        "3️⃣ Enter the movie name.\n"
        "4️⃣ Select the matching result.\n"
        "5️⃣ The bot sends the Telegram post.\n\n"
        "⏳ <b>Important:</b> Delivered posts auto-delete after 2 minutes.\n"
        "💾 Save or 📤 Share before the timer ends.\n\n"
        "📥 Can't find something? Use Request Movie.\n"
        "🎁 Need more searches? Use Share & Earn.",
        reply_markup=back_home_kb(),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ─── SETTINGS ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery):
    await call.message.edit_text(
        "⚙️ <b>SETTINGS</b>\n\n🔔 Notifications: Coming soon\n🌐 Language: English",
        reply_markup=back_home_kb(),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ─── UPDATES ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "updates")
async def cb_updates(call: CallbackQuery):
    url = JOIN_CHANNEL_URL or FORCE_JOIN_CHANNEL_URL or "https://t.me"
    await call.message.edit_text(
        "📢 <b>UPDATES</b>\n\nJoin our channel to stay updated with the latest content!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Join Channel", url=url)],
            [
                InlineKeyboardButton(text="🔙 Back",       callback_data="start"),
                InlineKeyboardButton(text="🏠 Main Menu",  callback_data="start"),
            ],
        ]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ─── SOURCE CHANNEL INDEXING ──────────────────────────────────────────────────
@router.channel_post()
async def handle_channel_post(message: Message):
    """
    Automatically index new posts arriving from SOURCE_CHANNEL_ID.
    Only processes posts from the configured source channel.
    Never downloads or modifies the original post.
    """
    if SOURCE_CHANNEL_ID is None:
        return  # Not configured

    if message.chat.id != SOURCE_CHANNEL_ID:
        return  # Ignore posts from other channels

    raw_text = (message.text or message.caption or "").strip()

    # Try to extract a filename from media if no text
    if not raw_text:
        if message.document and message.document.file_name:
            raw_text = message.document.file_name
        elif message.video and message.video.file_name:
            raw_text = message.video.file_name

    if not raw_text:
        log.info("Skipping channel post %s — no indexable text.", message.message_id)
        return

    title       = raw_text.split("\n")[0].strip()[:255]
    searchable  = normalize(raw_text)

    if message.video or message.document:
        ctype = "video"
    elif message.photo:
        ctype = "photo"
    elif message.text:
        ctype = "text"
    else:
        ctype = "other"

    async with await db_connect() as db:
        try:
            await db.execute(
                """INSERT OR IGNORE INTO telegram_posts
                       (source_chat_id, source_msg_id, title, searchable_text,
                        content_type, indexed_at, is_available)
                   VALUES (?,?,?,?,?,?,1)""",
                (message.chat.id, message.message_id, title, searchable, ctype, now_str()),
            )
            await db.commit()
            log.info(
                "✅ Indexed post %s from source channel — title: %s",
                message.message_id, title,
            )
        except Exception as e:
            log.error("Indexing error for msg %s: %s", message.message_id, e)

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "❓ <b>HELP</b>\n\n"
        "/search — Search movies\n"
        "/request — Request a movie\n"
        "/profile — Your profile\n"
        "/limits — Today's limits\n"
        "/referral — Share & earn",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    joined = await check_force_join(bot, message.from_user.id)
    if not joined:
        await message.answer(
            "🔐 Please join the required channel first.",
            reply_markup=force_join_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    await state.set_state(UserState.searching)
    await message.answer(
        "🔎 <b>SEARCH MOVIES</b>\n\nSend the movie name:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="start"),
        ]]),
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    async with await db_connect() as db:
        user    = await get_user(db, message.from_user.id)
        s_used  = await daily_search_used(db, message.from_user.id)
        s_limit = await daily_search_limit(db, message.from_user.id)
    if not user:
        await message.answer("Use /start first.")
        return
    await message.answer(
        f"👤 <b>MY PROFILE</b>\n\n"
        f"🆔 {message.from_user.id}\n"
        f"🔍 Searches Today: {s_used}/{s_limit}\n"
        f"⭐ Premium: {'Yes' if user['is_premium'] else 'No'}",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("limits"))
async def cmd_limits(message: Message):
    async with await db_connect() as db:
        s_used  = await daily_search_used(db, message.from_user.id)
        s_limit = await daily_search_limit(db, message.from_user.id)
        r_used  = await daily_request_used(db, message.from_user.id)
        r_limit = await daily_request_limit(db, message.from_user.id)
    await message.answer(
        f"📊 <b>TODAY'S LIMITS</b>\n\n"
        f"🔍 Search: {s_used}/{s_limit}\n"
        f"📥 Request: {r_used}/{r_limit}",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("referral"))
async def cmd_referral(message: Message):
    ref_link = f"https://t.me/{BOT_USERNAME}?start={message.from_user.id}"
    await message.answer(
        f"🎁 <b>YOUR REFERRAL LINK</b>\n\n<code>{ref_link}</code>",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("request"))
async def cmd_request(message: Message, state: FSMContext):
    async with await db_connect() as db:
        used  = await daily_request_used(db, message.from_user.id)
        limit = await daily_request_limit(db, message.from_user.id)
    if used >= limit:
        await message.answer("📊 Request limit reached for today.")
        return
    await state.set_state(UserState.requesting)
    await message.answer(
        f"📥 <b>REQUEST MOVIE</b>\n\nSend the movie name:\n\nRemaining: {limit - used}",
        parse_mode=ParseMode.HTML,
    )

# ─── ADMIN PANEL ──────────────────────────────────────────────────────────────
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await _show_admin_panel(message)

async def _show_admin_panel(target, edit: bool = False):
    text = "🛠️ <b>NETPRIME4U ADMIN PANEL</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Dashboard",  callback_data="adm_stats"),
            InlineKeyboardButton(text="🎬 Content",    callback_data="adm_content"),
        ],
        [
            InlineKeyboardButton(text="📥 Requests",   callback_data="adm_requests"),
            InlineKeyboardButton(text="👥 Users",      callback_data="adm_users"),
        ],
        [
            InlineKeyboardButton(text="📢 Channels",   callback_data="adm_channels"),
            InlineKeyboardButton(text="📣 Broadcast",  callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton(text="🎁 Referrals",  callback_data="adm_referrals"),
            InlineKeyboardButton(text="⚙️ Settings",   callback_data="adm_settings"),
        ],
        [InlineKeyboardButton(text="🔧 Maintenance",   callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="🏠 Main Menu",      callback_data="start")],
    ])
    if edit and hasattr(target, "edit_text"):
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "admin")
async def cb_admin(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫 Unauthorized", show_alert=True)
        return
    await _show_admin_panel(call.message, edit=True)
    await call.answer()

# ── Dashboard ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_stats")
async def adm_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        total_users    = (await db.execute_fetchone("SELECT COUNT(*) as c FROM users"))["c"]
        active_users   = (await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM users WHERE last_active >= datetime('now','-7 days')"
        ))["c"]
        today_users    = (await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM users WHERE date(joined_at) = date('now')"
        ))["c"]
        total_posts    = (await db.execute_fetchone("SELECT COUNT(*) as c FROM telegram_posts"))["c"]
        avail_posts    = (await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM telegram_posts WHERE is_available=1"
        ))["c"]
        total_searches = (await db.execute_fetchone("SELECT SUM(count) as c FROM search_usage"))["c"] or 0
        total_requests = (await db.execute_fetchone("SELECT COUNT(*) as c FROM requests"))["c"]
        pending_reqs   = (await db.execute_fetchone(
            "SELECT COUNT(*) as c FROM requests WHERE status='Pending'"
        ))["c"]
        total_refs     = (await db.execute_fetchone("SELECT COUNT(*) as c FROM referrals"))["c"]
        premium_users  = (await db.execute_fetchone("SELECT COUNT(*) as c FROM users WHERE is_premium=1"))["c"]

    # Channel status summary
    src_status  = f"<code>{SOURCE_CHANNEL_ID}</code>" if SOURCE_CHANNEL_ID else "❌ Not set"
    fj_status   = f"<code>{FORCE_JOIN_CHANNEL_ID}</code>" if FORCE_JOIN_CHANNEL_ID else "❌ Not set"
    jcu_status  = f"<code>{JOIN_CHANNEL_URL[:30]}…</code>" if JOIN_CHANNEL_URL else "❌ Not set"

    await call.message.edit_text(
        f"📊 <b>BOT STATISTICS</b>\n\n"
        f"👥 Total Users: <b>{total_users}</b>\n"
        f"🟢 Active (7d): <b>{active_users}</b>\n"
        f"📅 Joined Today: <b>{today_users}</b>\n\n"
        f"🎬 Indexed Posts: <b>{total_posts}</b> (available: {avail_posts})\n\n"
        f"🔍 Total Searches: <b>{total_searches}</b>\n"
        f"📥 Total Requests: <b>{total_requests}</b>\n"
        f"⏳ Pending: <b>{pending_reqs}</b>\n\n"
        f"👥 Total Referrals: <b>{total_refs}</b>\n"
        f"⭐ Premium Users: <b>{premium_users}</b>\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"📡 <b>Channel Config</b>\n"
        f"🎬 Source: {src_status}\n"
        f"🔐 Force Join: {fj_status}\n"
        f"🔗 Join URL: {jcu_status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ── Content ───────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_content")
async def adm_content(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        rows  = await db.execute_fetchall(
            "SELECT id, title, indexed_at, is_available FROM telegram_posts ORDER BY id DESC LIMIT 10"
        )
        total = (await db.execute_fetchone("SELECT COUNT(*) as c FROM telegram_posts"))["c"]

    lines = [f"🎬 <b>CONTENT INDEX</b>\n\nTotal: <b>{total}</b>\n\n🆕 Latest 10:\n"]
    for r in rows:
        avail = "✅" if r["is_available"] else "❌"
        lines.append(f"{avail} #{r['id']} {r['title'][:40]}")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ── Channel Config Panel ──────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_channels")
async def adm_channels(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return

    # Live-check source channel access
    src_status = "❌ Not configured"
    if SOURCE_CHANNEL_ID:
        try:
            chat = await bot.get_chat(SOURCE_CHANNEL_ID)
            src_status = f"✅ {chat.title}"
        except TelegramBadRequest:
            src_status = f"⚠️ Bot has no access (ID: {SOURCE_CHANNEL_ID})"
        except TelegramForbiddenError:
            src_status = f"🚫 Forbidden (ID: {SOURCE_CHANNEL_ID})"
        except Exception as e:
            src_status = f"⚠️ Error: {e}"

    # Live-check force-join channel access
    fj_status = "❌ Not configured"
    if FORCE_JOIN_CHANNEL_ID:
        try:
            chat = await bot.get_chat(FORCE_JOIN_CHANNEL_ID)
            fj_status = f"✅ {chat.title}"
        except TelegramBadRequest:
            fj_status = f"⚠️ Bot has no access (ID: {FORCE_JOIN_CHANNEL_ID})"
        except TelegramForbiddenError:
            fj_status = f"🚫 Forbidden (ID: {FORCE_JOIN_CHANNEL_ID})"
        except Exception as e:
            fj_status = f"⚠️ Error: {e}"

    fj_url  = FORCE_JOIN_CHANNEL_URL or "❌ Not set"
    jcu_url = JOIN_CHANNEL_URL       or "❌ Not set"

    await call.message.edit_text(
        f"📡 <b>CHANNEL CONFIGURATION</b>\n\n"
        f"<b>1. Source Channel</b>\n"
        f"ID: <code>{SOURCE_CHANNEL_ID or 'Not set'}</code>\n"
        f"Status: {src_status}\n"
        f"<i>Posts from this channel are auto-indexed.</i>\n\n"
        f"<b>2. Force Join Channel</b>\n"
        f"ID: <code>{FORCE_JOIN_CHANNEL_ID or 'Not set'}</code>\n"
        f"Status: {fj_status}\n"
        f"Join URL: <code>{fj_url}</code>\n"
        f"<i>Users must join before using the bot.</i>\n\n"
        f"<b>3. Content Delivery Button</b>\n"
        f"Join URL: <code>{jcu_url}</code>\n"
        f"<i>Button shown under every delivered post.</i>\n\n"
        f"⚙️ All values are set via .env — restart the bot after changing them.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh Status", callback_data="adm_channels")],
            [InlineKeyboardButton(text="🔙 Admin",          callback_data="admin")],
        ]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ── Requests (admin) ──────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_requests")
async def adm_requests(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM requests WHERE status='Pending' ORDER BY id DESC LIMIT 10"
        )
    if not rows:
        await call.message.edit_text(
            "📥 <b>PENDING REQUESTS</b>\n\nNo pending requests.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
            ]]),
            parse_mode=ParseMode.HTML,
        )
        await call.answer()
        return

    for r in rows:
        uname = f"@{r['username']}" if r["username"] else f"ID:{r['user_id']}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Complete", callback_data=f"req_complete_{r['id']}"),
            InlineKeyboardButton(text="❌ Reject",   callback_data=f"req_reject_{r['id']}"),
        ]])
        await call.message.answer(
            f"📥 <b>Request #{r['id']}</b>\n\n"
            f"👤 {uname} ({r['user_id']})\n"
            f"🎬 {r['request_text']}\n"
            f"📅 {r['created_at'][:10]}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    await call.answer()

# ── Users (admin) ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_users")
async def adm_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Search User",  callback_data="adm_search_user")],
        [InlineKeyboardButton(text="📋 Recent Users", callback_data="adm_recent_users")],
        [InlineKeyboardButton(text="🔙 Admin",        callback_data="admin")],
    ])
    await call.message.edit_text("👥 <b>USER MANAGEMENT</b>", reply_markup=kb, parse_mode=ParseMode.HTML)
    await call.answer()

@router.callback_query(F.data == "adm_search_user")
async def adm_search_user_cb(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    await state.set_state(UserState.admin_search_user)
    await call.message.edit_text(
        "🔍 Send user ID or @username:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
        ]]),
    )
    await call.answer()

@router.message(UserState.admin_search_user)
async def adm_search_user_msg(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    query = message.text.strip().lstrip("@")
    async with await db_connect() as db:
        if query.isdigit():
            user = await db.execute_fetchone("SELECT * FROM users WHERE user_id=?", (int(query),))
        else:
            user = await db.execute_fetchone("SELECT * FROM users WHERE username=?", (query,))

    if not user:
        await message.answer(
            "❌ User not found.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
            ]]),
        )
        return

    uid   = user["user_id"]
    uname = f"@{user['username']}" if user["username"] else "None"
    prem  = "✅" if user["is_premium"] else "❌"
    ban   = "🚫 Banned" if user["is_banned"] else "✅ Active"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🚫 Ban" if not user["is_banned"] else "✅ Unban",
                callback_data=f"adm_ban_{uid}",
            ),
            InlineKeyboardButton(
                text="⭐ Give Premium" if not user["is_premium"] else "❌ Remove Premium",
                callback_data=f"adm_prem_{uid}",
            ),
        ],
        [
            InlineKeyboardButton(text="🔍 +Search Bonus",  callback_data=f"adm_sbonus_{uid}"),
            InlineKeyboardButton(text="📥 +Request Bonus", callback_data=f"adm_rbonus_{uid}"),
        ],
        [InlineKeyboardButton(text="🔄 Reset Limits",      callback_data=f"adm_reset_{uid}")],
        [InlineKeyboardButton(text="🔙 Admin",             callback_data="admin")],
    ])
    await message.answer(
        f"👤 <b>USER INFO</b>\n\n"
        f"🆔 {uid}\n"
        f"👤 {uname}\n"
        f"📅 Joined: {user['joined_at'][:10]}\n"
        f"⭐ Premium: {prem}\n"
        f"Status: {ban}\n"
        f"🔍 Search Bonus: +{user['search_bonus']}\n"
        f"📥 Request Bonus: +{user['request_bonus']}",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )

@router.callback_query(F.data.startswith("adm_ban_"))
async def adm_ban(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    uid = int(call.data.split("_")[2])
    async with await db_connect() as db:
        user = await get_user(db, uid)
        if not user:
            await call.answer("User not found", show_alert=True)
            return
        new_val = 0 if user["is_banned"] else 1
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (new_val, uid))
        await db.commit()
    await call.answer(f"✅ User {'Banned' if new_val else 'Unbanned'}")

@router.callback_query(F.data.startswith("adm_prem_"))
async def adm_prem(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    uid = int(call.data.split("_")[2])
    async with await db_connect() as db:
        user = await get_user(db, uid)
        if not user:
            await call.answer("User not found", show_alert=True)
            return
        new_val = 0 if user["is_premium"] else 1
        until   = (datetime.now(TZ) + timedelta(days=30)).isoformat() if new_val else None
        await db.execute(
            "UPDATE users SET is_premium=?, premium_until=? WHERE user_id=?",
            (new_val, until, uid),
        )
        await db.commit()
    action = "Premium given (30 days)" if new_val else "Premium removed"
    await call.answer(f"⭐ {action}")
    try:
        msg = (
            "⭐ You've been given <b>Premium</b> access for 30 days!"
            if new_val
            else "❌ Your Premium access has been removed."
        )
        await bot.send_message(uid, msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass

@router.callback_query(F.data.startswith("adm_sbonus_"))
async def adm_sbonus(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    uid = int(call.data.split("_")[2])
    async with await db_connect() as db:
        await db.execute("UPDATE users SET search_bonus=search_bonus+1 WHERE user_id=?", (uid,))
        await db.commit()
    await call.answer("✅ +1 Search Bonus added")

@router.callback_query(F.data.startswith("adm_rbonus_"))
async def adm_rbonus(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    uid = int(call.data.split("_")[2])
    async with await db_connect() as db:
        await db.execute("UPDATE users SET request_bonus=request_bonus+1 WHERE user_id=?", (uid,))
        await db.commit()
    await call.answer("✅ +1 Request Bonus added")

@router.callback_query(F.data.startswith("adm_reset_"))
async def adm_reset(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    uid = int(call.data.split("_")[2])
    async with await db_connect() as db:
        await db.execute(
            "DELETE FROM search_usage WHERE user_id=? AND date_str=?", (uid, today_str())
        )
        await db.execute(
            "DELETE FROM request_usage WHERE user_id=? AND date_str=?", (uid, today_str())
        )
        await db.commit()
    await call.answer("✅ Limits reset for today")

@router.callback_query(F.data == "adm_recent_users")
async def adm_recent_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        rows = await db.execute_fetchall(
            "SELECT user_id, username, first_name, joined_at FROM users ORDER BY joined_at DESC LIMIT 10"
        )
    lines = ["👥 <b>RECENT USERS</b>\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "None"
        lines.append(f"• {r['first_name'] or 'N/A'} {uname} — {r['joined_at'][:10]}")
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ── Broadcast ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_cb(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    await state.set_state(UserState.broadcast_msg)
    await call.message.edit_text(
        "📣 <b>BROADCAST</b>\n\nSend the message to broadcast to all users:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Admin", callback_data="admin"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

@router.message(UserState.broadcast_msg)
async def handle_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    async with await db_connect() as db:
        users = await db.execute_fetchall("SELECT user_id FROM users WHERE is_banned=0")

    total   = len(users)
    success = fail = 0
    prog    = await message.answer(f"📣 Broadcasting to {total} users...")

    for i, u in enumerate(users):
        try:
            await bot.copy_message(
                chat_id=u["user_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            success += 1
        except TelegramForbiddenError:
            fail += 1
        except Exception:
            fail += 1
        if i % 20 == 0:
            await asyncio.sleep(1)  # Telegram flood control

    await prog.edit_text(
        f"✅ <b>BROADCAST COMPLETE</b>\n\n"
        f"📤 Sent: {success}\n❌ Failed: {fail}\n👥 Total: {total}",
        parse_mode=ParseMode.HTML,
    )

# ── Settings (admin) ──────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_settings")
async def adm_settings(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        sl = await get_setting(db, "daily_search_limit",    "3")
        rl = await get_setting(db, "daily_request_limit",   "2")
        sb = await get_setting(db, "referral_search_bonus", "1")
        rb = await get_setting(db, "referral_request_bonus","1")
        dd = await get_setting(db, "delete_delay",          "120")
        dw = await get_setting(db, "delete_warning_too",    "0")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"🔍 Search Limit: {sl}",  callback_data="adms_sl"),
            InlineKeyboardButton(text=f"📥 Request Limit: {rl}", callback_data="adms_rl"),
        ],
        [
            InlineKeyboardButton(text=f"🎁 Search Bonus: {sb}",  callback_data="adms_sb"),
            InlineKeyboardButton(text=f"🎁 Req Bonus: {rb}",     callback_data="adms_rb"),
        ],
        [
            InlineKeyboardButton(text=f"⏳ Delete Delay: {dd}s", callback_data="adms_dd"),
            InlineKeyboardButton(text=f"🗑️ Del Warning: {'ON' if dw=='1' else 'OFF'}", callback_data="adms_dw"),
        ],
        [InlineKeyboardButton(text="🔙 Admin",                   callback_data="admin")],
    ])
    await call.message.edit_text(
        "⚙️ <b>BOT SETTINGS</b>\n\n"
        "<i>Channel IDs and URLs are set in .env and require a bot restart to change.</i>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

_SETTING_MAP = {
    "adms_sl": ("daily_search_limit",    "🔍 Daily Search Limit"),
    "adms_rl": ("daily_request_limit",   "📥 Daily Request Limit"),
    "adms_sb": ("referral_search_bonus", "🎁 Referral Search Bonus"),
    "adms_rb": ("referral_request_bonus","🎁 Referral Request Bonus"),
    "adms_dd": ("delete_delay",          "⏳ Delete Delay (seconds, default 120)"),
    "adms_dw": ("delete_warning_too",    "🗑️ Delete Warning Too — enter 1 (ON) or 0 (OFF)"),
}

@router.callback_query(F.data.in_(set(_SETTING_MAP.keys())))
async def adm_setting_cb(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    key, label = _SETTING_MAP[call.data]
    await state.update_data(setting_key=key)
    await state.set_state(UserState.admin_add_bonus)
    await call.message.edit_text(
        f"⚙️ Setting: <b>{label}</b>\n\nSend the new value:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Settings", callback_data="adm_settings"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

@router.message(UserState.admin_add_bonus)
async def adm_setting_value(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key  = data.get("setting_key", "")
    await state.clear()
    val  = message.text.strip()
    async with await db_connect() as db:
        await set_setting(db, key, val)
    await message.answer(
        f"✅ Updated: <code>{key}</code> = <code>{val}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Settings", callback_data="adm_settings"),
        ]]),
        parse_mode=ParseMode.HTML,
    )

# ── Maintenance ───────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_maintenance")
async def adm_maintenance(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        current = await get_setting(db, "maintenance", "0")
        new_val = "0" if current == "1" else "1"
        await set_setting(db, "maintenance", new_val)
    status = "ENABLED ✅" if new_val == "1" else "DISABLED ❌"
    await call.answer(f"🔧 Maintenance {status}", show_alert=True)

# ── Referrals (admin) ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm_referrals")
async def adm_referrals(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("🚫", show_alert=True)
        return
    async with await db_connect() as db:
        total = (await db.execute_fetchone("SELECT COUNT(*) as c FROM referrals"))["c"]
        sb    = await get_setting(db, "referral_search_bonus", "1")
        rb    = await get_setting(db, "referral_request_bonus", "1")
    await call.message.edit_text(
        f"🎁 <b>REFERRAL SETTINGS</b>\n\n"
        f"👥 Total Referrals: {total}\n"
        f"🔍 Search Bonus per referral: +{sb}\n"
        f"📥 Request Bonus per referral: +{rb}\n\n"
        f"Adjust via ⚙️ Settings.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙️ Settings", callback_data="adm_settings"),
            InlineKeyboardButton(text="🔙 Admin",    callback_data="admin"),
        ]]),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()

# ─── ADMIN COMMANDS ───────────────────────────────────────────────────────────
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with await db_connect() as db:
        total = (await db.execute_fetchone("SELECT COUNT(*) as c FROM users"))["c"]
        posts = (await db.execute_fetchone("SELECT COUNT(*) as c FROM telegram_posts"))["c"]
    await message.answer(
        f"📊 Users: {total}\n🎬 Indexed Posts: {posts}", parse_mode=ParseMode.HTML
    )

@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with await db_connect() as db:
        current = await get_setting(db, "maintenance", "0")
        new_val = "0" if current == "1" else "1"
        await set_setting(db, "maintenance", new_val)
    await message.answer(f"🔧 Maintenance {'ENABLED' if new_val == '1' else 'DISABLED'}")

@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(UserState.broadcast_msg)
    await message.answer("📣 Send the broadcast message:")

# ─── STARTUP / MAIN ──────────────────────────────────────────────────────────
async def on_startup():
    _warn_config()
    await init_db()
    log.info("✅ Database initialized: %s", DB_PATH)
    await process_pending_deletions(bot)
    log.info("✅ Pending deletions processed.")

    # Verify bot identity
    me = await bot.get_me()
    log.info("🤖 Bot: @%s (ID %s)", me.username, me.id)

    # Verify source channel access
    if SOURCE_CHANNEL_ID:
        try:
            chat = await bot.get_chat(SOURCE_CHANNEL_ID)
            log.info("📡 Source channel: %s (%s)", chat.title, SOURCE_CHANNEL_ID)
        except TelegramForbiddenError:
            log.error(
                "❌ Bot cannot access SOURCE_CHANNEL_ID=%s — "
                "make sure the bot is an administrator in that channel.",
                SOURCE_CHANNEL_ID,
            )
        except TelegramBadRequest as e:
            log.error("❌ SOURCE_CHANNEL_ID=%s error: %s", SOURCE_CHANNEL_ID, e)

    # Verify force-join channel access
    if FORCE_JOIN_CHANNEL_ID:
        try:
            chat = await bot.get_chat(FORCE_JOIN_CHANNEL_ID)
            log.info("🔐 Force-join channel: %s (%s)", chat.title, FORCE_JOIN_CHANNEL_ID)
        except TelegramForbiddenError:
            log.error(
                "❌ Bot cannot access FORCE_JOIN_CHANNEL_ID=%s — "
                "add the bot to that channel so it can check membership.",
                FORCE_JOIN_CHANNEL_ID,
            )
        except TelegramBadRequest as e:
            log.error("❌ FORCE_JOIN_CHANNEL_ID=%s error: %s", FORCE_JOIN_CHANNEL_ID, e)

    log.info("🎬 NETPRIME4U MOVIES FINDER is running.")

async def main():
    await on_startup()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
