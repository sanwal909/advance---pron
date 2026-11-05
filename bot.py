# -*- coding: utf-8 -*-
import telebot
from telebot import types
import qrcode
import time
import random
import threading
from datetime import datetime, timedelta
import logging
from io import BytesIO
import json
import os
import sys

# Import config and verification
import config
from config import *
from verif import init_verification, make_colored_button, resolve_color, color_name_display, get_random_button_color_nondefault

# Second payment option: In666/CQPAY "auto-pay" (pay2.py). Optional — bot manual
# UPI/QR mode me bina iske bhi chalta rahega.
try:
    import pay2
    PAY2_AVAILABLE = True
except Exception as _pay2_err:
    pay2 = None
    PAY2_AVAILABLE = False
    print(f"ℹ️ pay2 module not loaded ({type(_pay2_err).__name__}) — Auto-Pay disabled.")

# Initialize bot — SAFE TOKEN VALIDATION & VERIFICATION
# Priority: env var via config.BOT_TOKEN (loaded from .env)
_token = getattr(config, 'BOT_TOKEN', os.getenv("BOT_TOKEN", ""))
_token = _token.strip().strip('"').strip("'")

# ---------- Step 1: Format-level check (cheap, local) ----------
if not _token:
    print("\n❌ FATAL: BOT_TOKEN environment variable is EMPTY!")
    print("How to fix:")
    print("  1. Go to @BotFather on Telegram")
    print("  2. Create a new bot (or use an existing one) and copy the HTTP API token")
    print("  3. In your hosting panel, set env variable BOT_TOKEN=<your-new-token>")
    print("  4. Restart the bot / redeploy")
    sys.exit(1)

# Telegram bot tokens always look like NNNNNNNN:AAAAAAAAAAAAAAAAAAAAAAAAAAA
# Format check: digits + ":" + base64-like chars (no whitespace)
import re as _re
_token_fmt_ok = bool(_re.fullmatch(r"\d{6,12}:[A-Za-z0-9_\-]{20,60}", _token))
if not _token_fmt_ok:
    print("\n❌ FATAL: BOT_TOKEN format looks INVALID!")
    print("Expected format: 1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ_123")
    print("Please double-check the token value from @BotFather.")
    print("Token starts with:", _token[:6], "..." if len(_token) > 6 else "")
    sys.exit(1)

# ---------- Step 2: Create TeleBot instance ----------
bot = telebot.TeleBot(_token, parse_mode="HTML", threaded=True)

# ---------- Step 3: REAL Telegram API validation via get_me() ----------
_bot_me = None
_bot_token_retry = 3
_bot_token_retry_wait = 2
for _attempt in range(1, _bot_token_retry + 1):
    try:
        _bot_me = bot.get_me()
        if _bot_me and getattr(_bot_me, 'id', None):
            break
    except telebot.apihelper.ApiTelegramException as _e:
        _err_desc = getattr(_e, 'description', str(_e))
        _err_code = getattr(_e, 'error_code', 0)
        # 401 = Unauthorized (token invalid / revoked)
        # 404 = Not Found (malformed token)
        if _err_code in (401, 404):
            print(f"\n❌ FATAL: BOT_TOKEN rejected by Telegram (HTTP {_err_code})!")
            print(f"Message: {_err_desc}")
            print("\nPossible causes:")
            print("  • Token is wrong / has typos")
            print("  • Old bot was banned/deleted — you need a NEW token from @BotFather")
            print("  • Token has been revoked")
            print("\nFix:")
            print("  1. Open @BotFather → /mybots → your bot → API Token → 'Revoke current token' → Generate new")
            print("  2. Replace BOT_TOKEN env var with the new token")
            print("  3. Restart the bot")
            sys.exit(1)
        else:
            print(f"⚠️ Telegram get_me attempt {_attempt}/{_bot_token_retry} failed: HTTP {_err_code} {_err_desc}")
            if _attempt < _bot_token_retry:
                time.sleep(_bot_token_retry_wait)
    except Exception as _e:
        print(f"⚠️ Token validation network error attempt {_attempt}/{_bot_token_retry}: {type(_e).__name__}")
        if _attempt < _bot_token_retry:
            time.sleep(_bot_token_retry_wait)
else:
    # For-loop exhausted without break AND token was not known-invalid → transient network issue
    # Don't crash — polling itself is where updates come from, and infinity_polling has its own
    # retries. But warn clearly.
    print("\n⚠️ WARNING: Could not reach Telegram API to validate the token RIGHT NOW.")
    print("  (Likely temporary network / DNS issue.)")
    print("  Bot will still start and retry polling. If token is bad, polling will keep failing.")
    _bot_me = None

# ---------- Step 4: Mark valid and cache bot identity info (never persist with data) ----------
config.startup_status["bot_token_valid"] = _bot_me is not None and getattr(_bot_me, 'id', None) is not None

# Identity info cached for logs only — NOT saved to DB
if _bot_me:
    _bot_id = getattr(_bot_me, 'id', None)
    _bot_username = getattr(_bot_me, 'username', None)
    _bot_name = getattr(_bot_me, 'first_name', None)
    print(f"✅ Telegram token verified. Bot ID: {_bot_id}  Username: @{_bot_username}  Name: {_bot_name}")
    logging.info(f"Bot identity verified: id={_bot_id} username=@{_bot_username}")
else:
    print("ℹ️ Telegram token NOT verified at startup (network). Will retry during polling.")
    logging.warning("Bot token NOT verified with Telegram API yet (startup network issue).")
    _bot_id = None
    _bot_username = None
    _bot_name = None

del _token_fmt_ok, _re, _attempt, _bot_token_retry, _bot_token_retry_wait
# NOTE: Keep `_token` local to the module; the `bot` instance is the handle.
# We intentionally do NOT log/print the actual token anywhere.

# ========== BUTTON COLOR FIX (Strict Background Color Support) ==========
_original_ikb_init = types.InlineKeyboardButton.__init__
def _patched_ikb_init(self, *args, **kwargs):
    # Extract color-related fields (supporting style, color, or button_color)
    style = kwargs.pop('style', None) or kwargs.pop('color', None) or kwargs.pop('button_color', None)
    _original_ikb_init(self, *args, **kwargs)
    if style:
        # Resolve common names to Telegram's required values: primary, success, danger
        s = str(style).lower()
        if s in ["blue", "primary"]: self.button_style = "primary"
        elif s in ["green", "positive", "success"]: self.button_style = "success"
        elif s in ["red", "negative", "danger"]: self.button_style = "danger"
        else: self.button_style = None

types.InlineKeyboardButton.__init__ = _patched_ikb_init

_original_ikb_to_dict = types.InlineKeyboardButton.to_dict
def _patched_ikb_to_dict(self):
    json_dict = _original_ikb_to_dict(self)
    style = getattr(self, 'button_style', None)
    if style in ["primary", "success", "danger"]:
        json_dict['style'] = style
    return json_dict

types.InlineKeyboardButton.to_dict = _patched_ikb_to_dict


# Initialize verification system
verif = init_verification(bot)

BUTTON_COLORS_BOT = [None, "primary", "success", "danger"]

def rnd_color():
    """Shortcut for random button color (always returns a premium color)"""
    if 'verif' in globals() and hasattr(verif, 'get_random_button_color_nondefault'):
        return verif.get_random_button_color_nondefault()
    return random.choice(["primary", "success", "danger"])

def make_button(text, **kwargs):
    """Create InlineKeyboardButton with random color if no URL."""
    if 'url' not in kwargs:
        kwargs['style'] = rnd_color()
    return types.InlineKeyboardButton(text, **kwargs)

def is_admin(user_id):
    """Check if a user is an admin"""
    admin_ids = settings.get('admin_ids', [])
    
    # Sync admins from .env into settings if needed
    changed = False
    for env_id in ADMIN_IDS_ENV:
        if str(env_id) not in [str(aid) for aid in admin_ids]:
            admin_ids.append(str(env_id))
            changed = True
            
    if changed:
        settings['admin_ids'] = admin_ids
        save_settings()
        
    # Convert all to strings for comparison
    return str(user_id) in [str(aid) for aid in admin_ids]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Auto-save thread
def auto_save_data():
    while True:
        time.sleep(300)
        save_all_data()

auto_save_thread = threading.Thread(target=auto_save_data, daemon=True)
auto_save_thread.start()

# Auto-clean expired pending payments (no proof uploaded after QR generated / after ask_for_screenshot)
# Silent cancel: no message to user, no admin alert. Just removes pending entry.
PENDING_PROOF_TIMEOUT_SEC = 600  # 10 minutes (same as QR auto-delete)
PENDING_INITIATED_TIMEOUT_SEC = 900  # 15 minutes (after QR generated, user clicked Payment Done but never send proof)

def auto_clean_pending_payments():
    while True:
        try:
            now = datetime.now()
            removed_count = 0
            to_delete = []

            for user_id_str, pending_data in pending_verifications.items():
                # Case 1: Screenshot already uploaded → admin will handle, do NOT auto-cancel
                if pending_data.get('screenshot_file_id'):
                    continue

                initiated_str = pending_data.get('initiated_at')
                screenshot_req_str = pending_data.get('screenshot_requested_at')

                # Decide which timestamp to use (use latest available)
                check_time_str = None
                timeout = PENDING_INITIATED_TIMEOUT_SEC
                if screenshot_req_str:
                    check_time_str = screenshot_req_str
                    timeout = PENDING_PROOF_TIMEOUT_SEC  # after asking screenshot - wait 10 min
                elif initiated_str:
                    check_time_str = initiated_str

                if check_time_str:
                    try:
                        check_time = datetime.strptime(check_time_str, "%Y-%m-%d %H:%M:%S")
                        if (now - check_time).total_seconds() > timeout:
                            to_delete.append(user_id_str)
                    except Exception as e:
                        logging.warning(f"Pending parse time error: {e}")

            for uid in to_delete:
                del pending_verifications[uid]
                removed_count += 1

            if removed_count > 0:
                # Save to both JSON and MongoDB (silent)
                try:
                    save_json_file(PENDING_VERIF_FILE, pending_verifications)
                except:
                    pass
                logging.info(f"🧹 Auto-cleaned {removed_count} expired pending payments (no proof uploaded).")

        except Exception as e:
            logging.error(f"Auto clean pending error: {e}")

        # Run every 60 seconds
        time.sleep(60)

# Start cleanup thread
_pending_cleanup_thread = threading.Thread(target=auto_clean_pending_payments, daemon=True)
_pending_cleanup_thread.start()
logging.info("🧹 Auto-clean pending payments thread started (10min timeout, no user msg).")

# Notify Admin about MongoDB status on startup (SAFE: never include Mongo URI in messages)
def notify_mongo_status():
    try:
        if not config.startup_status["mongo_connected"] and config.startup_status.get("mongo_error"):
            # Wait a bit for bot polling to be online so send_message succeeds
            time.sleep(5)
            admin_ids = settings.get('admin_ids', [])
            if admin_ids:
                raw_err = config.startup_status["mongo_error"] or config.mongo_error or "unknown"
                safe_err = config.redact_secrets_from_text(str(raw_err), max_len=300)
                msg = (
                    f"⚠️ <b>MongoDB Connection Failed!</b>\n\n"
                    f"<b>Error type:</b> <code>{safe_err}</code>\n\n"
                    f"💡 Bot is running on <b>Local JSON</b> mode. "
                    f"Your data will be saved locally in the <code>data/</code> folder.\n\n"
                    f"🔧 <b>How to fix:</b>\n"
                    f"1. Check MONGO_URI env var in your hosting panel\n"
                    f"2. Verify network / firewall allows outbound to MongoDB\n"
                    f"3. Restart / redeploy the bot\n\n"
                    f"ℹ️ <b>Note:</b> If the old bot was replaced with a new bot token, "
                    f"existing DB data (admins, users, settings) are AUTOMATICALLY restored "
                    f"once MongoDB reconnects."
                )
                for aid in admin_ids:
                    try:
                        bot.send_message(aid, msg, parse_mode="HTML")
                    except Exception as _ee:
                        logging.warning(f"notify_mongo_status: failed to send admin {aid}: {type(_ee).__name__}")
                        continue
    except Exception as e:
        logging.error(f"Notify Mongo Status Error: {type(e).__name__}")

# Run notification in a separate thread to not block startup
threading.Thread(target=notify_mongo_status, daemon=True).start()

# ---------- Token-change admin advisory (safe, informational only) ----------
def _notify_bot_identity_if_changed():
    """If bot identity info is available at startup, log a one-line summary (never DM) for debugging.
    We intentionally do NOT DM admins the bot username — that would cause message spam on every restart."""
    try:
        if '_bot_username' in dir() and _bot_username:
            logging.info(f"Startup: bot identity is @{_bot_username} (id={_bot_id}). "
                         f"DB data source: {'MongoDB' if config.startup_status['config_loaded_from_mongo'] else 'local JSON'}.")
    except Exception:
        pass

threading.Thread(target=_notify_bot_identity_if_changed, daemon=True).start()

user_demo_messages = {}
user_all_messages = {}

def track_msg(chat_id, message_id):
    """Track any bot message sent to user for later bulk deletion"""
    chat_id_str = str(chat_id)
    if chat_id_str not in user_all_messages:
        user_all_messages[chat_id_str] = []
    user_all_messages[chat_id_str].append(message_id)

def clear_user_demos(chat_id):
    chat_id_str = str(chat_id)
    if chat_id_str in user_demo_messages:
        for msg_id in user_demo_messages[chat_id_str]:
            try:
                bot.delete_message(chat_id, msg_id)
            except:
                pass
        user_demo_messages[chat_id_str] = []

def clear_all_user_msgs(chat_id):
    """Delete ALL tracked bot messages for a user (demos + descriptions + menu msgs)"""
    clear_user_demos(chat_id)
    chat_id_str = str(chat_id)
    if chat_id_str in user_all_messages:
        for msg_id in list(user_all_messages[chat_id_str]):
            try:
                bot.delete_message(chat_id, msg_id)
            except:
                pass
        user_all_messages[chat_id_str] = []

def delete_message_after_delay(chat_id, message_id, delay=300, send_timeout_msg=False):
    def delete():
        try:
            bot.delete_message(chat_id, message_id)
            logging.info(f"Successfully deleted message {message_id} in {chat_id}")
            if send_timeout_msg:
                tm = bot.send_message(
                    chat_id,
                    "<b>⏰ Session Timed Out!</b>\n\nThe payment QR code has been deleted for security. Please use /start to generate a new one.",
                    parse_mode="HTML"
                )
                track_msg(chat_id, tm.message_id)
        except Exception as e:
            logging.warning(f"Failed to delete message {message_id} in {chat_id}: {e}")
            
    # Start timer
    timer = threading.Timer(delay, delete)
    timer.daemon = True # Ensure thread doesn't block exit
    timer.start()
    logging.info(f"Scheduled deletion for message {message_id} in {chat_id} after {delay} seconds")

# ========== BOT-TOKEN MEDIA VALIDATION HELPERS ==========
# Telegram file_id values are BOT-SPECIFIC. Any media (start message, demos,
# screenshots) uploaded to the OLD bot becomes INVALID after the token is
# replaced with a new bot. These helpers detect that specific error and fall
# back gracefully instead of crashing the handler / leaving the user stuck.

def _is_invalid_media_error(err):
    """Return True if the Telegram error is 'wrong file identifier/HTTP URL specified' (400)."""
    try:
        desc = str(getattr(err, 'description', '') or err).lower()
        code = getattr(err, 'error_code', None)
        if 'wrong file identifier' in desc or 'wrong remote file identifier' in desc:
            return True
        if 'file identifier' in desc and 'http url' in desc:
            return True
        if code == 400 and 'file identifier' in desc:
            return True
    except Exception:
        pass
    return False

_admin_invalid_media_warned = set()

def _warn_admin_invalid_media(where):
    """One-time admin alert telling them to re-upload media with the NEW bot."""
    key = str(where)
    if key in _admin_invalid_media_warned:
        return
    _admin_invalid_media_warned.add(key)
    logging.error(
        f"Invalid media file_id detected in '{where}'. Telegram file_ids are bot-specific: "
        f"the OLD bot's file_ids do NOT work with the NEW bot token. Re-upload this media with the NEW bot."
    )
    msg = (
        f"⚠️ <b>Media re-upload zaroori hai!</b>\n\n"
        f"Jagah: <code>{where}</code>\n\n"
        f"Telegram <code>file_id</code> har bot ke liye alag hota hai. "
        f"Purane bot ke <code>file_id</code> naye bot ke saath kaam nahi karte.\n\n"
        f"✅ <b>Fix (naye bot se media bhejo, phir reset karo):</b>\n"
        f"• Custom start media → <code>/setstartmsg</code>\n"
        f"• Start demos → <code>/clear_start_demos</code> phir <code>/set_start_demos</code>\n"
        f"• Plan demos → <code>/clear_plan_demos &lt;plan&gt;</code> phir <code>/set_plan_demos</code>"
    )
    for aid in settings.get('admin_ids', []):
        try:
            bot.send_message(aid, msg, parse_mode="HTML")
        except Exception:
            pass

# ========== MEDIA CACHE HELPERS (auto re-upload on token change) ==========
# Telegram file_id is bot-specific. When the bot receives media from the admin we
# also download + store the actual bytes (config.media_cache_*). If a stored
# file_id ever becomes invalid (new token), we re-upload the cached bytes with the
# CURRENT bot to obtain a fresh file_id — no manual re-upload needed.
#
# Guard: if an OLDER config.py is deployed by mistake, the media_* helpers won't
# exist. Detect that ONCE so a partial deploy degrades gracefully (with a loud
# warning) instead of crashing the whole bot at startup.
_MEDIA_API_READY = all(hasattr(config, _n) for _n in (
    'media_new_id', 'media_cache_save', 'media_cache_get_bytes',
    'media_cache_get_info', 'media_cache_delete', 'media_cache_count',
    'MEDIA_MAX_BYTES'))
if not _MEDIA_API_READY:
    logging.error("config.py is OUTDATED — media auto-fetch helpers are MISSING. "
                  "Deploy the full updated config.py, then restart. "
                  "Bot will keep running but media auto-fetch is DISABLED.")

def _media_cache_delete_safe(media_id):
    """Delete a cached blob only if the media API is available. Never raises."""
    if _MEDIA_API_READY and media_id:
        try:
            config.media_cache_delete(media_id)
        except Exception:
            pass

def _message_media_file_id(msg):
    """Extract (file_id, type) from a telebot Message, or (None, None)."""
    try:
        if getattr(msg, 'photo', None):
            return msg.photo[-1].file_id, 'photo'
        if getattr(msg, 'video', None):
            return msg.video.file_id, 'video'
        if getattr(msg, 'document', None):
            return msg.document.file_id, 'document'
    except Exception:
        pass
    return None, None

def _cache_file_id(file_id, ftype, file_name=""):
    """Download a (currently valid) file_id with the CURRENT bot and store its bytes
    in the persistent cache. Returns media_id or None. Never raises."""
    if not _MEDIA_API_READY or not file_id:
        return None
    try:
        file_info = bot.get_file(file_id)
        if file_info.file_size and file_info.file_size > config.MEDIA_MAX_BYTES:
            logging.warning(f"Media too large to cache ({file_info.file_size} bytes) — file_id-only mode.")
            return None
        downloaded = bot.download_file(file_info.file_path)
        if not downloaded:
            return None
        if not file_name:
            file_name = os.path.basename(file_info.file_path or "") or f"{ftype or 'file'}.bin"
        mid = config.media_new_id()
        if config.media_cache_save(mid, downloaded, ftype, file_name=file_name) is None:
            return None
        return mid
    except Exception as e:
        logging.warning(f"_cache_file_id failed: {type(e).__name__}")
        return None

def _cache_replied_media(replied_msg):
    """Download media attached to `replied_msg` with the CURRENT bot and store its
    bytes in the persistent cache. Returns (media_id, file_type) or (None, ftype).
    Best-effort — never raises."""
    if not _MEDIA_API_READY:
        return None, None
    try:
        file_id, ftype = _message_media_file_id(replied_msg)
        if not file_id:
            return None, None
        file_name = ""
        if getattr(replied_msg, 'document', None):
            file_name = replied_msg.document.file_name or ""
        mid = _cache_file_id(file_id, ftype, file_name)
        return mid, ftype
    except Exception as e:
        logging.warning(f"_cache_replied_media failed: {type(e).__name__}")
        return None, None

def _bio_from_cached(media_id, file_type):
    """Build a named BytesIO from a cached media blob, or None if not cached."""
    if not _MEDIA_API_READY or not media_id:
        return None
    data = config.media_cache_get_bytes(media_id)
    if not data:
        return None
    bio = BytesIO(data)
    info = config.media_cache_get_info(media_id) or {}
    name = info.get('filename') or ''
    if not name:
        name = f"media.{'jpg' if file_type == 'photo' else ('mp4' if file_type == 'video' else 'bin')}"
    bio.name = name
    return bio

def _input_media_for_item(item):
    """Build an InputMedia for a demo item. Prefers the stored file_id, but falls
    back to the cached bytes in MongoDB (auto-fetch) so file_id is NEVER required.
    Returns an InputMedia object, or None if nothing usable is available."""
    if not isinstance(item, dict):
        return None
    ftype = item.get('type', 'video')
    if ftype not in ('photo', 'video', 'document'):
        return None
    src = item.get('id')
    if not src:
        src = _bio_from_cached(item.get('media_id'), ftype)
        if src is None:
            return None  # not in Mongo cache either → nothing we can send
    if ftype == 'photo':
        return types.InputMediaPhoto(src, parse_mode="HTML")
    if ftype == 'video':
        return types.InputMediaVideo(src, parse_mode="HTML")
    return types.InputMediaDocument(src, parse_mode="HTML")

def _reupload_cached_media(chat_id, media_id, file_type, caption="", reply_markup=None):
    """Upload cached bytes with the CURRENT bot → (new_file_id, message) or (None, None)."""
    bio = _bio_from_cached(media_id, file_type)
    if bio is None:
        return None, None
    try:
        if file_type == 'photo':
            m = bot.send_photo(chat_id, photo=bio, caption=caption or "", reply_markup=reply_markup, parse_mode="HTML")
        elif file_type == 'video':
            m = bot.send_video(chat_id, video=bio, caption=caption or "", reply_markup=reply_markup, parse_mode="HTML")
        else:
            m = bot.send_document(chat_id, document=bio, caption=caption or "", reply_markup=reply_markup, parse_mode="HTML")
        nfid, _ = _message_media_file_id(m)
        return nfid, m
    except Exception as e:
        logging.error(f"_reupload_cached_media failed: {type(e).__name__}: {e}")
        return None, None

def _heal_start_message_media(chat_id, media_type, text, message):
    """Re-upload the cached custom-start media to fix an invalid file_id after a
    token change. Returns True if the media was successfully re-sent."""
    mid = start_message_data.get('media_id')
    if not mid:
        _warn_admin_invalid_media('start message media')
        return False
    nfid, m = _reupload_cached_media(chat_id, mid, media_type, caption=text,
                                     reply_markup=verif.main_menu_keyboard())
    if m is None:
        _warn_admin_invalid_media('start message media')
        return False
    if nfid:
        start_message_data['file_id'] = nfid
    save_start_message()
    track_msg(chat_id, m.message_id)
    logging.info("Start-message media healed from cache after token change.")
    return True

def _persist_fresh_file_ids(msgs, items):
    """After a successful media-group send, capture the fresh file_ids Telegram
    assigned and store them back into the settings items (auto file_id cache).
    file_id is OPTIONAL — this only makes future sends faster. Never raises."""
    try:
        changed = False
        for m, it in zip(msgs, items or []):
            if not isinstance(it, dict):
                continue
            nfid, _ = _message_media_file_id(m)
            if nfid and it.get('id') != nfid:
                it['id'] = nfid  # same dict object lives inside settings → persisted
                changed = True
        if changed:
            save_settings()
    except Exception as e:
        logging.warning(f"_persist_fresh_file_ids failed: {type(e).__name__}")

def _send_media_chunk(chat_id, chunk, items, chat_id_str):
    """Send one media-group chunk. If the stored file_ids are invalid (token
    change), rebuild the chunk from the persistent media cache, capture the fresh
    file_ids and persist them back into settings. Returns True on success."""
    if not chunk:
        return True
    try:
        msgs = bot.send_media_group(chat_id, chunk)
        _persist_fresh_file_ids(msgs, items)
        for m in msgs:
            user_demo_messages[chat_id_str].append(m.message_id)
            delete_message_after_delay(chat_id, m.message_id, delay=600)
        return True
    except Exception as e:
        logging.error(f"Error sending media group chunk: {e}")
        if not _is_invalid_media_error(e):
            if "can't use file of type" in str(e):
                for aid in settings.get('admin_ids', []):
                    try:
                        bot.send_message(aid, "⚠️ <b>Demo Media Error!</b>\n\nSome files have wrong types. Please clear and reset using reply method.", parse_mode="HTML")
                    except Exception:
                        pass
            return False
        # ---- Token-change heal: rebuild chunk from cached bytes ----
        healed_chunk, healed_items = [], []
        for it in items:
            if not isinstance(it, dict):
                continue
            ftype = it.get('type', 'video')
            bio = _bio_from_cached(it.get('media_id'), ftype)
            if bio is None:
                continue
            if ftype == 'photo':
                healed_chunk.append(types.InputMediaPhoto(bio, parse_mode="HTML"))
            elif ftype == 'video':
                healed_chunk.append(types.InputMediaVideo(bio, parse_mode="HTML"))
            else:
                healed_chunk.append(types.InputMediaDocument(bio, parse_mode="HTML"))
            healed_items.append(it)
        if not healed_chunk:
            _warn_admin_invalid_media('start/plan demo media')
            return False
        try:
            msgs = bot.send_media_group(chat_id, healed_chunk)
            for m in msgs:
                user_demo_messages[chat_id_str].append(m.message_id)
                delete_message_after_delay(chat_id, m.message_id, delay=600)
            _persist_fresh_file_ids(msgs, healed_items)
            logging.info("Demo media healed from cache after token change.")
            if len(healed_items) < len(items):
                _warn_admin_invalid_media('some demo media (not cached)')
            return True
        except Exception as e2:
            logging.error(f"Demo media heal failed: {e2}")
            _warn_admin_invalid_media('start/plan demo media')
            return False

def send_demo_videos(chat_id, video_list, caption=""):
    """Send demo items (media or text) and schedule deletion.
    Stored file_ids are auto-healed from the media cache on token change."""
    if not video_list:
        return

    chat_id_str = str(chat_id)
    if chat_id_str not in user_demo_messages:
        user_demo_messages[chat_id_str] = []

    current_media_group = []
    current_media_items = []

    def _flush(chunk, items):
        if not chunk:
            return
        for i in range(0, len(chunk), 10):
            _send_media_chunk(chat_id, chunk[i:i + 10], items[i:i + 10], chat_id_str)

    for item in video_list:
        try:
            # Check if it's a text item
            if isinstance(item, dict) and item.get('type') == 'text':
                _flush(current_media_group, current_media_items)
                current_media_group, current_media_items = [], []

                # Send the text message
                txt_msg = bot.send_message(chat_id, item.get('text', ''), parse_mode="HTML", disable_web_page_preview=False)
                user_demo_messages[chat_id_str].append(txt_msg.message_id)
                delete_message_after_delay(chat_id, txt_msg.message_id, delay=600)

            elif isinstance(item, dict):
                # file_id is optional — _input_media_for_item falls back to MongoDB cache
                media = _input_media_for_item(item)
                if media is None:
                    logging.warning("Demo media item has no file_id and no cached bytes — skipped.")
                    _warn_admin_invalid_media('demo media (no file_id / no cache)')
                    continue
                current_media_group.append(media)
                current_media_items.append(item)
            else:
                # Fallback for old string format
                current_media_group.append(types.InputMediaVideo(item, parse_mode="HTML"))
                current_media_items.append({"id": item, "type": "video"})
        except Exception as e:
            logging.error(f"Error preparing demo item: {e}")
            continue

    # Send any remaining media group
    _flush(current_media_group, current_media_items)

    # Send Description as a separate message
    if caption:
        try:
            desc_msg = bot.send_message(chat_id, caption, parse_mode="HTML")
            user_demo_messages[chat_id_str].append(desc_msg.message_id)
            delete_message_after_delay(chat_id, desc_msg.message_id, delay=600)
        except Exception as e:
            logging.error(f"Error sending separate demo description: {e}")

def initialize_spam_data():
    """Ensure all existing users have spam_data entries"""
    initialized = 0
    for user_id_str in users_data.keys():
        if user_id_str not in spam_data:
            spam_data[user_id_str] = {
                "requests": [],
                "warnings": 0,
                "blocked_until": 0,
                "block_level": 0,
                "ban_reason": "",
                "banned_by": 0
            }
            initialized += 1
    if initialized > 0:
        print(f"Initialized spam data for {initialized} users")

# ========== FORCE JOIN REQUEST HANDLER REMOVED ==========

# ============ SPAM PROTECTION FUNCTIONS ============
def update_user_activity(user_id):
    user_id_str = str(user_id)
    current_time = time.time()
    
    if user_id_str not in spam_data:
        spam_data[user_id_str] = {
            "requests": [],
            "warnings": 0,
            "blocked_until": 0,
            "block_level": 0,
            "ban_reason": "",
            "banned_by": 0
        }
    
    if "requests" not in spam_data[user_id_str]:
        spam_data[user_id_str]["requests"] = []
    
    spam_data[user_id_str]["requests"] = [
        ts for ts in spam_data[user_id_str]["requests"] 
        if current_time - ts < SPAM_TIME_WINDOW
    ]
    
    spam_data[user_id_str]["requests"].append(current_time)
    return len(spam_data[user_id_str]["requests"])

def check_user_blocked(user_id):
    user_id_str = str(user_id)
    
    if user_id_str not in spam_data:
        return False, None
    
    user_data = spam_data[user_id_str]
    
    if "blocked_until" not in user_data:
        user_data["blocked_until"] = 0
    
    current_time = time.time()
    
    if user_data["blocked_until"] > current_time:
        time_left = int(user_data["blocked_until"] - current_time)
        minutes = time_left // 60
        seconds = time_left % 60
        hours = minutes // 60
        minutes = minutes % 60
        
        warning_msg = f"⛔ <b>YOU ARE BLOCKED!</b>\n\n"
        
        if user_data.get("ban_reason"):
            warning_msg += f"<b>Reason:</b> {user_data['ban_reason']}\n"
        
        if hours > 0:
            warning_msg += f"⏳ Please wait <b>{hours} hours {minutes} minutes</b>\n\n"
        else:
            warning_msg += f"⏳ Please wait <b>{minutes}:{seconds:02d}</b>\n\n"
        
        return True, warning_msg
    
    return False, None

def check_spam(user_id):
    user_id_str = str(user_id)
    
    is_blocked, block_msg = check_user_blocked(user_id)
    if is_blocked:
        return block_msg
    
    current_time = time.time()
    request_count = update_user_activity(user_id)
    
    if "warnings" not in spam_data[user_id_str]:
        spam_data[user_id_str]["warnings"] = 0
    if "block_level" not in spam_data[user_id_str]:
        spam_data[user_id_str]["block_level"] = 0
    if "blocked_until" not in spam_data[user_id_str]:
        spam_data[user_id_str]["blocked_until"] = 0
    
    if request_count >= MAX_SPAM_COUNT:
        user_data = spam_data[user_id_str]
        user_data["block_level"] = min(2, user_data.get("block_level", 0) + 1)
        block_duration = BLOCK_DURATIONS[user_data["block_level"]]
        user_data["blocked_until"] = current_time + block_duration
        user_data["requests"] = []
        user_data["warnings"] = 0
        
        # Notify admin
        try:
            admin_msg = f"""
🚨 <b>USER BLOCKED FOR SPAM</b>

👤 User ID: <code>{user_id}</code>
📛 Block Level: {user_data['block_level'] + 1}
⏰ Duration: {block_duration//60} minutes
🔢 Spam Count: {request_count}
            """
            for aid in settings.get('admin_ids', []):
                try:
                    bot.send_message(aid, admin_msg, parse_mode="HTML")
                except:
                    continue
        except:
            pass
        
        minutes = block_duration // 60
        seconds = block_duration % 60
        
        return f"⛔ <b>BLOCKED FOR SPAM!</b>\n\n⏳ Wait {minutes}:{seconds:02d}"
    
    if request_count >= 3:
        warning_level = min(2, request_count - 3)
        if spam_data[user_id_str].get("warnings", 0) < warning_level + 1:
            spam_data[user_id_str]["warnings"] = warning_level + 1
            warning_msg = f"{WARNING_MESSAGES[warning_level]}\n\n⚠️ {MAX_SPAM_COUNT - request_count} attempts left!"
            try:
                bot.send_message(user_id, warning_msg, parse_mode="HTML")
            except:
                pass
    
    return None

def reset_spam_counter(user_id):
    user_id_str = str(user_id)
    if user_id_str in spam_data:
        if spam_data[user_id_str].get("blocked_until", 0) < time.time():
            spam_data[user_id_str]["requests"] = []
            spam_data[user_id_str]["warnings"] = 0

def ban_user(user_id, duration_seconds, reason="", banned_by=None):
    if banned_by is None:
        banned_by = settings.get('admin_ids', [None])[0]
    user_id_str = str(user_id)
    current_time = time.time()
    
    if user_id_str not in spam_data:
        spam_data[user_id_str] = {
            "requests": [],
            "warnings": 0,
            "blocked_until": 0,
            "block_level": 0,
            "ban_reason": reason,
            "banned_by": banned_by
        }
    
    spam_data[user_id_str]["blocked_until"] = current_time + duration_seconds
    spam_data[user_id_str]["ban_reason"] = reason
    spam_data[user_id_str]["banned_by"] = banned_by
    spam_data[user_id_str]["block_level"] = 3
    
    try:
        if duration_seconds >= 3600:
            time_display = f"{int(duration_seconds/3600)} hours"
        elif duration_seconds >= 60:
            time_display = f"{int(duration_seconds/60)} minutes"
        else:
            time_display = f"{duration_seconds} seconds"
        
        bot.send_message(
            int(user_id),
            f"⛔ <b>BANNED</b>\n\nDuration: {time_display}\nReason: {reason}",
            parse_mode="HTML"
        )
    except:
        pass
    
    return True

# ============ PREMIUM BOT CLASS ============
class PremiumBot:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def generate_qr_code(self, upi_id, amount, name):
        try:
            # Accept '100', '₹100', '100rs' etc. — reject anything non-numeric safely
            amt = config.parse_amount(amount)
            if amt is None:
                logging.error(
                    "QR Generation Error: invalid amount %r — plan amount must be a "
                    "plain number like 100 or 99.50 (no 'rs'/'₹').",
                    amount
                )
                return None
            # Format amount to 2 decimal places for UPI standard
            formatted_amount = "{:.2f}".format(amt)
            upi_url = f"upi://pay?pa={upi_id}&pn={name.replace(' ', '%20')}&am={formatted_amount}&cu=INR"
            
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(upi_url)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            img_bytes = BytesIO()
            img.save(img_bytes, format='PNG')
            img_bytes.seek(0)
            return img_bytes
        except Exception as e:
            logging.error(f"QR Generation Error: {e}")
            return None

premium_bot = PremiumBot()

# ========== PAY2 (AUTO-PAY) HELPERS ==========
def pay2_enabled():
    """Auto-pay tabhi chale jab module loaded ho, mode ON ho aur account ho."""
    return (PAY2_AVAILABLE
            and settings.get('payment_gateway') == 'pay2'
            and bool(settings.get('pay2_accounts')))


def sync_pay2_accounts():
    """settings ke pay2 accounts ko pay2 module me daalo (startup + har change par).
    Active account count return karta hai."""
    if not PAY2_AVAILABLE:
        return 0
    try:
        return pay2.configure_accounts(settings.get('pay2_accounts', []) or [])
    except Exception as e:
        logging.error(f"pay2 configure_accounts failed: {type(e).__name__}")
        return 0


def _mask_secret(value, keep=4):
    """Password jaisa secret log/message me dikhane se pehle mask karo."""
    s = str(value or "")
    if len(s) <= keep:
        return "•" * len(s)
    return "•" * (len(s) - keep) + s[-keep:]


def _mask_proxy(url):
    """Proxy URL ke credentials mask karo (password kabhi pura na dikhe)."""
    s = str(url or "")
    if "://" not in s:
        return _mask_secret(s) if s else s
    scheme, rest = s.split("://", 1)
    if "@" in rest:
        cred, host = rest.split("@", 1)
        if ":" in cred:
            user, pw = cred.split(":", 1)
            cred = f"{user}:{_mask_secret(pw)}"
        else:
            cred = _mask_secret(cred)
        return f"{scheme}://{cred}@{host}"
    return s


def sync_pay2_proxy():
    """settings ka proxy override pay2 module me lagao. Effective URL return karta hai.

    "" -> env/default; "off" -> disable; warna pura proxy URL.
    """
    if not PAY2_AVAILABLE:
        return ""
    val = str(settings.get('pay2_proxy', '') or "").strip()
    try:
        return pay2.set_proxy(val if val else None)
    except Exception as e:
        logging.error(f"pay2 set_proxy failed: {type(e).__name__}")
        return ""


def pay2_proxy_label():
    """Panel me dikhane ke liye proxy ki short status line."""
    if not PAY2_AVAILABLE:
        return "❌ module load nahi hua"
    val = str(settings.get('pay2_proxy', '') or "").strip().lower()
    if val in ("off", "none", "no", "false", "0"):
        return "🚫 Disabled"
    try:
        eff, source = pay2.proxy_status()
    except Exception:
        eff, source = "", "off"
    if not eff:
        return "🚫 Disabled"
    tag = {"bot": "custom", "env": "env", "default": "default"}.get(source, source)
    return f"✅ {_mask_proxy(eff)}  <i>({tag})</i>"


def _normalize_proxy(raw):
    """Admin ke input ko proxy URL me normalize karo. "" / "off" bhi handle."""
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in ("off", "none", "disable", "disabled", "no"):
        return "off"
    if "://" in s:
        return s
    if "@" in s:
        return "http://" + s
    parts = s.split(":")
    if len(parts) == 4:  # host:port:user:pass
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    if len(parts) == 2:  # host:port
        return "http://" + s
    return s


def pay2_proxy_menu_text():
    return (f"<b>🌐 AUTO-PAY PROXY</b>\n\n"
            f"<b>Current:</b> {pay2_proxy_label()}\n\n"
            f"Gateway (In666/CQPAY) ke saare calls isi proxy se jaate hain. "
            f"India residential proxy recommended hai.\n\n"
            f"• <b>Set/Change</b> — apna proxy URL dein (custom)\n"
            f"• <b>Reset</b> — default/env par wapas\n"
            f"• <b>Disable</b> — bina proxy direct connection")


def pay2_proxy_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✏️ Set / Change Proxy", callback_data="pay_proxy_set", color="primary"))
    kb.add(types.InlineKeyboardButton("♻️ Reset to Default", callback_data="pay_proxy_reset", color="primary"))
    kb.add(types.InlineKeyboardButton("🚫 Disable Proxy", callback_data="pay_proxy_disable", color="negative"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_pay"))
    return kb


def _safe_edit_text(chat_id, message_id, text, reply_markup=None):
    """edit_message_text safely — 'message is not modified' (same content) par crash na ho."""
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                              reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        pass


def pay2_proxy_ask_url(message):
    """Admin se proxy URL lo, normalize karke save + sync karo (creds message delete)."""
    if not is_admin(message.from_user.id):
        return
    chat_id = message.chat.id
    raw = (message.text or "").strip()
    # Security: proxy credentials wala message turant delete karo
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    val = _normalize_proxy(raw)
    if not val:
        m = bot.send_message(chat_id, "❌ Proxy URL nahi mila. Panel se dobara try karein.", parse_mode="HTML")
        track_msg(chat_id, m.message_id)
        return

    settings['pay2_proxy'] = val
    save_settings()
    effective = sync_pay2_proxy()

    note = ("🚫 Proxy disable kar diya." if val == "off"
            else f"✅ Proxy set ho gaya:\n<code>{_mask_proxy(effective)}</code>")
    m = bot.send_message(chat_id, f"{note}\n\n{pay2_proxy_menu_text()}",
                         reply_markup=pay2_proxy_keyboard(), parse_mode="HTML")
    track_msg(chat_id, m.message_id)


def notify_pay2_error(context, error):
    """Auto-pay fail hone par admins ko chhota alert bhejo (secrets ke bina)."""
    try:
        msg = (f"⚠️ <b>AUTO-PAY ERROR</b>\n\nContext: {context}\n"
               f"Reason: <code>{type(error).__name__}</code>")
        for aid in settings.get('admin_ids', []):
            try:
                bot.send_message(aid, msg, parse_mode="HTML")
            except Exception:
                continue
    except Exception:
        pass


def pay2_utr_prompt(pv):
    """UTR maangne wala prompt text (order details ke saath)."""
    order_num = (pv or {}).get('order_number', 'N/A')
    amount = (pv or {}).get('amount', '')
    return (
        f"⏳ <b>PAYMENT ABHI RECEIVE NAHI HUI (Order #{order_num})</b>\n\n"
        f"Agar aapne <b>₹{amount}</b> pay kar diya hai to apna "
        f"<b>UTR / Reference Number</b> (12-digit) yaha bhejein — "
        f"main third-party site par verify kar dunga.\n\n"
        f"📸 <i>Ya payment ka screenshot PHOTO ke roop me bhejein (manual verification).</i>"
    )


def pay2_verify_success(chat_id, user_id, source):
    """Auto-verify hui order ko wahi path se complete karo jo admin verify use karta hai
    (join link + sale record + premium flag)."""
    try:
        ok, _ = verif.verify_payment(str(user_id), "AUTO-PAY")
    except Exception as e:
        logging.error(f"pay2 auto-verify failed: {type(e).__name__}")
        ok = False
    if ok:
        try:
            m = bot.send_message(
                chat_id,
                f"🎉 <b>PAYMENT AUTO-VERIFIED!</b>\n\n{source}\n"
                f"Aapka order confirm ho gaya — join link upar bhej diya gaya hai.",
                parse_mode="HTML"
            )
            track_msg(chat_id, m.message_id)
        except Exception:
            pass
    return ok


def pay2_auto_verify(chat_id, user_id):
    """Payment Done par: pehle get_info se status check -> paid ho to verify,
    warna user se UTR maango. Background thread se chalta hai."""
    pv = pending_verifications.get(str(user_id))
    if not pv:
        return

    token = pv.get('pay2_token')
    plan_type = pv.get('plan')

    # pay2 token nahi (manual flow) -> purana screenshot flow
    if not token:
        screenshot_msg = verif.ask_for_screenshot(chat_id, user_id, plan_type)
        if screenshot_msg:
            track_msg(chat_id, screenshot_msg.message_id)
        return

    note = None
    try:
        note = bot.send_message(chat_id, "🔎 <b>Payment check kar raha hoon...</b>", parse_mode="HTML")
        track_msg(chat_id, note.message_id)
    except Exception:
        note = None

    paid = False
    try:
        paid = pay2.is_paid(pay2.check_payment(token))
    except Exception as e:
        logging.error(f"pay2 check_payment error: {type(e).__name__}")

    if paid:
        if note:
            try:
                bot.delete_message(chat_id, note.message_id)
            except Exception:
                pass
            if note.message_id in user_all_messages.get(str(chat_id), []):
                user_all_messages[str(chat_id)].remove(note.message_id)
        pay2_verify_success(chat_id, user_id, "Gateway ne payment confirm kar di.")
        return

    # Payment abhi confirm nahi -> UTR maango
    text = pay2_utr_prompt(pv)
    prompt_msg = None
    if note:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=note.message_id, parse_mode="HTML")
            prompt_msg = note
        except Exception:
            prompt_msg = None
    if prompt_msg is None:
        try:
            prompt_msg = bot.send_message(chat_id, text, parse_mode="HTML")
            track_msg(chat_id, prompt_msg.message_id)
        except Exception:
            return
    bot.register_next_step_handler(prompt_msg, pay2_handle_utr)


def pay2_handle_utr(message):
    """User ka UTR lo -> refshjsas se verify -> status re-check -> verify ya screenshot fallback."""
    user_id = message.from_user.id
    chat_id = message.chat.id
    pv = pending_verifications.get(str(user_id))
    if not pv or not pv.get('pay2_token'):
        return

    # User ne UTR ki jagah screenshot bhej diya -> manual verification
    if message.photo:
        verif.handle_screenshot(message)
        return

    utr = (message.text or "").strip()
    if not utr or utr.startswith('/'):
        m = bot.send_message(
            chat_id,
            "❌ UTR number sahi se bhejein (jaise <code>100121212121</code>).",
            parse_mode="HTML"
        )
        track_msg(chat_id, m.message_id)
        bot.register_next_step_handler(m, pay2_handle_utr)
        return

    token = pv['pay2_token']
    paid = False
    try:
        pay2.submit_utr(token, utr)
        paid = pay2.is_paid(pay2.check_payment(token))
    except Exception as e:
        logging.error(f"pay2 submit_utr error: {type(e).__name__}")

    if paid:
        pay2_verify_success(chat_id, user_id, f"UTR <code>{utr}</code> verify ho gaya.")
        return

    # Fallback: manual screenshot verification (admin)
    m = bot.send_message(
        chat_id,
        "⚠️ <b>Auto-verify nahi ho paya.</b>\n\n"
        "Kripya payment ka <b>screenshot PHOTO</b> ke roop me bhejein — admin manually verify karega.",
        parse_mode="HTML"
    )
    track_msg(chat_id, m.message_id)
    screenshot_msg = verif.ask_for_screenshot(chat_id, user_id, pv.get('plan'))
    if screenshot_msg:
        track_msg(chat_id, screenshot_msg.message_id)


def pay2_show_balance(chat_id, msg_id=None):
    """Admin ke liye har account ka latest balance dikhao (background thread)."""
    if not PAY2_AVAILABLE:
        text = "❌ pay2 module load nahi hua (pycryptodome install karein)."
    else:
        rows = []
        try:
            rows = pay2.get_balances(fresh=True)
        except Exception as e:
            logging.error(f"pay2 get_balances error: {type(e).__name__}")
        if not rows:
            text = ("<b>💰 AUTO-PAY BALANCES</b>\n\n"
                    "Koi account configured nahi hai ya fetch fail ho gaya.")
        else:
            lines = []
            for r in rows:
                phone = r.get('phone', '?')
                bal = r.get('balance')
                if bal is None:
                    bal_txt = f"⚠️ fail ({r.get('error', 'unknown')})"
                else:
                    bal_txt = f"₹{bal:,.2f}"
                lines.append(f"• <code>{phone}</code> — <b>{bal_txt}</b>")
            text = "<b>💰 AUTO-PAY BALANCES</b>\n\n" + "\n".join(lines)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 Refresh", callback_data="pay_balance", color="primary"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_pay", color="primary"))
    try:
        if msg_id:
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                  reply_markup=kb, parse_mode="HTML")
        else:
            bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


if PAY2_AVAILABLE:
    sync_pay2_proxy()
    _pay2_active = sync_pay2_accounts()
    if _pay2_active:
        print(f"✅ pay2 (Auto-Pay): {_pay2_active} account(s) configured.")
    elif settings.get('payment_gateway') == 'pay2':
        print("⚠️ Auto-Pay ON hai par koi pay2 account configured nahi — manual UPI/QR use hoga.")

# ========== IMPORTANT LOGS ==========
def log_important_event(event_type, user_data=None, plan=None):
    try:
        if not settings.get('log_channel'):
            return
            
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if event_type == "new_user":
            log_msg = f"""
🆕 <b>NEW USER</b>
👤 Name: {user_data.get('first_name', 'N/A')}
👤 User: @{user_data.get('username' , 'N/A')}
🆔 ID: <code>{user_data.get('id', 'N/A')}</code>
⏰ Time: {timestamp}
📊 Total Users: {len(users_data)}
            """
        elif event_type == "payment_initiated":
            log_msg = f"""
💰 <b>PAYMENT INITIATED</b>
👤 Name: {user_data.get('first_name', 'N/A')}
👤 User: @{user_data.get('username', 'N/A')}
🆔 ID: <code>{user_data.get('id', 'N/A')}</code>
📅 Plan: {plan}
⏰ Time: {timestamp}
            """
        else:
            return
        
        target_chat = settings.get('log_channel')
        if not target_chat:
            target_chat = settings.get('admin_ids', [None])[0]
            
        if target_chat:
            bot.send_message(target_chat, log_msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Log error: {e}")

# ========== /START COMMAND ==========
@bot.message_handler(commands=['start'])
def handle_start(message):
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id

        spam_result = check_spam(user_id)
        if spam_result:
            m = bot.send_message(chat_id, spam_result, parse_mode="HTML")
            track_msg(chat_id, m.message_id)
            return

        clear_all_user_msgs(chat_id)

        start_demos = settings.get('start_demo_videos', [])
        if start_demos:
            start_desc = settings.get('start_demo_desc', "")
            send_demo_videos(chat_id, start_demos, caption=start_desc)

        is_new_user = str(user_id) not in users_data

        if is_new_user:
            users_data[str(user_id)] = {
                'id': user_id,
                'username': message.from_user.username,
                'first_name': message.from_user.first_name,
                'last_name': message.from_user.last_name or "",
                'start_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'is_premium': False
            }
            log_important_event("new_user", users_data[str(user_id)])

        reset_spam_counter(user_id)

        if start_message_data and 'has_media' in start_message_data:
            text = start_message_data.get('text', "")

            if start_message_data['has_media']:
                media_type = start_message_data.get('media_type', '')
                file_id = start_message_data.get('file_id', '')

                def _start_text_fallback():
                    """User ko menu zaroor mile — chahe media fail ho jaye."""
                    if text and text.strip():
                        try:
                            fm = bot.send_message(
                                chat_id,
                                text,
                                reply_markup=verif.main_menu_keyboard(),
                                parse_mode="HTML"
                            )
                            track_msg(chat_id, fm.message_id)
                            return
                        except Exception:
                            pass
                    send_default_start(message)

                if media_type in ('photo', 'video'):
                    if not file_id:
                        # file_id is OPTIONAL — resolve the media from the MongoDB cache
                        if not _heal_start_message_media(chat_id, media_type, text, message):
                            _start_text_fallback()
                    else:
                        try:
                            if media_type == 'photo':
                                m = bot.send_photo(
                                    chat_id,
                                    photo=file_id,
                                    caption=text,
                                    reply_markup=verif.main_menu_keyboard(),
                                    parse_mode="HTML"
                                )
                            else:
                                m = bot.send_video(
                                    chat_id,
                                    video=file_id,
                                    caption=text,
                                    reply_markup=verif.main_menu_keyboard(),
                                    parse_mode="HTML"
                                )
                            track_msg(chat_id, m.message_id)
                        except Exception as _me:
                            if _is_invalid_media_error(_me):
                                if not _heal_start_message_media(chat_id, media_type, text, message):
                                    _start_text_fallback()
                            else:
                                raise
                else:
                    send_default_start(message)
            else:
                m = bot.send_message(
                    chat_id,
                    text,
                    reply_markup=verif.main_menu_keyboard(),
                    parse_mode="HTML"
                )
                track_msg(chat_id, m.message_id)
        else:
            send_default_start(message)

    except Exception as e:
        logging.error(f"Start error: {e}")

def send_default_start(message):
    welcome_text = f"""
🔥 <b>PREMIUM CONTENT</b> 🔥

Welcome to the Premium Bot! Access high-quality exclusive content.

👇 <b>Select an option:</b>
    """

    m = bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=verif.main_menu_keyboard(),
        parse_mode="HTML"
    )
    track_msg(message.chat.id, m.message_id)

# ========== CHECK JOINED CALLBACK REMOVED ==========

# ========== GET MEMBERSHIP CALLBACK REMOVED ==========

@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def handle_main_menu_callback(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    clear_all_user_msgs(chat_id)

    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass

    start_demos = settings.get('start_demo_videos', [])
    if start_demos:
        start_desc = settings.get('start_demo_desc', "")
        send_demo_videos(chat_id, start_demos, caption=start_desc)

    welcome_text = f"""
🔥 <b>PREMIUM CONTENT</b> 🔥

Welcome to the Premium Bot! Access high-quality exclusive content.

👇 <b>Select an option:</b>
    """
    m = bot.send_message(
        chat_id,
        welcome_text,
        reply_markup=verif.main_menu_keyboard(),
        parse_mode="HTML"
    )
    track_msg(chat_id, m.message_id)
    bot.answer_callback_query(call.id)

# ========== PLAN SELECTION ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith('plan_'))
def handle_plan_selection(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    spam_result = check_spam(user_id)
    if spam_result:
        m = bot.send_message(chat_id, spam_result, parse_mode="HTML")
        track_msg(chat_id, m.message_id)
        bot.answer_callback_query(call.id)
        return

    reset_spam_counter(user_id)

    clear_user_demos(chat_id)

    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass
    if msg_id in user_all_messages.get(str(chat_id), []):
        user_all_messages[str(chat_id)].remove(msg_id)

    plan_type = call.data.split('_')[1]

    plan = None
    if plan_type in config.PLANS:
        plan = config.PLANS[plan_type]
    else:
        channels = settings.get("premium_channels", [])
        plan = next((ch for ch in channels if ch['id'] == plan_type), None)

    if not plan:
        bot.answer_callback_query(call.id, "❌ Plan not found!", show_alert=True)
        return

    plan_demos = settings.get('plan_demo_videos', {}).get(plan_type, [])
    if plan_demos:
        plan_desc = settings.get('plan_demo_descs', {}).get(plan_type, "")
        send_demo_videos(chat_id, plan_demos, caption=plan_desc)

    custom_desc = plan.get('description', '') or ""
    if custom_desc:
        features_text = custom_desc
    else:
        features_text = """✅ High Quality Content
✅ Direct Access After Payment
✅ 24/7 Support Available"""

    desc_text = f"""
<b>💎 {plan['name'].upper()} DESCRIPTION:</b>

💰 <b>Amount:</b> ₹{plan['amount']}
⏳ <b>Duration:</b> {plan.get('duration', 'N/A')}

<b>Features:</b>
{features_text}

<i>Click "💳 Buy Now" below to get the payment QR code.</i>
    """

    keyboard = types.InlineKeyboardMarkup(row_width=2)
    btn_buy = make_button("💳 Buy Now", callback_data=f"buy_{plan_type}")
    btn_back = make_button("🔙 Back", callback_data="back_to_main")
    keyboard.add(btn_buy, btn_back)

    m = bot.send_message(
        chat_id,
        desc_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    track_msg(chat_id, m.message_id)

    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('buy_'))
def handle_buy_now(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    plan_type = call.data.split('_')[1]

    if str(user_id) in pending_verifications:
        pending_data = pending_verifications[str(user_id)]
        order_num = pending_data.get('order_number', 'N/A')
        screenshot_uploaded = pending_data.get('screenshot_file_id', False)

        if screenshot_uploaded:
            msg = f"""
⛔ <b>PAYMENT PENDING!</b>

Aapka Order #{order_num} pehle se hi pending hai.
Screenshot bhi upload ho chuka hai.
Jab tak admin purane payment ko verify/reject nahi kar dete, aap naya payment create nahi kar sakte.

⏳ <i>Please wait for admin verification...</i>
            """
            try:
                bot.delete_message(chat_id, msg_id)
            except:
                pass
            if msg_id in user_all_messages.get(str(chat_id), []):
                user_all_messages[str(chat_id)].remove(msg_id)

            m = bot.send_message(chat_id, msg, parse_mode="HTML")
            track_msg(chat_id, m.message_id)
            bot.answer_callback_query(call.id)
            return
        else:
            del pending_verifications[str(user_id)]
            bot.answer_callback_query(
                call.id,
                "✅ Purani pending payment cancel ho gayi. Naya QR generate ho raha hai...",
                show_alert=False
            )

    plan = None
    if plan_type in config.PLANS:
        plan = config.PLANS[plan_type]
    else:
        channels = settings.get("premium_channels", [])
        plan = next((ch for ch in channels if ch['id'] == plan_type), None)

    if not plan:
        bot.answer_callback_query(call.id, "❌ Plan not found!", show_alert=True)
        return

    # Auto-Pay mode ON hai to gateway se payment link; warna manual UPI/QR.
    use_pay2 = pay2_enabled()

    qr_image = None
    if not use_pay2:
        # Generate QR FIRST — fail fast (and cleanly) if this plan's amount is invalid,
        # instead of raising an error later and leaving a broken order.
        qr_image = premium_bot.generate_qr_code(settings['upi_id'], plan['amount'], settings['upi_name'])
        if qr_image is None:
            logging.error(f"Order blocked: invalid amount for plan {plan_type}: {plan['amount']!r}")
            bot.answer_callback_query(
                call.id,
                "⚠️ Payment QR is unavailable for this plan.\n"
                "Admin: plan ka amount sahi karo (sirf number, jaise 100).",
                show_alert=True
            )
            return

    settings['total_orders'] = settings.get('total_orders', 0) + 1
    order_num = settings['total_orders']
    save_settings()

    pending_verifications[str(user_id)] = {
        'plan': plan_type,
        'amount': plan['amount'],
        'order_number': order_num,
        'initiated_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'username': call.from_user.username,
        'first_name': call.from_user.first_name
    }

    try:
        bot.delete_message(chat_id, msg_id)
    except: pass
    if msg_id in user_all_messages.get(str(chat_id), []):
        user_all_messages[str(chat_id)].remove(msg_id)

    if use_pay2:
        # Link banane me kuch second lag sakte hain — callback turant answer karo
        # aur kaam background thread me karo (UI block na ho).
        bot.answer_callback_query(call.id, "⏳ Secure payment link ban raha hai...")
        threading.Thread(
            target=send_pay2_payment,
            args=(chat_id, user_id, plan_type, plan['amount'], plan['name'], order_num),
            daemon=True
        ).start()
        return

    caption = f"""
<b>💰 ORDER #{order_num}: PAY ₹{plan['amount']} FOR {plan['name'].upper()}</b>

<b>UPI Details:</b>
└ ID: <code>{settings['upi_id']}</code>
└ Name: {settings['upi_name']}
└ Amount: <b>₹{plan['amount']}</b>

<b>Instructions:</b>
1. Scan QR with any UPI app
2. Pay ₹{plan['amount']}
3. Click "✅ Payment Done" below

⚠️ <b>IMPORTANT NOTE:</b> ❌ <b>DO NOT USE GPAY</b> for payment.
Please use any other UPI app (PhonePe, Paytm, BHIM, etc.) for payment.

⏳ <i>This QR will auto-delete in 10 minutes.</i>
    """

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    btn1 = make_button("✅ Payment Done", callback_data="payment_done")
    btn2 = make_button("🔙 Back", callback_data=f"plan_{plan_type}")
    keyboard.add(btn1, btn2)

    if qr_image:
        sent_msg = bot.send_photo(chat_id, photo=qr_image, caption=caption, reply_markup=keyboard, parse_mode="HTML")
        track_msg(chat_id, sent_msg.message_id)
        delete_message_after_delay(chat_id, sent_msg.message_id, 600, send_timeout_msg=True)
    else:
        sent_msg = bot.send_message(chat_id, caption, reply_markup=keyboard, parse_mode="HTML")
        track_msg(chat_id, sent_msg.message_id)
        delete_message_after_delay(chat_id, sent_msg.message_id, 600, send_timeout_msg=True)

    bot.answer_callback_query(call.id)

def send_pay2_payment(chat_id, user_id, plan_type, amount, plan_name, order_num):
    """Auto-Pay: pay2.py se payment link banao aur user ko button ke saath bhejo.
    Background thread se chalta hai (network call slow ho sakti hai)."""
    placeholder = None
    try:
        placeholder = bot.send_message(
            chat_id,
            "⏳ <b>Secure payment link ban raha hai...</b>\n<i>Please wait thodi der...</i>",
            parse_mode="HTML"
        )
        track_msg(chat_id, placeholder.message_id)
    except Exception:
        placeholder = None

    try:
        res = pay2.get_payment_url(amount)
        url = res.get("url")
        if not url:
            raise RuntimeError("paysubmit ne URL nahi diya")
    except Exception as e:
        logging.error(f"pay2 order failed (plan={plan_type}, amount={amount}): {type(e).__name__}")
        # Failed order → pending entry hata do taaki user dobara try kar sake
        pending_verifications.pop(str(user_id), None)
        fail_text = ("❌ <b>Payment link generate nahi ho paya.</b>\n\n"
                     "Thodi der baad dobara koshish karein. Admin ko alert bhej diya gaya hai.")
        try:
            if placeholder:
                bot.edit_message_text(fail_text, chat_id=chat_id,
                                      message_id=placeholder.message_id, parse_mode="HTML")
            else:
                bot.send_message(chat_id, fail_text, parse_mode="HTML")
        except Exception:
            pass
        notify_pay2_error(f"plan={plan_type}, amount={amount}", e)
        return

    order_no = res.get("orderNo") or order_num
    # Auto-verify ke liye gateway token/URL pending order me save karo
    pv = pending_verifications.get(str(user_id))
    if pv is not None:
        pv['pay2_url'] = url
        pv['pay2_token'] = pay2.pay_token(url)
        pv['pay2_order_no'] = res.get("orderNo") or ""
        pending_verifications[str(user_id)] = pv

    text = f"""
<b>💰 ORDER #{order_num}: PAY ₹{amount} FOR {plan_name.upper()}</b>

✅ <b>Secure payment link ready hai.</b>

<b>Instructions:</b>
1. Neeche <b>💳 Pay Now</b> button dabao
2. Payment page par ₹{amount} pay karo
3. Wapas aake <b>✅ Payment Done</b> dabao

<b>Gateway Order:</b> <code>{order_no}</code>

⏳ <i>Ye link 10 minute me auto-delete ho jayega.</i>
    """

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    # URL button par Telegram color support nahi karta
    keyboard.add(types.InlineKeyboardButton("💳 Pay Now", url=url))
    keyboard.add(make_button("✅ Payment Done", callback_data="payment_done"))
    keyboard.add(make_button("🔙 Back", callback_data=f"plan_{plan_type}"))

    if placeholder:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=placeholder.message_id,
                                  reply_markup=keyboard, parse_mode="HTML")
            delete_message_after_delay(chat_id, placeholder.message_id, 600)
            return
        except Exception:
            pass
    m = bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    track_msg(chat_id, m.message_id)
    delete_message_after_delay(chat_id, m.message_id, 600)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def handle_back_to_main(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    clear_all_user_msgs(chat_id)

    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass

    start_demos = settings.get('start_demo_videos', [])
    if start_demos:
        start_desc = settings.get('start_demo_desc', "")
        send_demo_videos(chat_id, start_demos, caption=start_desc)

    welcome_text = f"""
🔥 <b>PREMIUM CONTENT</b> 🔥

Welcome to the Premium Bot! Access high-quality exclusive content.

👇 <b>Select an option:</b>
        """
    m = bot.send_message(
        chat_id,
        welcome_text,
        reply_markup=verif.main_menu_keyboard(),
        parse_mode="HTML"
    )
    track_msg(chat_id, m.message_id)
    bot.answer_callback_query(call.id)

# ========== HOW TO GET REMOVED ==========

@bot.callback_query_handler(func=lambda call: call.data in ["demo_not_set", "support_not_set", "proof_not_set"])
def handle_not_set_alerts(call):
    text = "This is not configured by admin yet."
    if call.data == "support_not_set":
        text = "Support username is not configured yet."
    elif call.data == "proof_not_set":
        text = "Payment proof channel link is not configured yet."
    bot.answer_callback_query(call.id, text, show_alert=True)

# ========== GET PREMIUM ==========
@bot.callback_query_handler(func=lambda call: call.data == "get_premium")
def handle_get_premium(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    spam_result = check_spam(user_id)
    if spam_result:
        m = bot.send_message(chat_id, spam_result, parse_mode="HTML")
        track_msg(chat_id, m.message_id)
        bot.answer_callback_query(call.id)
        return

    reset_spam_counter(user_id)

    clear_user_demos(chat_id)
    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass
    if msg_id in user_all_messages.get(str(chat_id), []):
        user_all_messages[str(chat_id)].remove(msg_id)

    m = bot.send_message(
        chat_id,
        "👇 <b>Choose your membership plan:</b>",
        reply_markup=verif.plan_selection_keyboard(),
        parse_mode="HTML"
    )
    track_msg(chat_id, m.message_id)

    bot.answer_callback_query(call.id)

# ========== PAYMENT DONE ==========
@bot.callback_query_handler(func=lambda call: call.data == "payment_done")
def handle_payment_done(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    spam_result = check_spam(user_id)
    if spam_result:
        m = bot.send_message(chat_id, spam_result, parse_mode="HTML")
        track_msg(chat_id, m.message_id)
        bot.answer_callback_query(call.id)
        return

    reset_spam_counter(user_id)

    if str(user_id) not in pending_verifications:
        bot.answer_callback_query(
            call.id,
            "Please select a plan first!",
            show_alert=True
        )
        return

    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass
    if msg_id in user_all_messages.get(str(chat_id), []):
        user_all_messages[str(chat_id)].remove(msg_id)

    pv = pending_verifications[str(user_id)]
    # pay2 order -> pehle auto-verify (get_info); warna purana manual screenshot flow
    if PAY2_AVAILABLE and pv.get('pay2_token'):
        bot.answer_callback_query(call.id, "🔎 Payment check kar raha hoon...")
        threading.Thread(target=pay2_auto_verify, args=(chat_id, user_id), daemon=True).start()
        return

    screenshot_msg = verif.ask_for_screenshot(chat_id, user_id, pv['plan'])
    if screenshot_msg:
        track_msg(chat_id, screenshot_msg.message_id)

    bot.answer_callback_query(call.id)

# ========== HANDLE SCREENSHOTS & FILE IDs ==========
@bot.message_handler(content_types=['photo', 'video', 'document'])
def handle_admin_files(message):
    user_id = message.from_user.id
    user_id_str = str(user_id)

    # 0. NORMAL USER WITH PENDING → If they send DOCUMENT/VIDEO instead of PHOTO
    if not is_admin(user_id) and user_id_str in pending_verifications:
        pending_data = pending_verifications[user_id_str]
        if not pending_data.get('screenshot_file_id'):
            # Wants to upload proof but sent wrong type
            if message.document or message.video:
                bot.reply_to(
                    message,
                    """❌ <b>WRONG FORMAT!</b>

Aapne payment proof ko <b>File / Document / Video</b> ke roop mein bheja hai.

✅ <b>Sahi tareeka:</b>
• Payment ka <b>Screenshot lekar PHOTO (Image)</b> ke roop mein bhejein
• File/Document/Videos accept nahi hote

<b>Example:</b>
1. UPI App open karein
2. Payment history se transaction open karein
3. <b>Screenshot</b> lein (gallery mein save hoga as IMAGE)
4. Yaha <b>Photo</b> select karke bhejein (NOT Document)
                    """,
                    parse_mode="HTML"
                )
                return

    # 1. If admin sends a video/photo, show them the file_id (for setting demos)
    if is_admin(user_id):
        file_id = None
        file_type = None
        
        if message.video:
            file_id = message.video.file_id
            file_type = "Video"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_type = "Photo"
        elif message.document:
            file_id = message.document.file_id
            file_type = "Document/Video"
            
        if file_id:
            # Auto-forward to backup channel if set
            backup_ch = settings.get('backup_channel')
            forward_status = ""
            if backup_ch:
                try:
                    bot.forward_message(backup_ch, message.chat.id, message.message_id)
                    forward_status = f"\n✅ <b>Stored in Backup Channel:</b> <code>{backup_ch}</code>"
                except Exception as e:
                    forward_status = f"\n❌ <b>Backup Failed:</b> {str(e)}"
            
            bot.reply_to(
                message, 
                f"<b>📄 {file_type} File ID:</b>\n\n<code>{file_id}</code>\n{forward_status}\n\nUse this ID in <code>/set_start_demos</code> or <code>/set_plan_demos</code>", 
                parse_mode="HTML"
            )
            # If it was just for file_id, we can return. But if it was a photo, it might be a payment screenshot.
            if message.video or message.document:
                return

    # 2. Check if this is a payment screenshot (for users or admin testing)
    if message.photo:
        if verif.handle_screenshot(message):
            return

# ========== VERIFICATION CALLBACKS ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith('verify_'))
def handle_verify(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Admin only!")
        return
    
    user_id = call.data.split('_')[1]
    
    success, msg = verif.verify_payment(user_id, call.from_user.id)
    
    if success:
        bot.answer_callback_query(call.id, "✅ Payment verified! Unique join link sent to user.")
        
        # Update the admin message
        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=call.message.caption + "\n\n✅ <b>VERIFIED - UNIQUE LINK SENT</b>",
                parse_mode="HTML"
            )
        except:
            pass
    else:
        bot.answer_callback_query(call.id, f"❌ Error: {msg}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('reject_'))
def handle_reject(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Admin only!")
        return
    
    user_id = call.data.split('_')[1]
    
    success, msg = verif.reject_payment(user_id, call.from_user.id)
    
    if success:
        bot.answer_callback_query(call.id, "❌ Payment rejected. User notified.")
        
        # Update the admin message
        try:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=call.message.caption + "\n\n❌ <b>REJECTED</b>",
                parse_mode="HTML"
            )
        except:
            pass
    else:
        bot.answer_callback_query(call.id, f"❌ Error: {msg}", show_alert=True)

# ========== /VERIFY COMMAND ==========
@bot.message_handler(commands=['verify'])
def handle_manual_verify(message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()
    if len(args) != 2:
        bot.reply_to(
            message,
            "Usage: /verify [user_id]\nExample: /verify 123456789"
        )
        return
    
    user_id = args[1]
    
    if user_id not in pending_verifications:
        bot.reply_to(message, "❌ User not in pending verifications")
        return
    
    success, msg = verif.verify_payment(user_id, message.from_user.id)
    bot.reply_to(message, msg)

# ========== /CANCEL_ORDER COMMAND ==========
@bot.message_handler(commands=['cancel_order'])
def handle_cancel_order(message):
    user_id = message.from_user.id
    user_id_str = str(user_id)

    if user_id_str not in pending_verifications:
        bot.reply_to(
            message,
            "✅ <b>No Pending Order Found!</b>\n\nAapka koi pending order nahi hai. Order cancel karne ke liye pehle ek order create karo.",
            parse_mode="HTML"
        )
        return

    pending_data = pending_verifications[user_id_str]
    order_num = pending_data.get('order_number', 'N/A')
    screenshot_uploaded = pending_data.get('screenshot_file_id', False)
    plan_type = pending_data.get('plan', 'N/A')
    amount = pending_data.get('amount', 'N/A')

    if screenshot_uploaded:
        try:
            admin_notify_msg = f"""
🚨 <b>USER CANCELLED PENDING ORDER</b>

👤 Name: {pending_data.get('first_name', 'N/A')}
👤 User: @{pending_data.get('username', 'N/A')}
🆔 ID: <code>{user_id}</code>
🧾 Order #: {order_num}
📅 Plan: {plan_type}
💰 Amount: ₹{amount}
⏰ Cancelled At: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

⚠️ <b>Note:</b> User ne screenshot upload kar diya tha but ab cancel kar raha hai.
Agar payment already receive ho gaya hai to manually user se contact karein.
            """

            target_chat = settings.get('log_channel')
            sent_to_admin = False
            try:
                if target_chat:
                    bot.send_message(target_chat, admin_notify_msg, parse_mode="HTML")
                    sent_to_admin = True
            except Exception as e:
                logging.warning(f"Cancel notify log channel error: {e}")

            if not sent_to_admin:
                for aid in settings.get('admin_ids', []):
                    try:
                        bot.send_message(aid, admin_notify_msg, parse_mode="HTML")
                        break
                    except:
                        continue

            admin_chat_id = pending_data.get('admin_chat_id')
            admin_msg_id = pending_data.get('admin_msg_id')
            if admin_chat_id and admin_msg_id:
                try:
                    bot.send_message(
                        admin_chat_id,
                        f"🚫 <b>ORDER #{order_num} CANCELLED BY USER</b>\n\nPlease ignore the above pending screenshot for User ID: <code>{user_id}</code>",
                        parse_mode="HTML",
                        reply_to_message_id=admin_msg_id
                    )
                except:
                    try:
                        bot.send_message(
                            admin_chat_id,
                            f"🚫 <b>ORDER #{order_num} CANCELLED BY USER</b>\nUser ID: <code>{user_id}</code>. Please ignore earlier pending screenshot.",
                            parse_mode="HTML"
                        )
                    except:
                        pass
        except Exception as e:
            logging.error(f"Cancel order admin notify error: {e}")

    del pending_verifications[user_id_str]
    save_json_file(PENDING_VERIF_FILE, pending_verifications)

    if screenshot_uploaded:
        user_msg = f"""
✅ <b>ORDER #{order_num} CANCELLED!</b>

💰 Amount: ₹{amount}
📅 Plan: {plan_type}

📢 <b>Important:</b> Aapne payment screenshot upload kar diya tha.
Agar aapne actual mein payment kar diya hai to please admin se contact karein.
Agar payment nahi kiya hai to koi action nahi lena hai - naya order /start se create kar sakte ho.
        """
    else:
        user_msg = f"""
✅ <b>ORDER #{order_num} CANCELLED!</b>

💰 Amount: ₹{amount}
📅 Plan: {plan_type}

Order successfully cancel ho gaya. Naya order create karne ke liye /start use karein.
        """

    bot.reply_to(message, user_msg, parse_mode="HTML")

# ========== /SETTINGS COMMAND (FIXED HTML) ==========
# ========== ADMIN PANEL (Button Based) ==========
def pay2_menu_keyboard():
    on = settings.get('payment_gateway') == 'pay2'
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        f"💳 Auto-Pay: {'ON ✅' if on else 'OFF ❌'}",
        callback_data="pay_toggle",
        color="positive" if on else "negative"
    ))
    kb.add(types.InlineKeyboardButton("➕ Add Account", callback_data="pay_add_acc", color="primary"))
    kb.add(types.InlineKeyboardButton(
        f"📋 List Accounts ({len(settings.get('pay2_accounts', []))})",
        callback_data="pay_list_acc", color="primary"
    ))
    kb.add(types.InlineKeyboardButton("💰 Balance / Orders", callback_data="pay_balance", color="positive"))
    kb.add(types.InlineKeyboardButton("🌐 Proxy Settings", callback_data="pay_proxy", color="primary"))
    kb.add(types.InlineKeyboardButton("🗑 Clear All Accounts", callback_data="pay_clear_acc", color="negative"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_main"))
    return kb


def pay2_menu_text():
    accounts = settings.get('pay2_accounts', [])
    if not PAY2_AVAILABLE:
        status = "❌ module load nahi hua (pycryptodome install karein)"
    elif settings.get('payment_gateway') == 'pay2' and not accounts:
        status = "⚠️ ON hai par koi account nahi — manual UPI/QR fallback chalega"
    else:
        status = "ON ✅" if settings.get('payment_gateway') == 'pay2' else "OFF (manual UPI/QR)"
    return (f"<b>💳 AUTO-PAY (pay2) SETTINGS</b>\n\n"
            f"<b>Mode:</b> {status}\n"
            f"<b>Accounts:</b> {len(accounts)}\n"
            f"<b>Proxy:</b> {pay2_proxy_label()}\n\n"
            f"ON hone par users ko gateway ka <b>payment link</b> milega "
            f"(manual UPI/QR OFF). OFF karne par purana UPI/QR flow chalu rahega.\n\n"
            f"<b>Add Account:</b> phone country code ke saath, jaise "
            f"<code>+91-9XXXXXXXXX</code>")


def admin_menu_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    
    # Row 1: Content Management
    keyboard.add(
        types.InlineKeyboardButton("📺 Manage Channels", callback_data="admin_channels", color="primary"),
        types.InlineKeyboardButton("🎥 Demo Settings", callback_data="admin_demo", color="primary")
    )
    
    # Row 2: Finance/Settings
    keyboard.add(
        types.InlineKeyboardButton("💰 UPI & Payment", callback_data="admin_upi", color="positive"),
        types.InlineKeyboardButton("📊 Stats & Sales", callback_data="admin_stats", color="positive")
    )
    
    # Row 3: Auto-Pay gateway (pay2)
    keyboard.add(
        types.InlineKeyboardButton("💳 Auto-Pay (pay2)", callback_data="admin_pay", color="primary")
    )

    # Row 4: Admin & Modes
    keyboard.add(
        types.InlineKeyboardButton("👑 Admin Mgmt", callback_data="admin_users", color="negative"),
        types.InlineKeyboardButton("🔐 Bot Modes", callback_data="admin_modes", color="negative")
    )
    
    # Row 4: Tools
    keyboard.add(
        types.InlineKeyboardButton("📤 Broadcast", callback_data="admin_broadcast", color="primary"),
        types.InlineKeyboardButton("📥 Clone Config", callback_data="admin_clone", color="primary")
    )
    
    # Row 5: Close
    keyboard.add(types.InlineKeyboardButton("❌ Close Menu", callback_data="admin_close"))
    
    return keyboard

@bot.message_handler(commands=['admin'])
def handle_admin_command(message):
    if not is_admin(message.from_user.id):
        return
    
    bot.reply_to(
        message,
        "<b>🛠️ ADMIN CONTROL PANEL</b>\n\nSelect an option to manage your bot:",
        reply_markup=admin_menu_keyboard(),
        parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def handle_admin_callbacks(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Not an Admin!")
        return
    
    data = call.data
    
    if data == "admin_close":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return
        
    if data == "admin_channels":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Add New Channel", callback_data="chan_add", color="positive"),
            types.InlineKeyboardButton("📝 Edit Existing Channel", callback_data="chan_list_edit", color="primary"),
            types.InlineKeyboardButton("❌ Remove Channel", callback_data="chan_list_del", color="negative"),
            types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_main")
        )
        bot.edit_message_text(
            "<b>📺 CHANNEL MANAGEMENT</b>\n\nAdd, edit or remove premium channels:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML"
        )
        
    elif data == "admin_main":
        bot.edit_message_text(
            "<b>🛠️ ADMIN CONTROL PANEL</b>\n\nSelect an option to manage your bot:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=admin_menu_keyboard(),
            parse_mode="HTML"
        )
        
    elif data == "admin_upi":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("📝 Set UPI ID", callback_data="upi_set_id", color="primary"),
            types.InlineKeyboardButton("👤 Set UPI Name", callback_data="upi_set_name", color="primary"),
            types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_main")
        )
        text = f"<b>💰 UPI SETTINGS</b>\n\n<b>Current ID:</b> <code>{settings.get('upi_id', 'Not Set')}</code>\n<b>Current Name:</b> {settings.get('upi_name', 'Not Set')}"
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb, parse_mode="HTML")

    elif data == "upi_set_id":
        msg = bot.send_message(call.message.chat.id, "Enter New UPI ID:")
        bot.register_next_step_handler(msg, lambda m: update_setting(m, 'upi_id', "UPI ID"))
        
    elif data == "upi_set_name":
        msg = bot.send_message(call.message.chat.id, "Enter New UPI Name:")
        bot.register_next_step_handler(msg, lambda m: update_setting(m, 'upi_name', "UPI Name"))

    elif data == "admin_pay":
        bot.edit_message_text(
            pay2_menu_text(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=pay2_menu_keyboard(),
            parse_mode="HTML"
        )

    elif data == "pay_toggle":
        if not PAY2_AVAILABLE:
            bot.answer_callback_query(call.id, "❌ pay2 module load nahi hua (pycryptodome install karein)", show_alert=True)
            return
        turning_on = settings.get('payment_gateway') != 'pay2'
        if turning_on and not settings.get('pay2_accounts'):
            bot.answer_callback_query(call.id, "⚠️ Pehle kam-se-kam ek account add karein.", show_alert=True)
            return
        settings['payment_gateway'] = 'pay2' if turning_on else 'manual'
        save_settings()
        sync_pay2_accounts()
        bot.edit_message_text(
            pay2_menu_text(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=pay2_menu_keyboard(),
            parse_mode="HTML"
        )

    elif data == "pay_add_acc":
        msg = bot.send_message(
            call.message.chat.id,
            "<b>➕ Add Auto-Pay Account</b>\n\nEnter Phone (country code ke saath, e.g. <code>+91-9XXXXXXXXX</code>):",
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, pay2_ask_password)

    elif data == "pay_list_acc":
        accounts = settings.get('pay2_accounts', [])
        if not accounts:
            bot.answer_callback_query(call.id, "Koi account configured nahi hai.", show_alert=True)
            return
        lines = [f"{i}. <code>{a.get('phone','')}</code> — 🔑 <code>{_mask_secret(a.get('password',''))}</code>"
                 for i, a in enumerate(accounts, 1)]
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_pay"))
        bot.edit_message_text(
            "<b>📋 AUTO-PAY ACCOUNTS</b>\n\n" + "\n".join(lines) + "\n\n<i>Passwords masked hain.</i>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML"
        )

    elif data == "pay_balance":
        try:
            bot.edit_message_text(
                "⏳ <b>Balance check ho raha hai...</b>\n<i>Gateway se fresh balance aa raha hai...</i>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode="HTML"
            )
        except Exception:
            pass
        threading.Thread(
            target=pay2_show_balance,
            args=(call.message.chat.id, call.message.message_id),
            daemon=True
        ).start()
        bot.answer_callback_query(call.id)

    elif data == "pay_proxy":
        _safe_edit_text(call.message.chat.id, call.message.message_id,
                        pay2_proxy_menu_text(), pay2_proxy_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "pay_proxy_set":
        msg = bot.edit_message_text(
            "🌐 <b>PROXY SET KARO</b>\n\n"
            "Poora proxy URL bhejein, jaise:\n"
            "<code>http://user:pass@host:port</code>\n\n"
            "• Bare format bhi chalega: <code>host:port:user:pass</code>\n"
            "• SOCKS5: <code>socks5://user:pass@host:port</code>\n"
            "• Aapka ye message security ke liye turant delete ho jayega.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode="HTML"
        )
        bot.register_next_step_handler(msg, pay2_proxy_ask_url)
        bot.answer_callback_query(call.id)

    elif data == "pay_proxy_disable":
        settings['pay2_proxy'] = 'off'
        save_settings()
        sync_pay2_proxy()
        _safe_edit_text(call.message.chat.id, call.message.message_id,
                        pay2_proxy_menu_text(), pay2_proxy_keyboard())
        bot.answer_callback_query(call.id, "🚫 Proxy disabled")

    elif data == "pay_proxy_reset":
        settings['pay2_proxy'] = ''
        save_settings()
        sync_pay2_proxy()
        _safe_edit_text(call.message.chat.id, call.message.message_id,
                        pay2_proxy_menu_text(), pay2_proxy_keyboard())
        bot.answer_callback_query(call.id, "♻️ Default par reset")

    elif data == "pay_clear_acc":
        settings['pay2_accounts'] = []
        settings['payment_gateway'] = 'manual'
        save_settings()
        sync_pay2_accounts()
        bot.edit_message_text(
            pay2_menu_text(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=pay2_menu_keyboard(),
            parse_mode="HTML"
        )

    elif data == "admin_stats":
        total_users = len(users_data)
        premium_users = sum(1 for u in users_data.values() if u.get('is_premium'))
        total_sales = sum(float(s.get('amount', 0)) for s in sales_data)
        text = f"<b>📊 BOT STATISTICS</b>\n\n👤 <b>Total Users:</b> {total_users}\n💎 <b>Premium Users:</b> {premium_users}\n💰 <b>Total Revenue:</b> ₹{total_sales:.2f}\n📦 <b>Total Orders:</b> {len(sales_data)}"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_main"))
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb, parse_mode="HTML")

    elif data == "admin_modes":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"🔐 Join-Request: {'ON' if settings.get('join_request_mode') else 'OFF'}", callback_data="mode_toggle_join"),
            types.InlineKeyboardButton(f"🧾 Proof Status: {'ON' if settings.get('payment_proof_status') else 'OFF'}", callback_data="mode_toggle_proof"),
            types.InlineKeyboardButton(f"🛡️ Force Join: {'ON' if settings.get('force_join_status') else 'OFF'}", callback_data="mode_toggle_force"),
            types.InlineKeyboardButton(f"🤖 Auto-Accept: {'ON' if settings.get('auto_accept_requests') else 'OFF'}", callback_data="mode_toggle_auto"),
            types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_main")
        )
        bot.edit_message_text("<b>🔐 BOT MODES</b>\n\nToggle security and automation modes:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb, parse_mode="HTML")

    elif data.startswith("mode_toggle_"):
        mode = data.replace("mode_toggle_", "")
        key = {"join": "join_request_mode", "proof": "payment_proof_status", "force": "force_join_status", "auto": "auto_accept_requests"}.get(mode)
        if key:
            settings[key] = not settings.get(key, False)
            save_settings()
            handle_admin_callbacks(types.CallbackQuery(id='0', from_user=call.from_user, chat_instance='0', message=call.message, data='admin_modes', json_string='{}'))

    elif data == "admin_demo":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton(f"🔄 Status: {'PAID' if settings.get('demo_paid_status') else 'FREE'}", callback_data="demo_toggle_status"),
            types.InlineKeyboardButton("💰 Set Demo Price", callback_data="demo_set_price"),
            types.InlineKeyboardButton("🔗 Set Demo Link", callback_data="demo_set_link"),
            types.InlineKeyboardButton("🆔 Set Demo ID", callback_data="demo_set_id"),
            types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_main")
        )
        text = f"<b>🎥 DEMO SETTINGS</b>\n\n<b>Status:</b> {'PAID' if settings.get('demo_paid_status') else 'FREE'}\n<b>Price:</b> ₹{settings.get('demo_amount', '10')}\n<b>ID:</b> <code>{settings.get('demo_channel_id', 'Not Set')}</code>\n<b>Link:</b> {settings.get('demo_channel_link', 'Not Set')}\n<b>Color:</b> Auto-Random 🎲"
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb, parse_mode="HTML")

    elif data == "demo_toggle_status":
        settings['demo_paid_status'] = not settings.get('demo_paid_status', False)
        save_settings()
        handle_admin_callbacks(types.CallbackQuery(id='0', from_user=call.from_user, chat_instance='0', message=call.message, data='admin_demo', json_string='{}'))

    elif data == "demo_set_price":
        msg = bot.send_message(call.message.chat.id, "Enter Demo Price (₹):")
        bot.register_next_step_handler(msg, lambda m: update_setting(m, 'demo_amount', "Demo Price"))

    elif data == "admin_users":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Add Admin", callback_data="admin_add_user"),
            types.InlineKeyboardButton("❌ Remove Admin", callback_data="admin_rem_user"),
            types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="admin_main")
        )
        text = f"<b>👑 ADMIN MANAGEMENT</b>\n\n<b>Admins:</b> <code>{', '.join(settings.get('admin_ids', []))}</code>"
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb, parse_mode="HTML")

    elif data == "admin_broadcast":
        msg = bot.send_message(call.message.chat.id, "Send the message (Text/Photo/Video) you want to broadcast to all users:")
        bot.register_next_step_handler(msg, handle_broadcast)
        
    elif data == "admin_clone":
        msg = bot.send_message(call.message.chat.id, "Enter Source DB Name to clone config from:")
        bot.register_next_step_handler(msg, lambda m: handle_clone_config(m))

    elif data == "admin_add_user":
        msg = bot.send_message(call.message.chat.id, "Enter User ID to ADD as Admin:")
        bot.register_next_step_handler(msg, handle_add_admin)
        
    elif data == "admin_rem_user":
        msg = bot.send_message(call.message.chat.id, "Enter User ID to REMOVE from Admins:")
        bot.register_next_step_handler(msg, handle_remove_admin)

    elif data == "demo_set_link":
        msg = bot.send_message(call.message.chat.id, "Enter New Demo Channel Link:")
        bot.register_next_step_handler(msg, lambda m: update_setting(m, 'demo_channel_link', "Demo Link"))

    elif data == "demo_set_id":
        msg = bot.send_message(call.message.chat.id, "Enter New Demo Channel ID:")
        bot.register_next_step_handler(msg, lambda m: update_setting(m, 'demo_channel_id', "Demo ID"))

    elif data == "chan_list_edit":
        kb = types.InlineKeyboardMarkup(row_width=1)
        for ch in settings.get('premium_channels', []):
            kb.add(types.InlineKeyboardButton(f"📝 Edit {ch['name']}", callback_data=f"chan_edit_{ch['id']}"))
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_channels"))
        bot.edit_message_text("Select channel to EDIT:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)

    elif data.startswith("chan_edit_"):
        chan_id = call.data.replace("chan_edit_", "")
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("📝 Name", callback_data=f"cedit_{chan_id}_name"),
            types.InlineKeyboardButton("💰 Price", callback_data=f"cedit_{chan_id}_amount"),
            types.InlineKeyboardButton("🆔 ID", callback_data=f"cedit_{chan_id}_channel_id"),
            types.InlineKeyboardButton("⏰ Time", callback_data=f"cedit_{chan_id}_duration"),
            types.InlineKeyboardButton("⬅️ Back", callback_data="chan_list_edit")
        )
        bot.edit_message_text("Select field to edit (Colors are now Auto-Random):", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)

    elif data.startswith("cedit_"):
        parts = data.split('_')
        chan_id = parts[1]
        field = parts[2]
        
        msg = bot.send_message(call.message.chat.id, f"Enter New {field.capitalize()}:")
        bot.register_next_step_handler(msg, process_chan_edit, chan_id, field)

    bot.answer_callback_query(call.id)

def process_chan_edit(message, chan_id, field):
    val = message.text.strip()
    if field == "amount": val = clean_amount_str(val)
    for ch in settings.get('premium_channels', []):
        if ch['id'] == chan_id:
            ch[field] = val
            break
    save_settings()
    bot.send_message(message.chat.id, f"✅ {field.capitalize()} updated to: {val}")

def update_setting(message, key, name):
    settings[key] = message.text.strip()
    save_settings()
    bot.send_message(message.chat.id, f"✅ {name} updated to: <code>{settings[key]}</code>", parse_mode="HTML")

# ========== AUTO-PAY (pay2) ACCOUNT ADD FLOW ==========
def pay2_ask_password(message):
    """Step 1 (phone) mil gaya → ab password maango."""
    phone = (message.text or "").strip()
    if " " in phone or len(phone) < 6 or len(phone) > 20:
        bot.reply_to(message, "❌ Invalid phone. Format: <code>+91-9XXXXXXXXX</code>\nDubara <b>➕ Add Account</b> se try karein.", parse_mode="HTML")
        return
    msg = bot.send_message(
        message.chat.id,
        f"<b>Phone:</b> <code>{phone}</code>\n\nEnter Password:",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, pay2_save_account, phone)


def pay2_save_account(message, phone):
    """Step 2 (password) → account save karo aur password wala message delete karo."""
    password = (message.text or "").strip()
    # Security: password wala message turant delete kar do
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    if not password:
        bot.send_message(message.chat.id, "❌ Password khaali tha. Dubara <b>➕ Add Account</b> se try karein.", parse_mode="HTML")
        return

    accounts = [a for a in settings.get('pay2_accounts', []) if a.get('phone') != phone]
    accounts.append({"phone": phone, "password": password})
    settings['pay2_accounts'] = accounts
    save_settings()
    active = sync_pay2_accounts()

    bot.send_message(
        message.chat.id,
        f"✅ <b>Account added!</b>\n\n"
        f"<b>Phone:</b> <code>{phone}</code>\n"
        f"<b>Password:</b> <code>{_mask_secret(password)}</code>\n"
        f"<b>Active accounts:</b> {active}\n\n"
        f"Auto-Pay ON karne ke liye <code>/pay2</code> khol kar toggle karein.",
        parse_mode="HTML"
    )


# ========== /PAY2 COMMANDS (AUTO-PAY GATEWAY) ==========
@bot.message_handler(commands=['pay2'])
def handle_pay2_command(message):
    if not is_admin(message.from_user.id):
        return
    bot.reply_to(message, pay2_menu_text(), reply_markup=pay2_menu_keyboard(), parse_mode="HTML")


@bot.message_handler(commands=['pay2_toggle'])
def handle_pay2_toggle(message):
    if not is_admin(message.from_user.id):
        return
    if not PAY2_AVAILABLE:
        bot.reply_to(message, "❌ pay2 module load nahi hua. <code>pip install pycryptodome</code> karke restart karein.", parse_mode="HTML")
        return
    turning_on = settings.get('payment_gateway') != 'pay2'
    if turning_on and not settings.get('pay2_accounts'):
        bot.reply_to(message, "⚠️ Pehle kam-se-kam ek account add karein: <code>/pay2_add</code>", parse_mode="HTML")
        return
    settings['payment_gateway'] = 'pay2' if turning_on else 'manual'
    save_settings()
    sync_pay2_accounts()
    bot.reply_to(message, f"✅ Auto-Pay ab <b>{'ON' if turning_on else 'OFF'}</b>.", parse_mode="HTML")


@bot.message_handler(commands=['pay2_add'])
def handle_pay2_add(message):
    if not is_admin(message.from_user.id):
        return
    msg = bot.send_message(
        message.chat.id,
        "<b>➕ Add Auto-Pay Account</b>\n\nEnter Phone (e.g. <code>+91-9XXXXXXXXX</code>):",
        parse_mode="HTML"
    )
    bot.register_next_step_handler(msg, pay2_ask_password)


@bot.message_handler(commands=['pay2_list'])
def handle_pay2_list(message):
    if not is_admin(message.from_user.id):
        return
    accounts = settings.get('pay2_accounts', [])
    if not accounts:
        bot.reply_to(message, "ℹ️ Koi account configured nahi hai. <code>/pay2_add</code> se add karein.", parse_mode="HTML")
        return
    lines = [f"{i}. <code>{a.get('phone','')}</code> — 🔑 <code>{_mask_secret(a.get('password',''))}</code>"
             for i, a in enumerate(accounts, 1)]
    bot.reply_to(message, "<b>📋 AUTO-PAY ACCOUNTS</b>\n\n" + "\n".join(lines) + "\n\n<i>Passwords masked hain.</i>", parse_mode="HTML")


@bot.message_handler(commands=['pay2_clear'])
def handle_pay2_clear(message):
    if not is_admin(message.from_user.id):
        return
    settings['pay2_accounts'] = []
    settings['payment_gateway'] = 'manual'
    save_settings()
    sync_pay2_accounts()
    bot.reply_to(message, "✅ Saare Auto-Pay accounts clear kar diye aur mode OFF kar diya.")


@bot.message_handler(commands=['pay2_balance'])
def handle_pay2_balance(message):
    if not is_admin(message.from_user.id):
        return
    m = bot.send_message(message.chat.id, "⏳ <b>Balance check ho raha hai...</b>", parse_mode="HTML")
    threading.Thread(target=pay2_show_balance, args=(message.chat.id, m.message_id), daemon=True).start()


@bot.message_handler(commands=['pay2_proxy'])
def handle_pay2_proxy(message):
    if not is_admin(message.from_user.id):
        return
    bot.send_message(
        message.chat.id,
        pay2_proxy_menu_text(),
        reply_markup=pay2_proxy_keyboard(),
        parse_mode="HTML"
    )

# ========== CHANNEL MANAGEMENT FLOWS ==========
@bot.callback_query_handler(func=lambda call: call.data.startswith('chan_'))
def handle_channel_callbacks(call):
    if not is_admin(call.from_user.id): return
    
    if call.data == "chan_add":
        msg = bot.send_message(call.message.chat.id, "<b>➕ Add New Channel</b>\n\nEnter Channel Name (e.g. <i>Premium VIP</i>):", parse_mode="HTML")
        bot.register_next_step_handler(msg, process_chan_name)
    
    elif call.data == "chan_list_del":
        kb = types.InlineKeyboardMarkup(row_width=1)
        for ch in settings.get('premium_channels', []):
            kb.add(types.InlineKeyboardButton(f"❌ Delete {ch['name']}", callback_data=f"chan_del_{ch['id']}"))
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="admin_channels"))
        bot.edit_message_text("Select channel to DELETE:", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=kb)

    elif call.data.startswith("chan_del_"):
        chan_id = call.data.replace("chan_del_", "")
        settings['premium_channels'] = [ch for ch in settings.get('premium_channels', []) if ch['id'] != chan_id]
        save_settings()
        bot.answer_callback_query(call.id, "✅ Channel Deleted")
        handle_admin_callbacks(types.CallbackQuery(id='0', from_user=call.from_user, chat_instance='0', message=call.message, data='admin_channels', json_string='{}'))

    bot.answer_callback_query(call.id)

def process_chan_name(message):
    name = message.text.strip()
    msg = bot.send_message(message.chat.id, f"<b>Name:</b> {name}\n\nEnter Amount (₹):", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_chan_amount, name)

def process_chan_amount(message, name):
    amount = clean_amount_str(message.text)
    msg = bot.send_message(message.chat.id, f"<b>Name:</b> {name}\n<b>Amount:</b> ₹{amount}\n\nEnter Telegram Channel ID (e.g. <code>-100...</code>):", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_chan_id, name, amount)

def process_chan_id(message, name, amount):
    ch_id = message.text.strip()
    msg = bot.send_message(message.chat.id, f"<b>Name:</b> {name}\n<b>Amount:</b> ₹{amount}\n<b>ID:</b> {ch_id}\n\nEnter Duration (e.g. <i>30 Days</i>):", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_chan_duration, name, amount, ch_id)

def process_chan_duration(message, name, amount, ch_id):
    duration = message.text.strip()
    
    new_id = f"ch_{random.randint(1000, 9999)}"
    new_chan = {
        "id": new_id,
        "name": name,
        "amount": amount,
        "channel_id": ch_id,
        "duration": duration,
        "color": "", # Color will be random every time it's displayed
        "description": ""
    }
    
    if 'premium_channels' not in settings: settings['premium_channels'] = []
    settings['premium_channels'].append(new_chan)
    save_settings()
    
    bot.send_message(message.chat.id, f"✅ <b>Channel Added Successfully!</b>\n\n<b>Name:</b> {name}\n<b>Price:</b> ₹{amount}\n<b>ID:</b> <code>{ch_id}</code>\n<b>Color:</b> Auto-Random 🎲", parse_mode="HTML")

@bot.message_handler(commands=['settings'])
def handle_settings(message):
    if not is_admin(message.from_user.id):
        return
    
    # Premium Channels List
    ch_info = ""
    for ch in settings.get('premium_channels', []):
        ch_id_display = ch.get('channel_id', ch.get('channel_ids', 'Not Set'))
        ch_desc = ch.get('description', '')
        desc_display = f"\n  📝 Desc: {ch_desc[:50]}{'...' if len(ch_desc) > 50 else ''}" if ch_desc else ""
        ch_info += f"• {ch.get('id', '??')}: {ch.get('name', 'Unknown')} (₹{ch.get('amount', '0')}) - <code>{ch_id_display}</code>\n  🎨 Color: Auto-Random 🎲{desc_display}\n"

    text = f"""
<b>⚙️ CURRENT SETTINGS</b>

<b>👑 Admins:</b> <code>{', '.join(settings.get('admin_ids', []))}</code>
<b>📢 Demo Link:</b> {settings.get('demo_channel_link', 'Not Set')}
<b>🆔 Demo ID:</b> <code>{settings.get('demo_channel_id', 'Not Set')}</code>
<b>💰 Demo Price:</b> ₹{settings.get('demo_amount', '10')}
<b>🎨 Demo Button Color:</b> Auto-Random 🎲
<b>🔄 Demo Status:</b> {'PAID' if settings.get('demo_paid_status', False) else 'FREE'}

<b>📋 Log Channel:</b> {settings.get('log_channel', 'Not Set')}
<b>🧾 Proof Channel ID:</b> <code>{settings.get('proof_channel_id', 'Not Set')}</code>
<b>🧾 Proof Channel Link:</b> {settings.get('payment_proof_link', 'Not Set')}
<b>🧾 Proof Status:</b> {'ON' if settings.get('payment_proof_status', False) else 'OFF'}
<b>🛡️ Force Join:</b> {'ON' if settings.get('force_join_status', True) else 'OFF'}
<b>🤖 Auto-Accept:</b> {'ON' if settings.get('auto_accept_requests', False) else 'OFF'}
<b>🔐 Join-Request Mode:</b> {'ON (admin approve)' if settings.get('join_request_mode', False) else 'OFF (direct link)'}

<b>📞 Support:</b>
• Username: @{settings.get('support_username', 'Not Set') or 'Not Set'}
• Status: {'ON' if settings.get('support_status', True) else 'OFF'}

<b>💰 UPI Settings:</b>
• UPI ID: <code>{settings.get('upi_id', 'Not Set')}</code>
• Name: {settings.get('upi_name', 'Not Set')}

<b>📺 Premium Channels:</b>
{ch_info}
<b>To change settings, use specific commands in /help</b>
    """
    
    bot.reply_to(message, text, parse_mode="HTML")

# ========== /SET COMMAND (FIXED HTML) ==========
@bot.message_handler(commands=['set'])
def handle_set(message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.reply_to(message, "Usage: /set [key] [value]\nExample: /set monthly_amount 129")
        return
    
    key = args[1].lower()
    value = args[2]
    
    # Map keys to settings
    key_map = {
        "demo_channel": "demo_channel_link",
        "support": "support_username",
        "log_channel": "log_channel",
        "upi_id": "upi_id",
        "upi_name": "upi_name",
        "monthly_name": "monthly_name",
        "monthly_amount": "monthly_amount",
        "monthly_channel": "monthly_channel_id",
        "lifetime_name": "lifetime_name",
        "lifetime_amount": "lifetime_amount",
        "lifetime_channel": "lifetime_channel_id"
    }
    
    if key not in key_map:
        bot.reply_to(message, f"❌ Invalid key. Available: {', '.join(key_map.keys())}")
        return
    
    settings[key_map[key]] = value
    save_settings()
    
    bot.reply_to(message, f"✅ Updated {key} to: {value}")

@bot.message_handler(commands=['ban'])
def handle_ban(message):
    bot.reply_to(message, "❌ Ban system has been removed from this bot.")

@bot.message_handler(commands=['unban'])
def handle_unban(message):
    bot.reply_to(message, "❌ Ban system has been removed from this bot.")

@bot.message_handler(commands=['banlist'])
def handle_banlist(message):
    bot.reply_to(message, "❌ Ban system has been removed from this bot.")

# ========== /BROADCAST COMMAND ==========
@bot.message_handler(commands=['broadcast'])
def handle_broadcast(message):
    """Broadcast message to all users"""
    if not is_admin(message.from_user.id):
        return
    
    if not message.reply_to_message:
        help_text = """
<b>📢 BROADCAST COMMAND</b>

<code>Reply to any message with /broadcast</code>

<b>Supported:</b> Text, Photos, Videos, Documents, GIFs, Audio, Voice

<b>How to use:</b>
1. Send the message you want to broadcast
2. Reply to it with <code>/broadcast</code>
        """
        bot.reply_to(message, help_text, parse_mode="HTML")
        return
    
    replied_msg = message.reply_to_message
    progress_msg = bot.reply_to(message, "📤 <b>Broadcast Starting...</b>", parse_mode="HTML")
    
    total_users = len(users_data)
    if total_users == 0:
        bot.edit_message_text("❌ No users to broadcast", chat_id=message.chat.id, message_id=progress_msg.message_id)
        return
    
    def broadcast_thread():
        sent = 0
        failed = 0
        skipped = 0
        user_ids = list(users_data.keys())
        
        for idx, user_id_str in enumerate(user_ids):
            try:
                user_id = int(user_id_str)
                
                # Skip if blocked
                if user_id_str in spam_data:
                    if spam_data[user_id_str].get("blocked_until", 0) > time.time():
                        skipped += 1
                        continue
                
                # Send based on type
                if replied_msg.photo:
                    bot.send_photo(
                        user_id,
                        photo=replied_msg.photo[-1].file_id,
                        caption=replied_msg.caption or "",
                        parse_mode="HTML"
                    )
                elif replied_msg.video:
                    bot.send_video(
                        user_id,
                        video=replied_msg.video.file_id,
                        caption=replied_msg.caption or "",
                        parse_mode="HTML"
                    )
                elif replied_msg.document:
                    bot.send_document(
                        user_id,
                        document=replied_msg.document.file_id,
                        caption=replied_msg.caption or "",
                        parse_mode="HTML"
                    )
                elif replied_msg.animation:
                    bot.send_animation(
                        user_id,
                        animation=replied_msg.animation.file_id,
                        caption=replied_msg.caption or "",
                        parse_mode="HTML"
                    )
                elif replied_msg.audio:
                    bot.send_audio(
                        user_id,
                        audio=replied_msg.audio.file_id,
                        caption=replied_msg.caption or "",
                        parse_mode="HTML"
                    )
                elif replied_msg.voice:
                    bot.send_voice(
                        user_id,
                        voice=replied_msg.voice.file_id,
                        caption=replied_msg.caption or "",
                        parse_mode="HTML"
                    )
                elif replied_msg.text:
                    bot.send_message(user_id, replied_msg.text, parse_mode="HTML")
                elif replied_msg.caption:
                    bot.send_message(user_id, replied_msg.caption, parse_mode="HTML")
                
                sent += 1
                
                # Update progress every 50 users
                if (idx + 1) % 50 == 0:
                    percent = int((idx + 1) / total_users * 100)
                    try:
                        bot.edit_message_text(
                            f"📤 Broadcasting... {percent}% ({sent} sent, {failed} failed)", 
                            chat_id=message.chat.id, 
                            message_id=progress_msg.message_id
                        )
                    except:
                        pass
                
                time.sleep(0.05)  # Rate limit protection
                
            except Exception as e:
                failed += 1
                # Auto-remove dead users
                error_str = str(e).lower()
                if "forbidden" in error_str or "blocked" in error_str or "deactivated" in error_str:
                    if user_id_str in users_data:
                        del users_data[user_id_str]
        
        # Save all data after broadcast complete
        save_all_data()
        
        final_text = f"""
✅ <b>BROADCAST COMPLETE!</b>

📊 <b>Results:</b>
• ✅ Sent: {sent}
• ❌ Failed: {failed}
• ⏭️ Skipped: {skipped}
• 👥 Total: {total_users}
        """
        
        try:
            bot.edit_message_text(
                final_text, 
                chat_id=message.chat.id, 
                message_id=progress_msg.message_id, 
                parse_mode="HTML"
            )
        except:
            pass
    
    thread = threading.Thread(target=broadcast_thread)
    thread.start()
    
    bot.reply_to(message, f"📢 Broadcast started to {total_users} users!")

# ========== /STATS COMMAND ==========
@bot.message_handler(commands=['stats'])
def handle_stats(message):
    """Show bot statistics"""
    if not is_admin(message.from_user.id):
        return
    
    current_time = time.time()
    blocked_users = sum(1 for u in spam_data.values() if u.get("blocked_until", 0) > current_time)
    pending_count = len(pending_verifications)
    
    today = datetime.now().strftime('%Y-%m-%d')
    new_today = sum(1 for u in users_data.values() if u.get('start_time', '').startswith(today))
    
    # Count premium users
    premium_users = sum(1 for u in users_data.values() if u.get('is_premium', False))
    
    # Dynamic Pricing Info
    pricing_info = ""
    for ch in settings.get('premium_channels', []):
        pricing_info += f"• {ch['name']}: ₹{ch['amount']}\n"
    if not pricing_info:
        pricing_info = "• No channels configured\n"
        
    stats_text = f"""
<b>📊 BOT STATISTICS</b>

👥 <b>Users:</b>
• Total Users: {len(users_data)}
• Premium Users: {premium_users}
• New Today: {new_today}
• Pending Verification: {pending_count}

🛡️ <b>Spam Protection:</b>
• Currently Blocked: {blocked_users}
• Tracked Users: {len(spam_data)}

📩 <b>Join Requests:</b>
• Pending Tracked: {len(join_requests)}

💰 <b>Pricing Info:</b>
{pricing_info}• Demo: ₹{settings.get('demo_amount', '10')} ({'PAID' if settings.get('demo_paid_status') else 'FREE'})

📁 <b>Storage:</b>
• Data Files: {len(os.listdir(DATA_DIR))}

🚀 <b>Status:</b> ✅ Running
    """
    bot.reply_to(message, stats_text, parse_mode="HTML")

# ========== /SALES COMMAND ==========
@bot.message_handler(commands=['sales'])
def handle_sales(message):
    """Show sales statistics (Daily, Weekly, Monthly)"""
    if not is_admin(message.from_user.id):
        return
    
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    
    daily_total = 0
    weekly_total = 0
    monthly_total = 0
    
    upi_sales = {} # Track sales per UPI ID
    
    for sale in sales_data:
        try:
            sale_date = datetime.strptime(sale['date'], "%Y-%m-%d")
            amount = config.parse_amount(sale.get('amount'), 0.0) or 0.0
            upi = sale.get('upi_id', 'Unknown')
            
            if upi not in upi_sales:
                upi_sales[upi] = 0
            
            if sale['date'] == today_str:
                daily_total += amount
                upi_sales[upi] += amount
            
            if sale_date >= week_ago:
                weekly_total += amount
            
            if sale_date >= month_ago:
                monthly_total += amount
        except:
            continue
            
    upi_info = ""
    for upi, amt in upi_sales.items():
        upi_info += f"• <code>{upi}</code>: ₹{amt}\n"
        
    sales_text = f"""
<b>💰 SALES STATISTICS</b>

📅 <b>Total Revenue:</b>
• <b>Daily:</b> ₹{daily_total}
• <b>Weekly:</b> ₹{weekly_total}
• <b>Monthly:</b> ₹{monthly_total}

💳 <b>Revenue by UPI (Today):</b>
{upi_info if upi_info else '• No sales today'}

📊 <b>Total Transactions:</b> {len(sales_data)}
    """
    bot.reply_to(message, sales_text, parse_mode="HTML")

# ========== /ADMIN COMMANDS (NEW) ==========
@bot.message_handler(commands=['add_admin'])
def handle_add_admin(message, user_id=None):
    if not is_admin(message.from_user.id):
        return
        
    if user_id is None:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: <code>/add_admin user_id</code>", parse_mode="HTML")
            return
        new_admin = args[1].strip()
    else:
        new_admin = message.text.strip()
        
    if 'admin_ids' not in settings:
        settings['admin_ids'] = []
        
    if str(new_admin) not in [str(a) for a in settings['admin_ids']]:
        settings['admin_ids'].append(str(new_admin))
        save_settings()
        bot.send_message(message.chat.id, f"✅ User <code>{new_admin}</code> added to admins.", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, "User is already an admin.")

@bot.message_handler(commands=['remove_admin'])
def handle_remove_admin(message, user_id=None):
    if not is_admin(message.from_user.id):
        return
        
    if user_id is None:
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "Usage: <code>/remove_admin user_id</code>", parse_mode="HTML")
            return
        admin_to_remove = args[1].strip()
    else:
        admin_to_remove = message.text.strip()
    
    # Don't allow removing the last admin
    if len(settings.get('admin_ids', [])) <= 1:
        bot.send_message(message.chat.id, "❌ Cannot remove the last admin.")
        return
        
    admin_ids = [str(a) for a in settings.get('admin_ids', [])]
    if str(admin_to_remove) in admin_ids:
        settings['admin_ids'] = [a for a in settings['admin_ids'] if str(a) != str(admin_to_remove)]
        save_settings()
        bot.send_message(message.chat.id, f"✅ User <code>{admin_to_remove}</code> removed from admins.", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, "User is not in admin list.")

@bot.message_handler(commands=['clone_config'])
def handle_clone_config(message):
    """Clone THIS bot's CONFIG (settings + demos + media) from another DB in the same cluster.
    USER data (users, sales, verifications, join requests, invite links) is NEVER copied."""
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(
            message,
            "📥 <b>Clone Config</b>\n\n"
            "Usage: <code>/clone_config &lt;source_db_name&gt;</code>\n\n"
            f"• This bot's DB: <code>{config.DB_NAME}</code>\n"
            "• <code>source_db_name</code> = the other bot's MongoDB database name to copy config from\n\n"
            "<b>Copies:</b> settings, premium channels, demos + demo media, start message.\n"
            "<b>Never copies:</b> users, sales, verifications, join requests (kept separate).",
            parse_mode="HTML",
        )
        return

    source_db = args[1].strip().strip('"').strip("'")
    bot.reply_to(message, f"⏳ Cloning config from <code>{source_db}</code> into <code>{config.DB_NAME}</code>...", parse_mode="HTML")

    try:
        ok, summary, detail = config.clone_config_from(source_db)
    except Exception as e:
        bot.reply_to(message, f"❌ Clone failed: <code>{config.redact_secrets_from_text(str(e))}</code>", parse_mode="HTML")
        return

    if not ok:
        bot.reply_to(message, f"❌ Clone not applied.\n<code>{config.redact_secrets_from_text(detail)}</code>", parse_mode="HTML")
        return

    try:
        config.refresh_in_memory_data()
        reloaded = True
    except Exception as e:
        reloaded = False
        logging.warning(f"clone_config: in-memory refresh failed: {type(e).__name__}")

    bot.reply_to(
        message,
        "✅ <b>Config cloned successfully!</b>\n\n"
        f"<b>Source:</b> <code>{summary.get('source_db')}</code>\n"
        f"<b>Target:</b> <code>{summary.get('target_db')}</code>\n"
        f"<b>Settings:</b> {'✔ copied' if summary.get('settings') else '—'}\n"
        f"<b>Start message:</b> {'✔ copied' if summary.get('start_message') else '—'}\n"
        f"<b>Media files copied:</b> {summary.get('media', 0)}\n\n"
        + ("Settings reloaded live. ✅" if reloaded else "⚠️ Restart the bot to apply everything."),
        parse_mode="HTML",
    )

# Legacy channel commands removed

@bot.message_handler(commands=['demo_toggle'])
def handle_demo_toggle(message):
    if not is_admin(message.from_user.id):
        return

    current_status = settings.get('demo_paid_status', False)
    new_status = not current_status
    settings['demo_paid_status'] = new_status
    save_settings()

    status_text = "PAID" if new_status else "FREE"
    bot.reply_to(message, f"✅ Demo is now <b>{status_text}</b>.", parse_mode="HTML")

@bot.message_handler(commands=['demo_price'])
def handle_demo_price(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/demo_price amount</code>", parse_mode="HTML")
        return

    amount = args[1]
    settings['demo_amount'] = amount
    save_settings()
    bot.reply_to(message, f"✅ Demo price set to <b>₹{amount}</b>.", parse_mode="HTML")

@bot.message_handler(commands=['set_demo_color'])
def handle_set_demo_color(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(
            message,
            """Usage: <code>/set_demo_color color</code>
Available colors: <code>blue / green / red / default</code>
Examples:
<code>/set_demo_color green</code>
<code>/set_demo_color default</code>""",
            parse_mode="HTML"
        )
        return

    color_raw = args[1]
    resolved = resolve_color(color_raw)
    save_value = color_raw if resolved is not None else ""
    settings['demo_color'] = save_value
    save_settings()
    bot.reply_to(message, f"✅ Demo button color set to: {color_name_display(save_value)}", parse_mode="HTML")

@bot.message_handler(commands=['set_demo_ch'])
def handle_set_demo_ch(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/set_demo_ch channel_id</code>", parse_mode="HTML")
        return

    ch_id = args[1]
    settings['demo_channel_id'] = ch_id
    save_settings()
    bot.reply_to(message, f"✅ Demo Channel ID set to: <code>{ch_id}</code>", parse_mode="HTML")

@bot.message_handler(commands=['set_demo_link'])
def handle_set_demo_link(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/set_demo_link https://t.me/xxx</code>", parse_mode="HTML")
        return

    url = args[1]
    settings['demo_channel_link'] = url
    save_settings()
    bot.reply_to(message, f"✅ Demo Channel Link set to: {url}")

# Support and Force Join toggles removed

@bot.message_handler(commands=['proof_toggle'])
def handle_proof_toggle(message):
    if not is_admin(message.from_user.id):
        return

    current = settings.get("payment_proof_status", False)
    settings["payment_proof_status"] = not current
    save_settings()

    status = "ON" if settings["payment_proof_status"] else "OFF"
    bot.reply_to(message, f"✅ Payment Proof button is now <b>{status}</b>.", parse_mode="HTML")

@bot.message_handler(commands=['support_toggle'])
def handle_support_toggle(message):
    if not is_admin(message.from_user.id):
        return

    current = settings.get("support_status", True)
    settings["support_status"] = not current
    save_settings()

    status = "ON" if settings["support_status"] else "OFF"
    bot.reply_to(message, f"✅ Contact Support button is now <b>{status}</b>.", parse_mode="HTML")

# ========== JOIN REQUEST (ADMIN APPROVAL) MODE ==========
@bot.message_handler(commands=['join_request_toggle', 'approval_toggle'])
def handle_join_request_toggle(message):
    """Toggle admin-approval (join-request) mode for post-payment links.

    ON  → user ko approval link milta hai; user request bhejta hai; admin
          Approve/Decline karta hai, tabhi user channel me add hota hai.
    OFF → pehle jaisa direct one-time link (member_limit=1) milta hai."""
    if not is_admin(message.from_user.id):
        return

    settings['join_request_mode'] = not bool(settings.get('join_request_mode', False))
    save_settings()

    if settings['join_request_mode']:
        txt = ("✅ <b>Join-Request Mode: ON</b>\n\n"
               "Ab payment verify hone ke baad user ko <b>approval link</b> milega.\n"
               "User link pe click karke <b>join request</b> bhejega → aapko "
               "<b>✅ Approve / ❌ Decline</b> buttons aayenge → aapke approve karne par hi "
               "user channel me add hoga.")
    else:
        txt = ("✅ <b>Join-Request Mode: OFF</b>\n\n"
               "Ab payment verify hote hi user ko pehle jaise <b>direct one-time link</b> "
               "(member_limit=1) milega.")
    bot.reply_to(message, txt, parse_mode="HTML")

def handle_chat_join_request(request):
    """User ne approval-link se join request bheja → admins ko Approve/Decline buttons bhejo."""
    try:
        chat_id = request.chat.id
        chat_title = getattr(request.chat, 'title', str(chat_id))
        user = request.from_user
        user_id = user.id

        invite_url = ""
        try:
            invite_url = request.invite_link.invite_link or ""
        except Exception:
            invite_url = ""

        record = {
            'user_id': str(user_id),
            'chat_id': str(chat_id),
            'chat_title': chat_title,
            'username': getattr(user, 'username', '') or '',
            'first_name': getattr(user, 'first_name', '') or '',
            'invite_link': invite_url,
            'status': 'pending',
            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Track (dedupe by user+chat, keep newest) — mutate the SAME list object
        # that config holds so persistence stays consistent.
        jr = config.join_requests
        if isinstance(jr, list):
            jr[:] = [r for r in jr if not (
                isinstance(r, dict)
                and str(r.get('user_id')) == str(user_id)
                and str(r.get('chat_id')) == str(chat_id)
            )]
            jr.append(record)
            config.save_join_requests()

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"jreq_ok_{chat_id}_{user_id}"),
            types.InlineKeyboardButton("❌ Decline", callback_data=f"jreq_no_{chat_id}_{user_id}")
        )
        admin_text = (
            f"🔔 <b>NEW JOIN REQUEST</b>\n\n"
            f"👤 {record['first_name'] or 'User'}"
            f"{' (@' + record['username'] + ')' if record['username'] else ''}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"📢 Channel: {chat_title}\n"
            f"🕒 {record['time']}\n\n"
            f"<i>Aapke approve karne ke baad user channel me add hoga.</i>"
        )
        for admin in settings.get('admin_ids', []):
            try:
                bot.send_message(admin, admin_text, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logging.warning(f"Could not notify admin {admin} about join request: {type(e).__name__}")
    except Exception as e:
        logging.error(f"chat_join_request handler error: {e}")

# Register the join-request handler only if this pyTelegramBotAPI build supports it.
if hasattr(bot, 'chat_join_request_handler'):
    bot.chat_join_request_handler()(handle_chat_join_request)
else:
    logging.warning(
        "Installed pyTelegramBotAPI does not support chat_join_request — "
        "join-request mode will not notify admins automatically. Upgrade pyTelegramBotAPI."
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('jreq_ok_') or call.data.startswith('jreq_no_'))
def handle_join_request_decision(call):
    """Admin ne Approve/Decline click kiya → request ko approve/decline karo."""
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Only admins can do this.", show_alert=True)
        return

    try:
        action, chat_id_str, user_id_str = call.data.split('_')[1], call.data.split('_')[2], call.data.split('_')[3]
        chat_id = int(chat_id_str)
        user_id = int(user_id_str)
    except Exception:
        bot.answer_callback_query(call.id, "⚠️ Invalid request data.", show_alert=True)
        return

    try:
        if action == 'ok':
            bot.approve_chat_join_request(chat_id, user_id)
            status, verdict = 'approved', "✅ <b>APPROVED</b>"
            user_msg = ("🎉 <b>Request Approved!</b>\n\n"
                        "Aapko channel me add kar diya gaya hai. Welcome! 🎊")
        else:
            bot.decline_chat_join_request(chat_id, user_id)
            status, verdict = 'declined', "❌ <b>DECLINED</b>"
            user_msg = ("😔 <b>Request Declined</b>\n\n"
                        "Aapka join request admin ne decline kar diya.\n"
                        "Koi dikkat ho to support se contact karein.")
    except Exception as e:
        logging.error(f"Join request approve/decline failed: {e}")
        bot.answer_callback_query(call.id, f"⚠️ Telegram error: {str(e)[:120]}", show_alert=True)
        return

    # Update stored record
    try:
        jr = config.join_requests
        if isinstance(jr, list):
            for r in jr:
                if (isinstance(r, dict)
                        and str(r.get('user_id')) == str(user_id)
                        and str(r.get('chat_id')) == str(chat_id)):
                    r['status'] = status
                    r['handled_by'] = str(call.from_user.id)
                    r['handled_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            config.save_join_requests()
    except Exception as e:
        logging.error(f"Join request record update failed: {e}")

    # Notify the user
    try:
        bot.send_message(user_id, user_msg, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"Could not DM user {user_id} about join request: {type(e).__name__}")

    # Mark the admin message
    try:
        base = getattr(call.message, 'html_text', None) or call.message.text or ""
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{base}\n\n{verdict} by admin",
            parse_mode="HTML",
            reply_markup=None
        )
    except Exception as e:
        logging.warning(f"Could not edit admin join-request message: {type(e).__name__}")

    bot.answer_callback_query(call.id, "Approved ✅" if action == 'ok' else "Declined ✅")

@bot.message_handler(commands=['set_proof_link'])
def handle_set_proof_link(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/set_proof_link [url]</code>", parse_mode="HTML")
        return

    url = args[1]
    settings['payment_proof_link'] = url
    save_settings()
    bot.reply_to(message, f"✅ Payment Proof Link set to: {url}", parse_mode="HTML")

# set_buy_url removed

@bot.message_handler(commands=['set_backup_ch'])
def handle_set_backup_ch(message):
    """Set the channel for auto-storing demo videos"""
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/set_backup_ch channel_id</code>", parse_mode="HTML")
        return
        
    ch_id = args[1]
    settings['backup_channel'] = ch_id
    save_settings()
    bot.reply_to(message, f"✅ Backup channel set to: <code>{ch_id}</code>\nAll videos you send to bot will be auto-forwarded here.", parse_mode="HTML")

# ========== DEMO VIDEO MANAGEMENT ==========
@bot.message_handler(commands=['set_start_demos'])
def handle_set_start_demos(message):
    """Set demo items (media/text) for /start. Support reply to media/text or other methods."""
    if not is_admin(message.from_user.id):
        return
    
    # 1. Handle Reply
    if message.reply_to_message:
        reply = message.reply_to_message
        item = None
        
        if reply.text:
            # Text message
            item = {"type": "text", "text": reply.text}
        elif reply.caption:
            # Caption with media
            item = {"type": "text", "text": reply.caption}
        elif reply.video:
            item = {"id": reply.video.file_id, "type": 'video'}
        elif reply.photo:
            item = {"id": reply.photo[-1].file_id, "type": 'photo'}
        elif reply.document:
            fid = reply.document.file_id
            mime = reply.document.mime_type or ""
            ftype = 'video'
            if "image" in mime:
                ftype = 'photo'
            item = {"id": fid, "type": ftype}
            
        if item:
            if item.get('type') != 'text':
                # Cache raw bytes for token-change auto re-upload
                mid, _ = _cache_replied_media(reply)
                if mid:
                    item['media_id'] = mid
            if 'start_demo_videos' not in settings:
                settings['start_demo_videos'] = []
            
            # Check duplicates
            exists = False
            if item.get('type') == 'text':
                exists = any(x.get('type') == 'text' and x.get('text') == item.get('text') for x in settings['start_demo_videos'])
            else:
                exists = any(x.get('type') != 'text' and x.get('id') == item.get('id') for x in settings['start_demo_videos'])
                
            if not exists:
                settings['start_demo_videos'].append(item)
                save_settings()
                bot.reply_to(message, f"✅ Added {item.get('type')} to /start demos. Total: {len(settings['start_demo_videos'])}")
            else:
                bot.reply_to(message, "❌ This item is already in the /start demos list.")
        else:
            bot.reply_to(message, "❌ Please reply to a video/photo/text to add it to demos.")
        return

    # 2. Handle Space-separated file_ids (Overwrite mode)
    args = message.text.split()[1:]
    if args:
        # Convert IDs to new format (assume video for IDs)
        new_list = [{"id": fid, "type": "video"} for fid in args]
        settings['start_demo_videos'] = new_list
        save_settings()
        bot.reply_to(message, f"✅ Set {len(args)} demo items for /start (Overwrite).")
        return
        
    bot.reply_to(message, "Usage: Reply to video/photo/text with <code>/set_start_demos</code>\nOR use <code>/set_start_demos file_id1 file_id2 ...</code>", parse_mode="HTML")

@bot.message_handler(commands=['clear_start_demos'])
def handle_clear_start_demos(message):
    if not is_admin(message.from_user.id):
        return
    
    for _it in list(settings.get('start_demo_videos', [])):
        if isinstance(_it, dict) and _it.get('media_id'):
            _media_cache_delete_safe(_it.get('media_id'))
    settings['start_demo_videos'] = []
    save_settings()
    bot.reply_to(message, "✅ Cleared /start demo videos.")

@bot.message_handler(commands=['set_plan_demos'])
def handle_set_plan_demos(message):
    """Set demo items (media/text) for a plan. Support reply to media/text or space-separated file_ids."""
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/set_plan_demos plan_id [file_id1 file_id2 ...]</code>", parse_mode="HTML")
        return
        
    plan_id = args[1]
    
    # 1. Handle Reply (Append mode)
    if message.reply_to_message:
        reply = message.reply_to_message
        item = None
        
        if reply.text:
            item = {"type": "text", "text": reply.text}
        elif reply.caption:
            item = {"type": "text", "text": reply.caption}
        elif reply.video:
            item = {"id": reply.video.file_id, "type": 'video'}
        elif reply.photo:
            item = {"id": reply.photo[-1].file_id, "type": 'photo'}
        elif reply.document:
            fid = reply.document.file_id
            mime = reply.document.mime_type or ""
            ftype = 'video'
            if "image" in mime:
                ftype = 'photo'
            item = {"id": fid, "type": ftype}
            
        if item:
            if item.get('type') != 'text':
                # Cache raw bytes for token-change auto re-upload
                mid, _ = _cache_replied_media(reply)
                if mid:
                    item['media_id'] = mid
            if 'plan_demo_videos' not in settings:
                settings['plan_demo_videos'] = {}
            if plan_id not in settings['plan_demo_videos']:
                settings['plan_demo_videos'][plan_id] = []
                
            # Check duplicates
            exists = False
            if item.get('type') == 'text':
                exists = any(x.get('type') == 'text' and x.get('text') == item.get('text') for x in settings['plan_demo_videos'][plan_id])
            else:
                exists = any(x.get('type') != 'text' and x.get('id') == item.get('id') for x in settings['plan_demo_videos'][plan_id])
                
            if not exists:
                settings['plan_demo_videos'][plan_id].append(item)
                save_settings()
                bot.reply_to(message, f"✅ Added {item.get('type')} to demos for plan <code>{plan_id}</code>. Total: {len(settings['plan_demo_videos'][plan_id])}", parse_mode="HTML")
            else:
                bot.reply_to(message, f"❌ This item is already in the demos list for plan <code>{plan_id}</code>.", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Please reply to a video/photo/text to add it to plan demos.")
        return

    # 2. Handle Space-separated file_ids (Overwrite mode)
    if len(args) >= 3:
        video_ids = args[2:]
        if 'plan_demo_videos' not in settings:
            settings['plan_demo_videos'] = {}
            
        # Convert IDs to new format
        new_list = [{"id": fid, "type": "video"} for fid in video_ids]
        settings['plan_demo_videos'][plan_id] = new_list
        save_settings()
        bot.reply_to(message, f"✅ Set {len(video_ids)} demo items for plan: <code>{plan_id}</code> (Overwrite)", parse_mode="HTML")
        return
        
    bot.reply_to(message, "Usage: Reply to video/photo/text with <code>/set_plan_demos plan_id</code>\nOR use <code>/set_plan_demos plan_id file_id1 file_id2 ...</code>", parse_mode="HTML")

@bot.message_handler(commands=['clear_plan_demos'])
def handle_clear_plan_demos(message):
    if not is_admin(message.from_user.id):
        return
    
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/clear_plan_demos plan_id</code>", parse_mode="HTML")
        return
    
    plan_id = args[1]
    if 'plan_demo_videos' in settings and plan_id in settings['plan_demo_videos']:
        for _it in list(settings['plan_demo_videos'].get(plan_id, [])):
            if isinstance(_it, dict) and _it.get('media_id'):
                _media_cache_delete_safe(_it.get('media_id'))
        del settings['plan_demo_videos'][plan_id]
        # Also clear description
        if 'plan_demo_descs' in settings and plan_id in settings['plan_demo_descs']:
            del settings['plan_demo_descs'][plan_id]
        save_settings()
        bot.reply_to(message, f"✅ Cleared demo videos & desc for plan: <code>{plan_id}</code>", parse_mode="HTML")
    else:
        bot.reply_to(message, f"❌ No demo videos found for plan: <code>{plan_id}</code>", parse_mode="HTML")

@bot.message_handler(commands=['set_demo_desc'])
def handle_set_demo_desc(message):
    """Set description for demo album. Usage: /set_demo_desc [start/plan_id] [Description]"""
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        bot.reply_to(message, "Usage: <code>/set_demo_desc [start/plan_id] [Your Description]</code>", parse_mode="HTML")
        return
        
    target = args[1].lower()
    desc = args[2]
    
    if target == 'start':
        settings['start_demo_desc'] = desc
        bot.reply_to(message, "✅ /start demo description updated!")
    else:
        if 'plan_demo_descs' not in settings:
            settings['plan_demo_descs'] = {}
        settings['plan_demo_descs'][target] = desc
        bot.reply_to(message, f"✅ Demo description for plan <code>{target}</code> updated!", parse_mode="HTML")
    
    save_settings()

@bot.message_handler(commands=['clear_demo_desc'])
def handle_clear_demo_desc(message):
    """Clear description for demo album. Usage: /clear_demo_desc [start/plan_id]"""
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/clear_demo_desc [start/plan_id]</code>", parse_mode="HTML")
        return
        
    target = args[1].lower()
    
    if target == 'start':
        settings['start_demo_desc'] = ""
        bot.reply_to(message, "✅ /start demo description cleared!")
    else:
        if 'plan_demo_descs' in settings and target in settings['plan_demo_descs']:
            del settings['plan_demo_descs'][target]
            bot.reply_to(message, f"✅ Demo description for plan <code>{target}</code> cleared!", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ No description found to clear.")
            return
            
    save_settings()

@bot.message_handler(commands=['add_premium_ch'])
def handle_add_premium_ch(message):
    if not is_admin(message.from_user.id):
        return
        
    try:
        # Split by spaces
        args = message.text.split()
        if len(args) < 5:
            bot.reply_to(message, """Usage: <code>/add_premium_ch id Full Name price channel_id [color]</code>
Colors: <code>blue / green / red / default</code>
Example: <code>/add_premium_ch ch1 Randi Ki Dukan 99 -100xxx green</code>""", parse_mode="HTML")
            return

        # ID is always 2nd element
        ch_id = args[1]

        # Last: check if last arg is a color name, otherwise use standard parse
        last_arg = args[-1].lower()
        if last_arg in ('blue', 'green', 'red', 'default', 'primary', 'positive', 'negative', 'none'):
            # has color as last arg → price/channel are args[-3] and args[-2]
            color_raw = args[-1]
            telegram_id = args[-2]
            price = args[-3]
            name = " ".join(args[2:-3])
        else:
            # Standard format: last two = price & channel_id, no color
            telegram_id = args[-1]
            price = args[-2]
            name = " ".join(args[2:-2])
            color_raw = ""

        resolved_color = resolve_color(color_raw)

        _amt = config.parse_amount(price)
        if _amt is None:
            bot.reply_to(message, "❌ <b>Invalid amount!</b>\nSirf number likho, jaise <code>100</code> ya <code>99.50</code>.\n(<code>rs</code>/<code>₹</code> likhna zaroori nahi.)", parse_mode="HTML")
            return
        price = config.clean_amount_str(_amt)

        if 'premium_channels' not in settings:
            settings['premium_channels'] = []

        # Check if id already exists
        for ch in settings['premium_channels']:
            if ch['id'] == ch_id:
                bot.reply_to(message, f"❌ ID {ch_id} already exists.")
                return

        settings['premium_channels'].append({
            "id": ch_id,
            "name": name,
            "amount": price,
            "channel_id": telegram_id,
            "duration": "30 Days",
            "description": "",
            "color": color_raw if resolved_color is not None else ""
        })
        save_settings()
        color_info = color_name_display(color_raw if resolved_color is not None else "")
        bot.reply_to(message, f"✅ Added <b>{name}</b> (₹{price}) to membership list.\n🎨 Button Color: {color_info}", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['remove_premium_ch'])
def handle_remove_premium_ch(message):
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/remove_premium_ch id</code>", parse_mode="HTML")
        return
        
    ch_id = args[1]
    found = False
    for i, ch in enumerate(settings.get('premium_channels', [])):
        if ch['id'] == ch_id:
            settings['premium_channels'].pop(i)
            found = True
            break
            
    if found:
        save_settings()
        bot.reply_to(message, f"✅ Removed channel {ch_id}.")
    else:
        bot.reply_to(message, f"❌ Channel ID {ch_id} not found.")

@bot.message_handler(commands=['list_ch'])
def handle_list_ch(message):
    if not is_admin(message.from_user.id):
        return

    channels = settings.get('premium_channels', [])
    if not channels:
        bot.reply_to(message, "❌ No premium channels added yet. Use /add_premium_ch first.")
        return

    lines = ["<b>📋 PREMIUM CHANNELS (Current Order)</b>\n"]
    lines.append("<code>Pos  ID       Name                       Price  Color</code>")
    lines.append("<code>──── ──────── ────────────────────────── ───── ─────────────</code>")
    for idx, ch in enumerate(channels, 1):
        ch_id = str(ch.get('id', '??'))[:8].ljust(8)
        name = (ch.get('name', 'Unknown') or 'Unknown')[:26].ljust(26)
        price = "₹" + str(ch.get('amount', '0'))
        color = color_name_display(ch.get('color', ''))
        lines.append(f"<code>{idx:>3}.  {ch_id} {name} {price:>5}</code>  {color}")

    lines.append(f"\n<b>Total:</b> {len(channels)} channels")
    lines.append("\n<i>Order change karne ke liye use karein:</i>")
    lines.append("  <code>/reorder_ch ch_id NEW_POSITION</code>")
    lines.append("  <code>/swap_ch ch_id1 ch_id2</code>")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

@bot.message_handler(commands=['reorder_ch'])
def handle_reorder_ch(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(
            message,
            """Usage: <code>/reorder_ch channel_id NEW_POSITION</code>

<b>Example:</b>
  <code>/list_ch</code>                          → Pehle current order dekh lo
  <code>/reorder_ch ch1 5</code>                → ch1 ko 5th position par le jaao
  <code>/reorder_ch ch5 2</code>                → ch5 ko 2nd position par le jaao

<b>Note:</b> Positions <code>1</code> se start hoti hai. Last position = total channels.""",
            parse_mode="HTML"
        )
        return

    ch_id = args[1]
    try:
        new_pos = int(args[2])
    except ValueError:
        bot.reply_to(message, "❌ Position must be a number (1, 2, 3, ...).")
        return

    channels = settings.get('premium_channels', [])
    total = len(channels)

    if new_pos < 1 or new_pos > total:
        bot.reply_to(message, f"❌ Invalid position! Total channels = {total}. Use 1 to {total}.")
        return

    old_idx = None
    moving_ch = None
    for i, ch in enumerate(channels):
        if ch.get('id') == ch_id:
            old_idx = i
            moving_ch = ch
            break

    if old_idx is None or moving_ch is None:
        bot.reply_to(message, f"❌ Channel ID '{ch_id}' not found. Use /list_ch to see IDs.")
        return

    old_pos = old_idx + 1
    if old_pos == new_pos:
        bot.reply_to(message, f"ℹ️ Channel {ch_id} already at position {new_pos}. No change.")
        return

    channels.pop(old_idx)
    channels.insert(new_pos - 1, moving_ch)
    settings['premium_channels'] = channels
    save_settings()

    bot.reply_to(
        message,
        f"✅ <b>Order Updated!</b>\n\n"
        f"📦 <b>{moving_ch.get('name', ch_id)}</b> (<code>{ch_id}</code>)\n"
        f"   Old Position: #{old_pos}\n"
        f"   New Position: #{new_pos}\n\n"
        f"Verify with: <code>/list_ch</code>",
        parse_mode="HTML"
    )

@bot.message_handler(commands=['swap_ch'])
def handle_swap_ch(message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(
            message,
            """Usage: <code>/swap_ch channel_id1 channel_id2</code>

Do channels ki position aapas mein swap/swap kar dega.

<b>Example:</b>
  <code>/list_ch</code>             → Pehle current order dekh lo
  <code>/swap_ch ch1 ch5</code>     → ch1 aur ch5 ki positions swap ho jaayengi""",
            parse_mode="HTML"
        )
        return

    ch_id1 = args[1]
    ch_id2 = args[2]

    if ch_id1 == ch_id2:
        bot.reply_to(message, "❌ Both channel IDs are same. Nothing to swap.")
        return

    channels = settings.get('premium_channels', [])
    idx1 = None
    idx2 = None
    for i, ch in enumerate(channels):
        if ch.get('id') == ch_id1:
            idx1 = i
        if ch.get('id') == ch_id2:
            idx2 = i

    if idx1 is None:
        bot.reply_to(message, f"❌ Channel ID '{ch_id1}' not found.")
        return
    if idx2 is None:
        bot.reply_to(message, f"❌ Channel ID '{ch_id2}' not found.")
        return

    ch1 = channels[idx1]
    ch2 = channels[idx2]
    channels[idx1], channels[idx2] = channels[idx2], channels[idx1]
    settings['premium_channels'] = channels
    save_settings()

    bot.reply_to(
        message,
        f"✅ <b>Channels Swapped!</b>\n\n"
        f"🔀 <b>{ch1.get('name', ch_id1)}</b> (<code>{ch_id1}</code>) ↔️  <b>{ch2.get('name', ch_id2)}</b> (<code>{ch_id2}</code>)\n\n"
        f"Verify with: <code>/list_ch</code>",
        parse_mode="HTML"
    )

@bot.message_handler(commands=['edit_premium_ch'])
def handle_edit_premium_ch(message):
    if not is_admin(message.from_user.id):
        return
        
    try:
        args = message.text.split()
        if len(args) < 4:
            bot.reply_to(message, """Usage: <code>/edit_premium_ch id key New Value</code>
Keys: <code>name, amount, channel_id, duration, description, color</code>
<b>Color values:</b> <code>blue / green / red / default</code>""", parse_mode="HTML")
            return

        ch_id = args[1]
        key = args[2].lower()

        # Everything after key is the new value
        value = " ".join(args[3:])

        allowed_keys = ['name', 'amount', 'channel_id', 'duration', 'description', 'color']
        if key not in allowed_keys:
            bot.reply_to(message, f"❌ Invalid key! Use: {', '.join(allowed_keys)}")
            return

        if key == 'color':
            resolved = resolve_color(value)
            save_value = value if resolved is not None else ""
            found = False
            for ch in settings.get('premium_channels', []):
                if ch['id'] == ch_id:
                    ch['color'] = save_value
                    found = True
                    break
            if found:
                save_settings()
                bot.reply_to(message, f"✅ Updated <b>color</b> for <b>{ch_id}</b> to: {color_name_display(save_value)}", parse_mode="HTML")
            else:
                bot.reply_to(message, f"❌ Channel ID {ch_id} not found.")
            return

        if key == 'amount':
            _amt = config.parse_amount(value)
            if _amt is None:
                bot.reply_to(message, "❌ <b>Invalid amount!</b>\nSirf number likho, jaise <code>100</code> ya <code>99.50</code>.\n(<code>rs</code>/<code>₹</code> likhna zaroori nahi.)", parse_mode="HTML")
                return
            value = config.clean_amount_str(_amt)

        found = False
        for ch in settings.get('premium_channels', []):
            if ch['id'] == ch_id:
                ch[key] = value
                found = True
                break

        if found:
            save_settings()
            bot.reply_to(message, f"✅ Updated <b>{key}</b> for <b>{ch_id}</b> to: <code>{value}</code>", parse_mode="HTML")
        else:
            bot.reply_to(message, f"❌ Channel ID {ch_id} not found.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['set_price'])
def handle_set_price(message):
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "Usage: <code>/set_price single/all amount</code>", parse_mode="HTML")
        return
        
    type_ = args[1].lower()
    amount = args[2]

    _amt = config.parse_amount(amount)
    if _amt is None:
        bot.reply_to(message, "❌ <b>Invalid amount!</b>\nSirf number likho, jaise <code>100</code> ya <code>99.50</code>.\n(<code>rs</code>/<code>₹</code> likhna zaroori nahi.)", parse_mode="HTML")
        return
    amount = config.clean_amount_str(_amt)
    
    if type_ == "single":
        settings['ch_price'] = amount
        bot.reply_to(message, f"✅ Single channel price set to <b>₹{amount}</b>.", parse_mode="HTML")
    elif type_ == "all":
        settings['all_price'] = amount
        bot.reply_to(message, f"✅ All channels price set to <b>₹{amount}</b>.", parse_mode="HTML")
    else:
        bot.reply_to(message, "Invalid type. Use 'single' or 'all'.")
        return
        
    save_settings()

@bot.message_handler(commands=['set_ch'])
def handle_set_ch(message):
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "Usage: <code>/set_ch 1-7 channel_id</code>", parse_mode="HTML")
        return
        
    ch_num = args[1]
    ch_id = args[2]
    
    if ch_num not in [str(i) for i in range(1, 8)]:
        bot.reply_to(message, "Invalid channel number. Use 1-7.")
        return
        
    settings[f'ch{ch_num}_id'] = ch_id
    save_settings()
    bot.reply_to(message, f"✅ Channel {ch_num} ID set to <code>{ch_id}</code>.", parse_mode="HTML")

@bot.message_handler(commands=['imp_to_mongo'])
def handle_imp_to_mongo(message):
    """Import data from a JSON file directly to MongoDB via reply with merging"""
    if not is_admin(message.from_user.id):
        return
    
    if not message.reply_to_message or not message.reply_to_message.document:
        bot.reply_to(message, "❌ <b>Usage:</b> Reply to a JSON file with <code>/imp_to_mongo</code>", parse_mode="HTML")
        return
    
    try:
        status_msg = bot.reply_to(message, "⏳ <b>Processing file...</b>", parse_mode="HTML")
        
        file_info = bot.get_file(message.reply_to_message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # Parse JSON
        imported_data = json.loads(downloaded_file.decode('utf-8'))
        filename = message.reply_to_message.document.file_name.lower()
        
        collection_name = ""
        # Determine collection name based on filename OR content
        if "user" in filename or (isinstance(imported_data, dict) and any(k.isdigit() for k in list(imported_data.keys())[:5]) and "username" in str(list(imported_data.values())[0])):
            collection_name = "users_data"
        elif "setting" in filename or (isinstance(imported_data, dict) and "upi_id" in imported_data):
            collection_name = "settings"
        elif "spam" in filename or (isinstance(imported_data, dict) and any(k.isdigit() for k in list(imported_data.keys())[:5]) and "spam_count" in str(list(imported_data.values())[0])):
            collection_name = "spam_data"
        elif "request" in filename or (isinstance(imported_data, list) and all(isinstance(i, int) for i in imported_data[:5])):
            collection_name = "join_requests"
        elif "pending" in filename or (isinstance(imported_data, dict) and any(k.isdigit() for k in list(imported_data.keys())[:5]) and "screenshot_file_id" in str(list(imported_data.values())[0])):
            collection_name = "pending_verifications"
        elif "link" in filename or (isinstance(imported_data, dict) and any(k.isdigit() for k in list(imported_data.keys())[:5]) and isinstance(list(imported_data.values())[0], (list, str))):
            collection_name = "invite_links"
        elif "start" in filename or (isinstance(imported_data, dict) and "custom_text" in imported_data):
            collection_name = "start_message"

        # 1. Handle Full Export File
        if not collection_name and "users" in imported_data and isinstance(imported_data["users"], dict):
            merged_count = 0
            for key in ["users", "spam_data", "pending", "settings", "join_requests"]:
                val = imported_data.get(key)
                if val:
                    col_map = {"users": "users_data", "pending": "pending_verifications"}
                    target_col = col_map.get(key, key)
                    
                    # Merge Logic
                    current_db_data = db_load(target_col, {} if isinstance(val, dict) else [])
                    if isinstance(val, dict):
                        current_db_data.update(val)
                    elif isinstance(val, list):
                        current_db_data = list(set(current_db_data + val))
                    
                    db_save(target_col, current_db_data)
                    merged_count += 1
            
            bot.edit_message_text(f"✅ <b>Full Export Merged!</b>\nMerged {merged_count} modules into MongoDB successfully.", chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="HTML")
            return

        # 2. Handle Single Module File
        if not collection_name:
            bot.edit_message_text("❌ <b>Error:</b> Could not determine data type from filename. Rename file to <code>users_data.json</code> etc.", chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="HTML")
            return

        # Merge Logic for Single File
        current_db_data = db_load(collection_name, {} if isinstance(imported_data, dict) else [])
        item_count = 0
        
        if isinstance(imported_data, dict):
            current_db_data.update(imported_data)
            item_count = len(imported_data)
            # Special handling for globals
            if collection_name == "settings":
                global settings
                settings.update(imported_data)
            elif collection_name == "users_data":
                global users_data
                users_data.update(imported_data)
        elif isinstance(imported_data, list):
            current_db_data = list(set(current_db_data + imported_data))
            item_count = len(imported_data)
            if collection_name == "join_requests":
                global join_requests
                join_requests = current_db_data

        if db_save(collection_name, current_db_data):
            bot.edit_message_text(f"✅ <b>Import & Merge Successful!</b>\n<b>Collection:</b> <code>{collection_name}</code>\n<b>New Items Merged:</b> {item_count}", chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="HTML")
        else:
            bot.edit_message_text("❌ <b>MongoDB Merge Failed!</b>", chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="HTML")
            
    except Exception as e:
        bot.edit_message_text(f"❌ <b>Error:</b> {str(e)}", chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="HTML")

@bot.message_handler(commands=['migrate_to_mongo'])
def handle_migrate_to_mongo(message):
    """Manually migrate all local JSON data to MongoDB"""
    if not is_admin(message.from_user.id):
        return

    msg = bot.reply_to(message, "⏳ <b>Migration started...</b>", parse_mode="HTML")

    success, result = force_migrate_to_mongodb()

    if success:
        files_str = ", ".join(result) if result else "None"
        bot.edit_message_text(
            f"✅ <b>Migration Successful!</b>\n\n<b>Migrated:</b> {files_str}\n\nData is now synced with MongoDB.",
            chat_id=message.chat.id,
            message_id=msg.message_id,
            parse_mode="HTML"
        )
    else:
        bot.edit_message_text(
            f"❌ <b>Migration Failed:</b> {result}",
            chat_id=message.chat.id,
            message_id=msg.message_id,
            parse_mode="HTML"
        )

# Force join logic removed

# ========== /EXPORTDATA COMMAND ==========
@bot.message_handler(commands=['exportdata'])
def handle_export_data(message):
    """Export all data as JSON"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        status_msg = bot.reply_to(message, "📥 Preparing export...", parse_mode="HTML")
        
        export_data = {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_users": len(users_data),
            "users": users_data,
            "spam_data": spam_data,
            "pending": pending_verifications
        }
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"export_{timestamp}.json"
        filepath = os.path.join(DATA_DIR, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=4)
        
        with open(filepath, 'rb') as f:
            bot.send_document(
                message.chat.id,
                f,
                caption=f"📊 Export: {len(users_data)} users\n⏰ {timestamp}"
            )
        
        bot.delete_message(message.chat.id, status_msg.message_id)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Export failed: {str(e)}")

# ========== /IMPDATA COMMAND ==========
@bot.message_handler(commands=['impdata'])
def handle_impdata(message):
    """Import data from JSON file"""
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin access required!")
        return
    
    if not message.reply_to_message or not message.reply_to_message.document:
        bot.reply_to(message, "❌ Reply to a JSON file with /impdata")
        return
    
    try:
        status_msg = bot.reply_to(message, "📥 Downloading file...", parse_mode="HTML")
        
        file_info = bot.get_file(message.reply_to_message.document.file_id)
        file_name = message.reply_to_message.document.file_name
        
        if not file_name.lower().endswith('.json'):
            bot.edit_message_text("❌ File must be JSON", chat_id=message.chat.id, message_id=status_msg.message_id)
            return
        
        downloaded_file = bot.download_file(file_info.file_path)
        
        temp_path = f"/tmp/{file_name}"
        with open(temp_path, 'wb') as f:
            f.write(downloaded_file)
        
        with open(temp_path, 'r', encoding='utf-8') as f:
            imported_data = json.load(f)
        
        users_before = len(users_data)
        imported_count = 0
        updated_count = 0
        
        # Handle different formats
        if "users" in imported_data:
            data_to_import = imported_data["users"]
        else:
            data_to_import = imported_data
        
        for user_id_str, user_data in data_to_import.items():
            if user_id_str in users_data:
                users_data[user_id_str].update(user_data)
                updated_count += 1
            else:
                users_data[user_id_str] = user_data
                imported_count += 1
        
        save_users_data()
        os.remove(temp_path)
        
        success_msg = f"""
✅ <b>IMPORT COMPLETE!</b>

• Before: {users_before}
• After: {len(users_data)}
• New: {imported_count}
• Updated: {updated_count}
        """
        
        bot.edit_message_text(
            success_msg, 
            chat_id=message.chat.id, 
            message_id=status_msg.message_id, 
            parse_mode="HTML"
        )
        
    except Exception as e:
        bot.edit_message_text(
            f"❌ Error: {str(e)}", 
            chat_id=message.chat.id, 
            message_id=status_msg.message_id
        )

# ========== /BACKUP COMMAND ==========
@bot.message_handler(commands=['backup'])
def handle_backup(message):
    """Create data backup"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        backup_data = {
            "users": users_data,
            "spam": spam_data,
            "pending": pending_verifications,
            "backup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"backup_{timestamp}.json"
        backup_path = os.path.join(DATA_DIR, backup_file)
        
        with open(backup_path, 'w') as f:
            json.dump(backup_data, f, indent=4)
        
        with open(backup_path, 'rb') as f:
            bot.send_document(
                message.chat.id, 
                f, 
                caption=f"📦 Backup: {len(users_data)} users\n⏰ {timestamp}"
            )
        
    except Exception as e:
        bot.reply_to(message, f"❌ Backup failed: {str(e)}")

# ========== /SAVEDATA COMMAND ==========
@bot.message_handler(commands=['savedata'])
def handle_save_data(message):
    """Force save all data"""
    if not is_admin(message.from_user.id):
        return

    try:
        save_all_data()
        bot.reply_to(
            message, 
            f"✅ All data saved!\n👥 Users: {len(users_data)}\n💾 Location: {DATA_DIR}",
            parse_mode="HTML"
        )
    except Exception as e:
        bot.reply_to(message, f"❌ Save failed: {str(e)}")

# ========== /CLEANBACKUPS COMMAND ==========
@bot.message_handler(commands=['cleanbackups'])
def handle_clean_backups(message):
    """Clean old backup files"""
    if not is_admin(message.from_user.id):
        return
    
    try:
        backup_files = [f for f in os.listdir(DATA_DIR) if f.startswith('backup_') and f.endswith('.json')]
        backup_files.sort(key=lambda x: os.path.getmtime(os.path.join(DATA_DIR, x)))
        
        if len(backup_files) <= 5:
            bot.reply_to(message, f"✅ Only {len(backup_files)} backups found (keeping all)")
            return
        
        files_to_delete = backup_files[:-5]
        deleted_count = 0
        deleted_size = 0
        
        for filename in files_to_delete:
            filepath = os.path.join(DATA_DIR, filename)
            file_size = os.path.getsize(filepath)
            os.remove(filepath)
            deleted_count += 1
            deleted_size += file_size
        
        result_msg = f"""
🧹 <b>CLEANUP COMPLETE</b>

📁 Deleted: {deleted_count} files
💾 Freed: {deleted_size//1024} KB
📊 Remaining: {len(backup_files) - deleted_count} backups
        """
        
        bot.reply_to(message, result_msg, parse_mode="HTML")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Cleanup failed: {str(e)}")

# ========== /SETSTARTMSG COMMAND ==========
@bot.message_handler(commands=['setstartmsg'])
def handle_set_start_message(message):
    """Set custom start message"""
    if not is_admin(message.from_user.id):
        return
    
    if not message.reply_to_message:
        bot.reply_to(message, "❌ Reply to a message with /setstartmsg")
        return
    
    replied_msg = message.reply_to_message
    
    start_message_data['text'] = replied_msg.caption or replied_msg.text or ""
    start_message_data['has_media'] = False
    
    if replied_msg.photo:
        start_message_data['media_type'] = 'photo'
        start_message_data['file_id'] = replied_msg.photo[-1].file_id
        start_message_data['has_media'] = True
    elif replied_msg.video:
        start_message_data['media_type'] = 'video'
        start_message_data['file_id'] = replied_msg.video.file_id
        start_message_data['has_media'] = True
    elif replied_msg.document:
        start_message_data['media_type'] = 'document'
        start_message_data['file_id'] = replied_msg.document.file_id
        start_message_data['has_media'] = True

    # Cache the raw bytes so a future token change can auto re-upload (no manual redo)
    if start_message_data.get('has_media'):
        old_mid = start_message_data.get('media_id')
        mid, _ = _cache_replied_media(replied_msg)
        if mid:
            start_message_data['media_id'] = mid
            if old_mid and old_mid != mid:
                _media_cache_delete_safe(old_mid)
        else:
            start_message_data.pop('media_id', None)
    else:
        start_message_data.pop('media_id', None)

    save_start_message()
    bot.reply_to(message, "✅ Start message updated!")

# ========== /GETSTARTMSG COMMAND ==========
@bot.message_handler(commands=['getstartmsg'])
def handle_get_start_message(message):
    """View current start message"""
    if not is_admin(message.from_user.id):
        return
    
    if not start_message_data:
        bot.reply_to(message, "❌ No custom start message set")
        return
    
    media_type = start_message_data.get('media_type', 'text')
    has_media = start_message_data.get('has_media', False)
    text_preview = start_message_data.get('text', '')[:100]
    if len(start_message_data.get('text', '')) > 100:
        text_preview += "..."
    
    info_msg = f"""
<b>📋 CURRENT START MESSAGE</b>

<b>Type:</b> {media_type if has_media else 'Text Only'}
<b>Has Media:</b> {'✅ Yes' if has_media else '❌ No'}
<b>Preview:</b> {text_preview}
    """
    
    bot.reply_to(message, info_msg, parse_mode="HTML")

# ========== /MEDIA_CACHE_NOW COMMAND ==========
@bot.message_handler(commands=['media_cache_now', 'cache_media'])
def handle_media_cache_now(message):
    """Admin: fetch & save EVERY currently-configured media into MongoDB so a future
    token change auto-restores it. Uses the CURRENT (valid) file_ids — no re-upload."""
    if not is_admin(message.from_user.id):
        return
    if not _MEDIA_API_READY:
        bot.reply_to(message, "⚠️ <b>config.py outdated</b> — media cache helpers missing.\nDeploy the full updated <code>config.py</code> and restart.", parse_mode="HTML")
        return

    bot.reply_to(message, "⏳ Media cache ho raha hai... thoda time lagega, please wait. (Command khatam hone par report aayegi.)")

    saved = 0
    failed = 0
    failed_items = []

    # 1) Custom start message media
    try:
        if start_message_data.get('has_media') and start_message_data.get('file_id') \
                and not start_message_data.get('media_id'):
            mid = _cache_file_id(start_message_data.get('file_id'),
                                 start_message_data.get('media_type', 'photo'))
            if mid:
                start_message_data['media_id'] = mid
                save_start_message()
                saved += 1
            else:
                failed += 1
                failed_items.append("Start message media")
    except Exception as e:
        logging.error(f"media_cache_now (start message): {e}")

    # 2) Demo lists (start demos + every plan's demos)
    def _backfill_list(items, label):
        nonlocal saved, failed
        for idx, it in enumerate(items or []):
            if not isinstance(it, dict) or it.get('type') == 'text':
                continue
            if it.get('media_id'):
                continue  # already cached
            fid = it.get('id')
            if not fid:
                failed += 1
                failed_items.append(f"{label} #{idx + 1} (no file_id)")
                continue
            mid = _cache_file_id(fid, it.get('type', 'video'))
            if mid:
                it['media_id'] = mid
                saved += 1
            else:
                failed += 1
                failed_items.append(f"{label} #{idx + 1} (download failed / too large)")

    try:
        _backfill_list(settings.get('start_demo_videos', []), "Start demos")
    except Exception as e:
        logging.error(f"media_cache_now (start demos): {e}")
    try:
        for plan_id, items in (settings.get('plan_demo_videos', {}) or {}).items():
            _backfill_list(items, f"Plan '{plan_id}'")
    except Exception as e:
        logging.error(f"media_cache_now (plan demos): {e}")

    if saved:
        save_settings()

    report = [
        "✅ <b>MEDIA CACHE UPDATE</b>", "",
        f"Newly cached: <b>{saved}</b>",
        f"Failed: <b>{failed}</b>",
        f"Total blobs in cache: <b>{config.media_cache_count()}</b>", "",
    ]
    if failed_items:
        report.append("⚠️ <b>Ye items cache nahi ho paaye</b> (20MB se bade ho sakte hain):")
        report.extend(f"• {x}" for x in failed_items[:20])
        report.append("")
        report.append("Inhe ek baar reply-method se dobara set karo (chhota/compressed file):")
        report.append("<code>/setstartmsg</code> / <code>/set_start_demos</code> / <code>/set_plan_demos &lt;plan&gt;</code>")
        report.append("")
    report.append("Ab <code>/media_check</code> chalao — ✅ dikhna chahiye.")

    bot.send_message(message.chat.id, "\n".join(report), parse_mode="HTML")

# ========== /MEDIA_CHECK COMMAND ==========
@bot.message_handler(commands=['media_check'])
def handle_media_check(message):
    """Admin: verify which media are safely cached in MongoDB (token-change safe)."""
    if not is_admin(message.from_user.id):
        return

    if not _MEDIA_API_READY:
        bot.reply_to(message, "⚠️ <b>config.py outdated</b> — media cache helpers missing.\nDeploy the full updated <code>config.py</code> and restart.", parse_mode="HTML")
        return

    def _cached(item):
        if not isinstance(item, dict):
            return False
        mid = item.get('media_id')
        return bool(mid) and (config.media_cache_get_bytes(mid) is not None)

    def _media_only(items):
        return [x for x in (items or []) if isinstance(x, dict) and x.get('type') != 'text']

    lines = ["<b>🗂 MEDIA CACHE STATUS</b>", ""]

    if start_message_data and start_message_data.get('has_media'):
        ok = _cached(start_message_data)
        lines.append(f"• Start message media: {'✅ cached' if ok else '❌ NOT cached'}")
    else:
        lines.append("• Start message media: ➖ not set")

    sd = _media_only(settings.get('start_demo_videos', []))
    if settings.get('start_demo_videos'):
        lines.append(f"• Start demos: {sum(1 for x in sd if _cached(x))}/{len(sd)} media cached")
    else:
        lines.append("• Start demos: ➖ none")

    pd = settings.get('plan_demo_videos', {})
    if pd:
        for plan_id, items in pd.items():
            mi = _media_only(items)
            lines.append(f"• Plan '{plan_id}' demos: {sum(1 for x in mi if _cached(x))}/{len(mi)} cached")
    else:
        lines.append("• Plan demos: ➖ none")

    lines.append("")
    lines.append(f"Total blobs in cache: <b>{config.media_cache_count()}</b>")
    lines.append("")
    lines.append("✅ = token change ke baad auto re-upload ho jayega")
    lines.append("❌ = ek baar dobara set karna hoga (reply method se)")

    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")

# ========== /CLEARSTARTMSG COMMAND ==========
@bot.message_handler(commands=['clearstartmsg'])
def handle_clear_start_message(message):
    """Clear custom start message"""
    if not is_admin(message.from_user.id):
        return

    global start_message_data
    if start_message_data.get('media_id'):
        _media_cache_delete_safe(start_message_data.get('media_id'))
    start_message_data = {}
    save_start_message()
    bot.reply_to(message, "✅ Custom start message cleared")

# ========== SAFE LONG-HTML SENDER (never breaks a tag while splitting) ==========
def _send_long_html(chat_id, text, reply_to_message=None, limit=3800):
    """Send a possibly-long HTML message, splitting into valid chunks.
    Falls back to plain text if Telegram still rejects the entities."""
    chunks = config.split_html_safe(text, limit=limit) or [text]
    for idx, chunk in enumerate(chunks):
        try:
            if reply_to_message is not None and idx == 0:
                bot.reply_to(reply_to_message, chunk, parse_mode="HTML")
            else:
                bot.send_message(chat_id, chunk, parse_mode="HTML")
        except Exception as e:
            logging.error(f"_send_long_html failed (chunk {idx + 1}/{len(chunks)}): {e}")
            try:
                if reply_to_message is not None and idx == 0:
                    bot.reply_to(reply_to_message, chunk)
                else:
                    bot.send_message(chat_id, chunk)
            except Exception as e2:
                logging.error(f"_send_long_html plain retry failed: {e2}")

# ========== /PENDING COMMAND ==========
@bot.message_handler(commands=['pending'])
def handle_pending(message):
    """Show pending verifications"""
    if not is_admin(message.from_user.id):
        return
    
    if not pending_verifications:
        bot.reply_to(message, "✅ No pending verifications")
        return
    
    text = "<b>⏳ PENDING VERIFICATIONS:</b>\n\n"
    for uid, data in pending_verifications.items():
        plan_id = data['plan']
        plan_name = config.PLANS[plan_id]['name'] if plan_id in config.PLANS else plan_id
        text += f"👤 Name: {data.get('first_name', 'N/A')}\n"
        text += f"👤 ID: <code>{uid}</code>\n"
        text += f"📅 Plan: {plan_name}\n"
        text += f"💰 Amount: ₹{data['amount']}\n"
        text += f"⏰ Time: {data['initiated_at']}\n"
        text += f"📸 Screenshot: {'✅' if 'screenshot_file_id' in data else '❌'}\n"
        text += "───────────────\n"
    
    _send_long_html(message.chat.id, text, reply_to_message=message)

# ========== /CLEAR_ALL_PAYMENTS COMMAND ==========
@bot.message_handler(commands=['clear_all_payments'])
def handle_clear_all_payments(message):
    """Clear ALL payments - pending verifications AND sales records"""
    if not is_admin(message.from_user.id):
        return

    pending_count_before = len(pending_verifications)
    sales_count_before = len(sales_data)

    pending_order_nums = []
    for uid, data in pending_verifications.items():
        onum = data.get('order_number', 'N/A')
        pending_order_nums.append(f"#{onum}")

    sales_order_nums = []
    for sale in sales_data:
        onum = sale.get('order_number', 'N/A')
        sales_order_nums.append(f"#{onum}")

    # Clear all data
    pending_verifications.clear()
    sales_data.clear()
    save_all_data()

    pending_str = ", ".join(pending_order_nums) if pending_order_nums else "None"
    sales_str = ", ".join(sales_order_nums) if sales_order_nums else "None"

    if len(pending_str) > 3000:
        pending_str = pending_str[:3000] + " ..."
    if len(sales_str) > 3000:
        sales_str = sales_str[:3000] + " ..."

    result_text = f"""
🧹 <b>ALL PAYMENTS CLEARED!</b>

⏳ <b>Pending Verifications:</b> {pending_count_before} removed
🧾 <b>Orders:</b> {pending_str}

💰 <b>Sales Records:</b> {sales_count_before} removed
🧾 <b>Orders:</b> {sales_str}

✅ All payment data has been completely cleared.
    """

    bot.reply_to(message, result_text, parse_mode="HTML")

# ========== /CLEAR_PENDING_PAY COMMAND ==========
@bot.message_handler(commands=['clear_pending_pay'])
def handle_clear_pending_pay(message):
    """Clear ONLY pending payments (NOT verified sales)."""
    if not is_admin(message.from_user.id):
        return

    pending_count_before = len(pending_verifications)
    pending_order_nums = []
    for uid, data in pending_verifications.items():
        onum = data.get('order_number', 'N/A')
        pending_order_nums.append(f"#{onum}")

    pending_verifications.clear()
    save_all_data()

    pending_str = ", ".join(pending_order_nums) if pending_order_nums else "None"
    if len(pending_str) > 3000:
        pending_str = pending_str[:3000] + " ..."

    result_text = f"""
🧹 <b>PENDING PAYMENTS CLEARED!</b>

⏳ <b>Pending Removed:</b> {pending_count_before}
🧾 <b>Order #:</b> {pending_str}

✅ Only pending verifications cleared.
✅ Verified sales records (sales_data) <b>SAFE</b> - NOT removed.
    """
    bot.reply_to(message, result_text, parse_mode="HTML")

# ========== /CLEAR_VERIFIED COMMAND ==========
@bot.message_handler(commands=['clear_verified'])
def handle_clear_verified(message):
    """Clear ONLY verified sales records (NOT pending payments)."""
    if not is_admin(message.from_user.id):
        return

    sales_count_before = len(sales_data)
    # Also clear all user invite_links history (verified users links) and premium flags from users_data
    invite_cleared = 0
    premium_flags_cleared = 0

    sales_order_nums = []
    for sale in sales_data:
        onum = sale.get('order_number', 'N/A')
        sales_order_nums.append(f"#{onum}")

    # Clear invite_links history (these are generated on verify)
    if isinstance(invite_links, dict):
        invite_cleared = len(invite_links)
        invite_links.clear()

    # Clear premium flags from users_data (mark as non-premium)
    for uid, udata in users_data.items():
        if udata.get('is_premium'):
            udata['is_premium'] = False
            udata['premium_plan'] = None
            udata['premium_until'] = None
            udata['invite_link'] = None
            premium_flags_cleared += 1

    sales_data.clear()
    save_all_data()

    sales_str = ", ".join(sales_order_nums) if sales_order_nums else "None"
    if len(sales_str) > 3000:
        sales_str = sales_str[:3000] + " ..."

    result_text = f"""
🧹 <b>VERIFIED RECORDS CLEARED!</b>

💰 <b>Sales Records Removed:</b> {sales_count_before}
🧾 <b>Order #:</b> {sales_str}

🔗 <b>Invite Links History Cleared:</b> {invite_cleared} users
👤 <b>Premium Flags Reset:</b> {premium_flags_cleared} users

✅ Only verified sales / links / premium flags removed.
✅ Pending verifications <b>SAFE</b> - NOT removed.
    """
    bot.reply_to(message, result_text, parse_mode="HTML")

# ========== /CLEAR_ORDERS COMMAND ==========
@bot.message_handler(commands=['clear_orders'])
def handle_clear_orders(message):
    """Reset ONLY the order counter (total_orders) so new orders start from 1 again.
    Does NOT touch pending or sales records."""
    if not is_admin(message.from_user.id):
        return

    old_count = settings.get('total_orders', 0)
    settings['total_orders'] = 0
    save_settings()
    save_all_data()

    result_text = f"""
🧹 <b>ORDER COUNTER RESET!</b>

🔢 <b>Old Order Count:</b> {old_count}
🔢 <b>New Order Count:</b> 0

✅ Next order number will be: <b>#1</b>
✅ Pending verifications <b>SAFE</b>
✅ Sales records <b>SAFE</b>
⚠️ <i>Note:</i> Existing pending/sales records still show their old order # (history preserved)
    """
    bot.reply_to(message, result_text, parse_mode="HTML")

# ========== /SET_PROOF_CHANNEL COMMAND ==========
@bot.message_handler(commands=['set_proof_channel'])
def handle_set_proof_channel(message):
    """Set proof channel ID where verified proofs will be sent"""
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/set_proof_channel channel_id</code>\nExample: <code>/set_proof_channel -1001234567890</code>", parse_mode="HTML")
        return

    ch_id = args[1]
    settings['proof_channel_id'] = ch_id
    save_settings()

    bot.reply_to(message, f"✅ Proof Channel ID set to: <code>{ch_id}</code>\n\nJab bhi koi payment verify hoga, uska proof is channel mein automatically send ho jayega with:\n• Name, User ID, Username\n• Plan, Amount, Order Number\n• Date & Payment Screenshot", parse_mode="HTML")

# ========== /HELP COMMAND (FIXED HTML) ==========
@bot.message_handler(commands=['help'])
def handle_help(message):
    """Show help message with ALL commands"""
    if not is_admin(message.from_user.id):
        # ========== USER HELP (ALL commands) ==========
        support_uname = settings.get('support_username', '')
        support_info = f"@{support_uname.lstrip('@')}" if support_uname else "Not Set"
        demo_link = settings.get('demo_channel_link', '') or "Not Set"
        proof_link = settings.get('payment_proof_link', '') or "Not Set"
        support_on = settings.get('support_status', True)
        proof_on = settings.get('payment_proof_status', True)

        user_help = f"""
<b>📋 ALL USER COMMANDS</b>

━━━━━━━━━━━━━━━
<b>🔹 BASIC COMMANDS:</b>
/start         - Start the bot / Show Main Menu
/help          - Show this complete help message
/cancel_order  - ❌ Cancel your pending payment order (anytime)

━━━━━━━━━━━━━━━
<b>🔹 QUICK LINKS:</b>
📢 Demo Channel: {demo_link}
🧾 Payment Proofs: {proof_link} {'<i>(OFF)</i>' if not proof_on else ''}
📞 Contact Support: {support_info} {'<i>(OFF)</i>' if not support_on else ''}

━━━━━━━━━━━━━━━
<b>🔹 HOW TO BUY PREMIUM:</b>
1. Send <code>/start</code> → Click any <b>💎 Premium Channel</b> button
2. Click <b>💳 Buy Now</b> → Scan QR / Pay via UPI
   ⚠️ <b>NOTE:</b> ❌ DO NOT USE GPAY. Use PhonePe / Paytm / BHIM
3. Click <b>✅ Payment Done</b> → Send <b>payment screenshot</b> as PHOTO
4. Admin will verify & send unique join link

━━━━━━━━━━━━━━━
<b>🔹 HOW TO CANCEL ORDER:</b>
• Agar payment nahi kiya: <code>/cancel_order</code> → Order cancel
• Agar payment + screenshot kar diya: <code>/cancel_order</code> → Order cancel + admin ko alert
• Agar payment kar diya but screenshot nahi bheja: <code>/cancel_order</code> → Seedha cancel
• After cancel: <code>/start</code> se naya order bana sakte ho

━━━━━━━━━━━━━━━
<b>💡 TIPS:</b>
• Use Main Menu buttons for quick access
• If pending = screenshot uploaded → wait for admin OR use /cancel_order
• For any issue click <b>📞 Contact Support</b> button
        """
        _send_long_html(message.chat.id, user_help, reply_to_message=message)
        return

    # ========== ADMIN HELP (ALL commands) ==========
    admin_help = """
<b>📋 COMPLETE ADMIN COMMANDS LIST</b>

━━━━━━━━━━━━━━━
<b>🔹 BASIC:</b>
/start   - Start bot / Main Menu
/help    - Show this complete help
/settings - View ALL current settings

━━━━━━━━━━━━━━━
<b>🔹 PAYMENT VERIFICATION:</b>
/pending     - Show ALL pending verifications (list)
/verify [user_id] - Manually verify a pending user
/clear_all_payments - Delete EVERYTHING (pending + sales)
/clear_pending_pay  - ❌ ONLY pending payments (sales = SAFE)
/clear_verified     - ❌ ONLY verified sales + invite links + premium flags (pending = SAFE)
/clear_orders       - 🔄 Reset order counter back to #1 (pending & sales SAFE)

💡 <b>USER CANCEL:</b> Users can cancel their own pending order anytime with <code>/cancel_order</code>
   • If screenshot uploaded → Admin gets alert + ignore-tag on your pending message
   • If no screenshot → Silent cancel (no admin alert needed)

━━━━━━━━━━━━━━━
<b>🔹 QUICK SETTINGS (/set):</b>
Use: <code>/set [key] [value]</code>
<b>Available keys:</b>
  demo_channel  - Demo channel link
  support       - Support username (e.g. @my_support)
  log_channel   - Admin log channel ID
  upi_id        - Your UPI ID
  upi_name      - Your name (shown in QR)
  monthly_name  - Legacy plan name
  monthly_amount - Legacy price
  monthly_channel - Legacy channel ID
  lifetime_name  - Legacy plan name
  lifetime_amount - Legacy price
  lifetime_channel - Legacy channel ID
<b>Example:</b> <code>/set support @custom_support</code>

━━━━━━━━━━━━━━━
<b>🔹 DEMO CHANNEL SETTINGS:</b>
/demo_toggle        - Toggle demo between FREE/PAID
/demo_price [amt]   - Set demo price (e.g. /demo_price 10)
/set_demo_color [color] - 🎨 Demo button color: blue / green / red / default
/set_demo_ch [id]   - Set demo channel ID (for invite link)
/set_demo_link [url] - Set demo channel link (direct URL)

━━━━━━━━━━━━━━━
<b>🔹 PAYMENT PROOF SETTINGS:</b>
/proof_toggle        - Toggle 🧾 Payment Proofs button ON/OFF
/set_proof_link [url] - Set payment proof channel link
/set_proof_channel [id] - Set proof channel ID (auto-post verified proofs there)

━━━━━━━━━━━━━━━
<b>🔹 CONTACT SUPPORT SETTINGS:</b>
/support_toggle      - Toggle 📞 Contact Support button ON/OFF
/set support @uname  - Set support username (via /set)

━━━━━━━━━━━━━━━
<b>🔹 JOIN-REQUEST (ADMIN APPROVAL) MODE:</b>
/join_request_toggle - ON/OFF karo
    → <b>ON:</b> payment verify hone par user ko <b>approval link</b> milega,
      user <b>join request</b> bhejega, aur <b>aap Approve/Decline</b> karenge
      (tabhi user channel me add hoga)
    → <b>OFF:</b> pehle jaise <b>direct one-time link</b> (member_limit=1)

━━━━━━━━━━━━━━━
<b>🔹 AUTO-PAY (pay2) GATEWAY:</b>
/pay2        - 💳 Auto-Pay panel (ON/OFF + accounts), button based
/pay2_toggle - 🔁 Manual UPI/QR ⇄ Gateway payment link
    → <b>ON:</b> user ko gateway ka <b>payment link</b> button milega
    → <b>OFF:</b> purana manual UPI/QR + screenshot flow
/pay2_add    - ➕ Account add (step-by-step: phone → password)
/pay2_list   - 📋 Accounts list (passwords masked)
/pay2_balance- 💰 Har account ka latest balance (fresh gateway login)
/pay2_proxy  - 🌐 Proxy set/change/reset/disable (bot panel se, restart ke bina)
/pay2_clear  - 🗑 Saare accounts clear + mode OFF
    → <b>Phone format:</b> <code>+91-9XXXXXXXXX</code> (country code zaruri)
    → Accounts round-robin rotate hote hain, fail par auto-switch
    → Saare calls India residential proxy se jaate hain (panel se set karo ya PAY2_PROXY env var)
    → Payment Done par: gateway se <b>auto-verify</b>; na ho to UTR (refNo) se verify

━━━━━━━━━━━━━━━
<b>🔹 PREMIUM CHANNEL MANAGEMENT:</b>
/add_premium_ch [id] [Full Name] [price] [channel_id] [color]
    → Add new premium channel (optional color at END)
    → <b>Colors:</b> <code>blue / green / red / default</code>
    → <b>Example:</b> <code>/add_premium_ch ch8 Pro Movies 199 -1001234567890 green</code>

/remove_premium_ch [id]
    → Remove a channel (e.g. /remove_premium_ch ch8)

/edit_premium_ch [id] [key] [New Value]
    → <b>Keys:</b> name, amount, channel_id, duration, description, <b>color</b>
    → <b>Example:</b> <code>/edit_premium_ch ch1 color green</code>
    → <b>Example:</b> <code>/edit_premium_ch ch1 amount 149</code>

/list_ch  - 📋 Show ALL channels in current order (with position numbers + color)

/reorder_ch [id] [NEW_POSITION]  - 🔄 Move a channel to exact position
    → Positions start at 1. Last position = total channels
    → <b>Example (your case):</b>
      <code>/list_ch</code>                     → Pehle current order dekh lo
      <code>/reorder_ch ch1 5</code>           → 1st wale ko 5th position par
      <code>/reorder_ch ch5 2</code>           → 5th wale ko 2nd position par

/swap_ch [id1] [id2]  - 🔀 Swap positions of 2 channels
    → <b>Example:</b> <code>/swap_ch ch1 ch5</code>  → ch1 ↔ ch5 (direct swap)

🎨 <b>BUTTON COLOR SYSTEM:</b>
Telegram officially supports 3 colors + default:
  🟢 <b>green / positive</b>  → Success-style buttons
  🔵 <b>blue / primary</b>    → Standard colored buttons
  🔴 <b>red / negative</b>   → Warning/danger buttons
  ⚪ <b>default / none</b>   → Default white (bot will pick random color)

💡 <b>Random Colors:</b>
All other buttons (Buy Now, Back, Payment Done, etc.) automatically get random colors (blue/green/red/default) every time they are rendered.

/set_price single [amt]  - Legacy single channel price
/set_price all [amt]     - Legacy all channels price
/set_ch [plan] [id]      - Legacy set channel id

━━━━━━━━━━━━━━━
<b>🔹 DEMO VIDEO / CONTENT SETUP:</b>
/set_backup_ch [channel_id] - Backup channel for auto-storing videos

<b>Start Screen Demos (shown after /start):</b>
/set_start_demos [file_id1] [file_id2] ... - Set start demo videos (or reply to media)
/clear_start_demos - Clear all start demos

<b>Plan-Wise Demos (shown inside plan):</b>
/set_plan_demos [plan_id] [file_id1] ... - Set plan demos (or reply to media)
/clear_plan_demos [plan_id] - Clear specific plan demos

<b>Album Captions (Descriptions):</b>
/set_demo_desc [id] [text]  - Set album caption (start_demo or plan_id)
/clear_demo_desc [id]       - Clear album caption

━━━━━━━━━━━━━━━
<b>🔹 CUSTOM START MESSAGE:</b>
/setstartmsg  (reply to any message/text/photo) - Set custom /start message
/getstartmsg   - View current custom start message
/clearstartmsg - Reset to default start message

━━━━━━━━━━━━━━━
<b>🔹 BROADCAST:</b>
/broadcast (reply to a message)
    → Send message to ALL bot users
    → <b>Supports:</b> Text, Photo, Video, Document, GIF, Audio, Voice
    → <b>How:</b> Send message first → Reply to it with <code>/broadcast</code>

━━━━━━━━━━━━━━━
<b>🔹 STATISTICS & REPORTS:</b>
/stats  - Bot statistics (Total users, pending, sales, orders, etc.)
/sales  - Daily / Weekly / Monthly sales report with graphs-like summary

━━━━━━━━━━━━━━━
<b>🔹 ADMIN MANAGEMENT:</b>
/add_admin [user_id]    - Make someone admin
/remove_admin [user_id] - Remove from admins
/settings               - View all settings in one place

━━━━━━━━━━━━━━━
<b>🔹 DATA MANAGEMENT (BACKUP / EXPORT / IMPORT):</b>
/savedata       - Force save ALL data to JSON + MongoDB
/backup         - Create timestamped backup file (all JSONs)
/cleanbackups   - Delete old backup files (keeps only recent)
/exportdata     - Export users data as JSON file

/impdata (reply to JSON file)  - Import users data from backup
/migrate_to_mongo              - Force sync ALL JSON files to MongoDB
/imp_to_mongo (reply to JSON)  - Import specific JSON file to MongoDB

━━━━━━━━━━━━━━━
<b>🔹 OTHER / LEGACY:</b>
/ban       → ❌ Ban system removed
/unban     → ❌ Ban system removed
/banlist   → ❌ Ban system removed

━━━━━━━━━━━━━━━
<b>💡 QUICK START (NEW SETUP):</b>
1. <code>/set support @your_username</code>
2. <code>/set upi_id your@upi</code>
3. <code>/set log_channel -100xxxx</code>
4. Add channels → <code>/add_premium_ch ...</code>
5. <code>/settings</code> - Verify everything
    """

    _send_long_html(message.chat.id, admin_help, reply_to_message=message)

# ========== SILENT HANDLER ==========
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    # Log user messages for debugging if needed
    # logging.debug(f"Received message: {message.text} from {message.from_user.id}")
    pass

# ========== POLLING HELPERS (conflict detection, recovery) ==========
_polling_started = False
_polling_lock = threading.Lock()

def safe_start_polling():
    """Start telegram polling safely — prevents duplicate polling processes in this process,
    handles 'Conflict: terminated by other getUpdates request' (HTTP 409) with backoff,
    and never swallows unrecoverable errors silently."""
    global _polling_started
    with _polling_lock:
        if _polling_started:
            logging.warning("safe_start_polling: polling already started in this process; ignoring duplicate call.")
            return
        _polling_started = True

    _ALLOWED_UPDATES = ["message", "callback_query", "chat_member", "chat_join_request"]
    backoff = 1  # seconds
    max_backoff = 32
    restart_count = 0
    MAX_RESTARTS = 100  # effectively infinite, but bounded

    config.startup_status["bot_initialized"] = True

    while restart_count < MAX_RESTARTS:
        try:
            print("🚀 Starting Telegram long-polling...")
            logging.info("Telegram polling: starting infinity_polling (foreground).")
            # infinity_polling already does retries; but we wrap to handle
            # specific HTTP 409 (conflict) and other unexpected exits.
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                allowed_updates=_ALLOWED_UPDATES,
                restart_on_change=False,  # we control restarts explicitly below
            )
            # If we reach here, infinity_polling returned cleanly → intentional stop
            print("⚠️ Telegram polling returned. Restarting after 3s...")
            time.sleep(3)
            backoff = 1
            continue
        except telebot.apihelper.ApiTelegramException as e:
            err_code = getattr(e, 'error_code', 0)
            desc = getattr(e, 'description', str(e))
            if err_code == 409:
                # HTTP 409 Conflict — another process / another instance is running getUpdates
                print(f"\n❌ TELEGRAM POLLING CONFLICT (HTTP 409):")
                print(f"   {desc}")
                print("   Likely cause: the OLD bot (with old token) or another bot process")
                print("   is still running and calling getUpdates.")
                if _bot_username:
                    print(f"\n   If you just replaced BOT_TOKEN:")
                    print(f"   → OLD bot (@???) with OLD token may still be polling. Stop it first.")
                    print(f"   → Or: the old token's bot simply hasn't stopped yet on Telegram side.")
                print(f"\n   Retrying in {backoff}s (backoff)...")
                logging.error(f"Polling 409 conflict: {desc}. Backoff {backoff}s.")
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                restart_count += 1
                continue
            elif err_code in (401, 404):
                # Token is now invalid mid-flight (revoked / banned) — FATAL
                print(f"\n❌ FATAL: Telegram token REJECTED mid-polling (HTTP {err_code}).")
                print(f"   Message: {desc}")
                print("   Action: Get a NEW bot token from @BotFather, update BOT_TOKEN, restart.")
                logging.critical(f"Polling: token rejected mid-flight HTTP {err_code}: {desc}. Exiting 1.")
                sys.exit(1)
            else:
                logging.error(f"Polling ApiException HTTP {err_code}: {desc}. Backoff {backoff}s.")
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                restart_count += 1
                continue
        except Exception as e:
            err_type = type(e).__name__
            print(f"⚠️ Polling error ({err_type}): {e}")
            print(f"   Restarting polling in {backoff}s...")
            logging.error(f"Polling general error {err_type}: {e}. Backoff {backoff}s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            restart_count += 1
            continue

    # We don't expect to exit here; if we do, something is very wrong
    logging.critical("safe_start_polling: reached MAX_RESTARTS — exiting process.")
    print("❌ MAX POLLING RESTARTS REACHED. Exiting.")
    sys.exit(2)

# ========== START BOT ==========
if __name__ == "__main__":
    print("=" * 60)
    print("🤖 PREMIUM BOT STARTING")
    print("=" * 60)

    # ---------- STATUS REPORT (SAFE: NO SECRETS) ----------
    s = config.startup_status
    print(f"✅ Token validation:     {'PASS (verified)' if s['bot_token_valid'] else 'SKIP (deferred to polling)'}")
    if '_bot_username' in dir() and _bot_username:
        print(f"✅ Bot username:         @{_bot_username}")
    if '_bot_id' in dir() and _bot_id:
        print(f"✅ Bot numeric ID:       {_bot_id}")
    print(f"✅ MongoDB connected:    {'YES' if s['mongo_connected'] else 'NO (JSON fallback)'}")
    if s['mongo_error'] and not s['mongo_connected']:
        print(f"⚠️  MongoDB last error:  {s['mongo_error'][:120]}")
        print("ℹ️  (Your settings and user data remain safe in local data/*.json files.)")
    print(f"✅ Config source:        {'MongoDB' if s['config_loaded_from_mongo'] else 'local JSON' if s['config_loaded_from_json'] else 'defaults (first run)'}")
    admin_list = settings.get('admin_ids', [])
    print(f"✅ Admins:               {len(admin_list)} configured{(' (IDs: ' + ', '.join(admin_list) + ')') if admin_list else ''}")
    print(f"✅ Registered users:     {len(users_data)}")
    print(f"✅ Pending payments:     {len(pending_verifications)}")
    print(f"✅ Verified sales:       {len(sales_data)}")
    print(f"✅ Premium channels:     {len(settings.get('premium_channels', []))}")
    if _MEDIA_API_READY:
        try:
            print(f"✅ Cached media (Mongo): {config.media_cache_count()}  (token-change safe)")
        except Exception:
            print("✅ Cached media (Mongo): (unavailable)")
    else:
        print("⚠️  Cached media:          DISABLED — config.py is OUTDATED (media_* helpers missing).")
        print("⚠️  ACTION: redeploy the FULL updated config.py and delete __pycache__/, then restart.")
    print("=" * 60)
    print("📋 Admin: type /help in Telegram for all commands")
    print("📋 Admin: type /settings in Telegram to view current config")
    print("ℹ️  After token replacement: Existing admins & settings are AUTOMATICALLY restored from DB/JSON.")
    print("ℹ️  Users must send /start to the NEW bot (old chat history does NOT transfer).")
    print("=" * 60)

    # Mark initialization complete in status even if polling start below throws
    try:
        safe_start_polling()
    except KeyboardInterrupt:
        print("\n⏹️  Bot stopped by user (Ctrl+C). Saving data...")
        try:
            save_all_data()
            print("💾 All data saved safely. Bye!")
        except Exception as _e:
            logging.error(f"Final save error: {_e}")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as _e:
        logging.critical(f"Unhandled top-level exception: {type(_e).__name__}: {_e}")
        print(f"❌ FATAL unhandled error: {type(_e).__name__}: {_e}")
        try:
            save_all_data()
        except Exception as _e2:
            logging.error(f"Final save also failed: {_e2}")
        time.sleep(5)
        sys.exit(1)
