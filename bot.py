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
from verif import init_verification, get_random_button_color, make_colored_button

# Initialize bot
# Use config.BOT_TOKEN to avoid NameError if star import hasn't processed it yet
_token = getattr(config, 'BOT_TOKEN', os.getenv("BOT_TOKEN", ""))

if not _token or ":" not in _token:
    print("\n❌ ERROR: BOT_TOKEN is missing or invalid in .env file!")
    print("Please make sure you have created a .env file with BOT_TOKEN=your_token_here")
    sys.exit(1)

bot = telebot.TeleBot(_token, parse_mode="HTML")

# ========== BUTTON COLOR FIX (to_dict patch for Telegram Bot API Mini Apps colors) ==========
# pyTelegramBotAPI doesn't include button_color in to_dict() by default
_original_ikb_to_dict = types.InlineKeyboardButton.to_dict
def _patched_ikb_to_dict(self):
    json_dict = _original_ikb_to_dict(self)
    # Add button_color if set (supports: primary, positive, negative, default=None)
    extra_color = getattr(self, 'button_color', None)
    if extra_color is not None:
        json_dict['color'] = extra_color
    # Also try alternate name for compatibility
    if not json_dict.get('color'):
        extra_color2 = getattr(self, 'color', None)
        if extra_color2 is not None:
            json_dict['color'] = extra_color2
    return json_dict
types.InlineKeyboardButton.to_dict = _patched_ikb_to_dict

# Initialize verification system
verif = init_verification(bot)

BUTTON_COLORS_BOT = [None, "primary", "positive", "negative"]

def rnd_color():
    """Shortcut for random button color"""
    return random.choice(BUTTON_COLORS_BOT)

def make_button(text, **kwargs):
    """Create InlineKeyboardButton with random color if no url (color passed in constructor)"""
    if 'url' not in kwargs and 'button_color' not in kwargs:
        color = rnd_color()
        if color is not None:
            kwargs['button_color'] = color
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

# Notify Admin about MongoDB status on startup
def notify_mongo_status():
    try:
        if config.mongo_error:
            admin_ids = settings.get('admin_ids', [])
            if admin_ids:
                msg = f"⚠️ <b>MongoDB Connection Failed!</b>\n\nError: <code>{config.mongo_error}</code>\n\n💡 Bot is running on <b>Local JSON</b> mode. Your data will be saved locally in the <code>data/</code> folder."
                for aid in admin_ids:
                    try:
                        bot.send_message(aid, msg, parse_mode="HTML")
                    except:
                        continue
    except Exception as e:
        logging.error(f"Notify Mongo Status Error: {e}")

# Run notification in a separate thread to not block startup
threading.Thread(target=notify_mongo_status, daemon=True).start()

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

def send_demo_videos(chat_id, video_list, caption=""):
    """Send demo items (media or text) and schedule deletion"""
    if not video_list:
        return
    
    chat_id_str = str(chat_id)
    if chat_id_str not in user_demo_messages:
        user_demo_messages[chat_id_str] = []
    
    current_media_group = []
    for item in video_list:
        try:
            # Check if it's a text item
            if isinstance(item, dict) and item.get('type') == 'text':
                # First send any pending media group
                if current_media_group:
                    for i in range(0, len(current_media_group), 10):
                        chunk = current_media_group[i:i + 10]
                        try:
                            msgs = bot.send_media_group(chat_id, chunk)
                            for m in msgs:
                                user_demo_messages[chat_id_str].append(m.message_id)
                                delete_message_after_delay(chat_id, m.message_id, delay=600)
                        except Exception as e:
                            logging.error(f"Error sending media group chunk: {e}")
                    current_media_group = []
                
                # Send the text message
                txt_msg = bot.send_message(chat_id, item.get('text', ''), parse_mode="HTML", disable_web_page_preview=False)
                user_demo_messages[chat_id_str].append(txt_msg.message_id)
                delete_message_after_delay(chat_id, txt_msg.message_id, delay=600)
                
            elif isinstance(item, dict):
                fid = item.get('id')
                ftype = item.get('type', 'video')
                if ftype == 'photo':
                    current_media_group.append(types.InputMediaPhoto(fid, parse_mode="HTML"))
                elif ftype == 'video':
                    current_media_group.append(types.InputMediaVideo(fid, parse_mode="HTML"))
            else:
                # Fallback for old string format
                current_media_group.append(types.InputMediaVideo(item, parse_mode="HTML"))
        except Exception as e:
            logging.error(f"Error preparing demo item: {e}")
            continue
    
    # Send any remaining media group
    if current_media_group:
        for i in range(0, len(current_media_group), 10):
            chunk = current_media_group[i:i + 10]
            try:
                msgs = bot.send_media_group(chat_id, chunk)
                for m in msgs:
                    user_demo_messages[chat_id_str].append(m.message_id)
                    delete_message_after_delay(chat_id, m.message_id, delay=600)
            except Exception as e:
                logging.error(f"Error sending media group chunk: {e}")
                # Notify admin if type mismatch
                if "can't use file of type" in str(e):
                    admin_ids = settings.get('admin_ids', [])
                    if admin_ids:
                        error_msg = "⚠️ <b>Demo Media Error!</b>\n\nSome files have wrong types. Please clear and reset using reply method."
                        for aid in admin_ids:
                            try: bot.send_message(aid, error_msg, parse_mode="HTML")
                            except: pass

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
            # Format amount to 2 decimal places for UPI standard
            formatted_amount = "{:.2f}".format(float(amount))
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

                if media_type == 'photo' and file_id:
                    m = bot.send_photo(
                        chat_id,
                        photo=file_id,
                        caption=text,
                        reply_markup=verif.main_menu_keyboard(),
                        parse_mode="HTML"
                    )
                    track_msg(chat_id, m.message_id)
                elif media_type == 'video' and file_id:
                    m = bot.send_video(
                        chat_id,
                        video=file_id,
                        caption=text,
                        reply_markup=verif.main_menu_keyboard(),
                        parse_mode="HTML"
                    )
                    track_msg(chat_id, m.message_id)
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
        else:
            msg = f"""
⛔ <b>PAYMENT PENDING!</b>

Aapka Order #{order_num} pehle se hi pending hai.
Jab tak is payment ko verify/reject nahi kar dete, aap naya payment create nahi kar sakte.

<b>To aage badhne ke liye:</b>
✅ Payment complete karke screenshot upload karein
⏳ Ya phir admin se purane payment ko cancel karne ke liye contact karein
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

    plan = None
    if plan_type in config.PLANS:
        plan = config.PLANS[plan_type]
    else:
        channels = settings.get("premium_channels", [])
        plan = next((ch for ch in channels if ch['id'] == plan_type), None)

    if not plan:
        bot.answer_callback_query(call.id, "❌ Plan not found!", show_alert=True)
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

    qr_image = premium_bot.generate_qr_code(settings['upi_id'], plan['amount'], settings['upi_name'])

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

⏳ <i>This QR will auto-delete in 10 minutes.</i>
    """

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    btn1 = make_button("✅ Payment Done", callback_data="payment_done")
    btn2 = make_button("🔙 Back", callback_data=f"plan_{plan_type}")
    keyboard.add(btn1, btn2)

    try:
        bot.delete_message(chat_id, msg_id)
    except: pass
    if msg_id in user_all_messages.get(str(chat_id), []):
        user_all_messages[str(chat_id)].remove(msg_id)

    if qr_image:
        sent_msg = bot.send_photo(chat_id, photo=qr_image, caption=caption, reply_markup=keyboard, parse_mode="HTML")
        track_msg(chat_id, sent_msg.message_id)
        delete_message_after_delay(chat_id, sent_msg.message_id, 600, send_timeout_msg=True)
    else:
        sent_msg = bot.send_message(chat_id, caption, reply_markup=keyboard, parse_mode="HTML")
        track_msg(chat_id, sent_msg.message_id)
        delete_message_after_delay(chat_id, sent_msg.message_id, 600, send_timeout_msg=True)

    bot.answer_callback_query(call.id)

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

    screenshot_msg = verif.ask_for_screenshot(chat_id, user_id, pending_verifications[str(user_id)]['plan'])
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

# ========== /SETTINGS COMMAND (FIXED HTML) ==========
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
        ch_info += f"• {ch.get('id', '??')}: {ch.get('name', 'Unknown')} (₹{ch.get('amount', '0')}) - <code>{ch_id_display}</code>{desc_display}\n"

    text = f"""
<b>⚙️ CURRENT SETTINGS</b>

<b>👑 Admins:</b> <code>{', '.join(settings.get('admin_ids', []))}</code>
<b>📢 Demo Link:</b> {settings.get('demo_channel_link', 'Not Set')}
<b>🆔 Demo ID:</b> <code>{settings.get('demo_channel_id', 'Not Set')}</code>
<b>💰 Demo Price:</b> ₹{settings.get('demo_amount', '10')}
<b>🔄 Demo Status:</b> {'PAID' if settings.get('demo_paid_status', False) else 'FREE'}

<b>📋 Log Channel:</b> {settings.get('log_channel', 'Not Set')}
<b>🧾 Proof Channel ID:</b> <code>{settings.get('proof_channel_id', 'Not Set')}</code>
<b>🧾 Proof Channel Link:</b> {settings.get('payment_proof_link', 'Not Set')}
<b>🧾 Proof Status:</b> {'ON' if settings.get('payment_proof_status', False) else 'OFF'}
<b>🛡️ Force Join:</b> {'ON' if settings.get('force_join_status', True) else 'OFF'}
<b>🤖 Auto-Accept:</b> {'ON' if settings.get('auto_accept_requests', False) else 'OFF'}

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
            amount = float(sale['amount'])
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
def handle_add_admin(message):
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/add_admin user_id</code>", parse_mode="HTML")
        return
        
    new_admin = args[1].strip()
    if 'admin_ids' not in settings:
        settings['admin_ids'] = []
        
    if new_admin not in settings['admin_ids']:
        settings['admin_ids'].append(new_admin)
        save_settings()
        bot.reply_to(message, f"✅ User <code>{new_admin}</code> added to admins.", parse_mode="HTML")
    else:
        bot.reply_to(message, "User is already an admin.")

@bot.message_handler(commands=['remove_admin'])
def handle_remove_admin(message):
    if not is_admin(message.from_user.id):
        return
        
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: <code>/remove_admin user_id</code>", parse_mode="HTML")
        return
        
    admin_to_remove = args[1].strip()
    
    # Don't allow removing the last admin
    if len(settings.get('admin_ids', [])) <= 1:
        bot.reply_to(message, "❌ Cannot remove the last admin.")
        return
        
    if admin_to_remove in settings.get('admin_ids', []):
        settings['admin_ids'].remove(admin_to_remove)
        save_settings()
        bot.reply_to(message, f"✅ User <code>{admin_to_remove}</code> removed from admins.", parse_mode="HTML")
    else:
        bot.reply_to(message, "User is not in admin list.")

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
            bot.reply_to(message, "Usage: <code>/add_premium_ch id Full Name price channel_id</code>\nExample: <code>/add_premium_ch ch1 Randi Ki Dukan 99 -100xxx</code>", parse_mode="HTML")
            return
            
        # ID is always 2nd element
        ch_id = args[1]
        
        # Last two elements are always price and channel_id
        telegram_id = args[-1]
        price = args[-2]
        
        # Everything in between is the name
        name = " ".join(args[2:-2])
        
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
            "description": ""
        })
        save_settings()
        bot.reply_to(message, f"✅ Added <b>{name}</b> (₹{price}) to membership list.", parse_mode="HTML")
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

@bot.message_handler(commands=['edit_premium_ch'])
def handle_edit_premium_ch(message):
    if not is_admin(message.from_user.id):
        return
        
    try:
        args = message.text.split()
        if len(args) < 4:
            bot.reply_to(message, "Usage: <code>/edit_premium_ch id key New Value</code>\nKeys: <code>name, amount, channel_id, duration</code>", parse_mode="HTML")
            return
            
        ch_id = args[1]
        key = args[2].lower()
        
        # Everything after key is the new value
        value = " ".join(args[3:])
        
        allowed_keys = ['name', 'amount', 'channel_id', 'duration', 'description']
        if key not in allowed_keys:
            bot.reply_to(message, f"❌ Invalid key! Use: {', '.join(allowed_keys)}")
            return
            
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

# ========== /CLEARSTARTMSG COMMAND ==========
@bot.message_handler(commands=['clearstartmsg'])
def handle_clear_start_message(message):
    """Clear custom start message"""
    if not is_admin(message.from_user.id):
        return

    global start_message_data
    start_message_data = {}
    save_start_message()
    bot.reply_to(message, "✅ Custom start message cleared")

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
    
    # Split if too long
    if len(text) > 4000:
        parts = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for part in parts:
            bot.send_message(message.chat.id, part, parse_mode="HTML")
    else:
        bot.reply_to(message, text, parse_mode="HTML")

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
/start  - Start the bot / Show Main Menu
/help   - Show this complete help message

━━━━━━━━━━━━━━━
<b>🔹 QUICK LINKS:</b>
📢 Demo Channel: {demo_link}
🧾 Payment Proofs: {proof_link} {'<i>(OFF)</i>' if not proof_on else ''}
📞 Contact Support: {support_info} {'<i>(OFF)</i>' if not support_on else ''}

━━━━━━━━━━━━━━━
<b>🔹 HOW TO BUY PREMIUM:</b>
1. Send <code>/start</code> → Click any <b>💎 Premium Channel</b> button
2. Click <b>💳 Buy Now</b> → Scan QR / Pay via UPI
3. Send <b>payment screenshot</b> as photo
4. Admin will verify & send unique join link

━━━━━━━━━━━━━━━
<b>💡 TIPS:</b>
• Use Main Menu buttons for quick access
• If you have pending payment, wait for admin verification
• For any issue click <b>📞 Contact Support</b> button
        """
        # Split if too long
        if len(user_help) > 4000:
            parts = [user_help[i:i+4000] for i in range(0, len(user_help), 4000)]
            for part in parts:
                bot.send_message(message.chat.id, part, parse_mode="HTML")
        else:
            bot.reply_to(message, user_help, parse_mode="HTML")
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
/set_demo_ch [id]   - Set demo channel ID (for invite link)
/set_demo_link [url] - Set demo channel link (direct URL)

━━━━━━━━━━━━━━━
<b>🔹 PAYMENT PROOF SETTINGS:</b>
/proof_toggle        - Toggle 🧾 Payment Proofs button ON/OFF
/set_proof_link [url] - Set payment proof channel link
/set_proof_channel [id] - Set proof channel ID (auto-post verified proofs)

━━━━━━━━━━━━━━━
<b>🔹 CONTACT SUPPORT SETTINGS:</b>
/support_toggle      - Toggle 📞 Contact Support button ON/OFF
/set support @uname  - Set support username (via /set)

━━━━━━━━━━━━━━━
<b>🔹 PREMIUM CHANNEL MANAGEMENT:</b>
/add_premium_ch [id] [Full Name] [price] [channel_id]
    → Add new premium channel
    → <b>Example:</b> <code>/add_premium_ch ch8 Pro Movies 199 -1001234567890</code>

/remove_premium_ch [id]
    → Remove a channel (e.g. /remove_premium_ch ch8)

/edit_premium_ch [id] [key] [New Value]
    → <b>Keys:</b> name, amount, channel_id, duration, description
    → <b>Example:</b> <code>/edit_premium_ch ch1 amount 149</code>

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

    # Split if too long (Telegram limit ~4096)
    if len(admin_help) > 4000:
        parts = [admin_help[i:i+4000] for i in range(0, len(admin_help), 4000)]
        for part in parts:
            bot.send_message(message.chat.id, part, parse_mode="HTML")
    else:
        bot.reply_to(message, admin_help, parse_mode="HTML")

# ========== SILENT HANDLER ==========
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    # Log user messages for debugging if needed
    # logging.debug(f"Received message: {message.text} from {message.from_user.id}")
    pass

# ========== START BOT ==========
if __name__ == "__main__":
    print("=" * 60)
    print("🤖 PREMIUM BOT - TWO CHANNELS + DYNAMIC CONFIG")
    print("=" * 60)
    
    print(f"✅ Bot Token: {BOT_TOKEN[:15]}...")
    print(f"✅ Admin IDs: {', '.join(settings.get('admin_ids', []))}")
    print(f"✅ Users Loaded: {len(users_data)}")
    print(f"✅ Pending: {len(pending_verifications)}")
    print(f"✅ Single Channel Price: ₹{settings.get('ch_price', '99')}")
    print(f"✅ All Channels Price: ₹{settings.get('all_price', '299')}")
    print("=" * 60)
    print("📋 Type /help for all commands")
    print("📋 Type /settings to view/edit config")
    print("=" * 60)
    
    try:
        print("🚀 Bot is starting polling...")
        bot.infinity_polling(
            timeout=60, 
            long_polling_timeout=60,
            allowed_updates=["message", "callback_query", "chat_member", "chat_join_request"]
        )
    except Exception as e:
        print(f"Bot Error: {e}")
        time.sleep(10)
        sys.exit(1)
