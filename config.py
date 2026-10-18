import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, PyMongoError
import urllib.parse

# Load environment variables from .env file
load_dotenv(override=True)

# ============ CONFIG FROM ENVIRONMENT ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip('"')
ADMIN_IDS_ENV = [id.strip() for id in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).split(",") if id.strip()]
ADMIN_ID = ADMIN_IDS_ENV[0] if ADMIN_IDS_ENV else ""
LOG_CHANNEL = os.getenv("LOG_CHANNEL", "").strip('"')
DEMO_CHANNEL_LINK = os.getenv("DEMO_CHANNEL_LINK", "").strip('"')
UPI_ID = os.getenv("UPI_ID", "").strip('"')
UPI_NAME = os.getenv("UPI_NAME", "").strip('"')
MONGO_URI = os.getenv("MONGO_URI", "").strip('"')

# ============ SAFE LOGGING HELPERS ============
# NEVER log secrets
_SENSITIVE_KEYS = {"token", "password", "secret", "api_key", "uri", "credentials", "private_key"}

def _is_safe_log_value(key: str, value) -> bool:
    key_lower = str(key).lower()
    return not any(s in key_lower for s in _SENSITIVE_KEYS)

def _safe_repr(obj, max_len=80):
    """Redact sensitive values in logs"""
    if isinstance(obj, dict):
        safe = {}
        for k, v in obj.items():
            if _is_safe_log_value(k, v):
                safe[k] = v
            else:
                safe[k] = "[REDACTED]"
        return str(safe)[:max_len]
    s = str(obj)
    return s if len(s) <= max_len else s[:max_len] + "..."

def redact_secrets_from_text(text, max_len=500):
    """Remove any accidentally embedded connection strings / passwords / tokens from error text.
    Generic utility — safe to call on any error message you want to print/forward."""
    if not text:
        return text
    s = str(text)
    # MongoDB URIs (standard + srv)
    s = re.sub(r"(mongodb(?:\+srv)?://[^\s'\"<>]+)", "[REDACTED_MONGO_URI]", s, flags=re.IGNORECASE)
    # Generic password=xxx patterns
    s = re.sub(r"(password[=:]\s*[^\s,;\"'<>]+)", "password=[REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"(secret[=:]\s*[^\s,;\"'<>]+)", "secret=[REDACTED]", s, flags=re.IGNORECASE)
    s = re.sub(r"(api[_-]?key[=:]\s*[^\s,;\"'<>]+)", "api_key=[REDACTED]", s, flags=re.IGNORECASE)
    # Telegram bot token pattern (digits:secret)
    s = re.sub(r"\b\d{6,12}:[A-Za-z0-9_\-]{20,60}\b", "[REDACTED_BOT_TOKEN]", s)
    return s[:max_len]

# Spam protection settings
MAX_SPAM_COUNT = int(os.environ.get("MAX_SPAM_COUNT", "5"))
SPAM_TIME_WINDOW = int(os.environ.get("SPAM_TIME_WINDOW", "10"))
WARNING_MESSAGES = ["⚠️ Please don't spam!", "⚠️ This is your last warning!", "⛔ You are being blocked for spamming!"]
BLOCK_DURATIONS = [300, 900, 1800]  # 5min, 15min, 30min (seconds)

# ============ STARTUP STATUS TRACKING ============
startup_status = {
    "mongo_connected": False,
    "mongo_error": None,
    "mongo_attempts": 0,
    "bot_token_valid": False,
    "config_loaded_from_mongo": False,
    "config_loaded_from_json": False,
    "bot_initialized": False,
}

# ============ MONGO DB SETUP (with retry & backoff) ============
client = None
db = None
mongo_error = None
_MONGO_MAX_RETRIES = 3
_MONGO_BACKOFF_BASE = 2  # seconds

def _build_safe_mongo_uri(raw_uri):
    """Quote password in MongoDB URI (common for Mongo Atlas with special chars).
    Never prints/logs the actual URI."""
    if not raw_uri:
        return raw_uri
    try:
        if "://" in raw_uri and "@" in raw_uri:
            prefix = raw_uri.split("://")[0]
            rest = raw_uri.split("://")[1]
            last_at_idx = rest.rfind("@")
            if last_at_idx != -1:
                user_pass = rest[:last_at_idx]
                host_rest = rest[last_at_idx+1:]
                if ":" in user_pass:
                    user = user_pass.split(":")[0]
                    password = user_pass[len(user)+1:]
                    encoded_pass = urllib.parse.quote_plus(password)
                    return f"{prefix}://{user}:{encoded_pass}@{host_rest}"
    except Exception:
        pass
    return raw_uri

def _connect_mongodb():
    """Attempt MongoDB connection with retry & exponential backoff.
    Sets db / mongo_error / startup_status globals."""
    global client, db, mongo_error
    global startup_status

    if not MONGO_URI or "your_mongodb_uri_here" in MONGO_URI:
        reason = "not_configured" if not MONGO_URI else "placeholder_uri"
        mongo_error = f"MONGO_URI {reason}"
        print("ℹ️ MongoDB URI not configured. Using local JSON files as storage.")
        logging.info(f"MongoDB: {reason} - JSON fallback active.")
        return False

    safe_uri = _build_safe_mongo_uri(MONGO_URI)

    for attempt in range(1, _MONGO_MAX_RETRIES + 1):
        startup_status["mongo_attempts"] = attempt
        try:
            client = MongoClient(safe_uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=10000)
            db = client['PremiumBot']
            # Immediately test
            client.admin.command('ping')
            startup_status["mongo_connected"] = True
            startup_status["mongo_error"] = None
            mongo_error = None
            print(f"✅ MongoDB Connected Successfully! (attempt {attempt}/{_MONGO_MAX_RETRIES})")
            logging.info(f"MongoDB: connection OK on attempt {attempt}")
            return True
        except (ConnectionFailure, PyMongoError) as e:
            mongo_error = f"{type(e).__name__}: {e}"
            startup_status["mongo_error"] = mongo_error
            db = None
            client = None
            wait_s = _MONGO_BACKOFF_BASE ** attempt
            print(f"⚠️ MongoDB attempt {attempt}/{_MONGO_MAX_RETRIES} failed. Retrying in {wait_s}s...")
            logging.warning(f"MongoDB connect attempt {attempt} failed: {type(e).__name__}. Backoff {wait_s}s.")
            time.sleep(wait_s)
        except Exception as e:
            mongo_error = f"{type(e).__name__}: {e}"
            startup_status["mongo_error"] = mongo_error
            db = None
            client = None
            logging.error(f"MongoDB unexpected error on attempt {attempt}: {type(e).__name__}")
            break

    # All retries exhausted
    print(f"❌ MongoDB connection FAILED after {_MONGO_MAX_RETRIES} attempts.")
    print("💡 Bot will continue using local JSON files (no persistent DB).")
    print("💡 Fix: Check MONGO_URI, network, firewall, then restart. Your saved data is NOT lost.")
    logging.error(f"MongoDB unreachable. JSON fallback mode active. Last error: {mongo_error}")
    return False

# Actually connect now
_connect_mongodb()

def get_collection(name):
    if db is not None:
        try:
            # Re-verify connection liveness lazily
            client.admin.command('ping')
            return db[name]
        except Exception as e:
            logging.warning(f"MongoDB get_collection({name}) lost connection: {type(e).__name__}")
            return None
    return None

def db_save(collection_name, data):
    """Save data to MongoDB (best-effort). Returns True if successful.
    NEVER overwrites valid DB with empty None/{} — callers check data first."""
    # Safety: never persist empty dict/list as an "upgrade" over valid non-empty data —
    # the dual-write already keeps JSON around as source of truth, but we still guard.
    col = get_collection(collection_name)
    if col is None:
        return False
    try:
        # Atomic upsert of the single main_data document
        col.replace_one({"_id": "main_data"}, {"_id": "main_data", "data": data}, upsert=True)
        return True
    except (ConnectionFailure, PyMongoError) as e:
        logging.warning(f"DB Save Warning ({collection_name}): {type(e).__name__} — data still safe in local JSON.")
        return False
    except Exception as e:
        logging.error(f"DB Save Error ({collection_name}): {type(e).__name__}: {e}")
        return False

def db_load(collection_name, default=None):
    """Load data from MongoDB. Returns None if: not in DB, DB unreachable, error.
    Never raises — caller decides if JSON fallback is needed."""
    col = get_collection(collection_name)
    if col is None:
        return None
    try:
        doc = col.find_one({"_id": "main_data"})
        if doc:
            return doc.get("data", default)
        return None  # Explicit: doc not found → caller should use JSON/default
    except (ConnectionFailure, PyMongoError) as e:
        logging.warning(f"DB Load Warning ({collection_name}): {type(e).__name__} — using local JSON fallback.")
        return None
    except Exception as e:
        logging.error(f"DB Load Error ({collection_name}): {type(e).__name__}: {e}")
        return None

def force_migrate_to_mongodb():
    """Force upload all local JSON files to MongoDB"""
    if db is None:
        return False, "MongoDB not connected"
        
    files = {
        "users_data": USERS_DATA_FILE,
        "spam_data": SPAM_DATA_FILE,
        "start_message": START_MESSAGE_FILE,
        "pending_verifications": PENDING_VERIF_FILE,
        "invite_links": INVITE_LINKS_FILE,
        "settings": SETTINGS_FILE,
        "join_requests": JOIN_REQUESTS_FILE,
        "sales_data": SALES_DATA_FILE
    }
    
    migrated = []
    for name, path in files.items():
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data:
                        db_save(name, data)
                        migrated.append(name)
            except Exception as e:
                logging.error(f"Migration error for {name}: {e}")
                
    return True, migrated

# ============ DATA DIRECTORY ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"✅ Created data directory: {DATA_DIR}")

# Data files
USERS_DATA_FILE = os.path.join(DATA_DIR, "users_data.json")
SPAM_DATA_FILE = os.path.join(DATA_DIR, "spam_data.json")
START_MESSAGE_FILE = os.path.join(DATA_DIR, "start_message.json")
PENDING_VERIF_FILE = os.path.join(DATA_DIR, "pending_verifications.json")
INVITE_LINKS_FILE = os.path.join(DATA_DIR, "invite_links.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
JOIN_REQUESTS_FILE = os.path.join(DATA_DIR, "join_requests.json")
SALES_DATA_FILE = os.path.join(DATA_DIR, "sales_data.json")

# ============ MEDIA CACHE (token-change safe) ============
# Telegram file_id is BOT-SPECIFIC: media uploaded to the OLD bot cannot be sent
# by a NEW bot. To survive a BOT_TOKEN change we ALSO store the actual file bytes
# (GridFS inside the existing 'PremiumBot' DB; local disk as mirror) so the new bot
# can re-upload them and obtain a fresh, valid file_id automatically.
MEDIA_DIR = os.path.join(DATA_DIR, "media")
try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
except Exception:
    pass
MEDIA_MANIFEST_FILE = os.path.join(MEDIA_DIR, "manifest.json")
MEDIA_MAX_BYTES = 20 * 1024 * 1024  # Telegram getFile download limit (20 MB)

def media_new_id():
    """Generate a stable, token-independent media identifier."""
    return uuid.uuid4().hex

def _get_media_bucket():
    """GridFS bucket on the existing DB, or None if Mongo is unavailable."""
    if db is None:
        return None
    try:
        client.admin.command('ping')
    except Exception:
        return None
    try:
        import gridfs
        return gridfs.GridFS(db, collection="media_files")
    except Exception as e:
        logging.warning(f"GridFS unavailable: {type(e).__name__}")
        return None

def _media_local_manifest_read():
    try:
        with open(MEDIA_MANIFEST_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _media_local_manifest_write(m):
    try:
        tmp = MEDIA_MANIFEST_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(m, f)
        os.replace(tmp, MEDIA_MANIFEST_FILE)
    except Exception:
        pass

def media_cache_save(media_id, data, file_type, file_name="", mime=""):
    """Persist raw media bytes + metadata. GridFS primary, local disk mirror.
    Returns an info dict on success, None on failure. Never raises."""
    if not media_id or not data:
        return None
    if len(data) > MEDIA_MAX_BYTES:
        logging.warning(f"media_cache_save: file too large ({len(data)} bytes > {MEDIA_MAX_BYTES}); not cached.")
        return None
    info = {
        "type": file_type or "",
        "filename": file_name or "",
        "mime": mime or "",
        "size": len(data),
    }
    stored = False
    bucket = _get_media_bucket()
    if bucket is not None:
        try:
            try:
                bucket.delete(media_id)
            except Exception:
                pass
            bucket.put(data, _id=media_id, filename=(file_name or media_id),
                       content_type=(mime or "application/octet-stream"))
            info["storage"] = "gridfs"
            stored = True
        except Exception as e:
            logging.warning(f"media_cache_save: GridFS write failed: {type(e).__name__}")
    try:
        with open(os.path.join(MEDIA_DIR, media_id), 'wb') as f:
            f.write(data)
        if not stored:
            info["storage"] = "local"
        stored = True
    except Exception as e:
        logging.warning(f"media_cache_save: local write failed: {type(e).__name__}")
    if not stored:
        return None
    col = get_collection("media_cache")
    if col is not None:
        try:
            doc = {"_id": media_id}
            doc.update(info)
            col.replace_one({"_id": media_id}, doc, upsert=True)
        except Exception:
            pass
    m = _media_local_manifest_read()
    m[media_id] = info
    _media_local_manifest_write(m)
    return info

def media_cache_get_info(media_id):
    if not media_id:
        return None
    col = get_collection("media_cache")
    if col is not None:
        try:
            d = col.find_one({"_id": media_id})
            if d:
                d.pop("_id", None)
                return d
        except Exception:
            pass
    return _media_local_manifest_read().get(media_id)

def media_cache_get_bytes(media_id):
    """Return raw bytes for a media_id, or None. Never raises."""
    if not media_id:
        return None
    bucket = _get_media_bucket()
    if bucket is not None:
        try:
            gf = bucket.get(media_id)
            if gf is not None:
                return gf.read()
        except Exception:
            pass
    try:
        with open(os.path.join(MEDIA_DIR, media_id), 'rb') as f:
            return f.read()
    except Exception:
        return None

def media_cache_delete(media_id):
    """Remove a cached media blob + manifest entry. Never raises."""
    if not media_id:
        return
    bucket = _get_media_bucket()
    if bucket is not None:
        try:
            bucket.delete(media_id)
        except Exception:
            pass
    try:
        os.remove(os.path.join(MEDIA_DIR, media_id))
    except Exception:
        pass
    col = get_collection("media_cache")
    if col is not None:
        try:
            col.delete_one({"_id": media_id})
        except Exception:
            pass
    m = _media_local_manifest_read()
    if media_id in m:
        m.pop(media_id, None)
        _media_local_manifest_write(m)

def media_cache_count():
    """Number of media blobs currently cached (GridFS preferred, local fallback)."""
    bucket = _get_media_bucket()
    if bucket is not None:
        try:
            return len(list(bucket.find({})))
        except Exception:
            pass
    try:
        return len(_media_local_manifest_read())
    except Exception:
        return 0

# ============ DEFAULT SETTINGS ============
DEFAULT_SETTINGS = {
    "admin_ids": ADMIN_IDS_ENV,
    "log_channel": LOG_CHANNEL,
    "backup_channel": "",
    "proof_channel_id": "",
    "demo_channel_link": DEMO_CHANNEL_LINK,
    "upi_id": UPI_ID,
    "upi_name": UPI_NAME,
    "demo_channel_id": "",
    "demo_paid_status": False,
    "demo_amount": "10",
    "demo_color": "",
    "payment_proof_link": "",
    "payment_proof_status": True,
    "support_username": "",
    "support_status": True,
    "how_to_buy_url": "",
    "total_orders": 0,
    "start_demo_videos": [],
    "plan_demo_videos": {},
    "start_demo_desc": "",
    "plan_demo_descs": {},
    "premium_channels": [
        {"id": "ch1", "name": "Channel 1", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "ch2", "name": "Channel 2", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "ch3", "name": "Channel 3", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "ch4", "name": "Channel 4", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "ch5", "name": "Channel 5", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "ch6", "name": "Channel 6", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "ch7", "name": "Channel 7", "amount": "99", "channel_id": "", "duration": "30 Days", "description": "", "color": ""},
        {"id": "all", "name": "All Channels", "amount": "299", "channel_ids": [], "duration": "30 Days", "description": "", "color": ""}
    ]
}

# ============ DATA LOAD/SAVE FUNCTIONS ============
def _data_is_empty(data):
    """Check if data represents empty defaults (empty dict/list/None) — used to avoid wiping valid data."""
    if data is None:
        return True
    if isinstance(data, (dict, list, str, tuple, set)):
        return len(data) == 0
    return False

def load_json_file(filepath, default=None):
    """Load persistent data SAFELY:
    Priority: MongoDB (if reachable AND has non-empty data) → local JSON (if exists) → default.
    NEVER overwrites the winning source with empty data from another.
    Idempotent — calling repeatedly does not mutate records.
    """
    global startup_status
    if default is None:
        default = {}
    # Guard: we only allow default to match the expected type of the file
    filename = os.path.basename(filepath).replace(".json", "")

    # 1. Try loading from MongoDB first
    db_data = db_load(filename)
    used_db = False
    used_json = False
    data_from_db = db_data is not None and not _data_is_empty(db_data)

    if data_from_db:
        # DB had real data — use it, write-through to local JSON so JSON stays in sync
        # (Always sync, not just when JSON is missing/empty — ensures the offline copy
        # matches DB in case hosting ephemeral filesystem resets between deploys.)
        result = db_data
        used_db = True
        startup_status["config_loaded_from_mongo"] = True
        try:
            # Always overwrite JSON with authoritative DB data (only triggered when DB
            # is NON-empty, so JSON never gets replaced with empty defaults).
            _write_json_raw(filepath, result)
        except Exception as e:
            logging.warning(f"load_json_file({filename}): write-through JSON sync failed: {e}")
    else:
        # 2. DB unavailable/empty. Try loading from local JSON (authoritative fallback)
        json_data = _read_json_raw(filepath)
        if json_data is not None and not _data_is_empty(json_data):
            result = json_data
            used_json = True
            startup_status["config_loaded_from_json"] = True
            # Try to push JSON → MongoDB now that we have valid data, but only if DB is reachable
            # AND DB doesn't already have something (we already know DB was empty/unreachable above)
            if db_data is None and not _data_is_empty(json_data):
                try:
                    db_save(filename, json_data)
                except Exception as e:
                    logging.info(f"load_json_file({filename}): could not upload JSON to Mongo yet: {type(e).__name__}")
        else:
            # 3. Fresh setup: create defaults in BOTH JSON (always for offline availability) and MongoDB (best-effort)
            result = default
            _write_json_raw(filepath, default)
            db_save(filename, default)

    logging.info(
        f"Loaded '{filename}': db={used_db}, json={used_json}, "
        f"type={type(result).__name__}, size={len(result) if hasattr(result, '__len__') else 'N/A'}"
    )
    return result

def _read_json_raw(filepath):
    """Read JSON file returning dict/list or None if missing/invalid. Never creates files."""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"JSON read corrupt/missing for {os.path.basename(filepath)}: {type(e).__name__}")
    return None

def _write_json_raw(filepath, data):
    """Write JSON file atomically via temp rename to avoid truncation on crash."""
    directory = os.path.dirname(filepath)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    tmp_path = filepath + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, filepath)
    return True

def save_json_file(filepath, data):
    """Save to both local JSON (atomic) and MongoDB (best-effort).
    Safe guard: if data passed in is empty defaults but local JSON has real content,
    we still save (callers are expected to pass the live state) — but
    we do NOT save if MongoDB is down AND JSON write also fails (double failure
    would risk silent loss; we log loudly in that case)."""
    filename = os.path.basename(filepath).replace(".json", "")

    # 1. Save to local JSON first (atomic) — this is the crash-safe copy
    json_ok = False
    try:
        _write_json_raw(filepath, data)
        json_ok = True
    except Exception as e:
        logging.critical(f"CRITICAL: JSON save FAILED for {filename}: {type(e).__name__}: {e}")

    # 2. Save to MongoDB (best-effort) — don't fail call if DB is temporarily down
    mongo_ok = db_save(filename, data)

    return json_ok or mongo_ok

# ============ SAFE SETTINGS MERGE ============
def _deep_merge_settings(current, defaults):
    """Recursively merge defaults into current settings.
    ONLY adds missing keys from defaults; NEVER overwrites an existing non-empty
    value in current. Returns the merged dict (mutates current in place).
    Idempotent: calling multiple times with same arguments is a no-op.
    """
    if not isinstance(current, dict) or not isinstance(defaults, dict):
        return current

    changed = False
    for key, default_val in defaults.items():
        if key not in current:
            current[key] = default_val
            changed = True
            continue
        cur_val = current[key]
        # Special: if current is empty list/dict/str AND default is non-empty, fill it
        if isinstance(default_val, list) and isinstance(cur_val, list) and len(cur_val) == 0 and len(default_val) > 0:
            # Only auto-fill lists for well-known skeleton keys like premium_channels
            if key in ("premium_channels",):
                current[key] = list(default_val)
                changed = True
        elif isinstance(default_val, dict) and isinstance(cur_val, dict):
            # Recurse for nested dicts (e.g., plan_demo_videos, plan_demo_descs)
            nested_changed = _deep_merge_settings(cur_val, default_val) or False
            if nested_changed:
                changed = True
    return changed

def _run_idempotent_migrations(settings_obj):
    """Run all structural migrations for settings.
    Each migration is idempotent (safe to run every restart).
    Returns True if any change was made (caller should save)."""
    changed = False

    # S1: Ensure premium_channels list exists with description+color on every item
    if 'premium_channels' not in settings_obj or not isinstance(settings_obj['premium_channels'], list):
        settings_obj['premium_channels'] = DEFAULT_SETTINGS['premium_channels']
        changed = True
    for ch in settings_obj.get('premium_channels', []):
        if isinstance(ch, dict):
            for field, default in (('description', ""), ('color', ""), ('duration', "30 Days")):
                if field not in ch:
                    ch[field] = default
                    changed = True

    # S2: Proof channel
    for key, default in (('proof_channel_id', ""), ('payment_proof_status', True),
                         ('payment_proof_link', ""), ('force_join_status', True),
                         ('auto_accept_requests', False), ('backup_channel', ""),
                         ('log_channel', ""), ('demo_channel_link', ""),
                         ('demo_channel_id', ""), ('demo_paid_status', False),
                         ('demo_amount', "10"), ('demo_color', ""),
                         ('support_username', ""), ('support_status', True),
                         ('how_to_buy_url', ""), ('total_orders', 0),
                         ('upi_id', ""), ('upi_name', ""),
                         ('start_demo_videos', []), ('plan_demo_videos', {}),
                         ('start_demo_desc', ""), ('plan_demo_descs', {})):
        if key not in settings_obj:
            settings_obj[key] = default
            changed = True

    # S3: Sync admin_ids from env ONLY to ADD missing admins (never removes DB admins)
    env_admin_ids = [str(a) for a in ADMIN_IDS_ENV if a]
    current_admins = [str(a) for a in settings_obj.get('admin_ids', [])]
    admin_added = False
    for eid in env_admin_ids:
        if eid not in current_admins:
            current_admins.append(eid)
            admin_added = True
    if admin_added or 'admin_ids' not in settings_obj:
        settings_obj['admin_ids'] = current_admins
        changed = True

    return changed

# ============ LOAD ALL DATA ============
users_data = load_json_file(USERS_DATA_FILE, {})
spam_data = load_json_file(SPAM_DATA_FILE, {})
start_message_data = load_json_file(START_MESSAGE_FILE, {})
pending_verifications = load_json_file(PENDING_VERIF_FILE, {})
join_requests = load_json_file(JOIN_REQUESTS_FILE, [])
sales_data = load_json_file(SALES_DATA_FILE, [])

# FIXED: Load invite_links and ensure all values are LISTS
invite_links = load_json_file(INVITE_LINKS_FILE, {})
_invite_changed = False
for user_id in list(invite_links.keys()):
    if not isinstance(invite_links[user_id], list):
        if isinstance(invite_links[user_id], dict):
            old_data = invite_links[user_id]
            invite_links[user_id] = [old_data]
        else:
            invite_links[user_id] = []
        _invite_changed = True
if _invite_changed:
    save_json_file(INVITE_LINKS_FILE, invite_links)

# ============ LOAD & MERGE SETTINGS (SAFE) ============
# First load raw settings from JSON/Mongo with safe default
_raw_settings = load_json_file(SETTINGS_FILE, {})

# Apply deep defaults merge (never overwrites existing non-empty keys)
_deep_merge_settings(_raw_settings, DEFAULT_SETTINGS)

# Apply idempotent structural migrations
_structural_changed = _run_idempotent_migrations(_raw_settings)

settings = _raw_settings

if _structural_changed:
    save_json_file(SETTINGS_FILE, settings)

# Update PLANS with settings
def get_plans():
    plans = {}
    
    # Standard channels from settings
    for ch in settings.get("premium_channels", []):
        # Ensure duration exists to avoid KeyErrors
        if 'duration' not in ch:
            ch['duration'] = "30 Days"
        plans[ch['id']] = ch
        
    # Add demo plan separately
    plans["demo"] = {
        "name": "Premium Demo Link",
        "amount": settings.get("demo_amount", "10"),
        "duration": "Lifetime",
        "channel_id": settings.get("demo_channel_id", "")
    }
    return plans

PLANS = get_plans()

# Individual save functions
def save_users_data():
    """Save users data"""
    save_json_file(USERS_DATA_FILE, users_data)

def save_spam_data():
    """Save spam data"""
    save_json_file(SPAM_DATA_FILE, spam_data)

def save_start_message():
    """Save start message"""
    save_json_file(START_MESSAGE_FILE, start_message_data)

def save_settings():
    """Save settings"""
    save_json_file(SETTINGS_FILE, settings)
    # Update PLANS with new settings
    global PLANS
    PLANS = get_plans()

def save_all_data():
    """Save all data at once"""
    save_json_file(USERS_DATA_FILE, users_data)
    save_json_file(SPAM_DATA_FILE, spam_data)
    save_json_file(START_MESSAGE_FILE, start_message_data)
    save_json_file(PENDING_VERIF_FILE, pending_verifications)
    save_json_file(INVITE_LINKS_FILE, invite_links)
    save_json_file(SETTINGS_FILE, settings)
    save_json_file(JOIN_REQUESTS_FILE, join_requests)
    save_json_file(SALES_DATA_FILE, sales_data)
    print("💾 All data saved")

# Initialize spam data for existing users
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
        print(f"✅ Initialized spam data for {initialized} users")

initialize_spam_data()
