import telebot, asyncio, aiohttp, json, base64, random, re, string, time, uuid, os
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import cv2
import ddddocr
import numpy as np
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import logging

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

# ==================== GITHUB FUNCTIONS ====================
async def get_file_content(path):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                return json.loads(content), data['sha']
    except Exception as e:
        logger.error(f"GitHub get error: {e}")
    return {}, None

async def update_file_content(path, content, sha, message):
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
            return await response.text()
    except Exception as e:
        logger.error(f"GitHub update error: {e}")
        return None

# ==================== ADMIN PANEL FUNCTIONS ====================
def is_admin(user_id):
    return str(user_id) in ADMINS

def add_admin_log(action, details=""):
    global admin_logs
    log_entry = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'action': action,
        'details': details
    }
    admin_logs.append(log_entry)
    if len(admin_logs) > MAX_LOGS:
        admin_logs.pop(0)

async def update_admin_stats():
    global admin_stats
    auth_list, _ = await get_file_content("auth_list.json")
    results, _ = await get_file_content("result.json")
    
    total = len(auth_list)
    active = 0
    expired = 0
    unlimited = 0
    total_codes = 0
    
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
    
    for uid, codes in results.items():
        total_codes += len(codes)
    
    admin_stats.update({
        'total_users': total,
        'active_users': active,
        'expired_users': expired,
        'unlimited_users': unlimited,
        'total_codes_found': total_codes
    })

def generate_expiry(plan):
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
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"

# ==================== KEYBOARDS ====================

def get_admin_main_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📊 Dashboard", callback_data="admin_dashboard"),
        InlineKeyboardButton("👥 Users List", callback_data="admin_users"),
        InlineKeyboardButton("🔑 Generate Key", callback_data="admin_genkey"),
        InlineKeyboardButton("🗑 Delete Key", callback_data="admin_delkey"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("📋 View Logs", callback_data="admin_logs"),
        InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart"),
        InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"),
        InlineKeyboardButton("🔙 Back to Main", callback_data="menu_back")
    )
    return keyboard

def get_admin_back_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")
    )
    return keyboard

def get_plan_keyboard():
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

def get_main_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🎫 PAID USER", callback_data="menu_paid"),
        InlineKeyboardButton("🔗 STAR LINK Portal URL ထည့်ရန်", callback_data="menu_free_trial"),
        InlineKeyboardButton("📋 Success Codes ကြည့်မည်", callback_data="menu_result"),
        InlineKeyboardButton("🔄 Recheck ပြန်လုပ်စစ်မည်", callback_data="menu_recheck"),
        InlineKeyboardButton("🛑 Scan ရပ်မည်", callback_data="menu_stop"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_voucher_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔢 VOUCHER 6 လုံး", callback_data="scan_6"),
        InlineKeyboardButton("🔢 VOUCHER 7 လုံး", callback_data="scan_7"),
        InlineKeyboardButton("🔢 VOUCHER 8 လုံး", callback_data="scan_8"),
        InlineKeyboardButton("🔢 VOUCHER 9 လုံး", callback_data="scan_9"),
        InlineKeyboardButton("🔤 VOUCHER ascii-lower", callback_data="scan_ascii-lower"),
        InlineKeyboardButton("🔤 VOUCHER ascii-lower 9လုံး", callback_data="scan_ascii-lower9"),
        InlineKeyboardButton("🎲 VOUCHER all", callback_data="scan_all"),
        InlineKeyboardButton("🔤+🔢 MIXED 6လုံး", callback_data="scan_mixed"),
        InlineKeyboardButton("🔤+🔢 MIXED 7လုံး", callback_data="scan_mixed7"),
        InlineKeyboardButton("🔤+🔢 MIXED 8လုံး", callback_data="scan_mixed8"),
        InlineKeyboardButton("🔤+🔢 MIXED 9လုံး", callback_data="scan_mixed9"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_digit_keyboard(mode):
    keyboard = InlineKeyboardMarkup(row_width=5)
    buttons = []
    for i in range(10):
        buttons.append(InlineKeyboardButton(str(i), callback_data=f"digit_{mode}_{i}"))
    keyboard.add(*buttons)
    keyboard.add(InlineKeyboardButton("🎲 Random", callback_data=f"digit_{mode}_random"))
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
    return keyboard

def get_start_scam_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🚀 START SCAM", callback_data="menu_start_scam"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_paid_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✅ PAID USER ဖြစ်ရန်", callback_data="menu_enter_userid"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

def get_back_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("🔙 Back", callback_data="menu_back"))
    return keyboard

def get_scam_button_keyboard():
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🛑 STOP SCAM", callback_data="menu_stop"),
        InlineKeyboardButton("🔙 Back", callback_data="menu_back")
    )
    return keyboard

# ==================== BOT COMMANDS ====================

@bot.message_handler(commands=['start'])
async def start(message):
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
        await bot.send_message(message.chat.id, welcome_text, reply_markup=get_admin_main_keyboard())
        add_admin_log("Admin Login", f"User: {user_name} ({user_id})")
        return
    
    if user_id in paid_users or user_id in approve:
        approve[message.chat.id] = True
        welcome_text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

🎉 မင်္ဂလာပါခင်ဗျာ! 
✅ သင့်အနေနဲ့ PAID USER ဖြစ်ပါတယ်။
♾️ Unlimited Credit ဖြင့် သုံးစွဲနိုင်ပါသည်။

အောက်ပါ Menu မှ သင်လိုချင်တာကိုရွေးချယ်ပါ။"""
    else:
        welcome_text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

⚠️ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။

PAID USER ဖြစ်ရန် အောက်ပါ Menu မှ PAID USER ကိုနှိပ်ပါ။
👨‍💻 Admin: {ADMIN_USERNAME}"""
    
    await bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())

@bot.message_handler(commands=['admin'])
async def admin_panel(message):
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

@bot.message_handler(commands=['genkey_user'])
async def genkey_user(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "⛔ You are not authorized!")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, "Usage: `/genkey_user 123456789`", parse_mode="Markdown")
        return
    
    user_id = args[1]
    plan = "unlimited"  # Default
    
    auth_list, sha = await get_file_content("auth_list.json")
    if not auth_list:
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
    else:
        await bot.reply_to(message, "❌ Failed to generate key. Please try again.")

@bot.message_handler(commands=['delkey_user'])
async def delkey_user(message):
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
    else:
        await bot.reply_to(message, "❌ Failed to delete key. Please try again.")

@bot.message_handler(commands=['broadcast_send'])
async def broadcast_send(message):
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

@bot.message_handler(commands=['listkeys'])
async def listkeys(message):
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

# ==================== CALLBACK HANDLERS ====================

@bot.callback_query_handler(func=lambda call: True)
async def callback_handler(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
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
        await handle_paid(call)
        return
    
    if call.data == "menu_enter_userid":
        await handle_enter_userid(call)
        return
    
    if call.data == "menu_result":
        await handle_result(call)
        return
    
    if call.data == "menu_recheck":
        await handle_recheck(call)
        return
    
    if call.data == "menu_stop":
        await handle_stop(call)
        return
    
    if call.data.startswith("scan_"):
        await handle_scan_selection(call)
        return
    
    if call.data.startswith("digit_"):
        await handle_digit_selection(call)
        return

# ==================== ADMIN CALLBACK HANDLER ====================

async def admin_callback_handler(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    if not is_admin(user_id):
        await bot.answer_callback_query(call.id, "⛔ You are not an admin!", show_alert=True)
        return
    
    if call.data == "admin_panel":
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="🔧 **Admin Panel**\n\nSelect an option below:",
            reply_markup=get_admin_main_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_dashboard":
        await update_admin_stats()
        uptime = format_uptime()
        
        dashboard_text = f"""📊 **ADMIN DASHBOARD**

⏱ **Uptime:** {uptime}
🔍 **Active Scans:** {active_scans_count}/{MAX_CONCURRENT_SCANS}
👥 **Total Users:** {admin_stats['total_users']}
✅ **Active Users:** {admin_stats['active_users']}
❌ **Expired Users:** {admin_stats['expired_users']}
♾️ **Unlimited Users:** {admin_stats['unlimited_users']}
💾 **Total Codes Found:** {admin_stats['total_codes_found']}
📱 **Active Sessions:** {len(user_data)}

━━━━━━━━━━━━━━━━━━━━━
📈 **Recent Activity:**
• Total Scans: {len(scan_tasks)}
• Pending Tasks: {sum(1 for t in scan_tasks.values() if not t.get('task', {}).done())}"""
        
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=dashboard_text,
            reply_markup=get_admin_back_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_users":
        auth_list, _ = await get_file_content("auth_list.json")
        
        if not auth_list:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text="📋 **Users List**\n\nNo registered users found.",
                reply_markup=get_admin_back_keyboard(),
                parse_mode="Markdown"
            )
            await bot.answer_callback_query(call.id)
            return
        
        users_text = "📋 **REGISTERED USERS**\n\n"
        user_count = 0
        for uid, data in auth_list.items():
            if user_count >= 20:
                users_text += f"\n... and {len(auth_list) - 20} more users"
                break
            
            if isinstance(data, dict):
                plan = data.get("plan", "N/A")
                expires = data.get("expires_at", "N/A")
                
                is_expired = False
                if expires != "9999-12-31T23:59:59Z":
                    try:
                        exp_time = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                        if datetime.now(timezone.utc) >= exp_time:
                            is_expired = True
                    except:
                        is_expired = True
                
                status = "❌ Expired" if is_expired else "✅ Active"
                if expires == "9999-12-31T23:59:59Z":
                    status = "♾️ Unlimited"
                
                users_text += f"• **{uid}**\n  Plan: {plan} | Status: {status}\n"
            else:
                users_text += f"• {uid} (Legacy)\n"
            user_count += 1
        
        if len(users_text) > 4000:
            filename = f"users_{int(time.time())}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(users_text)
            with open(filename, "rb") as f:
                await bot.send_document(chat_id, f, caption="📋 Complete Users List")
            os.remove(filename)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text="📋 Users list sent as file",
                reply_markup=get_admin_back_keyboard()
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=users_text,
                reply_markup=get_admin_back_keyboard(),
                parse_mode="Markdown"
            )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_genkey":
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="🔑 **Generate Key**\n\nPlease select a plan:",
            reply_markup=get_plan_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data.startswith("plan_"):
        plan = call.data.replace("plan_", "")
        expiry = generate_expiry(plan)
        
        auth_list, sha = await get_file_content("auth_list.json")
        if not auth_list:
            auth_list = {}
        
        # Generate random user ID if not specified
        user_id = str(random.randint(100000000, 999999999))
        
        auth_list[user_id] = {
            "expires_at": expiry,
            "plan": plan
        }
        
        await update_file_content(
            "auth_list.json",
            auth_list,
            sha,
            f"Add key for {user_id} via Admin Panel"
        )
        
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ **Key Generated**\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"📋 Plan: `{plan}`\n"
            f"⏰ Expires: `{expiry}`",
            reply_markup=get_admin_back_keyboard(),
            parse_mode="Markdown"
        )
        add_admin_log("Key Generated", f"User: {user_id} | Plan: {plan}")
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_delkey":
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="🗑 **Delete Key**\n\nPlease send the User ID to delete.\n\nExample: `/delkey_user 123456789`",
            reply_markup=get_admin_back_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_broadcast":
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="📢 **Broadcast Message**\n\nPlease send the message to broadcast.\n\nExample: `/broadcast_send Your message here`",
            reply_markup=get_admin_back_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_logs":
        if not admin_logs:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text="📋 **Admin Logs**\n\nNo logs available.",
                reply_markup=get_admin_back_keyboard(),
                parse_mode="Markdown"
            )
            await bot.answer_callback_query(call.id)
            return
        
        logs_text = "📋 **RECENT ADMIN LOGS**\n\n"
        for log in admin_logs[-20:]:
            logs_text += f"• [{log['timestamp']}] {log['action']}\n  {log['details']}\n"
        
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=logs_text,
            reply_markup=get_admin_back_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_stats":
        await update_admin_stats()
        
        stats_text = f"""📊 **BOT STATISTICS**

━━━━━━━━━━━━━━━━━━━━━
👥 **Users**
• Total: {admin_stats['total_users']}
• Active: {admin_stats['active_users']}
• Expired: {admin_stats['expired_users']}
• Unlimited: {admin_stats['unlimited_users']}

🔍 **Scans**
• Active: {active_scans_count}/{MAX_CONCURRENT_SCANS}
• Total Tasks: {len(scan_tasks)}
• Total Codes Found: {admin_stats['total_codes_found']}

⚙️ **System**
• Uptime: {format_uptime()}
• Active Sessions: {len(user_data)}
• Paid Users: {len(paid_users)}
• Approved Users: {len(approve)}

💾 **Memory**
• User Data: {len(user_data)}
• Scan Tasks: {len(scan_tasks)}"""
        
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=stats_text,
            reply_markup=get_admin_back_keyboard(),
            parse_mode="Markdown"
        )
        await bot.answer_callback_query(call.id)
        return
    
    if call.data == "admin_restart":
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="🔄 **Restarting Bot...**\n\nBot will restart in a few seconds.",
            reply_markup=None,
            parse_mode="Markdown"
        )
        add_admin_log("Bot Restart", f"Initiated by {chat_id}")
        await bot.answer_callback_query(call.id)
        
        await asyncio.sleep(2)
        os._exit(0)

# ==================== NORMAL USER CALLBACK HANDLERS ====================

async def handle_menu_back(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    user_name = call.from_user.first_name or call.from_user.username or "User"
    
    if is_admin(user_id):
        text = f"🔧 **Admin Panel**\n\nWelcome back!"
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=get_admin_main_keyboard(),
            parse_mode="Markdown"
        )
    elif user_id in paid_users or user_id in approve:
        text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

✅ PAID USER - Unlimited Access"""
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=get_main_keyboard()
        )
    else:
        text = f"""✨ STAR LINK CODE HACK ✨

👤 NAME: {user_name}
🆔 USER ID: {user_id}

⚠️ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။"""
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=text,
            reply_markup=get_main_keyboard()
        )
    await bot.answer_callback_query(call.id)

async def handle_free_trial(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    if user_id not in paid_users and user_id not in approve:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"❌ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။\n\nPAID USER ဖြစ်ရန် Admin {ADMIN_USERNAME} သို့ ဆက်သွယ်ပါ။",
            reply_markup=get_back_keyboard()
        )
        await bot.answer_callback_query(call.id)
        return
    
    text = f"""🔗 Portal URL ထည့်သွင်းရန်:

/portal [your_portal_url]

ဥပမာ:
/portal https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?lang=en_US&mac=02:00:00:00:00:00"""
    
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=get_back_keyboard()
    )
    await bot.answer_callback_query(call.id)

async def handle_start_scam(call):
    # Simplified version - full implementation would be here
    await bot.answer_callback_query(call.id, "Scan feature - full implementation in original code", show_alert=True)

async def handle_paid(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    text = f"""🔑 PAID USER ဖြစ်ရန်

USER ID: {user_id}

✅ သင်၏ USER ID ကို Admin ထံ ပေးပို့ပြီး Key ဝယ်ယူပါ။
👨‍💻 Admin: {ADMIN_USERNAME}"""
    
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=get_paid_keyboard()
    )
    await bot.answer_callback_query(call.id)

async def handle_enter_userid(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    auth_list, _ = await get_file_content("auth_list.json")
    
    if user_id in auth_list:
        approve[chat_id] = True
        paid_users[user_id] = True
        if chat_id not in user_data:
            user_data[chat_id] = {}
        
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ PAID USER ဖြစ်ပါပြီ။\n\nUSER ID: {user_id}\n\nအောက်ပါ Menu မှ သင်လိုချင်တာကိုရွေးချယ်ပါ။",
            reply_markup=get_main_keyboard()
        )
    else:
        for admin_id in ADMINS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"🔔 New User Request:\nID: {user_id}"
                )
            except:
                pass
        
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"🙏 ကျေးဇူးပြု၍ Paid ဝယ်ယူပါ။\n\nUSER ID: {user_id}\n\nAdmin မှ အတည်ပြုပါမည်။",
            reply_markup=get_back_keyboard()
        )
    await bot.answer_callback_query(call.id)

async def handle_result(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    if user_id not in paid_users and user_id not in approve:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"❌ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။",
            reply_markup=get_back_keyboard()
        )
        await bot.answer_callback_query(call.id)
        return
    
    results, _ = await get_file_content("result.json")
    if user_id in results and results[user_id]:
        codes = "\n".join(results[user_id])
        text = f"✅ Found Codes:\n{codes}"
    else:
        text = "📋 သင့်တွင် ယခင်ကရရှိထားသော success code မရှိသေးပါ။"
    
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=get_back_keyboard()
    )
    await bot.answer_callback_query(call.id)

async def handle_recheck(call):
    await bot.answer_callback_query(call.id, "Recheck feature - full implementation in original code", show_alert=True)

async def handle_stop(call):
    await bot.answer_callback_query(call.id, "🛑 Scan stopped", show_alert=True)

async def handle_scan_selection(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)
    
    if user_id not in paid_users and user_id not in approve:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"❌ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။",
            reply_markup=get_back_keyboard()
        )
        await bot.answer_callback_query(call.id)
        return
    
    mode = call.data.replace("scan_", "")
    
    if chat_id not in user_data:
        user_data[chat_id] = {}
    
    if 'session_url' not in user_data[chat_id]:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text="🔗 ကျေးဇူးပြု၍ Portal URL ကိုအရင်ထည့်သွင်းပါ:\n\n/portal [your_portal_url]",
            reply_markup=get_back_keyboard()
        )
        await bot.answer_callback_query(call.id)
        return

    if mode in ["6", "7", "8", "9"]:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"🔢 VOUCHER {mode} လုံးအတွက် ထိပ်စီးနံပါတ်ရွေးပါ -",
            reply_markup=get_digit_keyboard(mode)
        )
        await bot.answer_callback_query(call.id)
        return

    user_data[chat_id]['selected_mode'] = mode
    user_data[chat_id]['start_digit'] = None
    
    text = f"""🔍 သင်ရွေးချယ်ထားသော VOUCHER အမျိုးအစား: {mode}

✅ START SCAM ခလုတ်ကိုနှိပ်ပြီး စတင်ပါ။"""
    
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=get_start_scam_keyboard()
    )
    await bot.answer_callback_query(call.id)

async def handle_digit_selection(call):
    parts = call.data.split("_")
    mode = parts[1]
    digit = parts[2]
    
    chat_id = call.message.chat.id
    
    if chat_id not in user_data:
        user_data[chat_id] = {}
    user_data[chat_id]['selected_mode'] = mode
    user_data[chat_id]['start_digit'] = None if digit == "random" else digit
    
    text = f"🔍 VOUCHER Mode: {mode}\n"
    if digit == "random":
        text += "🔢 ထိပ်စီးနံပါတ်: Random"
    else:
        text += f"🔢 ထိပ်စီးနံပါတ်: {digit} မှစ၍ရှာမည်"
        
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=call.message.message_id,
        text=text + "\n\n✅ START SCAM ခလုတ်ကိုနှိပ်ပြီး စတင်ပါ။",
        reply_markup=get_start_scam_keyboard()
    )
    await bot.answer_callback_query(call.id)

# ==================== OTHER COMMANDS ====================

@bot.message_handler(commands=['portal'])
async def handle_portal(message):
    user_id = str(message.chat.id)
    
    if user_id not in paid_users and user_id not in approve:
        await bot.reply_to(message, f"❌ သင်၏ user ID ကို registered မလုပ်ရသေးပါ။")
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "🔗 Portal URL ထည့်သွင်းရန်:\n\n/portal [your_portal_url]")
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

@bot.message_handler(commands=['status'])
async def status(message):
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
        f"📱 Sessions: {len(user_data)}"
    )

@bot.message_handler(commands=['key'])
async def handle_key(message):
    global approve, paid_users
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

# ==================== MAIN FUNCTION ====================

async def main():
    global session, _connector
    
    timeout = aiohttp.ClientTimeout(total=30)
    _connector = aiohttp.TCPConnector(
        limit=20000,
        limit_per_host=10000,
        ttl_dns_cache=300,
        ssl=False
    )
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=_connector,
        connector_owner=False
    )
    
    try:
        print("🤖 Bot is starting...")
        print("📊 Admin Panel available via /admin command")
        await bot.infinity_polling(timeout=20, request_timeout=20)
    finally:
        await session.close()
        await _connector.close()

if __name__ == '__main__':
    asyncio.run(main())
