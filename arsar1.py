import os
import time
import logging
import asyncio
import aiohttp
import random
from collections import Counter

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURATION & DYNAMIC KEYS
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8278838012:AAHcG9V3s-G2ZckB86eFhMGAOu-66wXhPeA")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8690698115")

# Dynamic Credentials (Bot Command ဖြင့် ချိန်းနိုင်သည်)
DYNAMIC_CONFIG = {
    "auth_token": os.getenv(
        "AUTHORIZATION_TOKEN",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJVc2VySWQiOiJiMTQyYTRiZi1hNTRkLTRiOTYtODdkYi02N2RhZTFhODc3M2QiLCJVc2VyTmFtZSI6Ijk6NzU0NDc0MjAiLCJuYW1lIjoiTWVtYmVyOE0yMUg1Q1AiLCJleHAiOjE3ODc0NzkzMzV9.S_MqVOnzAqC2y1aRtoTByHkG_Kt-3gcLbw_EvNF6g00"
    ),
    "random_key": "2i5lfrlexh6g4ylczs36j1dfu17kkbsv",
    "signature_key": "0000000000000000000000004180A8E8",
}

CHECK_INTERVAL = 3

ISSUE_API_URL = "https://qzgijlgwqxjwzlwctbke.supabase.co/functions/v1/get-game-issue"
HISTORY_API_URL = "https://qzgijlgwqxjwzlwctbke.supabase.co/functions/v1/get-game-history"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# GLOBAL STATE & ADMIN/USER CONTROL
# ============================================================

def get_admin_id():
    try:
        return int(ADMIN_CHAT_ID)
    except ValueError:
        return ADMIN_CHAT_ID

allowed_users = set()
if ADMIN_CHAT_ID:
    allowed_users.add(get_admin_id())

def is_admin(user_id: int) -> bool:
    admin_id = get_admin_id()
    return user_id == admin_id or str(user_id) == str(ADMIN_CHAT_ID)

def get_headers():
    """Dynamic Header ပြန်ပေးမည့် Helper"""
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": DYNAMIC_CONFIG["auth_token"],
        "content-type": "application/json",
        "origin": "https://mini-game.site",
        "referer": "https://mini-game.site/",
        "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
    }

bot_running = True
last_processed_issue = None
last_prediction_data = {}

round_counter = 0
session_wins = 0
session_losses = 0
history_logs = []

# ============================================================
# NEW PREDICTION ENGINE CLASS
# ============================================================

class BettingPredictor:
    def __init__(self, history: list):
        """
        history: အတိတ်ဂိမ်းရလဒ်များ list (ဥပမာ - ["Big", "Small", "Big"])
        """
        self.history = history

    def predict_trend_following(self) -> str:
        """
        1. Trend Following Strategy (Formula 2 / Dragon Line)
        နောက်ဆုံးထွက်ထားသည့် ရလဒ်အတိုင်း လိုက်ထပ်သည့် နည်းလမ်း
        """
        if not self.history:
            return "Big"
        return self.history[-1]

    def predict_pattern_matching(self, pattern_length: int = 3) -> str:
        """
        2. Pattern Matching Strategy
        နောက်ဆုံးထွက်ထားသော N-Length ပုံစံနှင့် တူညီသည့် အတိတ်က ပုံစံများကို ရှာပြီး နောက်ထပ်ထွက်မည့် ရလဒ်ကို ခန့်မှန်းခြင်း
        """
        if len(self.history) <= pattern_length:
            return self.predict_trend_following()

        current_pattern = self.history[-pattern_length:]
        next_outcomes = []

        for i in range(len(self.history) - pattern_length):
            window = self.history[i : i + pattern_length]
            if window == current_pattern:
                next_outcomes.append(self.history[i + pattern_length])

        if next_outcomes:
            return Counter(next_outcomes).most_common(1)[0][0]

        return self.predict_trend_following()

    def predict_statistical_probability(self, sample_size: int = 100) -> str:
        """
        3. Statistical Probability Strategy (Mean Reversion)
        နောက်ဆုံးပွဲများတွင် ရာခိုင်နှုန်း နည်းနေသည့် ဘက်ကို ပြန်လိုက်သည့် နည်းလမ်း
        """
        if not self.history:
            return "Big"

        recent_history = self.history[-sample_size:]
        counts = Counter(recent_history)
        
        big_count = counts.get("Big", 0)
        small_count = counts.get("Small", 0)

        if big_count < small_count:
            return "Big"
        elif small_count < big_count:
            return "Small"
        else:
            return random.choice(["Big", "Small"])

class PredictionEngineAdapter:
    """Telegram Bot တွင် သုံးစွဲနိုင်ရန် BettingPredictor ကို Adapter ရေးဆွဲပေးထားခြင်း"""
    @staticmethod
    def predict(history_numbers):
        if not history_numbers:
            return "B", "NO_DATA", []

        # Number history ကို "Big"/"Small" အဖြစ် ပြောင်းလဲခြင်း (0-4: Small, 5-9: Big)
        # အချိန်စဉ်အတိုင်း ဖြစ်စေရန် reverse လုပ်ထားပါသည်
        history_bs_full = ["Big" if n >= 5 else "Small" for n in reversed(history_numbers)]

        predictor = BettingPredictor(history_bs_full)
        
        # 1. Pattern Matching ကို ဦးစားပေး စစ်ဆေးခြင်း
        pred_full = predictor.predict_pattern_matching(pattern_length=3)
        strategy_name = "🧩 PATTERN_MATCHING (Len=3)"

        # 2. Pattern Match မရှိလျှင် Trend Following အသုံးပြုခြင်း
        if pred_full == predictor.predict_trend_following():
            strategy_name = "📊 TREND_FOLLOWING (Formula 2)"

        # 'Big' -> 'B', 'Small' -> 'S' ပြောင်းလဲခြင်း
        pred_code = "B" if pred_full == "Big" else "S"
        
        # Recent History 10 ပွဲအတွက် 
        recent_bs = ["B" if n >= 5 else "S" for n in reversed(history_numbers[:10])]
        
        return pred_code, strategy_name, recent_bs

# ============================================================
# NETWORK MANAGER
# ============================================================

class NetworkManager:
    @staticmethod
    async def fetch_api(session, url, payload):
        try:
            async with session.post(url, json=payload, headers=get_headers()) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
                else:
                    logger.error(f"API Error ({url}): Status {response.status}")
        except Exception as e:
            logger.error(f"API Exception ({url}): {e}")
        return None

# ============================================================
# GAME DATA FETCH
# ============================================================

async def get_game_data(session):
    current_ts = int(time.time())
    payload_base = {
        "typeId": 1,
        "language": 7,
        "random": DYNAMIC_CONFIG["random_key"],
        "timestamp": current_ts,
        "signature": DYNAMIC_CONFIG["signature_key"],
    }

    issue_res = await NetworkManager.fetch_api(session, ISSUE_API_URL, payload_base)
    history_payload = {**payload_base, "pageSize": 50, "pageNo": 1} # Pattern Matching အတွက် History 50 ပွဲအထိ ယူမည်
    history_res = await NetworkManager.fetch_api(session, HISTORY_API_URL, history_payload)

    next_issue = None
    if isinstance(issue_res, dict) and "data" in issue_res:
        data = issue_res["data"]
        if isinstance(data, dict):
            next_issue = str(data.get("issueNumber") or data.get("issue") or data.get("period") or data.get("actionNo") or "")
        elif isinstance(data, (str, int)):
            next_issue = str(data)

    history_numbers = []
    if isinstance(history_res, dict) and "data" in history_res:
        raw_list = history_res["data"]
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("list", [])
        if isinstance(raw_list, list):
            for item in raw_list:
                if isinstance(item, dict):
                    num = item.get("number") or item.get("resultNum")
                    if num is not None:
                        history_numbers.append(int(num))

    return next_issue, history_numbers

# ============================================================
# UI FORMATTING HELPER
# ============================================================

def generate_progress_bar(wins, total):
    if total == 0:
        return "░░░░░░░░░░ 0%"
    percentage = (wins / total) * 100
    filled = int(percentage // 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"{bar} {percentage:.1f}%"

# ============================================================
# TELEGRAM COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in allowed_users and not is_admin(chat_id):
        await update.message.reply_text(
            f"🚫 <b>Access Denied!</b>\n"
            f"သင့် Chat ID: <code>{chat_id}</code> ကို Admin ထံ ပေးပို့၍ ခွင့်ပြုချက်တောင်းခံပါ။",
            parse_mode="HTML"
        )
        return

    if is_admin(chat_id):
        msg = (
            "👑 <b>ADMIN PANEL ACTIVE</b> 👑\n"
            "━━━━━⬍⬍━━━━━\n"
            "🕹️ <b>User Management:</b>\n"
            "🔹 <code>/adduser &lt;chat_id&gt;</code> - Allow User\n"
            "🔹 <code>/removeuser &lt;chat_id&gt;</code> - Block User\n"
            "🔹 <code>/users</code> - List Allowed Users\n\n"
            "🔑 <b>API Credentials Management:</b>\n"
            "🔹 <code>/settoken &lt;token&gt;</code> - Authorization Token လဲရန်\n"
            "🔹 <code>/setsig &lt;signature&gt;</code> - Signature Key လဲရန်\n"
            "🔹 <code>/setrand &lt;random_key&gt;</code> - Random Key လဲရန်\n"
            "🔹 <code>/config</code> - လက်ရှိ Credentials ကြည့်ရန်\n\n"
            "⚙️ <b>Engine Control:</b>\n"
            "🔹 <code>/stop</code> - Pause Signals\n"
            "🔹 <code>/resume</code> - Resume Signals\n"
            "━━━━━⬍⬍━━━━━\n"
            "✨ <i>Prediction Engine (BettingPredictor Class) Running...</i>"
        )
    else:
        msg = "✅ <b>Welcome!</b> သင်သည် Signal များကို ပုံမှန် လက်ခံရရှိမည် ဖြစ်ပါသည်။"

    await update.message.reply_text(msg, parse_mode="HTML")

# ------------------------------------------------------------
# DYNAMIC CREDENTIALS COMMANDS (ADMIN ONLY)
# ------------------------------------------------------------

async def set_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("⚠️ <b>Usage:</b> <code>/settoken &lt;YOUR_TOKEN&gt;</code>", parse_mode="HTML")
        return

    new_token = " ".join(context.args)
    if not new_token.startswith("Bearer ") and not new_token.startswith("bearer "):
        new_token = "Bearer " + new_token

    DYNAMIC_CONFIG["auth_token"] = new_token
    await update.message.reply_text("✅ <b>Authorization Token successfully updated!</b>", parse_mode="HTML")

async def set_sig_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("⚠️ <b>Usage:</b> <code>/setsig &lt;YOUR_SIGNATURE&gt;</code>", parse_mode="HTML")
        return

    DYNAMIC_CONFIG["signature_key"] = context.args[0]
    await update.message.reply_text(f"✅ <b>Signature Key updated to:</b>\n<code>{DYNAMIC_CONFIG['signature_key']}</code>", parse_mode="HTML")

async def set_rand_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("⚠️ <b>Usage:</b> <code>/setrand &lt;YOUR_RANDOM_KEY&gt;</code>", parse_mode="HTML")
        return

    DYNAMIC_CONFIG["random_key"] = context.args[0]
    await update.message.reply_text(f"✅ <b>Random Key updated to:</b>\n<code>{DYNAMIC_CONFIG['random_key']}</code>", parse_mode="HTML")

async def show_config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    msg = (
        "🔑 <b>CURRENT API CREDENTIALS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Authorization:</b>\n<code>{DYNAMIC_CONFIG['auth_token'][:40]}...</code>\n\n"
        f"🔹 <b>Signature Key:</b>\n<code>{DYNAMIC_CONFIG['signature_key']}</code>\n\n"
        f"🔹 <b>Random Key:</b>\n<code>{DYNAMIC_CONFIG['random_key']}</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

# ------------------------------------------------------------
# USER MANAGEMENT COMMANDS
# ------------------------------------------------------------

async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("⚠️ <b>Usage:</b> <code>/adduser &lt;CHAT_ID&gt;</code>", parse_mode="HTML")
        return
    try:
        user_id = int(context.args[0])
        allowed_users.add(user_id)
        await update.message.reply_text(f"✅ <b>Added User:</b> <code>{user_id}</code>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("⚠️ <b>Chat ID သည် ကိန်းဂဏန်းသာ ဖြစ်ရပါမည်။</b>", parse_mode="HTML")

async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    if not context.args:
        await update.message.reply_text("⚠️ <b>Usage:</b> <code>/removeuser &lt;CHAT_ID&gt;</code>", parse_mode="HTML")
        return
    try:
        user_id = int(context.args[0])
        if user_id == get_admin_id():
            await update.message.reply_text("⚠️ <b>Admin ID ကို ဖျက်၍မရပါ။</b>", parse_mode="HTML")
            return
        if user_id in allowed_users:
            allowed_users.remove(user_id)
            await update.message.reply_text(f"🗑️ <b>Removed User:</b> <code>{user_id}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text("ℹ️ <b>ဤ User ID သည် စာရင်းထဲတွင် မရှိပါ။</b>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("⚠️ <b>Chat ID သည် ကိန်းဂဏန်းသာ ဖြစ်ရပါမည်။</b>", parse_mode="HTML")

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    if not allowed_users:
        await update.message.reply_text("ℹ️ <b>Allowed users စာရင်း မရှိသေးပါ။</b>", parse_mode="HTML")
        return
    user_list = [f"▸ <code>{uid}</code>" + (" (Admin)" if is_admin(uid) else "") for uid in allowed_users]
    msg = "👥 <b>ALLOWED USERS LIST</b>\n━━━━━━━━━━━━━━━\n" + "\n".join(user_list)
    await update.message.reply_text(msg, parse_mode="HTML")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    global bot_running
    bot_running = False
    await update.message.reply_text("⛔ <b>Prediction engine paused.</b>", parse_mode="HTML")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_chat.id): return
    global bot_running
    bot_running = True
    await update.message.reply_text("▶️ <b>Prediction engine resumed.</b>", parse_mode="HTML")

# ============================================================
# AUTO PREDICTION LOOP
# ============================================================

async def auto_prediction_loop(app):
    global last_processed_issue, last_prediction_data
    global round_counter, session_wins, session_losses, history_logs

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        while True:
            try:
                if bot_running and allowed_users:
                    next_issue, history_numbers = await get_game_data(session)

                    if next_issue and history_numbers and next_issue != last_processed_issue:
                        last_processed_issue = next_issue

                        last_result_status = (
                            "╔═════════════════════════════════╗\n"
                            "   🔄 <b>INITIALIZING FIRST SESSION...</b>\n"
                            "╚═════════════════════════════════╝"
                        )
                        summary_card = None

                        if last_prediction_data and history_numbers:
                            actual_last_number = history_numbers[0]
                            actual_last_bs = "B" if actual_last_number >= 5 else "S"
                            actual_text = "🔴 BIG" if actual_last_bs == "B" else "🔵 SMALL"

                            round_counter += 1
                            prev_pred = last_prediction_data.get("pred_code")

                            if prev_pred == actual_last_bs.lower():
                                session_wins += 1
                                history_logs.append("🟩")
                                last_result_status = (
                                    f"╭━━━ STATUS: <b>PREVIOUS RESULT</b> ━━━╮\n"
                                    f"│\n"
                                    f"│  🏆 <b>RESULT:</b> 🟢 <b>WIN (အောင်မြင်သည်)</b>\n"
                                    f"│  📌 <b>Issue:</b> <code>{last_prediction_data['issue']}</code>\n"
                                    f"│  🎯 <b>Outcome:</b> {actual_text} [<code>Num: {actual_last_number}</code>]\n"
                                    f"│\n"
                                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
                                )
                            else:
                                session_losses += 1
                                history_logs.append("🟥")
                                last_result_status = (
                                    f"╭━━━ STATUS: <b>PREVIOUS RESULT</b> ━━━╮\n"
                                    f"│\n"
                                    f"│  🔻 <b>RESULT:</b> 🔴 <b>LOSE (လွဲမှားသည်)</b>\n"
                                    f"│  📌 <b>Issue:</b> <code>{last_prediction_data['issue']}</code>\n"
                                    f"│  🎯 <b>Outcome:</b> {actual_text} [<code>Num: {actual_last_number}</code>]\n"
                                    f"│\n"
                                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
                                )

                            if round_counter % 10 == 0:
                                total_rounds = session_wins + session_losses
                                progress_bar = generate_progress_bar(session_wins, total_rounds)
                                logs_str = "".join(history_logs[-10:])

                                summary_card = (
                                    f"╔═════════════════════════╗\n"
                                    f"   📊 <b>10-ROUND SUMMARY CARD</b>\n"
                                    f"╠═════════════════════════╣\n"
                                    f" 📈 <b>Win Rate:</b> {progress_bar}\n"
                                    f" 🏆 <b>Wins:</b> {session_wins}  |  🔻 <b>Losses:</b> {session_losses}\n"
                                    f" 📜 <b>Logs:</b> {logs_str}\n"
                                    f"╚═════════════════════════╝"
                                )
                                session_wins = 0
                                session_losses = 0

                        # သစ်လွင်သော BettingPredictor Engine ဖြင့် တွက်ချက်ခြင်း
                        prediction, pattern_info, history_bs = PredictionEngineAdapter.predict(history_numbers)
                        pred_display = "🔴 BIG" if prediction == "B" else "🔵 SMALL"

                        formatted_bs = ["🟢 B" if x == "B" else "🔵 S" for x in history_bs]
                        history_str = " ➔ ".join(formatted_bs)

                        last_prediction_data = {
                            "issue": next_issue,
                            "pred_code": prediction.lower() if prediction else None
                        }

                        msg = (
                            f"{last_result_status}\n\n"
                            f"┌──────────────────────────────┐\n"
                            f"   🔮 <b>NEXT PREDICTION SIGNAL</b>\n"
                            f"└──────────────────────────────┘\n"
                            f"🆔 <b>ပွဲစဉ်:</b> <code>{next_issue}</code>\n"
                            f"🎯 <b>ခန့်မှန်းချက်:</b> <b>{pred_display}</b>\n"
                            f"⚙️ <b>STRATEGY:</b> <code>{pattern_info}</code>\n\n"
                            f"📊 <b>Recent History (10):</b>\n"
                            f"<code>{history_str}</code>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        )

                        for chat_id in list(allowed_users):
                            try:
                                await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
                                if summary_card:
                                    await app.bot.send_message(chat_id=chat_id, text=summary_card, parse_mode="HTML")
                            except Exception as e:
                                logger.error(f"Failed to send to {chat_id}: {e}")

            except Exception as e:
                logger.error(f"Error in prediction loop: {e}")

            await asyncio.sleep(CHECK_INTERVAL)

# ============================================================
# MAIN ENTRY POINT
# ============================================================

async def post_init(application):
    asyncio.create_task(auto_prediction_loop(application))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # User Control
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("adduser", add_user_command))
    app.add_handler(CommandHandler("removeuser", remove_user_command))
    app.add_handler(CommandHandler("users", list_users_command))

    # Dynamic Credentials Commands
    app.add_handler(CommandHandler("settoken", set_token_command))
    app.add_handler(CommandHandler("setsig", set_sig_command))
    app.add_handler(CommandHandler("setrand", set_rand_command))
    app.add_handler(CommandHandler("config", show_config_command))

    # Engine Control
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("resume", resume_command))

    app.run_polling()
