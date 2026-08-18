import telebot
from telebot import types
import time
import threading
import random
from datetime import datetime, timedelta
import logging

# Import config FIRST
import config
from config import *

logger = logging.getLogger(__name__)

# ========== BUTTON COLOR FIX (to_dict patch - same in bot.py) ==========
_original_ikb_to_dict_verif = types.InlineKeyboardButton.to_dict
def _patched_ikb_to_dict_verif(self):
    json_dict = _original_ikb_to_dict_verif(self)
    extra_color = getattr(self, 'button_color', None)
    if extra_color is not None:
        json_dict['color'] = extra_color
    if not json_dict.get('color'):
        extra_color2 = getattr(self, 'color', None)
        if extra_color2 is not None:
            json_dict['color'] = extra_color2
    return json_dict
types.InlineKeyboardButton.to_dict = _patched_ikb_to_dict_verif

BUTTON_COLORS = [None, "primary", "positive", "negative"]

def get_random_button_color():
    """Return a random button color for Telegram's colored buttons:
    None = default (white/light)
    primary = blue
    positive = green
    negative = red
    """
    return random.choice(BUTTON_COLORS)

def make_colored_button(text, **kwargs):
    """Create InlineKeyboardButton with random color (passed in constructor for proper serialization).
    Only callback buttons get color (not URL buttons).
    """
    if 'url' not in kwargs:
        color = get_random_button_color()
        if color is not None:
            kwargs['button_color'] = color
    return types.InlineKeyboardButton(text, **kwargs)

class VerificationSystem:
    def __init__(self, bot):
        self.bot = bot
        self.pending = pending_verifications
    
    def save_pending(self):
        """Save pending verifications"""
        # save_json_file(PENDING_VERIF_FILE, self.pending) # Removed for batch saving
        pass
    
    def create_invite_link(self, user_id, plan_type):
        """Create TWO unique invite links per channel for specific plan.
        Each link has member_limit=1 (single-use) so user gets 2 separate links.
        """
        global invite_links
        try:
            plan = config.PLANS[plan_type]
            INVITES_PER_CHANNEL = 2  # 2 links per channel

            # Special case for "all" channels
            if plan_type == "all":
                channel_ids = plan.get('channel_ids', [])
                valid_links = []
                for idx, cid in enumerate(channel_ids, 1):
                    if not cid: continue
                    for link_num in range(1, INVITES_PER_CHANNEL + 1):
                        try:
                            invite = self.bot.create_chat_invite_link(
                                chat_id=int(cid),
                                member_limit=1,  # each link = 1 use
                                expire_date=datetime.now() + timedelta(days=365)
                            )
                            valid_links.append(f"Channel {idx} - Link {link_num}: {invite.invite_link}")
                        except Exception as e:
                            logger.error(f"Error creating invite for {cid} (link #{link_num}): {e}")

                if not valid_links:
                    return "Error: No channel IDs configured for All Channels. Contact admin."

                # Store links
                user_id_str = str(user_id)
                if user_id_str not in invite_links:
                    invite_links[user_id_str] = []

                link_data = {
                    'plan': plan_type,
                    'links': valid_links,
                    'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                invite_links[user_id_str].append(link_data)
                # save_json_file(INVITE_LINKS_FILE, invite_links) # Removed for batch saving

                return "\n".join(valid_links)

            # Standard case for single channel
            channel_id = plan.get('channel_id', '')
            if not channel_id:
                # Special fallback for demo if ID is missing but link exists
                if plan_type == "demo" and settings.get('demo_channel_link'):
                    demo_link = settings.get('demo_channel_link')
                    return f"Link 1: {demo_link}\nLink 2: {demo_link}"
                return "Error: Channel ID not configured. Contact admin."

            # Create TWO invite links for single channel
            valid_links = []
            for link_num in range(1, INVITES_PER_CHANNEL + 1):
                try:
                    invite = self.bot.create_chat_invite_link(
                        chat_id=int(channel_id),
                        member_limit=1,  # each link = 1 use
                        expire_date=datetime.now() + timedelta(days=365)
                    )
                    valid_links.append(f"Link {link_num}: {invite.invite_link}")
                except Exception as e:
                    logger.error(f"Error creating invite #{link_num} for {channel_id}: {e}")

            if not valid_links:
                return "Error: Failed to create invite links. Contact admin."

            # Store links
            user_id_str = str(user_id)
            if user_id_str not in invite_links:
                invite_links[user_id_str] = []

            link_data = {
                'plan': plan_type,
                'links': valid_links,
                'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            invite_links[user_id_str].append(link_data)
            # save_json_file(INVITE_LINKS_FILE, invite_links) # Removed for batch saving

            return "\n".join(valid_links)
        except Exception as e:
            logger.error(f"Invite Link Error: {e}")
            return f"Error creating link: {str(e)}"

    def plan_selection_keyboard(self):
        """Dynamic Membership keyboard with random button colors"""
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        
        channels = settings.get("premium_channels", [])
        for ch in channels:
            btn = make_colored_button(
                f"🔗 {ch['name']} - ₹{ch['amount']}",
                callback_data=f"plan_{ch['id']}"
            )
            keyboard.add(btn)

        back_btn = make_colored_button("⬅️ Back to Menu", callback_data="main_menu")
        keyboard.add(back_btn)
        return keyboard

    def main_menu_keyboard(self):
        """Main menu with premium channels shown directly + random button colors + Contact Support"""
        keyboard = types.InlineKeyboardMarkup(row_width=1)

        # 1. Free Video Channel
        is_paid = settings.get('demo_paid_status', False)
        demo_amount = settings.get('demo_amount', '10')
        demo_link = settings.get('demo_channel_link', '')

        if is_paid:
            btn = make_colored_button(f"📢 Free Video Channel (₹{demo_amount})", callback_data="plan_demo")
            keyboard.add(btn)
        elif demo_link:
            btn = types.InlineKeyboardButton("📢 Free Video Channel", url=demo_link)
            keyboard.add(btn)
        else:
            btn = make_colored_button("📢 Free Video Channel (Not Set)", callback_data="demo_not_set")
            keyboard.add(btn)

        # 2. Premium Channels (Directly shown)
        channels = settings.get("premium_channels", [])
        for ch in channels:
            btn = make_colored_button(
                f"💎 {ch['name']} - ₹{ch['amount']}",
                callback_data=f"plan_{ch['id']}"
            )
            keyboard.add(btn)

        # 3. Payment Proof Channel
        if settings.get("payment_proof_status", True):
            proof_link = settings.get('payment_proof_link', '')
            if proof_link:
                btn = types.InlineKeyboardButton("🧾 Payment Proofs", url=proof_link)
                keyboard.add(btn)
            else:
                btn = make_colored_button("🧾 Payment Proofs (Not Set)", callback_data="proof_not_set")
                keyboard.add(btn)

        # 4. Contact Support (only if status ON)
        if settings.get("support_status", True):
            support_uname = settings.get('support_username', '')
            if support_uname:
                uname_clean = support_uname.lstrip('@')
                support_url = f"https://t.me/{uname_clean}"
                btn = types.InlineKeyboardButton("📞 Contact Support", url=support_url)
                keyboard.add(btn)
            else:
                btn = make_colored_button("📞 Contact Support (Not Set)", callback_data="support_not_set")
                keyboard.add(btn)

        return keyboard
    
    def ask_for_screenshot(self, chat_id, user_id, plan_type):
        """Ask user to send payment screenshot"""
        plan = config.PLANS[plan_type]
        pending_data = self.pending.get(str(user_id), {})
        order_num = pending_data.get('order_number', 'N/A')

        # Mark the time when user was asked for screenshot (for auto-cleanup)
        pending_data['screenshot_requested_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_pending()

        msg = self.bot.send_message(
            chat_id,
            f"""
<b>📸 SEND PAYMENT SCREENSHOT (Order #{order_num})</b>

<b>Plan Selected:</b> {plan['name']}
<b>Amount to Pay:</b> ₹{plan['amount']}
<b>UPI ID:</b> <code>{settings['upi_id']}</code>

✅ <b>Payment Done!</b>

Now please send the <b>payment screenshot</b> for verification.

<b>Instructions:</b>
1. Take screenshot of UPI payment
2. Send it here as <b>PHOTO / IMAGE</b> (NOT as a File/Document)
3. Admin will verify within few minutes
4. You'll receive unique join link after verification

⏳ <i>Please wait for admin verification...</i>
            """,
            parse_mode="HTML"
        )
        return msg
    
    def handle_screenshot(self, message):
        """Handle payment screenshot from user"""
        user_id = str(message.from_user.id)
        
        # Check if user has pending verification
        if user_id not in self.pending:
            return False
        
        pending_data = self.pending[user_id]
        
        # RESTRICTION: If screenshot already uploaded and not yet verified/rejected, block new one
        if pending_data.get('screenshot_file_id'):
            order_num = pending_data.get('order_number', 'N/A')
            self.bot.reply_to(
                message,
                f"""
⛔ <b>ALREADY SUBMITTED!</b>

Aapka payment screenshot (Order #{order_num}) pehle se hi submit ho chuka hai.
Jab tak admin purane payment ko verify/reject nahi kar dete, aap naya screenshot upload nahi kar sakte.

⏳ <i>Please wait for admin verification...</i>
                """,
                parse_mode="HTML"
            )
            return True
        
        if not message.photo:
            self.bot.reply_to(
                message,
                "❌ Please send a PHOTO (screenshot) of your payment."
            )
            return True
        
        plan_type = pending_data['plan']
        plan = config.PLANS[plan_type]
        
        # Get the largest photo
        photo = message.photo[-1]
        file_id = photo.file_id
        
        # Store screenshot info
        pending_data['screenshot_file_id'] = file_id
        pending_data['screenshot_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pending_data['screenshot_msg_id'] = message.message_id
        self.save_pending()
        
        # Create verification buttons for admin
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        verify_btn = types.InlineKeyboardButton(
            "✅ Verify Payment",
            callback_data=f"verify_{user_id}",
            button_color="positive"
        )
        reject_btn = types.InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject_{user_id}",
            button_color="negative"
        )
        keyboard.add(verify_btn, reject_btn)
        
        # Forward screenshot to admin log channel
        order_num = pending_data.get('order_number', 'N/A')
        caption = f"""
📸 <b>PAYMENT SCREENSHOT RECEIVED (Order #{order_num})</b>

👤 User: @{message.from_user.username or 'N/A'}
🆔 User ID: <code>{user_id}</code>
📅 Plan: {plan['name']}
💰 Amount: ₹{plan['amount']}
⏰ Time: {pending_data['screenshot_time']}

<b>Verify payment and send join link:</b>
        """
        
        try:
            # Send screenshot to log channel or fallback to first Admin ID
            target_chat = settings.get('log_channel')
            
            # Check if bot can send to target_chat
            try:
                sent_msg = self.bot.send_photo(
                    target_chat,
                    photo=file_id,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
            except Exception as e:
                # If log channel fails, try primary admin
                if "Forbidden" in str(e) or "chat not found" in str(e).lower():
                    primary_admin = settings['admin_ids'][0] if settings.get('admin_ids') else None
                    if primary_admin and str(primary_admin) != str(target_chat):
                        sent_msg = self.bot.send_photo(
                            primary_admin,
                            photo=file_id,
                            caption=caption + "\n\n⚠️ <i>(Log channel forbidden/not found, sent to admin)</i>",
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                    else:
                        raise e
                else:
                    raise e
            
            # Store admin message ID
            pending_data['admin_msg_id'] = sent_msg.message_id
            pending_data['admin_chat_id'] = sent_msg.chat.id
            self.save_pending()
            
            # Notify user
            self.bot.reply_to(
                message,
                f"""
✅ <b>Screenshot received!</b>

Admin will verify your payment soon.
You'll receive unique join link within few minutes.

⏳ <i>Thank you for your patience!</i>
                """,
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Error forwarding screenshot: {e}")
            self.bot.reply_to(
                message,
                f"❌ Error sending screenshot. Please try again later."
            )
        
        return True
    
    def verify_payment(self, user_id, admin_id):
        """Verify payment and send unique invite link"""
        user_id = str(user_id)
        
        if user_id not in self.pending:
            return False, "User not found in pending verifications"
        
        pending_data = self.pending[user_id]
        plan_type = pending_data['plan']
        plan = config.PLANS[plan_type]
        
        # Create unique invite link for specific channel
        invite_link = self.create_invite_link(user_id, plan_type)
        
        # Check if link creation failed
        if "Error" in invite_link:
            return False, invite_link
        
        # Send join link to user
        try:
            if plan_type == "demo":
                join_msg = f"""
🎉 <b>DEMO ACCESS VERIFIED!</b>

<b>Plan:</b> {plan['name']}
<b>Amount Paid:</b> ₹{plan['amount']}

<b>👇 Your Invite Links (2 Links, 1 use each):</b>
{invite_link}

⚠️ <b>Note:</b> You got <b>2 SEPARATE LINKS</b> — each link works only <b>1 TIME</b>.
   • Link 1 → Use for yourself
   • Link 2 → Share with 1 friend / family
📅 <b>Access Duration:</b> {plan.get('duration', '30 Days')}

<b>Enjoy your demo! 🍿</b>
                """
            else:
                join_msg = f"""
🎉 <b>PAYMENT VERIFIED SUCCESSFULLY!</b>

<b>Plan:</b> {plan['name']}
<b>Amount Paid:</b> ₹{plan['amount']}

<b>👇 Your Invite Links (2 Links, 1 use each):</b>
{invite_link}

⚠️ <b>Important:</b> You received <b>2 UNIQUE LINKS</b> — each link works for <b>ONLY 1 PERSON</b>.
   • <b>Link 1:</b> Join yourself
   • <b>Link 2:</b> Share with a friend / family (one more person)
📅 <b>Access Duration:</b> {plan.get('duration', '30 Days')}

<b>Welcome to Premium Family! 🎊</b>
                """
            
            self.bot.send_message(
                int(user_id),
                join_msg,
                parse_mode="HTML"
            )
            
            # Log verification
            order_num = pending_data.get('order_number', 'N/A')
            log_msg = f"""
✅ <b>PAYMENT VERIFIED (Order #{order_num})</b>

👤 User ID: <code>{user_id}</code>
📅 Plan: {plan['name']}
💰 Amount: ₹{plan['amount']}
👮 Verified By: Admin
🔗 Invite Link: {invite_link}
⏰ Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            """
            
            target_chat = settings.get('log_channel')
            try:
                if target_chat:
                    self.bot.send_message(
                        target_chat,
                        log_msg,
                        parse_mode="HTML"
                    )
            except Exception as e:
                logger.error(f"Log channel error: {e}")
                # Fallback to admin if log channel fails
                primary_admin = settings['admin_ids'][0] if settings.get('admin_ids') else None
                if primary_admin and str(primary_admin) != str(target_chat):
                    try:
                        self.bot.send_message(
                            primary_admin,
                            log_msg + "\n\n⚠️ <i>(Log channel failed)</i>",
                            parse_mode="HTML"
                        )
                    except:
                        pass
            
            # Record Sale
            sale_record = {
                'order_number': order_num,
                'user_id': user_id,
                'plan_type': plan_type,
                'plan_name': plan['name'],
                'amount': float(plan['amount']),
                'upi_id': settings.get('upi_id', 'Unknown'),
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'date': datetime.now().strftime("%Y-%m-%d"),
                'admin_id': admin_id,
                'screenshot_file_id': pending_data.get('screenshot_file_id', '')
            }
            sales_data.append(sale_record)

            # Send proof to proof channel
            proof_channel = settings.get('proof_channel_id', '')
            if proof_channel and pending_data.get('screenshot_file_id'):
                user_name = pending_data.get('first_name', 'N/A')
                proof_caption = f"""
✅ <b>PAYMENT PROOF VERIFIED</b>

👤 <b>Name:</b> {user_name}
🆔 <b>User ID:</b> <code>{user_id}</code>
📅 <b>Plan:</b> {plan['name']}
💰 <b>Amount:</b> ₹{plan['amount']}
🧾 <b>Order #:</b> {order_num}
⏰ <b>Date:</b> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                """
                try:
                    self.bot.send_photo(
                        proof_channel,
                        photo=pending_data['screenshot_file_id'],
                        caption=proof_caption,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Proof channel error: {e}")
            
            # Update user data to mark as premium
            if user_id in users_data:
                users_data[user_id]['is_premium'] = True
                users_data[user_id]['premium_plan'] = plan_type
                users_data[user_id]['premium_until'] = (
                    "lifetime" if plan_type == "lifetime" 
                    else (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
                )
                users_data[user_id]['invite_link'] = invite_link
                # save_users_data() # Removed for batch saving
            
            # Remove from pending
            del self.pending[user_id]
            self.save_pending()
            
            return True, "User verified and unique join link sent"
            
        except Exception as e:
            logger.error(f"Error sending join link: {e}")
            return False, f"Error sending message: {str(e)}"
    
    def reject_payment(self, user_id, admin_id):
        """Reject payment and notify user"""
        user_id = str(user_id)
        
        if user_id not in self.pending:
            return False, "User not found in pending verifications"
        
        pending_data = self.pending[user_id]
        
        # Notify user
        try:
            reject_msg = f"""
❌ <b>PAYMENT VERIFICATION FAILED</b>

Your payment screenshot could not be verified.

<b>Possible reasons:</b>
• Screenshot not clear
• Wrong amount paid
• Payment not received

<b>Please try again:</b>
            """
            
            self.bot.send_message(
                int(user_id),
                reject_msg,
                parse_mode="HTML"
            )
            
            # Log rejection
            log_msg = f"""
❌ <b>PAYMENT REJECTED</b>

👤 User ID: <code>{user_id}</code>
👮 Rejected By: Admin
⏰ Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            """
            
            target_chat = settings.get('log_channel')
            if not target_chat:
                target_chat = settings['admin_ids'][0] if settings.get('admin_ids') else None
                
            if target_chat:
                self.bot.send_message(
                    target_chat,
                    log_msg,
                    parse_mode="HTML"
                )
            
            # Remove from pending
            del self.pending[user_id]
            self.save_pending()
            
            return True, "Payment rejected and user notified"
            
        except Exception as e:
            logger.error(f"Error rejecting payment: {e}")
            return False, f"Error: {str(e)}"

# Initialize verification system
verification = None

def init_verification(bot_instance):
    global verification
    verification = VerificationSystem(bot_instance)
    return verification
