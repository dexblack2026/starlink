import os
import sys
import telebot
import asyncio
import aiohttp
import json
import base64
import random
import re
import string
import time
import uuid
import logging
import traceback
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8842120690:AAGzTkp7_EClsI58aDka0rejIyKq7V9qwXs"
GITHUB_TOKEN = "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"
REPO_OWNER = "winwin1993t-cmd"
REPO_NAME = "mma111"

ADMINS = ["8690698115"]
ADMIN_USERNAME = "@nyatvip"

MAX_CONCURRENT_SCANS = 20
CONCURRENCY = 1000
BATCH_SIZE = 1000

PROXY_LIST = [
    "w9nx03l4kl8vdf0:iwx3ijrwgcyil91@rp.scrapegw.com:6060",
]

# ==================== GLOBALS ====================
bot = AsyncTeleBot(BOT_TOKEN)
user_data = {}
approve = {}
paid_users = {}
scan_tasks = {}
success_texts = {}
limited_texts = {}
captcha_state = {}
session = None
_connector = None
active_scans_count = 0
active_scans_lock = asyncio.Lock()
_start_time = time.monotonic()
SUCCESS_CODE = asyncio.Queue()
_voucher_sem = None

# Admin Panel Data
admin_stats = {
    'total_users': 0,
    'active_users': 0,
    'expired_users': 0,
    'unlimited_users': 0,
    'total_codes_found': 0
}
admin_logs = []
MAX_LOGS = 100

# Pending user requests
pending_requests = {}  # {user_id: {'name': name, 'timestamp': time}}
pending_keys = {}  # {admin_id: {'user_id': user_id, 'plan': plan}}

# Global portal URL (admin can set)
global_portal_url = None

# ==================== GITHUB FUNCTIONS ====================
async def get_file_content(path):
    """Get file content from GitHub"""
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                return json.loads(content), data['sha']
            else:
                logger.warning(f"GitHub get error: {response.status}")
                return {}, None
    except Exception as e:
        logger.error(f"GitHub get error: {e}")
        return {}, None

async def update_file_content(path, content, sha, message):
    """Update file content on GitHub"""
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    encoded = base64.b64encode(json.dumps(content).encode()).decode()
    payload = {
        "message": message,
        "content": encoded,
        "sha": sha
    }
    try:
        async with session.put(url, headers=headers, json=payload) as response:
            if response.status in [200, 201]:
                return await response.text()
            else:
                logger.warning(f"GitHub update error: {response.status}")
                return None
    except Exception as e:
        logger.error(f"GitHub update error: {e}")
        return None

# ==================== ADMIN PANEL FUNCTIONS ====================
def is_admin(user_id):
    """Check if user is admin"""
    return str(user_id) in ADMINS

def add_admin_log(action, details=""):
    """Add log entry"""
    global admin_logs
    log_entry = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,
        'details': details
    }
    admin_logs.append(log_entry)
    if len(admin_logs) > MAX_LOGS:
        admin_logs.pop(0)
    logger.info(f"[ADMIN LOG] {action}: {details}")

async def update_admin_stats():
    """Update admin statistics"""
    global admin_stats
    try:
        auth_list, _ = await get_file_content("auth_list.json")
        results, _ = await get_file_content("result.json")
        
        total = len(auth_list) if auth_list else 0
        active = 0
        expired = 0
        unlimited = 0
        total_codes = 0
        
        if auth_list:
            for uid, data in auth_list.items():
                if isinstance(data, dict):
                    expires = data.get("expires_at", "")
                    if expires == "9999-12-31T23:59:59Z":
                        unlimited += 1
                        active += 1
                    elif expires:
                        try:
                            exp_time = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                            if datetime.now(timezone.utc) < exp_time:
                                active += 1
                            else:
                                expired += 1
                        except:
                            expired += 1
        
        if results:
            for uid, codes in results.items():
                total_codes += len(codes) if codes else 0
        
        admin_stats.update({
            'total_users': total,
            'active_users': active,
            'expired_users': expired,
            'unlimited_users': unlimited,
            'total_codes_found': total_codes
        })
    except Exception as e:
        logger.error(f"Update admin stats error: {e}")

def generate_expiry(plan):
    """Generate expiry date based on plan"""
    now = datetime.now(timezone.utc)
    plans = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "1m": timedelta(days=30),
        "1y": timedelta(days=365),
        "unlimited": None
    }
    if plan not in plans:
        return None
    if plan == "unlimited":
        return "9999-12-31T23:59:59Z"
    return (now + plans[plan]).isoformat()

def format_uptime():
    """Format uptime"""
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"

# ==================== KEYBOARDS ====================

def get_admin_main_keyboard():
    """Admin main keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 Dashboard", callback_data="admin_dashboard"),
        InlineKeyboardButton("👥 Users List", callback_data="admin_users"),
        InlineKeyboardButton("📩 Pending Requests", callback_data="admin_pending"),
        InlineKeyboardButton("🔑 Generate Key", callback_data="admin_genkey"),
        InlineKeyboardButton("🗑 Delete Key", callback_data="admin_delkey"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("📋 View Logs", callback_data="admin_logs"),
        InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
        InlineKeyboardButton("🔗 Portal URL", callback_data="admin_portal"),
        InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart"),
        InlineKeyboardButton("🔙 Back to Main", callback_data="menu_back")
    )
    return keyboard

def get_admin_back_keyboard():
    """Admin back keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")
    )
    return keyboard

def get_plan_keyboard():
    """Plan selection keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        InlineKeyboardButton("30m", callback_data="plan_30m"),
        InlineKeyboardButton("1h", callback_data="plan_1h"),
        InlineKeyboardButton("1d", callback_data="plan_1d"),
        InlineKeyboardButton("7d", callback_data="plan_7d"),
        InlineKeyboardButton("1m", callback_data="plan_1m"),
        InlineKeyboardButton("1y", callback_data="plan_1y"),
        InlineKeyboardButton("♾️ Unlimited", callback_data="plan_unlimited"),
        InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")
    )
    return keyboard

def get_pending_request_keyboard(user_id, user_name):
    """Pending request keyboard with accept/reject buttons"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton(f"✅ Accept {user_name}", callback_data=f"accept_{user_id}"),
        InlineKeyboardButton(f"❌ Reject {user_name}", callback_data=f"reject_{user_id}")
    )
    keyboard.add(
        InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")
    )
    return keyboard

def get_main_keyboard():
    """Main user keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎫 PAID USER", callback_data="menu_paid"),
        InlineKeyboardButton("🔗 Portal URL ထည့်ရန်", callback_data="menu_free_trial"),
        InlineKeyboardButton("📋 Success Codes", callback_data="menu_result"),
        InlineKeyboardButton("🔄 Recheck", callback_data="menu_recheck"),
        InlineKeyboardButton("🛑 Scan ရပ်မည်", callback_data="menu_stop"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_voucher_keyboard():
    """Voucher selection keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔢 VOUCHER 6 လုံး", callback_data="scan_6"),
        InlineKeyboardButton("🔢 VOUCHER 7 လုံး", callback_data="scan_7"),
        InlineKeyboardButton("🔢 VOUCHER 8 လုံး", callback_data="scan_8"),
        InlineKeyboardButton("🔢 VOUCHER 9 လုံး", callback_data="scan_9"),
        InlineKeyboardButton("🔤 ASCII Lower", callback_data="scan_ascii-lower"),
        InlineKeyboardButton("🔤 ASCII Lower 9", callback_data="scan_ascii-lower9"),
        InlineKeyboardButton("🎲 All", callback_data="scan_all"),
        InlineKeyboardButton("🔤+🔢 MIXED 6", callback_data="scan_mixed"),
        InlineKeyboardButton("🔤+🔢 MIXED 7", callback_data="scan_mixed7"),
        InlineKeyboardButton("🔤+🔢 MIXED 8", callback_data="scan_mixed8"),
        InlineKeyboardButton("🔤+🔢 MIXED 9", callback_data="scan_mixed9"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_digit_keyboard(mode):
    """Digit selection keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=5)
    buttons = []
    for i in range(10):
        buttons.append(InlineKeyboardButton(str(i), callback_data=f"digit_{mode}_{i}"))
    keyboard.add(*buttons)
    keyboard.add(InlineKeyboardButton("🎲 Random", callback_data=f"digit_{mode}_random"))
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
    return keyboard

def get_start_scam_keyboard():
    """Start scam keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🚀 START SCAM", callback_data="menu_start_scam"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_paid_keyboard():
    """Paid user keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✅ PAID USER ဖြစ်ရန်", callback_data="menu_enter_userid"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_back_keyboard():
    """Back keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
    return keyboard

def get_scam_button_keyboard():
    """Scam button keyboard"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🛑 STOP SCAM", callback_data="menu_stop"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

# ==================== BOT COMMANDS ====================

@bot.message_handler(commands=['start'])
async def start(message):
    """Start command handler"""
    try:
        user_id = str(message.chat.id)
        user_name = message.from_user.first_name or message.from_user.username or "User"
        
        if message.chat.id not in user_data:
            user_data[message.chat.id] = {}
        
        if is_admin(user_id):
            welcome_text = f"""✨ **STAR LINK CODE HACK - ADMIN** ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}
👑 ROLE: **ADMIN**

Welcome to Admin Panel!"""
            await bot.send_message(
                message.chat.id, 
                welcome_text, 
                reply_markup=get_admin_main_keyboard(),
                parse_mode="Markdown"
            )
            add_admin_log("Admin Login", f"User: {user_name} ({user_id})")
            return
        
        # Check if user is already approved
        if user_id in paid_users or user_id in approve:
            approve[message.chat.id] = True
            welcome_text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

🎉 မင်္ဂလာပါခင်ဗျာ! 
✅ သင့်အနေနဲ့ PAID USER ဖြစ်ပါတယ်။
♾️ Unlimited Credit ဖြင့် သုံးစွဲနိုင်ပါသည်။

အောက်ပါ Menu မှ သင်လိုချင်တာကိုရွေးချယ်ပါ။"""
            await bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())
            return
        
        # Check if user has pending request
        if user_id in pending_requests:
            welcome_text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

⏳ သင်၏ Request ကို Admin မှ စစ်ဆေးနေပါသည်။
ကျေးဇူးပြု၍ ခဏစောင့်ပါ။

👨‍💻 Admin: {ADMIN_USERNAME}"""
            await bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())
            return
        
        # New user - send request to admin
        pending_requests[user_id] = {
            'name': user_name,
            'timestamp': time.time()
        }
        
        # Notify all admins
        for admin_id in ADMINS:
            try:
                admin_text = f"""🔔 **NEW USER REQUEST**

👤 Name: {user_name}
🆔 User ID: `{user_id}`
⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Please approve or reject this request."""
                
                await bot.send_message(
                    admin_id,
                    admin_text,
                    reply_markup=get_pending_request_keyboard(user_id, user_name),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify admin {admin_id}: {e}")
        
        user_text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

✅ သင်၏ Request ကို Admin သို့ ပေးပို့ပြီးပါပြီ။
⏳ ကျေးဇူးပြု၍ Admin မှ အတည်ပြုသည်အထိ စောင့်ပါ။

👨‍💻 Admin: {ADMIN_USERNAME}"""
        
        await bot.send_message(message.chat.id, user_text, reply_markup=get_main_keyboard())
        add_admin_log("New User Request", f"User: {user_name} ({user_id})")
        
    except Exception as e:
        logger.error(f"Start command error: {e}")
        await bot.send_message(message.chat.id, "❌ An error occurred. Please try again.")

@bot.message_handler(commands=['admin'])
async def admin_panel(message):
    """Admin panel command"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ You are not authorized to use this command.")
            return
        
        await bot.reply_to(
            message,
            "🔧 **Admin Panel**\n\nWelcome to the Admin Control Panel.",
            reply_markup=get_admin_main_keyboard(),
            parse_mode="Markdown"
        )
        add_admin_log("Admin Panel Opened", f"User: {message.from_user.first_name} ({message.chat.id})")
    except Exception as e:
        logger.error(f"Admin panel error: {e}")
        await bot.reply_to(message, "❌ An error occurred. Please try again.")

@bot.message_handler(commands=['genkey_user'])
async def genkey_user(message):
    """Generate key for user"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ You are not authorized!")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await bot.reply_to(
                message, 
                "Usage: `/genkey_user 123456789`\n\n"
                "To generate with specific plan:\n"
                "`/genkey_user 123456789 unlimited`\n"
                "`/genkey_user 123456789 1m`\n"
                "`/genkey_user 123456789 7d`",
                parse_mode="Markdown"
            )
            return
        
        user_id = args[1]
        plan = args[2] if len(args) > 2 else "unlimited"
        
        # Validate plan
        valid_plans = ["30m", "1h", "1d", "7d", "1m", "1y", "unlimited"]
        if plan not in valid_plans:
            await bot.reply_to(
                message,
                f"❌ Invalid plan: `{plan}`\n\n"
                f"Valid plans: {', '.join(valid_plans)}",
                parse_mode="Markdown"
            )
            return
        
        auth_list, sha = await get_file_content("auth_list.json")
        if auth_list is None:
            auth_list = {}
        
        expiry = generate_expiry(plan)
        auth_list[user_id] = {
            "expires_at": expiry,
            "plan": plan
        }
        
        result = await update_file_content(
            "auth_list.json",
            auth_list,
            sha,
            f"Add key for {user_id} via Admin Panel"
        )
        
        if result:
            await bot.reply_to(
                message,
                f"✅ **Key Generated Successfully**\n\n"
                f"👤 User ID: `{user_id}`\n"
                f"📋 Plan: `{plan}`\n"
                f"⏰ Expires: `{expiry}`",
                parse_mode="Markdown"
            )
            add_admin_log("Key Generated", f"User: {user_id} | Plan: {plan}")
            
            # Notify user
            try:
                await bot.send_message(
                    int(user_id),
                    f"✅ **Key Generated for You!**\n\n"
                    f"📋 Plan: `{plan}`\n"
                    f"⏰ Expires: `{expiry}`\n\n"
                    f"Please use `/start` to access the bot.",
                    parse_mode="Markdown"
                )
            except:
                pass
        else:
            await bot.reply_to(message, "❌ Failed to generate key. Please try again.")
    except Exception as e:
        logger.error(f"Genkey error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['delkey_user'])
async def delkey_user(message):
    """Delete key for user"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ You are not authorized!")
            return
        
        args = message.text.split()
        if len(args) < 2:
            await bot.reply_to(message, "Usage: `/delkey_user 123456789`", parse_mode="Markdown")
            return
        
        user_id = args[1]
        
        auth_list, sha = await get_file_content("auth_list.json")
        if not auth_list or user_id not in auth_list:
            await bot.reply_to(message, f"❌ User ID `{user_id}` not found.", parse_mode="Markdown")
            return
        
        del auth_list[user_id]
        
        result = await update_file_content(
            "auth_list.json",
            auth_list,
            sha,
            f"Delete key for {user_id} via Admin Panel"
        )
        
        if result:
            await bot.reply_to(
                message,
                f"✅ **Key Deleted Successfully**\n\n👤 User ID: `{user_id}`",
                parse_mode="Markdown"
            )
            add_admin_log("Key Deleted", f"User: {user_id}")
            
            # Notify user
            try:
                await bot.send_message(
                    int(user_id),
                    f"❌ Your key has been deleted by admin.\n\nPlease contact {ADMIN_USERNAME} for support.",
                    parse_mode="Markdown"
                )
            except:
                pass
        else:
            await bot.reply_to(message, "❌ Failed to delete key. Please try again.")
    except Exception as e:
        logger.error(f"Delkey error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['broadcast_send'])
async def broadcast_send(message):
    """Send broadcast message"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ You are not authorized!")
            return
        
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await bot.reply_to(message, "Usage: `/broadcast_send Your message here`", parse_mode="Markdown")
            return
        
        broadcast_text = f"📢 **ADMIN BROADCAST**\n\n{args[1]}"
        
        auth_list, _ = await get_file_content("auth_list.json")
        
        if not auth_list:
            await bot.reply_to(message, "❌ No users to broadcast to.")
            return
        
        await bot.reply_to(message, f"📤 Sending broadcast to {len(auth_list)} users...")
        
        count = 0
        for uid in auth_list:
            try:
                await bot.send_message(int(uid), broadcast_text, parse_mode="Markdown")
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Broadcast error to {uid}: {e}")
                continue
        
        await bot.reply_to(
            message,
            f"✅ **Broadcast Complete**\n\n📤 Sent to: {count}/{len(auth_list)} users"
        )
        add_admin_log("Broadcast Sent", f"Sent to {count} users")
    except Exception as e:
        logger.error(f"Broadcast error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['listkeys'])
async def listkeys(message):
    """List all keys"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ You are not authorized!")
            return
        
        auth_list, _ = await get_file_content("auth_list.json")
        
        if not auth_list:
            await bot.reply_to(message, "📋 No registered keys found.")
            return
        
        lines = []
        for uid, data in auth_list.items():
            if isinstance(data, dict):
                expires = data.get("expires_at", "unknown")
                plan = data.get("plan", "unknown")
                
                if expires == "9999-12-31T23:59:59Z":
                    status = "♾️ Unlimited"
                else:
                    try:
                        exp_time = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        if exp_time < now:
                            status = "❌ Expired"
                        else:
                            diff = exp_time - now
                            days = diff.days
                            hours, rem = divmod(diff.seconds, 3600)
                            minutes = rem // 60
                            status = f"✅ {days}d {hours}h {minutes}m left"
                    except:
                        status = expires
            else:
                plan = "legacy"
                status = str(data)
            
            lines.append(f"👤 `{uid}`\n   📋 Plan: {plan}\n   ⏰ Status: {status}")
        
        text = f"📋 **Registered Keys** ({len(auth_list)})\n\n" + "\n\n".join(lines)
        
        if len(text) > 4000:
            filename = f"keys_{int(time.time())}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(text)
            with open(filename, "rb") as f:
                await bot.send_document(message.chat.id, f, caption="📋 Complete Keys List")
            os.remove(filename)
        else:
            await bot.reply_to(message, text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Listkeys error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['setportal'])
async def set_portal(message):
    """Set global portal URL for all users"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ You are not authorized!")
            return
        
        global global_portal_url
        
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await bot.reply_to(
                message,
                "🔗 **Set Global Portal URL**\n\n"
                "Usage: `/setportal https://portal-as.ruijienetworks.com/...`\n\n"
                "This URL will be used by all users.",
                parse_mode="Markdown"
            )
            return
        
        url = args[1]
        global_portal_url = url
        
        await bot.reply_to(
            message,
            f"✅ **Global Portal URL Set**\n\n"
            f"🔗 URL: `{url}`\n\n"
            f"All users can now use this URL.",
            parse_mode="Markdown"
        )
        add_admin_log("Global Portal URL Set", f"URL: {url}")
    except Exception as e:
        logger.error(f"Set portal error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['key'])
async def handle_key(message):
    """Handle key verification"""
    try:
        args = message.text.split()
        if len(args) < 2:
            await bot.reply_to(message, "🔑 /key [your_key_here]")
            return
        
        key = args[1]
        user_id = str(message.chat.id)
        
        auth_list, _ = await get_file_content("auth_list.json")
        
        if user_id in auth_list or key in auth_list:
            valid = True
            if user_id in auth_list:
                if isinstance(auth_list[user_id], dict):
                    expires = auth_list[user_id].get("expires_at", "")
                    if expires != "9999-12-31T23:59:59Z":
                        try:
                            exp_time = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                            if datetime.now(timezone.utc) >= exp_time:
                                valid = False
                        except:
                            valid = False
            
            if valid:
                approve[message.chat.id] = True
                paid_users[user_id] = True
                if message.chat.id not in user_data:
                    user_data[message.chat.id] = {}
                await bot.reply_to(
                    message,
                    f"✅ PAID USER ဖြစ်ပါပြီ။\n\nUSER ID: {user_id}"
                )
                # Remove from pending if exists
                pending_requests.pop(user_id, None)
            else:
                await bot.reply_to(
                    message,
                    "❌ Key Expired ဖြစ်နေပါသည်။"
                )
        else:
            await bot.reply_to(
                message,
                f"❌ သင်၏ key ကို registered မလုပ်ရသေးပါ။\n\nUSER ID: {user_id}"
            )
    except Exception as e:
        logger.error(f"Key handler error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['status'])
async def status(message):
    """Status command"""
    try:
        if not is_admin(message.chat.id):
            await bot.reply_to(message, "⛔ No Permission")
            return
        
        uptime_seconds = int(time.monotonic() - _start_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        await bot.reply_to(
            message,
            f"📊 Bot Status\n\n"
            f"⏱ Uptime: {hours}h {minutes}m {seconds}s\n"
            f"🔍 Active Scans: {active_scans_count}\n"
            f"👥 Users: {len(paid_users)}\n"
            f"📱 Sessions: {len(user_data)}\n"
            f"📩 Pending: {len(pending_requests)}"
        )
    except Exception as e:
        logger.error(f"Status error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['portal'])
async def handle_portal(message):
    """Portal URL handler - uses global portal if set"""
    try:
        user_id = str(message.chat.id)
        
        if user_id not in paid_users and user_id not in approve:
            await bot.reply_to(message, f"❌ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။")
            return
        
        args = message.text.split(maxsplit=1)
        
        # Use global portal URL if admin set it
        global global_portal_url
        
        if global_portal_url and len(args) < 2:
            # Auto use global portal
            if message.chat.id not in user_data:
                user_data[message.chat.id] = {}
            
            user_data[message.chat.id]['session_url'] = global_portal_url
            
            await bot.reply_to(
                message, 
                f"✅ **Portal URL Set (Global)**\n\n"
                f"🔗 URL: `{global_portal_url}`\n\n"
                f"VOUCHER ရွေးချယ်ရန် Menu ကိုသုံးပါ။",
                reply_markup=get_voucher_keyboard(),
                parse_mode="Markdown"
            )
            return
        
        if len(args) < 2:
            if global_portal_url:
                await bot.reply_to(
                    message,
                    f"🔗 **Using Global Portal URL**\n\n"
                    f"URL: `{global_portal_url}`\n\n"
                    f"To set custom URL:\n`/portal your_custom_url`",
                    parse_mode="Markdown"
                )
            else:
                await bot.reply_to(
                    message, 
                    "🔗 Portal URL ထည့်သွင်းရန်:\n\n"
                    "/portal [your_portal_url]\n\n"
                    "ဥပမာ:\n"
                    "/portal https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?lang=en_US&mac=02:00:00:00:00:00"
                )
            return
        
        url = args[1]
        
        if message.chat.id not in user_data:
            user_data[message.chat.id] = {}
        
        user_data[message.chat.id]['session_url'] = url
        
        await bot.reply_to(
            message, 
            "✅ Portal URL အားသိမ်းဆည်းပြီးပါပြီ။\n\nVOUCHER ရွေးချယ်ရန် Menu ကိုသုံးပါ။",
            reply_markup=get_voucher_keyboard()
        )
    except Exception as e:
        logger.error(f"Portal error: {e}")
        await bot.reply_to(message, f"❌ Error: {str(e)}")

# ==================== CALLBACK HANDLERS ====================

@bot.callback_query_handler(func=lambda call: True)
async def callback_handler(call):
    """Main callback handler"""
    try:
        chat_id = call.message.chat.id
        user_id = str(chat_id)
        
        # ===== HANDLE PENDING REQUESTS (Accept/Reject) =====
        if call.data.startswith("accept_") or call.data.startswith("reject_"):
            await handle_pending_request(call)
            return
        
        # ===== ADMIN CALLBACKS =====
        if call.data.startswith('admin_') or call.data.startswith('plan_'):
            await admin_callback_handler(call)
            return
        
        # ===== NORMAL USER CALLBACKS =====
        if call.data == "menu_back":
            await handle_menu_back(call)
            return
        
        if call.data == "menu_free_trial":
            await handle_free_trial(call)
            return
        
        if call.data == "menu_start_scam":
            await handle_start_scam(call)
            return
        
        if call.data == "menu_paid":
            await handle
