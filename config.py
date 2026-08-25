import json
import os
import time
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
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

# Spam protection settings
MAX_SPAM_COUNT = int(os.environ.get("MAX_SPAM_COUNT", "5"))
SPAM_TIME_WINDOW = int(os.environ.get("SPAM_TIME_WINDOW", "10"))
WARNING_MESSAGES = ["⚠️ Please don't spam!", "⚠️ This is your last warning!", "⛔ You are being blocked for spamming!"]
BLOCK_DURATIONS = [300, 900, 1800]  # 5min, 15min, 30min (seconds)

# ============ MONGO DB SETUP ============
db = None
mongo_error = None
if MONGO_URI and "your_mongodb_uri_here" not in MONGO_URI:
    try:
        # Check if password needs quoting (common for Mongo Atlas)
        if "://" in MONGO_URI and "@" in MONGO_URI:
            prefix = MONGO_URI.split("://")[0]
            rest = MONGO_URI.split("://")[1]
            
            # Find the LAST '@' which separates userinfo from host
            last_at_idx = rest.rfind("@")
            if last_at_idx != -1:
                user_pass = rest[:last_at_idx]
                host_rest = rest[last_at_idx+1:]
                
                if ":" in user_pass:
                    user = user_pass.split(":")[0]
                    password = user_pass[len(user)+1:] # Get everything after the first ':'
                    # Quote password only
                    encoded_pass = urllib.parse.quote_plus(password)
                    MONGO_URI = f"{prefix}://{user}:{encoded_pass}@{host_rest}"

        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = client['PremiumBot']
        # Test connection immediately
        client.admin.command('ping')
        print("✅ MongoDB Connected Successfully!")
    except Exception as e:
        db = None
        mongo_error = str(e)
        print(f"⚠️ MongoDB Connection Failed: {e}")
        print("💡 Bot will continue using local JSON files.")
        logging.error(f"MongoDB Connection Error: {e}")
else:
    if "your_mongodb_uri_here" in MONGO_URI:
        print("ℹ️ MongoDB URI not set. Using local JSON files.")
    else:
        print("ℹ️ No MONGO_URI found. Using local JSON files.")

def get_collection(name):
    if db is not None:
        return db[name]
    return None

def db_save(collection_name, data):
    col = get_collection(collection_name)
    if col is not None:
        try:
            # We store everything as one document with _id: "main_data" for simple key-value structures
            # or as separate documents for users. To keep it compatible with current dict structure:
            col.replace_one({"_id": "main_data"}, {"_id": "main_data", "data": data}, upsert=True)
            return True
        except Exception as e:
            logging.error(f"DB Save Error ({collection_name}): {e}")
    return False

def db_load(collection_name, default=None):
    col = get_collection(collection_name)
    if col is not None:
        try:
            doc = col.find_one({"_id": "main_data"})
            if doc:
                return doc.get("data", default)
        except Exception as e:
            logging.error(f"DB Load Error ({collection_name}): {e}")
    return None # Return None if not found in DB

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
def load_json_file(filepath, default=None):
    """Load from MongoDB first, then fallback to JSON file"""
    if default is None:
        default = {}
    
    filename = os.path.basename(filepath).replace(".json", "")
    
    # 1. Try loading from MongoDB
    db_data = db_load(filename)
    if db_data is not None:
        return db_data
        
    # 2. If not in DB, try loading from local JSON
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                data = json.load(f)
                # Migration: Save this local data to MongoDB
                db_save(filename, data)
                return data
        else:
            # Create default file and save to DB
            with open(filepath, 'w') as f:
                json.dump(default, f)
            db_save(filename, default)
            return default
    except Exception as e:
        logging.error(f"Error loading {filepath}: {e}")
        return default

def save_json_file(filepath, data):
    """Save to both local JSON and MongoDB"""
    filename = os.path.basename(filepath).replace(".json", "")
    
    # 1. Save to MongoDB
    db_save(filename, data)
    
    # 2. Save to local JSON (as backup)
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        logging.error(f"Error saving {filepath}: {e}")
        return False

# ============ LOAD ALL DATA ============
users_data = load_json_file(USERS_DATA_FILE, {})
spam_data = load_json_file(SPAM_DATA_FILE, {})
start_message_data = load_json_file(START_MESSAGE_FILE, {})
pending_verifications = load_json_file(PENDING_VERIF_FILE, {})
join_requests = load_json_file(JOIN_REQUESTS_FILE, []) # List of user IDs
sales_data = load_json_file(SALES_DATA_FILE, []) # List of sale records

# FIXED: Load invite_links and ensure all values are LISTS
invite_links = load_json_file(INVITE_LINKS_FILE, {})
for user_id in invite_links:
    if not isinstance(invite_links[user_id], list):
        # If it's not a list, convert to list or create new list
        if isinstance(invite_links[user_id], dict):
            # Old format - convert dict to list with one item
            old_data = invite_links[user_id]
            invite_links[user_id] = [old_data]
        else:
            # Unknown format - create empty list
            invite_links[user_id] = []

settings = load_json_file(SETTINGS_FILE, DEFAULT_SETTINGS)

# Migration: Ensure premium_channels exists in settings
if 'premium_channels' not in settings:
    settings['premium_channels'] = DEFAULT_SETTINGS['premium_channels']
    save_json_file(SETTINGS_FILE, settings)

# Migration: Ensure every premium channel has description field
for ch in settings.get('premium_channels', []):
    if 'description' not in ch:
        ch['description'] = ""
    if 'color' not in ch:
        ch['color'] = ""
save_json_file(SETTINGS_FILE, settings)

# Migration: Ensure proof_channel_id exists
if 'proof_channel_id' not in settings:
    settings['proof_channel_id'] = ""
    save_json_file(SETTINGS_FILE, settings)

# Migration: Ensure support_username and support_status exist
if 'support_username' not in settings:
    settings['support_username'] = ""
    save_json_file(SETTINGS_FILE, settings)
if 'support_status' not in settings:
    settings['support_status'] = True
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
