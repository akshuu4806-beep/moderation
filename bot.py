import os
import sys
import time
import pytz
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.types import Message, ChatPermissions
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram import StopPropagation
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    ChatPermissions, ChatPrivileges, ChatMemberUpdated
)
from PIL import Image, ImageDraw, ImageFont
import os
import google.generativeai as genai

# ============== OPTIMIZATION: CACHING LAYER ==============
class TTLCache:
    """Simple time‑based cache with per‑key TTL"""
    def __init__(self, ttl=60):
        self.cache = {}
        self.ttl = ttl

    def get(self, key):
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None

    def set(self, key, value):
        self.cache[key] = (value, time.time())

    def delete(self, key):
        if key in self.cache:
            del self.cache[key]

# Global caches
perms_cache = TTLCache(ttl=30)          # user permissions (30 sec)
night_cache = TTLCache(ttl=30)           # night mode config
approved_cache = TTLCache(ttl=30)        # approved users
locks_cache = TTLCache(ttl=15)           # locks (short TTL)
filters_cache = TTLCache(ttl=60)          # filters with compiled regex
admin_cache = TTLCache(ttl=60)            # admin status per (chat,user)
member_cache = TTLCache(ttl=60)           # chat member objects
settings_cache = TTLCache(ttl=30)         # free settings
sudo_cache = TTLCache(ttl=60)            # Sudo users (Global Admins)
bwords_cache = TTLCache(ttl=60)          # Blocked words
bspacks_cache = TTLCache(ttl=60)         # Blocked sticker packs
gban_cache = TTLCache(ttl=300) # GBan status cache (5 mins)

# ==========================================================

# Dictionary to store message times: {chat_id: {user_id: [time1, time2, ...]}}
spam_tracker = defaultdict(lambda: defaultdict(list))

SPAM_DELETE_LIMIT = 2   # 2 messages safely honge, 3rd aate hi delete hoga
SPAM_MUTE_LIMIT = 5     # Agar koi bot lagatar 5 message feke toh direct MUTE
SPAM_WINDOW = 5.0       # 2 seconds ka time window (Telegram lag ke liye perfect)

# ============== CONFIGURATION =============
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URL = os.environ.get("MONGO_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OWNER_ID = int(os.environ.get("OWNER_ID"))

if not all([API_ID, API_HASH, BOT_TOKEN, MONGO_URL]):
    print("❌ Missing environment variables!")
    sys.exit(1)

# Timezone setup for IST
IST = pytz.timezone("Asia/Kolkata")

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# 👇 ADD THIS LINE RIGHT HERE TO SILENCE PYROGRAM 👇
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# --- AI SETUP START ---
GEMINI_API_KEY = "AIzaSyDQ8tKK2YB66XljWPDjA7k8mZqYStjRK-k"
genai.configure(api_key=GEMINI_API_KEY)

working_model = "gemini-1.5-flash" # Backup option
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            working_model = m.name
            break
    print(f"🧠 AI Model Successfully Selected: {working_model}")
except Exception as e:
    print(f"⚠️ Model List Error: {e}")

ai_model = genai.GenerativeModel(working_model)
# --- AI SETUP END ---

async def unified_security_handler(client: Client, message: Message):
    if not message.from_user:
        return

    # 👇 PEHLE ID define karein
    user_id = message.from_user.id
    chat_id = message.chat.id
        
    # Ignore commands so admins don't get blocked
    if message.text and message.text.startswith('/'):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    # 1. EXEMPT APPROVED USERS & ADMINS
    # Note: Ensure `is_approved` is defined elsewhere as an async function
    if await is_approved(chat_id, user_id):
        return

    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return
    except Exception:
        pass

    # Fetch custom /free permissions (cached now)
    # Note: Ensure `get_user_perms` is defined elsewhere
    perms = await get_user_perms(chat_id, user_id)
    user_ns_perm = perms.get("night_silence", False)
    user_media_perm = perms.get("media", False)
    user_spam_perm = perms.get("spam", False)
    user_flood_perm = perms.get("flood", False)

    # Check if message contains ANY media/sticker
    has_media = bool(
        message.photo or
        message.video or
        message.document or
        message.sticker or
        message.animation or
        message.voice or
        message.video_note or
        message.audio
    )

    # 2. PROPERLY CHECK NIGHT MODE FROM DATABASE (cached)
    # Note: Ensure `get_night_config` is defined elsewhere
    night_conf = await get_night_config(chat_id)
    is_night = False

    if night_conf:
        current_hour = datetime.now(IST).hour
        start = night_conf.get('start', 0)
        end = night_conf.get('end', 7)

        if start > end:
            if current_hour >= start or current_hour < end:
                is_night = True
        else:
            if start <= current_hour < end:
                is_night = True

    # 3. APPLY RULES BASED ON TIME
    if is_night:
        # Agar Night Silence OFF hai, tab strict rules apply honge
        if not user_ns_perm:
            if has_media:
                try:
                    await message.delete()
                    raise StopPropagation
                except Exception:
                    pass
            # Force anti-spam active rakho night mein
            user_spam_perm = False
            user_flood_perm = False

    else:
        # ☀️ NORMAL DAY MODE
        if has_media and not user_media_perm:
            try:
                await message.delete()
                raise StopPropagation # <--- YAHAN LAGA DIJIYE
            except Exception:
                pass

    # 4. SPAM & FLOOD TRACKING LOGIC (DYNAMIC VARIABLES KE SAATH)
    if not (user_spam_perm or user_flood_perm):
        now_time = time.time()
        msg_id = message.id  # Hum message ID bhi track karenge
        
        # Purana history nikalo
        history = spam_tracker[chat_id][user_id]

        # SPAM_WINDOW (5.0 sec) check karega. Jo purane ho gaye unko hata dega
        history = [(t, m_id) for t, m_id in history if now_time - t < SPAM_WINDOW]
        
        # Naya message history mein add karo
        history.append((now_time, msg_id))
        spam_tracker[chat_id][user_id] = history

        msg_count = len(history)

        # ============== ANTI-SPAM (DELETE ONLY) ==============
        if msg_count >= SPAM_MUTE_LIMIT:
            # 1. Collect and delete all spam messages
            spam_message_ids = [m for _, m in history]
            try:
                await client.delete_messages(chat_id=chat_id, message_ids=spam_message_ids)
            except Exception:
                pass
            
            # 2. Reset the tracker for this user
            spam_tracker[chat_id][user_id] = []

            # 3. Stop all further processing for this message
            raise StopPropagation
        
        # DELETE LIMIT (Only deletes spam messages, NO MUTING)
        if msg_count > SPAM_DELETE_LIMIT:
            spam_message_ids = [m for _, m in history]
            try:
                # Delete all detected spam messages in one go
                await client.delete_messages(chat_id=chat_id, message_ids=spam_message_ids)
                
                # Optional: Send a temporary warning instead of a mute
                # warn_msg = await client.send_message(chat_id, "🚫 Spamming is not allowed here!")
                # asyncio.create_task(delete_msg_later(client, chat_id, warn_msg.id, 5))
                
            except Exception:
                pass
            raise StopPropagation # 🛑 STOP processing to prevent further spam

import asyncio
import threading
import psutil
import re
from datetime import datetime
from collections import defaultdict
from typing import List, Tuple

# Pyrogram imports
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, User
from pyrogram.enums import ChatAction, ChatMemberStatus, MessageEntityType
from pyrogram.errors import RPCError

from motor.motor_asyncio import AsyncIOMotorClient

import logging
logger = logging.getLogger(__name__)

# Ensure IST is defined (assuming it was defined earlier in your script)
IST = pytz.timezone("Asia/Kolkata")

# Assuming ADMIN_IDS and OWNER_ID are defined somewhere above in your main script
# ADMIN_IDS = [123456789]
# OWNER_ID = 123456789
# nsfw_detector = ... (from previous part)
# caches (locks_cache, etc.) are assumed to be initialized earlier.

# ============== ENHANCED DATABASE (MONGODB SYNCED) ==============
class Database:
    def __init__(self):
        self.data = {
            'groups': {}, 'warns': {}, 'rules': {}, 
            'group_admins': {}, 'pinned_messages': {}, 'muted_users': {}, 
            'banned_users': {}, 'tagging_sessions': {}, 'message_history': defaultdict(list),
            'vc_invite_tracking': {}, 'bounty_points': {},
            'admins': set(ADMIN_IDS) if 'ADMIN_IDS' in globals() else set(),
            'bot_status': {'start_time': time.time(), 'total_messages': 0, 'total_commands': 0, 'total_groups': 0, 'uptime': 0}
        }
        self.lock = threading.Lock()
        self.spam_cooldown = {} 
        self.user_last_msg = {} 
        self.flood_control = defaultdict(lambda: defaultdict(list))
        self.tag_stop = {}

    def _run_async(self, coro_or_future):
        """Helper to run async mongo updates in the background without blocking sync functions"""
        async def task_wrapper():
            try:
                await coro_or_future
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Background DB Error: {e}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(task_wrapper())
        except RuntimeError:
            pass

    async def load_from_mongo(self):
        """Loads saved data from MongoDB into memory when bot starts"""
        print("🔄 Loading Database from MongoDB...")
        cursor = chats_col.find({})
        async for chat in cursor:
            chat_id = chat.get("chat_id")
            if not chat_id: continue
            
            # Load group specific data (rules, free settings, etc)
            if "group_data" in chat:
                self.data['groups'][chat_id] = chat["group_data"]
            
            # Load warnings
            if "warns" in chat:
                for uid_str, warn_list in chat["warns"].items():
                    self.data['warns'][f"{chat_id}_{uid_str}"] = warn_list
                    
            # Load mutes
            if "mutes" in chat:
                for uid_str, until_iso in chat["mutes"].items():
                    self.data['muted_users'][f"{chat_id}_{uid_str}"] = until_iso
                    
            # Load bans
            if "bans" in chat:
                for uid_str, ban_iso in chat["bans"].items():
                    self.data['banned_users'][f"{chat_id}_{uid_str}"] = ban_iso
                    
            # Load bounty points
            if "bounty_points" in chat:
                self.data['bounty_points'][chat_id] = chat["bounty_points"]
                
        print("✅ Database loaded successfully from MongoDB!")

    def save_group_data(self, group_id: int, key: str, value):
        with self.lock:
            if group_id not in self.data['groups']:
                self.data['groups'][group_id] = {}
            self.data['groups'][group_id][key] = value
        # Sync to Mongo
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$set": {f"group_data.{key}": value}}, upsert=True))

    def get_group_data(self, group_id: int, key: str, default=None):
        return self.data['groups'].get(group_id, {}).get(key, default)

    def add_warn(self, user_id: int, group_id: int, reason: str = "No reason"):
        key = f"{group_id}_{user_id}"
        warn_obj = {'time': datetime.now().isoformat(), 'reason': reason, 'by': 'system'}
        with self.lock:
            if key not in self.data['warns']:
                self.data['warns'][key] = []
            self.data['warns'][key].append(warn_obj)
        # Sync to Mongo
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$push": {f"warns.{user_id}": warn_obj}}, upsert=True))

    def get_warns(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        return self.data['warns'].get(key, [])

    def remove_warn(self, user_id: int, group_id: int, warn_index: int = -1):
        key = f"{group_id}_{user_id}"
        success = False
        with self.lock:
            if key in self.data['warns']:
                if warn_index == -1:
                    del self.data['warns'][key]
                    success = True
                elif 0 <= warn_index < len(self.data['warns'][key]):
                    self.data['warns'][key].pop(warn_index)
                    if not self.data['warns'][key]:
                        del self.data['warns'][key]
                    success = True
        if success:
            # Sync to Mongo
            current_warns = self.data['warns'].get(key, [])
            if not current_warns:
                self._run_async(chats_col.update_one({"chat_id": group_id}, {"$unset": {f"warns.{user_id}": ""}}))
            else:
                self._run_async(chats_col.update_one({"chat_id": group_id}, {"$set": {f"warns.{user_id}": current_warns}}))
        return success

    def reset_warns(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        with self.lock:
            if key in self.data['warns']:
                del self.data['warns'][key]
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$unset": {f"warns.{user_id}": ""}}))
        return True

    def mute_user(self, user_id: int, group_id: int, until: datetime):
        key = f"{group_id}_{user_id}"
        iso_time = until.isoformat()
        with self.lock:
            self.data['muted_users'][key] = iso_time
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$set": {f"mutes.{user_id}": iso_time}}, upsert=True))

    def unmute_user(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        with self.lock:
            if key in self.data['muted_users']:
                del self.data['muted_users'][key]
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$unset": {f"mutes.{user_id}": ""}}))
        return True

    def is_muted(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        muted_until = self.data['muted_users'].get(key)
        if muted_until:
            try:
                until_time = datetime.fromisoformat(muted_until)
                return datetime.now() < until_time
            except: return False
        return False

    def ban_user(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        iso_time = datetime.now().isoformat()
        with self.lock:
            self.data['banned_users'][key] = iso_time
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$set": {f"bans.{user_id}": iso_time}}, upsert=True))

    def unban_user(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        with self.lock:
            if key in self.data['banned_users']:
                del self.data['banned_users'][key]
        self._run_async(chats_col.update_one({"chat_id": group_id}, {"$unset": {f"bans.{user_id}": ""}}))
        return True

    def is_banned(self, user_id: int, group_id: int):
        key = f"{group_id}_{user_id}"
        return key in self.data['banned_users']

    def reward_bounty_hunters(self, chat_id: int, user_ids: list, points: int = 10):
        with self.lock:
            if chat_id not in self.data['bounty_points']:
                self.data['bounty_points'][chat_id] = {}
            for uid in user_ids:
                str_uid = str(uid)
                current_points = self.data['bounty_points'][chat_id].get(str_uid, 0)
                new_points = current_points + points
                self.data['bounty_points'][chat_id][str_uid] = new_points
                self._run_async(chats_col.update_one({"chat_id": chat_id}, {"$set": {f"bounty_points.{str_uid}": new_points}}, upsert=True))

    # --- Ephemeral / Session Methods (Not Synced) ---
    def is_admin(self, user_id: int): return user_id in self.data['admins']
    def pin_message(self, group_id: int, message_id: int): self.data['pinned_messages'][group_id] = message_id
    def unpin_message(self, group_id: int):
        if group_id in self.data['pinned_messages']: del self.data['pinned_messages'][group_id]; return True
        return False
    def start_tagging_session(self, group_id: int, tag_type: str, message_id: int):
        self.data['tagging_sessions'][group_id] = {'type': tag_type, 'message_id': message_id, 'start_time': time.time()}
    def stop_tagging_session(self, group_id: int):
        if group_id in self.data['tagging_sessions']: del self.data['tagging_sessions'][group_id]; return True
        return False
    def get_tagging_session(self, group_id: int): return self.data['tagging_sessions'].get(group_id)
    def update_bot_stats(self, stat_type: str = "message"):
        if stat_type == "message": self.data['bot_status']['total_messages'] += 1
        elif stat_type == "command": self.data['bot_status']['total_commands'] += 1
        elif stat_type == "group": self.data['bot_status']['total_groups'] += 1
        self.data['bot_status']['uptime'] = time.time() - self.data['bot_status']['start_time']
    def add_message_to_history(self, group_id: int, user_id: int, name: str, message: str):
        if len(self.data['message_history'][group_id]) >= 100: self.data['message_history'][group_id].pop(0)
        self.data['message_history'][group_id].append({'user_id': user_id, 'name': name, 'message': message, 'time': datetime.now().isoformat()})
    def update_last_message(self, user_id: int): self.user_last_msg[user_id] = datetime.now(IST)
    def check_spam(self, user_id: int) -> bool:
        last_msg_time = self.user_last_msg.get(user_id)
        if not last_msg_time: return False
        return (datetime.now(IST) - last_msg_time).total_seconds() < 1.0
    def check_flood(self, chat_id: int, user_id: int) -> bool:
        now = datetime.now(IST)
        self.flood_control[chat_id][user_id] = [t for t in self.flood_control[chat_id][user_id] if (now - t).total_seconds() < 3]
        self.flood_control[chat_id][user_id].append(now)
        return len(self.flood_control[chat_id][user_id]) > 5
    def get_free_settings(self, group_id: int):
        default_settings = {'night': False, 'start': '22:00', 'end': '08:00', 'media': False, 'flood': False, 'spam': False}
        return self.get_group_data(group_id, 'security_settings') or default_settings
    def save_free_settings(self, group_id: int, settings: dict):
        self.save_group_data(group_id, 'security_settings', settings)
        settings_cache.delete(f"free:{group_id}")
    def add_bounty_report(self, chat_id: int, message_id: int, user_id: int):
        key = f"{chat_id}_{message_id}"
        if 'bounties' not in self.data: self.data['bounties'] = {}
        if key not in self.data['bounties']: self.data['bounties'][key] = set()
        self.data['bounties'][key].add(user_id)
        return len(self.data['bounties'][key]), list(self.data['bounties'][key])
    def add_vc_invite(self, chat_id: int, inviter_id: int, invited_id: int):
        self.data['vc_invite_tracking'][f"{chat_id}_{invited_id}"] = {'inviter': inviter_id, 'time': datetime.now().isoformat()}
    def get_vc_invite(self, chat_id: int, invited_id: int): return self.data['vc_invite_tracking'].get(f"{chat_id}_{invited_id}")

db = Database()


# -------------------- DATABASE --------------------
MONGO_URL = os.environ.get("MONGO_URL", "mongodb+srv://admin:Rishi9708697440@cluster0.pfafhkp.mongodb.net/?appName=Cluster0")
db_client = AsyncIOMotorClient(MONGO_URL)
mongo_db = db_client["sticker_manager"]
chats_col = mongo_db["chats"]
stats_col = mongo_db["chat_stats"]

# (Database Helpers for Pyrogram remain completely identical as they do not rely on framework types)
# ... [Keep your async database helper methods like is_locked, set_lock, etc. exactly as they were] ...

# MongoDB collection for GBans
gbans_col = mongo_db["gbans"]

async def is_gbanned(user_id: int):
    # 1. Pehle cache check karein
    cached_status = gban_cache.get(user_id)
    if cached_status is not None:
        return cached_status

    # 2. Agar cache mein nahi hai, toh DB se nikalein
    user = await gbans_col.find_one({"user_id": user_id})
    status = bool(user)
    
    # 3. Result ko cache mein save karein
    gban_cache.set(user_id, status)
    return status

# ==========================================
#           UTILS & HELPERS (Pyrogram Compatible)
# ==========================================

def escape_markdown(text):
    if not text:
        return ""
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

def format_text(text, user: User, chat_title=None):
    if not text:
        return ""
    now = datetime.now(IST)

    fullname = f"{user.first_name} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "No Username"

    fullname_escaped = escape_markdown(fullname)
    username_escaped = escape_markdown(username)
    chat_escaped = escape_markdown(chat_title) if chat_title else ""

    # Generate mention manually
    mention = f"[{escape_markdown(user.first_name)}](tg://user?id={user.id})"

    text = text.replace("{name}", mention)
    text = text.replace("{fullname}", fullname_escaped)
    text = text.replace("{username}", username_escaped)
    text = text.replace("{id}", str(user.id))
    text = text.replace("{date}", now.strftime("%d-%m-%Y"))
    text = text.replace("{time}", now.strftime("%I:%M %p"))
    if chat_title is not None:
        text = text.replace("{chat}", chat_escaped)

    return text

async def can_manage(client: Client, message: Message):
    """Check if the bot and the user have the necessary admin rights."""
    chat_id = message.chat.id
    user_id = message.from_user.id

    # 1. Pehle check karein ki bot group me admin hai ya nahi
    try:
        bot_member = await client.get_chat_member(chat_id, (await client.get_me()).id)
        if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text("❌ Main is group me admin nahi hu.")
            return False
            
        # Agar bot admin hai, tab safely uski permissions check karein
        if bot_member.status != ChatMemberStatus.OWNER:
            if not bot_member.privileges or not bot_member.privileges.can_change_info:
                await message.reply_text("❌ Mere paas 'Change Group Info' permission nahi hai. Kripya mujhe ye permission dein.")
                return False
    except Exception:
        return False

    # 2. Check karein ki message bhejne wala Sudo (Owner) hai kya
    if user_id == OWNER_ID:
        return True

    # 3. Check karein ki user group me admin hai kya
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await message.reply_text("❌ Aapke paas ye command chalane ki permission nahi hai.")
            return False
    except Exception:
        return False
        
    return True

async def delete_msg_later(client: Client, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass

def get_perm_keyboard(user_id, perms):
    def txt(key, label):
        status = "✅" if perms.get(key, False) else "❌"
        return f"{label} {status}"

    keyboard = [
        [
            InlineKeyboardButton(txt("spam", "Spam"), callback_data=f"perm_spam_{user_id}"),
            InlineKeyboardButton(txt("flood", "Flood"), callback_data=f"perm_flood_{user_id}")
        ],
        [
            InlineKeyboardButton(txt("media", "Media"), callback_data=f"perm_media_{user_id}"),
            InlineKeyboardButton(txt("check", "Check"), callback_data=f"perm_check_{user_id}")
        ],
        [InlineKeyboardButton(txt("night_silence", "Night Silence"), callback_data=f"perm_night_silence_{user_id}")],
        [InlineKeyboardButton("🔒 Unfree (Reset)", callback_data=f"perm_reset_{user_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def parse_time_to_hour(time_str):
    time_str = time_str.strip().lower()
    match = re.match(r'(\d{1,2})(?::\d{2})?\s*(am|pm)?', time_str)
    if not match:
        return None
    hour = int(match.group(1))
    modifier = match.group(2)

    if modifier == 'pm' and hour != 12:
        hour += 12
    elif modifier == 'am' and hour == 12:
        hour = 0

    if 0 <= hour <= 23:
        return hour
    return None

def format_time_duration(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)

async def is_admin(client: Client, message: Message) -> bool:
    user_id = message.from_user.id
    chat_id = message.chat.id

    cache_key = f"admin:{chat_id}:{user_id}"
    cached = admin_cache.get(cache_key) # Ensure admin_cache is defined
    if cached is not None:
        return cached

    if db.is_admin(user_id):
        admin_cache.set(cache_key, True)
        return True

    if message.chat.type.value in ['group', 'supergroup']:
        try:
            member = await client.get_chat_member(chat_id, user_id)
            result = member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
            admin_cache.set(cache_key, result)
            return result
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            return False

    return False

async def is_bot_admin(client: Client, message: Message) -> bool:
    if message.chat.type.value not in ['group', 'supergroup']:
        return True

    chat_id = message.chat.id
    try:
        bot_id = (await client.get_me()).id
        bot_member = await client.get_chat_member(chat_id, bot_id)
        return bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.error(f"Error checking bot admin status: {e}")
        return False

async def get_admins_list_with_status(client: Client, message: Message) -> List[Tuple[User, str]]:
    """Get list of admins in group with their status"""
    chat_id = message.chat.id
    admins = []

    try:
        bot_id = (await client.get_me()).id
        async for admin in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            if admin.user.id != bot_id:
                admins.append((admin.user, admin.status.value))
    except Exception as e:
        logger.error(f"Error getting admin list: {e}")

    return admins

async def get_admins_list(client: Client, message: Message) -> List[User]:
    """Get list of admins in group (without bot)"""
    admins_with_status = await get_admins_list_with_status(client, message)
    return [admin for admin, status in admins_with_status]

async def send_typing_action(client: Client, message: Message):
    try:
        await client.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING
        )
        await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"Error sending typing action: {e}")

async def get_user_info(client: Client, message: Message, target_user_id: int = None):
    """Get detailed user info including bio, IST joining time, and status"""
    if not target_user_id:
        if message.reply_to_message:
            target_user_id = message.reply_to_message.from_user.id
        else:
            target_user_id = message.from_user.id

    try:
        user = await client.get_chat(target_user_id)
        chat = message.chat

        # Defaults
        joined_str = "Not in Group"
        status_str = "Unknown"
        is_admin = False

        # Get member info for joining date and status
        try:
            member = await client.get_chat_member(chat.id, target_user_id)
            
            # Map the Pyrogram status to readable text
            if member.status == enums.ChatMemberStatus.OWNER:
                status_str = "👑 Owner"
                is_admin = True
            elif member.status == enums.ChatMemberStatus.ADMINISTRATOR:
                status_str = "👮‍♂️ Admin"
                is_admin = True
            elif member.status == enums.ChatMemberStatus.MEMBER:
                status_str = "👤 Member"
            elif member.status == enums.ChatMemberStatus.RESTRICTED:
                status_str = "🔇 Restricted/Muted"
            elif member.status == enums.ChatMemberStatus.BANNED:
                status_str = "🚫 Banned"
            elif member.status == enums.ChatMemberStatus.LEFT:
                status_str = "🚶‍♂️ Left"

            joined_date = member.joined_date
            if joined_date:
                # joined_date in Pyrogram is a standard datetime
                joined_date_ist = joined_date.astimezone(IST) if joined_date.tzinfo else pytz.utc.localize(joined_date).astimezone(IST)
                joined_str = joined_date_ist.strftime("%d-%m-%Y | %I:%M %p")
        except RPCError:
            pass # Defaults are already set above

        # Get warnings
        warns = db.get_warns(target_user_id, chat.id)
        warn_count = len(warns)

        # Get user bio/description
        bio = user.bio if getattr(user, "bio", None) else "No Bio/Work added."

        return {
            'user': user,
            'joined_str': joined_str,
            'status_str': status_str,
            'is_admin': is_admin,
            'warn_count': warn_count,
            'bio': bio
        }
    except Exception as e:
        logger.error(f"Error getting user info: {e}")
        return None
    
async def extract_target(client: Client, message: Message) -> tuple[int | None, str | None, str]:
    """
    Extracts target user ID, Name, and Reason from Reply, ID, Username, or Name.
    Returns: (user_id, user_name, reason/custom_text)
    """
    chat_id = message.chat.id
    
    # Command arguments (excluding the command itself)
    args = message.command[1:] if message.command and len(message.command) > 1 else []

    # 1. Check for Reply first
    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        reason = " ".join(args) if args else "No reason"
        return user.id, user.first_name, reason

    if not args:
        return None, None, "❗ Kripya kisi ko reply karein, ya User ID/Username/Name mention karein."

    identifier = args[0]
    reason = " ".join(args[1:]) if len(args) > 1 else "No reason"

    def _normalize_member_name(member_value):
        """Supports legacy string format and future dict-based member records."""
        if isinstance(member_value, dict):
            return member_value.get("name") or member_value.get("first_name") or ""
        return str(member_value or "")
    
    async def _resolve_from_identifier(raw_identifier: str):
        """Resolve user from ID, @username, or bare username."""
        # User ID
        if raw_identifier.isdigit() or (raw_identifier.startswith('-') and raw_identifier[1:].isdigit()):
            try:
                user_id = int(raw_identifier)
                chat_user = await client.get_users(user_id)
                return user_id, chat_user.first_name or "User"
            except Exception:
                return None, None

        # Username with or without @
        candidate = raw_identifier if raw_identifier.startswith('@') else f"@{raw_identifier}"
        if len(candidate) > 1:
            try:
                chat_user = await client.get_users(candidate)
                return chat_user.id, chat_user.first_name or "User"
            except Exception:
                return None, None

        return None, None


    # 2. Check for Text Mention (Entity)
    entities = message.entities or message.caption_entities
    if entities:
        for entity in entities:
            if entity.type == MessageEntityType.TEXT_MENTION:
                return entity.user.id, entity.user.first_name, reason
            if entity.type == MessageEntityType.MENTION and message.text:
                entity_username = message.text[entity.offset: entity.offset + entity.length]
                if entity_username:
                    resolved_id, resolved_name = await _resolve_from_identifier(entity_username)
                    if resolved_id:
                        return resolved_id, resolved_name, reason
    # 3. Check for User ID / @Username / bare username
    resolved_id, resolved_name = await _resolve_from_identifier(identifier)
    if resolved_id:
        return resolved_id, resolved_name, reason

    # 4. Name Search (Agar Group Data/Memory me naam match ho jaye)
    members = db.get_group_data(chat_id, 'members', {})

    # Multi-word names ko support karne ke liye longest prefix match
    lowered_name_map = {
        str_uid: _normalize_member_name(name).strip()
        for str_uid, name in members.items()
    }

    for consume_count in range(len(args), 0, -1):
        possible_name = " ".join(args[:consume_count]).strip().lower()
        if not possible_name:
            continue

        for str_uid, display_name in lowered_name_map.items():
            name_lower = display_name.lower()
            if not name_lower:
                continue

            if possible_name == name_lower or possible_name in name_lower.split():
                custom_reason = " ".join(args[consume_count:]) or "No reason"
                return int(str_uid), display_name, custom_reason

    # Agar kuch bhi match nahi hua
    return None, None, "❌ User nahi mila. Kripya sahi ID, Username, ya Reply ka use karein."



# ============== KEYBOARDS ==============
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🛡️ Security", callback_data="menu_security"),
            InlineKeyboardButton("⚖️ Moderation", callback_data="menu_moderation")
        ],
        [
            InlineKeyboardButton("🎮 Fun", callback_data="menu_fun"),
            InlineKeyboardButton("📊 Stats", callback_data="menu_stats")
        ],
        [
            InlineKeyboardButton("ℹ️ About", callback_data="menu_about"),
            InlineKeyboardButton("🔙 Back", callback_data="start_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

def get_tagging_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🛑 Cancel Tagging", callback_data="cancel_tagging"),
            InlineKeyboardButton("✅ Confirm", callback_data="confirm_tagging")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_confirmation_keyboard(action: str, target_id: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"confirm_{action}_{target_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"cancel_{action}_{target_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ============== USER PERMISSIONS (FREE) HELPERS ==============
async def get_user_perms(chat_id, user_id):
    cache_key = f"perms:{chat_id}:{user_id}"
    cached = perms_cache.get(cache_key)
    if cached is not None:
        return cached
    
    chat = await chats_col.find_one({"chat_id": chat_id})
    perms = chat.get("perms", {}).get(str(user_id), {}) if chat else {}
    perms_cache.set(cache_key, perms)
    return perms

async def set_user_perm(chat_id, user_id, perm: str, value: bool):
    await chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {f"perms.{user_id}.{perm}": value}},
        upsert=True
    )
    perms_cache.delete(f"perms:{chat_id}:{user_id}")

async def reset_user_perms(chat_id, user_id):
    await chats_col.update_one(
        {"chat_id": chat_id},
        {"$unset": {f"perms.{user_id}": ""}}
    )
    perms_cache.delete(f"perms:{chat_id}:{user_id}")

# ============== SUDO (GLOBAL ADMIN) HELPERS ==============
async def is_sudo(user_id):
    if user_id == OWNER_ID:
        return True
    cached = sudo_cache.get("sudos")
    if cached is not None:
        return user_id in cached
    doc = await mongo_db["bot_settings"].find_one({"_id": "sudos"})
    sudos = doc.get("list", []) if doc else []
    sudo_cache.set("sudos", sudos)
    return user_id in sudos

async def add_sudo_db(user_id):
    await mongo_db["bot_settings"].update_one({"_id": "sudos"}, {"$addToSet": {"list": user_id}}, upsert=True)
    sudo_cache.delete("sudos")

async def rm_sudo_db(user_id):
    await mongo_db["bot_settings"].update_one({"_id": "sudos"}, {"$pull": {"list": user_id}}, upsert=True)
    sudo_cache.delete("sudos")

# ============== GREETING (WELCOME/GOODBYE) HELPERS ==============
async def set_greet(chat_id, type_str, content):
    await chats_col.update_one({"chat_id": chat_id}, {"$set": {type_str: content}}, upsert=True)

async def get_greet(chat_id, type_str):
    chat = await chats_col.find_one({"chat_id": chat_id})
    return chat.get(type_str) if chat else None

async def del_greet(chat_id, type_str):
    await chats_col.update_one({"chat_id": chat_id}, {"$unset": {type_str: ""}})

async def set_welcome_enabled(chat_id, enabled: bool):
    await chats_col.update_one({"chat_id": chat_id}, {"$set": {"welcome_enabled": enabled}}, upsert=True)

async def get_welcome_enabled(chat_id):
    chat = await chats_col.find_one({"chat_id": chat_id})
    if chat and "welcome_enabled" in chat:
        return chat["welcome_enabled"]
    return True

async def set_goodbye_enabled(chat_id, enabled: bool):
    await chats_col.update_one({"chat_id": chat_id}, {"$set": {"goodbye_enabled": enabled}}, upsert=True)

async def get_goodbye_enabled(chat_id):
    chat = await chats_col.find_one({"chat_id": chat_id})
    if chat and "goodbye_enabled" in chat:
        return chat["goodbye_enabled"]
    return True

def add_profile_to_template(bg, pfp_path):
    circle_size = 520   # adjust if needed
    circle_x = 1420     # adjust according to template
    circle_y = 540

    pfp = Image.open(pfp_path).convert("RGBA")

    min_side = min(pfp.size)
    pfp = pfp.crop((
        (pfp.width - min_side) // 2,
        (pfp.height - min_side) // 2,
        (pfp.width + min_side) // 2,
        (pfp.height + min_side) // 2
    ))

    pfp = pfp.resize((circle_size, circle_size))

    mask = Image.new("L", (circle_size, circle_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, circle_size, circle_size), fill=255)

    paste_x = circle_x - circle_size // 2
    paste_y = circle_y - circle_size // 2

    bg.paste(pfp, (paste_x, paste_y), mask)

    return bg

# ============== BOT STATUS HELPER FUNCTION ==============
async def generate_bot_status_text() -> str:
    """Generate bot status text"""
    db.update_bot_stats()

    try:
        bot_stats = db.data['bot_status']
        uptime = format_time_duration(bot_stats['uptime'])
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        status_text = "<b>🤖 Bot Status 🤖</b>\n\n"
        status_text += "<b>Bot Statistics:</b>\n"
        status_text += f"• Uptime: {uptime}\n"
        status_text += f"• Total Messages: {bot_stats['total_messages']}\n"
        status_text += f"• Total Commands: {bot_stats['total_commands']}\n"
        status_text += f"• Monitored Groups: {len(db.data['groups'])}\n\n"

        status_text += "<b>Bot Features Status:</b>\n"
        status_text += "• Moderation System: ✅\n"
        status_text += "• Tagging System: ✅\n"
        status_text += "• Admin Tools: ✅\n"
        status_text += "• Database: ✅\n"
        status_text += "• Security System: ✅\n"
        status_text += "• VC Monitor: ✅\n"
        
        status_text += f"<i>Last Updated: {current_time}</i>"

        return status_text

    except Exception as e:
        logger.error(f"Error generating bot status: {e}")
        return f"Error getting status: {e}"
    
import html
import random
import asyncio
import platform
import psutil
from datetime import datetime, timedelta

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatPermissions, ChatPrivileges
)
from pyrogram.errors import RPCError

import edge_tts

# ==========================================
#            COMMAND HANDLERS
# ==========================================

async def start_command(client: Client, message: Message):
    await send_typing_action(client, message)

    user = message.from_user
    chat = message.chat

    db.update_bot_stats("command")

    welcome_text = f"""
✨ <b>Welcome {html.escape(user.first_name)}!</b> ✨

I'm a complete multi-purpose Telegram bot with advanced features for both groups and channels.

<b>🤖 Advanced Features:</b>
• Complete Group Management
• Security System (Spam/Flood/Media/Night Controls)  
• Edit Monitoring 
• Bot Status Monitoring
• And much more!

<b>📜</b> Select a category below.
    """

    if chat.type == enums.ChatType.PRIVATE:
        me = await client.get_me()
        buttons = [
            [InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{me.username}?startgroup=true")],
            [
                InlineKeyboardButton("📖 Help Menu", callback_data="main_help"),
                InlineKeyboardButton("🆘 Support", url="https://t.me/+rjE5xZlIK4U3ODA1") 
            ]
        ]

        await message.reply_text(
            welcome_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        
        # Save private user to database for broadcasts
        await chats_col.update_one(
            {"chat_id": chat.id},
            {"$set": {"title": user.first_name, "type": "private", "active": True}},
            upsert=True
        )
    else:
        if chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            await chats_col.update_one(
                {"chat_id": chat.id},
                {
                    "$set": {
                        "title": chat.title,
                        "type": chat.type.value,
                        "active": True
                   }
                },
                upsert=True
           )

        group_welcome = f"Hello {html.escape(user.first_name)}! I'm here to help manage {html.escape(chat.title or 'this group')}\n\nuse /menu to see what I can do."

        await message.reply_text(
            group_welcome,
            parse_mode=enums.ParseMode.HTML
        )

async def help_command(client: Client, message: Message):
    await send_typing_action(client, message)
    db.update_bot_stats("command")

    help_text = """
<b>🤖 COMPLETE BOT HELP MENU</b>

<b>📱</b> SELECT A CATEGORY BELOW:
    """

    await message.reply_text(
        help_text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Open Menu", callback_data="main_menu")]
        ])
    )

async def list_filters_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    filters_data = await get_all_filters(message.chat.id)
    if not filters_data:
        await message.reply_text("📭 No filters set in this group.")
        return
    keywords = list(filters_data.keys())
    text = "**📋 Active filters:**\n" + "\n".join(f"• `{kw}`" for kw in keywords)
    await message.reply_text(text)

async def set_night_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    try:
        args = message.command[1:] if len(message.command) > 1 else []
        if len(args) != 2:
            await message.reply_text(
                "Usage: `/setnight <start> <end>`\n"
                "Examples:\n"
                "• `/setnight 23 6`\n"
                "• `/setnight 11pm 6am`\n"
                "• `/setnight 11:30 PM 6:00 AM`"
            )
            return
            
        start_str, end_str = args[0], args[1]
        start_hour = parse_time_to_hour(start_str)
        end_hour = parse_time_to_hour(end_str)

        if start_hour is None or end_hour is None:
            await message.reply_text("❌ Invalid time format. Use numbers (0-23) or AM/PM like `11pm`, `6am`.")
            return

        await set_night_config(message.chat.id, start_hour, end_hour)

        def format_hour(h):
            if h == 0: return "12 AM"
            elif h < 12: return f"{h} AM"
            elif h == 12: return "12 PM"
            else: return f"{h-12} PM"

        start_disp = format_hour(start_hour)
        end_disp = format_hour(end_hour)
        
        await message.reply_text(
            f"🌙 **Night Mode Set**\nFrom `{start_disp}` to `{end_disp}`.\n\n"
            "Users with ❌ Night Silence will have their messages automatically deleted during this period."
        )
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

async def night_off_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    await chats_col.update_one({"chat_id": message.chat.id}, {"$unset": {"night_mode": ""}})
    night_cache.delete(f"night:{message.chat.id}")
    await message.reply_text("🌙 **Night mode disabled.**\n\nUse `/setnight` to enable again.")

async def welcome_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    args = message.command[1:] if len(message.command) > 1 else []
    if len(args) != 1 or args[0].lower() not in ["on", "off"]:
        await message.reply_text("Usage: `/welcome on` ya `/welcome off`")
        return
    new_state = args[0].lower() == "on"
    await set_welcome_enabled(message.chat.id, new_state)
    await message.reply_text(f"✅ Welcome message {args[0].lower()} kar diya gaya.")

async def goodbye_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    args = message.command[1:] if len(message.command) > 1 else []
    if len(args) != 1 or args[0].lower() not in ["on", "off"]:
        await message.reply_text("Usage: `/goodbye on` ya `/goodbye off`")
        return
    new_state = args[0].lower() == "on"
    await set_goodbye_enabled(message.chat.id, new_state)
    await message.reply_text(f"✅ Goodbye message {args[0].lower()} kar diya gaya.")

async def lock_unlock_handler(client: Client, message: Message):
    if not await can_manage(client, message):
        return

    cmd = message.command[0].lower()
    status = True if cmd == "lock" else False
    args = message.command[1:] if len(message.command) > 1 else []

    if len(args) < 1:
        await message.reply_text(
            f"**Usage:** `/{cmd} <type>`\n"
            "**Types:** `all`, `text`, `sticker`, `media`, `link`, `poll`, `emoji`"
        )
        return

    input_type = args[0].lower()
    valid_types = ["text", "sticker", "media", "link", "poll", "emoji"]

    if input_type == "all":
        for t in valid_types:
            await set_lock(message.chat.id, t, status)
        state = "Locked" if status else "Unlocked"
        emoji = "🔒" if status else "🔓"
        await message.reply_text(f"{emoji} **Everything has been {state}!**")
        return

    if input_type in valid_types:
        await set_lock(message.chat.id, input_type, status)
        state = "Locked" if status else "Unlocked"
        emoji = "🔒" if status else "🔓"
        await message.reply_text(f"{emoji} **{input_type.capitalize()} is now {state}!**")
    else:
        await message.reply_text(f"❌ **Invalid type!**\nUse: `all` or `{', '.join(valid_types)}`")

async def approve_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return

    target_id, target_name, _ = await extract_target(client, message)
    if not target_id:
        await message.reply_text("❗ Please reply to a user, or mention their User ID, Username, or Name.")
        return

    chat_id = message.chat.id
    import html
    safe_name = html.escape(target_name or str(target_id))

    # 👇 ADMIN CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text(f"🛡️ <b>{safe_name}</b> is an Admin. Admins are already approved by default!", parse_mode=enums.ParseMode.HTML)
            return
    except Exception:
        pass
    # 👆 ADMIN CHECK 👆

    await add_approve(chat_id, target_id)
    await message.reply_text(
        f"✅ **{safe_name}** (`{target_id}`) has been approved.\nThey will now bypass group locks and anti-spam filters.",
        parse_mode=enums.ParseMode.HTML
    )

async def unapprove_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return

    target_id, target_name, _ = await extract_target(client, message)
    if not target_id:
        await message.reply_text("❗ Please reply to a user, or mention their User ID, Username, or Name.")
        return

    chat_id = message.chat.id
    import html
    safe_name = html.escape(target_name or str(target_id))

    if target_id == OWNER_ID:
        await message.reply_text("❌ The bot owner cannot be unapproved.")
        return

    # 👇 ADMIN CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text(f"🛡️ <b>{safe_name}</b> is an Admin. You cannot unapprove an admin!", parse_mode=enums.ParseMode.HTML)
            return
    except Exception:
        pass
    # 👆 ADMIN CHECK 👆

    await remove_approve(chat_id, target_id)
    await message.reply_text(
        f"❌ **{safe_name}** (`{target_id}`) has been unapproved.\nThey are now subject to regular group rules and filters.",
        parse_mode=enums.ParseMode.HTML
    )

async def free_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return

    target_id, target_name, _ = await extract_target(client, message)
    if not target_id:
        await message.reply_text("❗ Please reply to a user, or mention their User ID, Username, or Name.")
        return

    chat_id = message.chat.id
    import html
    safe_name = html.escape(target_name or "User")

    # 👇 ADMIN CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text(f"🛡️ <b>{safe_name}</b> is an Admin. Admins have all permissions by default!", parse_mode=enums.ParseMode.HTML)
            return
    except Exception:
        pass
    # 👆 ADMIN CHECK 👆

    perms = await get_user_perms(chat_id, target_id)

    await message.reply_text(
        f"**Permissions for {safe_name}:**",
        reply_markup=get_perm_keyboard(target_id, perms)
    )
    try:
        await message.delete()
    except Exception:
        pass

async def unfree_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return

    target_id, target_name, _ = await extract_target(client, message)
    if not target_id:
        await message.reply_text("❗ Please reply to a user, or mention their User ID, Username, or Name.")
        return

    chat_id = message.chat.id
    import html
    safe_name = html.escape(target_name or "User")

    # 👇 ADMIN CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text(f"🛡️ <b>{safe_name}</b> is an Admin. You cannot restrict an admin's permissions!", parse_mode=enums.ParseMode.HTML)
            return
    except Exception:
        pass
    # 👆 ADMIN CHECK 👆

    await reset_user_perms(chat_id, target_id)

    await message.reply_text(f"❌ **{safe_name} is now UNFREE.**", parse_mode=enums.ParseMode.HTML)
    try:
        await message.delete()
    except Exception:
        pass    

# ============== SUDO COMMANDS ==============
async def addsudo_cmd(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("🚫 Ye command sirf Bot Owner ke liye hai.")
        return
        
    target_id, target_name, _ = await extract_target(client, message)
    if not target_id: 
        await message.reply_text("❗ Kripya kisi ko reply karein, ya User ID/Username mention karein.")
        return
        
    await add_sudo_db(target_id)
    await message.reply_text(f"✅ {html.escape(target_name)} ko Sudo (Global Admin) bana diya gaya hai.", parse_mode=enums.ParseMode.HTML)

async def rmsudo_cmd(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("🚫 Ye command sirf Bot Owner ke liye hai.")
        return
        
    target_id, target_name, _ = await extract_target(client, message)
    if not target_id: 
        await message.reply_text("❗ Kripya kisi ko reply karein, ya User ID/Username mention karein.")
        return
        
    await rm_sudo_db(target_id)
    await message.reply_text(f"❌ {html.escape(target_name)} ko Sudo list se hata diya gaya hai.", parse_mode=enums.ParseMode.HTML)

async def sudolist_cmd(client: Client, message: Message):
    if message.from_user.id != OWNER_ID and not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return

    doc = await mongo_db["bot_settings"].find_one({"_id": "sudos"})
    sudos = doc.get("list", []) if doc else []

    if not sudos:
        await message.reply_text("📭 Koi Global Admin (Sudo) nahi hai.")
        return

    text = "👑 <b>Global Admins (Sudo) List:</b>\n\n"
    for sid in sudos:
        try:
            user = await client.get_users(sid)
            name = html.escape(user.first_name)
            text += f"• <a href='tg://user?id={sid}'>{name}</a> (<code>{sid}</code>)\n"
        except Exception:
            text += f"• User ID: <code>{sid}</code>\n"

    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

# ============== BLOCKED WORDS & STICKERS COMMANDS ==============
async def addword_cmd(client: Client, message: Message):
    if not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return
        
    word = None
    args = message.command[1:] if len(message.command) > 1 else []
    
    if args:
        word = args[0].lower()
    elif message.reply_to_message and message.reply_to_message.text:
        word = message.reply_to_message.text.split()[0].lower()

    if not word:
        await message.reply_text("❗ Usage: `/addword <word>` ya kisi word par reply karein.")
        return

    await add_bword(message.chat.id, word)
    await message.reply_text(f"✅ Word `{word}` block kar diya gaya hai.")

async def rmword_cmd(client: Client, message: Message):
    if not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return
        
    word = None
    args = message.command[1:] if len(message.command) > 1 else []
    
    if args:
        word = args[0].lower()
    elif message.reply_to_message and message.reply_to_message.text:
        word = message.reply_to_message.text.split()[0].lower()

    if not word:
        await message.reply_text("❗ Usage: `/rmword <word>` ya kisi word par reply karein.")
        return

    await rm_bword(message.chat.id, word)
    await message.reply_text(f"✅ Word `{word}` unblock kar diya gaya hai.")
    
async def bwordlist_cmd(client: Client, message: Message):
    if not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return
    words = await get_bwords(message.chat.id)
    if not words:
        await message.reply_text("📭 Koi blocked word nahi hai.")
        return
    text = "📝 **Blocked Words List:**\n" + "\n".join(f"• `{w}`" for w in words)
    await message.reply_text(text)

async def addspack_cmd(client: Client, message: Message):
    if not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return
    
    reply = message.reply_to_message
    pack_name = None
    args = message.command[1:] if len(message.command) > 1 else []
    
    if reply and reply.sticker and reply.sticker.set_name:
        pack_name = reply.sticker.set_name
    elif args:
        pack_name = args[0]
    else:
        await message.reply_text("❗ Kripya kisi sticker par reply karein ya pack ka naam likhein.")
        return

    await add_bspack(message.chat.id, pack_name)
    await message.reply_text(f"✅ Sticker pack `{pack_name}` block kar diya gaya hai.")

async def rmspack_cmd(client: Client, message: Message):
    if not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return
    
    reply = message.reply_to_message
    pack_name = None
    args = message.command[1:] if len(message.command) > 1 else []
    
    if reply and reply.sticker and reply.sticker.set_name:
        pack_name = reply.sticker.set_name
    elif args:
        pack_name = args[0]
    else:
        await message.reply_text("❗ Kripya kisi sticker par reply karein ya pack ka naam likhein.")
        return

    await rm_bspack(message.chat.id, pack_name)
    await message.reply_text(f"✅ Sticker pack `{pack_name}` unblock kar diya gaya hai.")

async def stickerlist_cmd(client: Client, message: Message):
    if not await is_sudo(message.from_user.id):
        await message.reply_text("🚫 Sirf Sudo (Global Admins) ise use kar sakte hain.")
        return
    packs = await get_bspacks(message.chat.id)
    if not packs:
        await message.reply_text("📭 Koi blocked sticker pack nahi hai.")
        return
    text = "📝 **Blocked Sticker Packs:**\n" + "\n".join(f"• `{p}`" for p in packs)
    await message.reply_text(text)

async def permission_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()

    user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id

    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status not in [enums.ChatMemberStatus.OWNER, enums.ChatMemberStatus.ADMINISTRATOR]:
            await callback_query.answer("Only Admins!", show_alert=True)
            return
    except RPCError:
        return

    data = callback_query.data[5:]  # remove "perm_"
    last_underscore = data.rfind('_')
    if last_underscore == -1:
        await callback_query.answer("Invalid callback data", show_alert=True)
        return

    action = data[:last_underscore]
    try:
        target_user_id = int(data[last_underscore+1:])
    except ValueError:
        await callback_query.answer("Invalid user ID", show_alert=True)
        return

    if action == "reset":
        await reset_user_perms(chat_id, target_user_id)
        await callback_query.answer("Permissions reset!")
        await callback_query.edit_message_text("❌ User permissions reset.")
        return

    current_perms = await get_user_perms(chat_id, target_user_id)
    new_val = not current_perms.get(action, False)
    await set_user_perm(chat_id, target_user_id, action, new_val)
    updated_perms = await get_user_perms(chat_id, target_user_id)
    
    await callback_query.edit_message_reply_markup(reply_markup=get_perm_keyboard(target_user_id, updated_perms))
    await callback_query.answer(f"{action} -> {new_val}")

async def add_filter_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    args = message.text.split(maxsplit=2)
    keyword = args[1].lower() if len(args) > 1 else None
    
    if not keyword:
        await message.reply_text("Usage: <code>/filter &lt;keyword&gt;</code>", parse_mode=enums.ParseMode.HTML)
        return

    content = {}
    reply = message.reply_to_message
    if reply:
        if reply.video:
            content = {'type': 'video', 'file_id': reply.video.file_id, 'caption': reply.caption or ""}
        elif reply.photo:
            content = {'type': 'photo', 'file_id': reply.photo.file_id, 'caption': reply.caption or ""}
        elif reply.sticker:
            content = {'type': 'sticker', 'file_id': reply.sticker.file_id}
        elif reply.text:
            content = {'type': 'text', 'text': reply.text}
    elif len(args) > 2:
        content = {'type': 'text', 'text': args[2]}
    else:
        await message.reply_text("Reply or text needed.")
        return

    await add_filter_db(message.chat.id, keyword, content)
    await message.reply_text(f"✅ Filter <b>{keyword}</b> added.", parse_mode=enums.ParseMode.HTML)

async def stop_filter_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    args = message.command[1:] if len(message.command) > 1 else []
    if len(args) < 1:
        await message.reply_text("Usage: `/stop <keyword>`")
        return
    await del_filter_db(message.chat.id, args[0].lower())
    await message.reply_text("🗑️ Filter deleted.")

async def set_welcome_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    reply = message.reply_to_message
    if not reply:
        await message.reply_text("Reply to a message.")
        return

    content = {}
    if reply.sticker:
        content = {'type': 'sticker', 'file_id': reply.sticker.file_id}
    elif reply.photo:
        content = {'type': 'photo', 'file_id': reply.photo.file_id, 'caption': reply.caption or ""}
    elif reply.video:
        content = {'type': 'video', 'file_id': reply.video.file_id, 'caption': reply.caption or ""}
    elif reply.text:
        content = {'type': 'text', 'text': reply.text}

    await set_greet(message.chat.id, "welcome", content)
    await message.reply_text("✅ Welcome set.")

async def set_goodbye_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    reply = message.reply_to_message
    if not reply:
        await message.reply_text("Reply to a message.")
        return

    content = {}
    if reply.sticker:
        content = {'type': 'sticker', 'file_id': reply.sticker.file_id}
    elif reply.photo:
        content = {'type': 'photo', 'file_id': reply.photo.file_id, 'caption': reply.caption or ""}
    elif reply.video:
        content = {'type': 'video', 'file_id': reply.video.file_id, 'caption': reply.caption or ""}
    elif reply.text:
        content = {'type': 'text', 'text': reply.text}

    await set_greet(message.chat.id, "goodbye", content)
    await message.reply_text("✅ Goodbye set.")

async def del_welcome_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    await del_greet(message.chat.id, "welcome")
    await message.reply_text("🗑 Welcome deleted.")

async def del_goodbye_cmd(client: Client, message: Message):
    if not await can_manage(client, message):
        return
    await del_greet(message.chat.id, "goodbye")
    await message.reply_text("🗑 Goodbye deleted.")

async def menu_command(client: Client, message: Message):
    await send_typing_action(client, message)

    menu_text = "<b>📱 Interactive Menu</b>\n\nSelect a category below:"

    await message.reply_text(
        menu_text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=get_main_menu_keyboard()
    )

async def botstatus_command(client: Client, message: Message):
    await send_typing_action(client, message)
    status_text = await generate_bot_status_text()

    await message.reply_text(
        status_text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_botstatus")]
        ])
    )

async def promote_command(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # Iss command ke liye sirf promote permission required hai
    try:
        actor_member = await client.get_chat_member(chat_id, user_id)
        actor_has_promote_perm = (
            actor_member.status == enums.ChatMemberStatus.OWNER or
            (actor_member.status == enums.ChatMemberStatus.ADMINISTRATOR and actor_member.privileges and actor_member.privileges.can_promote_members)
        )
    except Exception:
        actor_has_promote_perm = False

    if not actor_has_promote_perm:
        await message.reply_text("🚫 Aapke paas add admin (promote members) permission nahi hai.")
        return
        
    try:
        bot_id = (await client.get_me()).id
        bot_member = await client.get_chat_member(chat_id, bot_id)
        bot_has_promote_perm = (
            bot_member.status == enums.ChatMemberStatus.OWNER or
            (bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR and bot_member.privileges and bot_member.privileges.can_promote_members)
        )
    except Exception:
        bot_has_promote_perm = False

    if not bot_has_promote_perm:
        await message.reply_text("🚫 Mere paas add admin (promote members) permission nahi hai.")
        return

    target_id, target_name, custom_title = await extract_target(client, message)

    if not target_id:
        await message.reply_text(custom_title)
        return

    title = custom_title if custom_title != "No reason" else None
    if title and len(title) > 16:
        title = title[:16]

    # Promote with minimal required right so it works even when bot only has add-admin permission
    # (can_promote_members). Unrelated permissions are intentionally kept disabled.

    try:
        await client.promote_chat_member(
            chat_id=chat_id,
            user_id=target_id,
            privileges=ChatPrivileges(
                can_manage_chat=False,
                can_delete_messages=False,
                can_restrict_members=False,
                can_promote_members=True,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
                can_manage_video_chats=False
            )
        )
    except Exception as e:
        await message.reply_text(f"❌ Promotion failed: {e}")
        return

    safe_name = html.escape(target_name or "User")
    if title:
        try:
            await client.set_administrator_title(chat_id=chat_id, user_id=target_id, title=title)
            await message.reply_text(f"⚡ {safe_name} has been promoted with title: <b>{html.escape(title)}</b>!", parse_mode=enums.ParseMode.HTML)
        except Exception:
            await message.reply_text(f"⚡ {safe_name} has been promoted, but custom title could not be set.", parse_mode=enums.ParseMode.HTML)
    else:
        await message.reply_text(f"⚡ {safe_name} has been promoted to admin!", parse_mode=enums.ParseMode.HTML)


async def demote_command(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # Iss command ke liye sirf promote permission required hai
    try:
        actor_member = await client.get_chat_member(chat_id, user_id)
        actor_has_promote_perm = (
            actor_member.status == enums.ChatMemberStatus.OWNER or
            (actor_member.status == enums.ChatMemberStatus.ADMINISTRATOR and actor_member.privileges and actor_member.privileges.can_promote_members)
        )
    except Exception:
        actor_has_promote_perm = False

    if not actor_has_promote_perm:
        await message.reply_text("🚫 Aapke paas add admin (promote members) permission nahi hai.")
        return
        
    try:
        bot_id = (await client.get_me()).id
        bot_member = await client.get_chat_member(chat_id, bot_id)
        bot_has_promote_perm = (
            bot_member.status == enums.ChatMemberStatus.OWNER or
            (bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR and bot_member.privileges and bot_member.privileges.can_promote_members)
        )
    except Exception:
        bot_has_promote_perm = False

    if not bot_has_promote_perm:
        await message.reply_text("🚫 Mere paas add admin (promote members) permission nahi hai.")
        return

    target_id, target_name, _ = await extract_target(client, message)

    if not target_id:
        await message.reply_text("❗ Please reply to a user, or mention their User ID/Username/Name.")
        return

    try:
        await client.promote_chat_member(
            chat_id=chat_id,
            user_id=target_id,
            privileges=ChatPrivileges(
                can_manage_chat=False, can_delete_messages=False, can_restrict_members=False,
                can_promote_members=False, can_change_info=False, can_invite_users=False,
                can_pin_messages=False, can_manage_video_chats=False
            )
        )
        safe_name = html.escape(target_name or "User")
        await message.reply_text(f"📉 {safe_name} has been demoted to member.", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def purge_command(client: Client, message: Message):
    chat = message.chat
    user = message.from_user

    try:
        member = await client.get_chat_member(chat.id, user.id)
        if member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("🚫 Sirf Admins ye command use kar sakte hain.")
            return
    except RPCError:
        return

    if not message.reply_to_message:
        await message.reply_text("❗ Please reply to the first message.")
        return

    message_id_start = message.reply_to_message.id
    message_id_end = message.id

    status_msg = await message.reply_text("⏳ Deleting messages... Please wait.")

    # In Pyrogram, we can bulk delete
    message_ids = list(range(message_id_start, message_id_end + 1))
    
    # Chunking limits to 100 messages per request
    deleted_count = 0
    for i in range(0, len(message_ids), 100):
        chunk = message_ids[i:i+100]
        try:
            await client.delete_messages(chat_id=chat.id, message_ids=chunk)
            deleted_count += len(chunk)
        except Exception:
            pass

    try:
        await status_msg.edit_text(f"✅ **Purge Complete!**\n🗑️ Deleted {deleted_count} messages.")
        await asyncio.sleep(3)
        await status_msg.delete()
    except Exception:
        pass

# 👇 YE LINE ADD KARNI HAI 👇
async def info_command(client: Client, message: Message):
    await send_typing_action(client, message)

    chat = message.chat
    chat_id = chat.id
    args = message.command[1:] if len(message.command) > 1 else []

    # ================= CHAT INFO MODE =================
    if args and args[0].lower() == "chat":
        try:
            member_count = await client.get_chat_members_count(chat_id)

            chat_info_text = f"""
<b>💬 Chat Information</b>
━━━━━━━━━━━━━━━━━━━━
• <b>Title:</b> {html.escape(chat.title or "Private Chat")}
• <b>Chat ID:</b> <code>{chat.id}</code>
• <b>Type:</b> {chat.type.value}
• <b>Total Members:</b> {member_count}
• <b>Description:</b> {html.escape(chat.description or "No Description")}
━━━━━━━━━━━━━━━━━━━━
"""
            await message.reply_text(chat_info_text, parse_mode=enums.ParseMode.HTML)
            return
        except Exception:
            await message.reply_text("❌ Unable to fetch chat info.")
            return

    # ================= USER INFO MODE =================
    if message.reply_to_message or args:
        target_id, target_name, _ = await extract_target(client, message)
        if not target_id:
            await message.reply_text("❗ Please reply to a user, or mention their User ID, Username, or Name.")
            return
    else:
        target_id = message.from_user.id

    user_info = await get_user_info(client, message, target_id)

    if not user_info:
        await message.reply_text("❌ Could not fetch user information.")
        return

    user = user_info['user']
    safe_name = html.escape(user.first_name or "")
    safe_last = html.escape(user.last_name or "")
    safe_username = html.escape(user.username or 'None')

    # 👇 Dynamically show warnings only if the user is NOT an admin 👇
    warns_line = ""
    if not user_info['is_admin']:
        warns_line = f"\n• <b>Warns:</b> {user_info['warn_count']}/3"

    info_text = f"""
<b>👤 User Information</b>
━━━━━━━━━━━━━━━━━━━━
• <b>Name:</b> {safe_name} {safe_last}
• <b>ID:</b> <code>{user.id}</code>
• <b>Username:</b> @{safe_username}
• <b>Status:</b> {user_info['status_str']}
• <b>Joined (IST):</b> <code>{user_info['joined_str']}</code>{warns_line}
• <b>Work/Bio:</b> <code>{html.escape(user_info['bio'])}</code>
━━━━━━━━━━━━━━━━━━━━
"""
    await message.reply_text(info_text, parse_mode=enums.ParseMode.HTML)

async def bounty_command(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("🎯 Please reply to a spam or toxic message with `/bounty` to place a hit on it!")
        return

    chat = message.chat
    chat_id = chat.id
    reporter = message.from_user
    target_msg = message.reply_to_message
    target_user = target_msg.from_user

    if target_user.is_bot:
        await message.reply_text("❌ You can't place a bounty on a bot!")
        return
    if reporter.id == target_user.id:
        await message.reply_text("❌ You can't place a bounty on yourself!")
        return
        
    try:
        member = await client.get_chat_member(chat_id, target_user.id)
        if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("❌ You cannot place a bounty on group admins!")
            return
    except Exception:
        pass

    report_count, reporters = db.add_bounty_report(chat_id, target_msg.id, reporter.id)
    REQUIRED_REPORTS = 3 

    if report_count >= REQUIRED_REPORTS:
        try:
            db.reward_bounty_hunters(chat_id, reporters, points=10)
            
            admin_tags = []
            async for admin in client.get_chat_members(chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
                if admin.user.id == (await client.get_me()).id:
                    continue 
                    
                if admin.user.username:
                    admin_tags.append(f"@{admin.user.username}")
                else:
                    name = html.escape(admin.user.first_name)
                    admin_tags.append(f'<a href="tg://user?id={admin.user.id}">{name}</a>')
            
            tag_string = " ".join(admin_tags)
            safe_name = html.escape(target_user.first_name)
            
            alert_text = (
                f"🚨 <b>BOUNTY ALERT REVEALED!</b> 🚨\n\n"
                f"Target {safe_name} has been flagged {REQUIRED_REPORTS} times by the community.\n"
                f"💰 `10 Bounty Coins` have been awarded to the hunters!\n\n"
                f"👮‍♂️ {tag_string} please review this message and take manual action."
            )
            
            await target_msg.reply_text(alert_text, parse_mode=enums.ParseMode.HTML)
            await message.delete()
            
        except Exception as e:
            logger.error(f"Error in bounty alert: {e}")
            await message.reply_text("⚠️ Tried to alert admins, but an error occurred.")
    else:
        await message.reply_text(
            f"🎯 **Bounty Placed!** ({report_count}/{REQUIRED_REPORTS} hunters)\n"
            f"Need {REQUIRED_REPORTS - report_count} more people to reply with `/bounty` on that message to alert the admins!"
        )      

async def hunters_command(client: Client, message: Message):
    chat_id = message.chat.id
    points_data = db.data.get('bounty_points', {}).get(chat_id, {})
    
    if not points_data:
        await message.reply_text("📭 No bounty hunters in this group yet. Reply to spammers with `/bounty` to start hunting!")
        return
        
    sorted_hunters = sorted(points_data.items(), key=lambda x: x[1], reverse=True)[:10]
    
    leaderboard = "🏆 **Top Bounty Hunters** 🏆\n\n"
    for i, (str_uid, points) in enumerate(sorted_hunters, 1):
        name = db.get_group_data(chat_id, 'members', {}).get(str_uid, f"Unknown User")
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f" {i}. "
        leaderboard += f"{medal} {html.escape(name)} — 💰 `{points} Coins`\n"
        
    await message.reply_text(leaderboard)

async def broadcast_command(client: Client, message: Message):
    # 👇 Changed: ONLY OWNER_ID can use this now
    if message.from_user.id != OWNER_ID:
        return
        
    reply_msg = message.reply_to_message
    args = message.command[1:] if len(message.command) > 1 else []
    text_to_send = " ".join(args)
    
    if not reply_msg and not text_to_send:
        await message.reply_text("❗ **Usage:** Reply to any message/sticker/media with `/broadcast` or type `/broadcast <text>`")
        return

    status_msg = await message.reply_text("⏳ **Starting Broadcast...**\nThis may take a moment.")
    
    cursor = chats_col.find({"active": True})
    chats = await cursor.to_list(length=None)
    
    success = 0
    failed = 0
    
    for chat in chats:
        chat_id = chat['chat_id']
        try:
            if reply_msg:
                await reply_msg.copy(chat_id)
            else:
                await client.send_message(chat_id, text_to_send)
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            error_str = str(e).lower()
            if "forbidden" in error_str or "kicked" in error_str or "blocked" in error_str or "not found" in error_str:
                await chats_col.update_one({"chat_id": chat_id}, {"$set": {"active": False}})
                
    await status_msg.edit_text(f"✅ **Broadcast Complete!**\n\n🎯 Successfully sent: `{success}`\n❌ Failed/Removed: `{failed}`")

async def gmsg_command(client: Client, message: Message):
    # 👇 Changed: ONLY OWNER_ID
    if message.from_user.id != OWNER_ID:
        return
        
    args = message.command[1:] if len(message.command) > 1 else []
    if not args or not args[0].isdigit():
        await message.reply_text("❗ **Usage:** `/gmsg <serial_no> <message>` or reply to media with `/gmsg <serial_no>`")
        return
        
    s_no = int(args[0])
    cursor = chats_col.find({"active": True, "type": {"$in": ["group", "supergroup"]}}).sort("chat_id", 1)
    groups = await cursor.to_list(length=None)
    
    if s_no < 1 or s_no > len(groups):
        await message.reply_text("❌ Invalid Serial Number.")
        return
        
    target_chat = groups[s_no - 1]
    chat_id = target_chat['chat_id']
    
    try:
        if message.reply_to_message:
            await message.reply_to_message.copy(chat_id)
        else:
            text = " ".join(args[1:])
            if not text:
                await message.reply_text("Please provide text or reply to a message/media.")
                return
            await client.send_message(chat_id, text)
            
        await message.reply_text(f"✅ Message successfully sent to **{html.escape(target_chat.get('title', 'Group'))}**.", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await message.reply_text(f"❌ Failed to send message.\nError: `{e}`")

async def getlink_command(client: Client, message: Message):
    # 👇 Changed: ONLY OWNER_ID
    if message.from_user.id != OWNER_ID:
        return
        
    args = message.command[1:] if len(message.command) > 1 else []
    if not args or not args[0].isdigit():
        await message.reply_text("❗ **Usage:** `/getlink <serial_no>`\nGet the serial number from `/grouplist`.")
        return
        
    s_no = int(args[0])
    cursor = chats_col.find({"active": True, "type": {"$in": ["group", "supergroup"]}}).sort("chat_id", 1)
    groups = await cursor.to_list(length=None)
    
    if s_no < 1 or s_no > len(groups):
        await message.reply_text("❌ Invalid Serial Number.")
        return
        
    target_chat = groups[s_no - 1]
    
    try:
        invite_link = await client.export_chat_invite_link(target_chat['chat_id'])
        await message.reply_text(f"🔗 **Link for {html.escape(target_chat.get('title', 'Group'))}:**\n{invite_link}", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await message.reply_text(f"❌ Could not generate link. Make sure I am an Admin with 'Invite Users' permission.\nError: `{e}`")

from pyrogram import enums

async def grouplist_cmd(client: Client, message: Message):
    if message.from_user.id != OWNER_ID:
        await message.reply_text("🚫 Only the Bot Owner can use this command.")
        return

    # Sort groups so serial numbers match for /gmsg and /getlink
    groups = chats_col.find({"type": {"$in": ["group", "supergroup"]}}).sort("chat_id", 1)

    text = "📋 **Monitored Groups List:**\n\n"
    count = 0
    
    # 👇 FETCH BOT ID ONCE BEFORE THE LOOP 👇
    bot_id = (await client.get_me()).id

    async for group in groups:
        chat_id = group.get("chat_id")
        title = group.get("title", "Unknown Group")
        
        is_active = False

        try:
            # 👇 USE THE SAVED bot_id HERE 👇
            member = await client.get_chat_member(chat_id, bot_id)
            
            if member.status in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED]:
                is_active = False
            else:
                is_active = True
                
        except Exception:
            is_active = False

        await chats_col.update_one(
            {"chat_id": chat_id},
            {"$set": {"active": is_active}}
        )

        count += 1
        status_symbol = "🟢" if is_active else "🔴"
        text += f"{count}. {status_symbol} {title} (`{chat_id}`)\n"
        
        # Add a tiny delay to prevent rate limits if you have hundreds of groups
        import asyncio
        await asyncio.sleep(0.1)

    if count == 0:
        text = "📭 No groups found in the database."

    # If list is too long, Telegram might reject it. Truncate if necessary.
    if len(text) > 4000:
        text = text[:4000] + "\n...[List Too Long]"

    await message.reply_text(text)

async def vc_invite_handler(client: Client, message: Message):
    """Triggered when users are invited to a voice chat"""
    # 1. Make sure the message actually contains an invite
    if not message.video_chat_members_invited:
        return

    # 2. Define who did the inviting
    inviter = message.from_user
    if not inviter:
        return

    # 3. Get the list of invited users
    invited_users = message.video_chat_members_invited.users

    # 4. Loop through and mention each invited user
    for invited_user in invited_users:
        try:
            # Optional: Save to your database tracking (if you have it)
            db.add_vc_invite(message.chat.id, inviter.id, invited_user.id)

            await message.reply_text(
                f"Hey {invited_user.mention} invited"
                f"by {inviter.mention}\n"
                f"Come join VC fast! 🎙️",
                parse_mode=enums.ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"VC Invite Error: {e}")

async def vc_start_handler(client: Client, message: Message):
    """Triggered when a voice chat starts"""
    try:
        chat_title = html.escape(message.chat.title or "this group")
        await message.reply_text(
            f"🎙️ Voice Chat Started!\n",
            parse_mode=enums.ParseMode.HTML
        )                      
    except Exception as e:
        logger.error(f"VC Start Error: {e}")

async def vc_end_handler(client: Client, message: Message):
    """Triggered when a voice chat ends"""
    try:
        chat_title = html.escape(message.chat.title or "this group")
        await message.reply_text(
            f"🔇 Voice Chat Ended!\n\n",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"VC End Error: {e}")

async def adminlist_command(client: Client, message: Message):
    await send_typing_action(client, message)
    chat_id = message.chat.id

    # 1. Delete the user's command message
    try:
        await message.delete()
    except Exception:
        pass

    try:
        owner = None
        co_founders = []
        admins = []
        bots = []

        # 2. Fetch all administrators directly from Telegram
        async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            user = member.user
            title = member.custom_title if member.custom_title else ""
            display_title = f" <i>{html.escape(title)}</i>" if title else ""
            
            # Format the user's name or username
            if user.username:
                mention = f"@{user.username}"
            else:
                safe_name = html.escape(user.first_name or "User")
                mention = f'<a href="tg://user?id={user.id}">{safe_name}</a>'
            
            entry = f"• {mention}{display_title}"

            # 3. Categorize them based on their status and privileges
            if user.is_bot:
                bots.append(entry)
            elif member.status == enums.ChatMemberStatus.OWNER:
                owner = entry
            elif member.status == enums.ChatMemberStatus.ADMINISTRATOR:
                privs = member.privileges
                # Co-Founder check: Must have BOTH 'add admins' and 'change info' rights
                if privs and privs.can_promote_members and privs.can_change_info:
                    co_founders.append(entry)
                else:
                    admins.append(entry)

        # 4. Build the final message text
        admin_list_text= "<b>👑 Group Administrators 👑</b>\n\n"

        if owner:
            admin_list_text += f"👑 <b>Owner:</b>\n{owner}\n\n"
            
        if co_founders:
            admin_list_text += "<b>🎖️ Co-Founders:</b>\n" + "\n".join(co_founders) + "\n\n"
            
        if admins:
            admin_list_text += "<b>👮‍♂️ Admins:</b>\n" + "\n".join(admins) + "\n\n"
            
        if bots:
            admin_list_text += "<b>🤖 Bots:</b>\n" + "\n".join(bots) + "\n\n"

        # Send the categorized list
        sent_msg = await message.reply_text(
            admin_list_text,
            parse_mode=enums.ParseMode.HTML
        )

    except Exception as e:
        logger.error(f"Error in adminlist: {e}")

async def about_command(client: Client, message: Message):
    await send_typing_action(client, message)

    cpu_usage = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    ram_usage = ram.percent
    total_ram = round(ram.total / (1024 ** 3), 2)
    used_ram = round(ram.used / (1024 ** 3), 2)

    disk = psutil.disk_usage('/')
    disk_usage = disk.percent

    python_version = platform.python_version()
    os_name = platform.system()
    os_release = platform.release()

    uptime_seconds = int(time.time() - START_TIME)
    uptime_string = str(datetime.utcfromtimestamp(uptime_seconds).strftime("%Hh %Mm %Ss"))

    about_text = f"""
<b>🤖 About This Bot</b>

<b>Version:</b> 3.0 Merged Edition (PTB + PyroBot)
<b>Framework:</b> Pyrogram
<b>Timezone:</b> Asia/Kolkata (IST)

<b>System Information:</b>
• 🖥 OS: {os_name} {os_release}
• 🐍 Python: {python_version}
• ⚙ CPU Usage: {cpu_usage}%
• 💾 RAM: {used_ram}GB / {total_ram}GB ({ram_usage}%)
• 📀 Disk Usage: {disk_usage}%
• ⏳ Uptime: {uptime_string}

<b>Features:</b>
• Complete Group Management
• Welcome/Goodbye Messages
• Rules System
• Warning System
• Ranking System
• Admin Tools
• User Statistics
• Message History
• Tagging System
• Bot Status Monitoring
• Security System (Spam/Flood/Media/Night)
• VC Invite Monitor
• Promote/Demote Commands

<b>Database:</b> In-memory storage + MongoDB
<b>Status:</b> 🟢 Running 24/7

<b>Support:</b> For support, contact the bot administrator (@anurag_9X)
"""
    await message.reply_text(about_text, parse_mode=enums.ParseMode.HTML)


async def contact_command(client: Client, message: Message):
    await message.reply_text(
        "<b>📞 Contact Admin:</b>\n\nFor support or issues, contact the bot administrator directly (@anurag_9X).",
        parse_mode=enums.ParseMode.HTML
    )

ai_cooldowns = {}
AI_COOLDOWN_SECONDS = 30

async def ai_command(client: Client, message: Message):
    user_id = message.from_user.id
    now_time = time.time()
    
    # Apply cooldown (Bot Owner bypasses this)
    if user_id != OWNER_ID:
        if user_id in ai_cooldowns:
            time_passed = now_time - ai_cooldowns[user_id]
            if time_passed < AI_COOLDOWN_SECONDS:
                remaining_time = int(AI_COOLDOWN_SECONDS - time_passed)
                await message.reply_text(f"⏳ **Cooldown active!** Please wait {remaining_time} seconds before asking the AI again.")
                return
        
        # Save the new time the user used the command
        ai_cooldowns[user_id] = now_time

    args = message.command[1:] if len(message.command) > 1 else []
    query = ' '.join(args)

    if not query and message.reply_to_message and message.reply_to_message.text:
        query = message.reply_to_message.text

    if not query:
        await message.reply_text("❗ Please ask something.\nExample: `/ai What is Python?` or reply to a message with `/ai`.")
        return

    await send_typing_action(client, message)
    status_msg = await message.reply_text("🧠 Thinking...")
    db.update_bot_stats("command")

    try:
        response = ai_model.generate_content(query)
        answer = response.text

        if len(answer) > 4000:
            answer = answer[:4000] + "\n\n...[Message Truncated]"

        await status_msg.edit_text(answer)

    except Exception as e:
        logger.error(f"AI Error: {e}")
        await status_msg.edit_text("❌ Sorry, my AI brain is resting right now or the API key is missing. Try again later!")

async def notify_command(client: Client, message: Message):
    await message.reply_text(
        "<b>🔔 Notification Settings</b>\n\nNotification features are currently under development.",
        parse_mode=enums.ParseMode.HTML
    )

async def rules_command(client: Client, message: Message):
    await send_typing_action(client, message)
    chat_id = message.chat.id
    rules = db.get_group_data(chat_id, 'rules', "No rules set yet.")

    await message.reply_text(
        f"<b>📜 Group Rules:</b>\n\n{html.escape(rules)}",
        parse_mode=enums.ParseMode.HTML
    )

async def set_rules_command(client: Client, message: Message):
    # Sirf Admin ke liye check
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id
    rules_text = ""

    # 1. Pehle check karein ki kya kisi message par reply kiya gaya hai
    if message.reply_to_message:
        # Reply wale message ka text ya caption rules ban jayega
        rules_text = message.reply_to_message.text or message.reply_to_message.caption
    
    # 2. Agar reply nahi hai, toh command ke saath wale text ko check karein
    if not rules_text:
        args = message.command[1:] if len(message.command) > 1 else []
        if args:
            rules_text = ' '.join(args)

    # 3. Agar dono tareekon se text nahi mila, toh error message dikhayein
    if not rules_text:
        await message.reply_text("❗ **Usage:** `/setrules <text>` ya kisi text message par **reply** karke `/setrules` likhein.")
        return

    # 4. Rules ko database mein save karein
    db.save_group_data(chat_id, 'rules', rules_text)

    await message.reply_text("✅ Rules have been updated!")

async def unban_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return
    if not await is_bot_admin(client, message):
        await message.reply_text("🚫 I need to be an admin to unban users.")
        return

    chat_id = message.chat.id
    target_id, target_name, _ = await extract_target(client, message)

    if not target_id:
        await message.reply_text("❗ Kripya kisi ko reply karein, ya User ID/Username/Name mention karein.")
        return

    try:
        await client.unban_chat_member(chat_id, target_id)
        db.unban_user(target_id, chat_id)

        safe_name = html.escape(target_name or str(target_id))
        await message.reply_text(
            f"✅ User {safe_name} (`{target_id}`) has been unbanned!",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def warn_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id

    # Warn tabhi chalega jab bot ke paas ban/restrict permission ho
    try:
        bot_id = (await client.get_me()).id
        bot_member = await client.get_chat_member(chat_id, bot_id)
        bot_has_ban_perm = (
            bot_member.status == enums.ChatMemberStatus.OWNER or
            (bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR and bot_member.privileges and bot_member.privileges.can_restrict_members)
        )
    except Exception:
        bot_has_ban_perm = False

    if not bot_has_ban_perm:
        await message.reply_text("🚫 I don't have ban permission.")
        return
    target_id, target_name, reason = await extract_target(client, message)

    if not target_id:
        await message.reply_text(reason)
        return

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot warn a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    if db.is_muted(target_id, chat_id):
        warns = db.get_warns(target_id, chat_id)
        await message.reply_text(
            f"⚠️ {target_name} is already muted.\n"
            f"Total warnings: {len(warns)}/3\n"
            f"Status: 🔇 User is muted"
        )
        return

    db.add_warn(target_id, chat_id, reason)
    warns = db.get_warns(target_id, chat_id)
    total_warns = len(warns)

    safe_name = html.escape(target_name or "User")
    safe_reason = html.escape(reason)

    warning_msg = (
        f"⚠️ {safe_name} has been warned!\n"
        f"Reason: {safe_reason}\n"
        f"Total warnings: {total_warns}/3"
    )

    if total_warns >= 3:
        can_restrict_members = False
        try:
            bot_id = (await client.get_me()).id
            bot_member = await client.get_chat_member(chat_id, bot_id)
            can_restrict_members = (
                bot_member.status == enums.ChatMemberStatus.OWNER or
                (bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR and bot_member.privileges and bot_member.privileges.can_restrict_members)
            )
        except Exception:
            can_restrict_members = False

        if not can_restrict_members:
            db.reset_warns(target_id, chat_id)
            await message.reply_text("i dont have member restricting right")
            return

        try:
            await client.restrict_chat_member(
                chat_id,
                target_id,
                permissions=ChatPermissions(can_send_messages=False)
            )

            db.mute_user(target_id, chat_id, datetime.now() + timedelta(days=365))

            warning_msg += (
                f"\n\n🚨 <b>{safe_name}</b> has been muted for reaching 3 warnings!"
                f"\nStatus: 🔇 User is muted"
            )

        except Exception:
            db.reset_warns(target_id, chat_id)
            await message.reply_text("i dont have member restricting right")
            return

    await message.reply_text(warning_msg, parse_mode=enums.ParseMode.HTML)

async def dwarn_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id

    # Delete & Warn tabhi chalega jab bot ke paas ban/restrict permission ho
    try:
        bot_id = (await client.get_me()).id
        bot_member = await client.get_chat_member(chat_id, bot_id)
        bot_has_ban_perm = (
            bot_member.status == enums.ChatMemberStatus.OWNER or
            (bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR and bot_member.privileges and bot_member.privileges.can_restrict_members)
        )
    except Exception:
        bot_has_ban_perm = False

    if not bot_has_ban_perm:
        await message.reply_text("🚫 I don't have ban permission.")
        return
    target_id, target_name, reason = await extract_target(client, message)

    if not target_id:
        await message.reply_text(reason)
        return

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot warn a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    if message.reply_to_message:
        try:
            await message.reply_to_message.delete()
        except Exception:
            pass

    if db.is_muted(target_id, chat_id):
        warns = db.get_warns(target_id, chat_id)
        await message.reply_text(
            f"⚠️ {target_name} is already muted.\n"
            f"Total warnings: {len(warns)}/3\n"
            f"Status: 🔇 User is muted"
        )
        return

    db.add_warn(target_id, chat_id, reason)
    warns = db.get_warns(target_id, chat_id)
    total_warns = len(warns)

    warning_msg = (
        f"⚠️ {target_name} has been warned (Delete & Warn)!\n"
        f"Reason: {reason}\n"
        f"Total warnings: {total_warns}/3"
    )

    if total_warns >= 3:
        can_restrict_members = False
        try:
            bot_id = (await client.get_me()).id
            bot_member = await client.get_chat_member(chat_id, bot_id)
            can_restrict_members = (
                bot_member.status == enums.ChatMemberStatus.OWNER or
                (bot_member.status == enums.ChatMemberStatus.ADMINISTRATOR and bot_member.privileges and bot_member.privileges.can_restrict_members)
            )
        except Exception:
            can_restrict_members = False

        if not can_restrict_members:
            db.reset_warns(target_id, chat_id)
            await message.reply_text("i dont have member restricting right")
            return
        
        try:
            await client.restrict_chat_member(
                chat_id,
                target_id,
                permissions=ChatPermissions(can_send_messages=False)
            )

            db.mute_user(target_id, chat_id, datetime.now() + timedelta(days=365))

            warning_msg += (
                f"\n\n🚨 {target_name} has been muted for reaching 3 warnings!"
                f"\nStatus: 🔇 User is muted"
            )
        except Exception:
            db.reset_warns(target_id, chat_id)
            await message.reply_text("i dont have member restricting right")
            return

    await message.reply_text(warning_msg)

async def unwarn_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id
    target_id, target_name, args_text = await extract_target(client, message)

    if not target_id:
        await message.reply_text("❗ Kripya kisi ko reply karein, ya User ID/Username/Name mention karein.")
        return

    import html
    safe_name = html.escape(target_name or "User")

    # 👇 ADMIN CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text(f"🛡️ <b>{safe_name}</b> is an Admin. Admins do not have any warnings to remove!", parse_mode=enums.ParseMode.HTML)
            return
    except Exception:
        pass
    # 👆 ADMIN CHECK 👆

    remove_all = False
    warn_index = -1

    if args_text and args_text != "No reason":
        first_arg = args_text.split()[0].lower()
        if first_arg == "all":
            remove_all = True
        elif first_arg.isdigit():
            warn_index = int(first_arg) - 1

    warns = db.get_warns(target_id, chat_id)

    if not warns:
        await message.reply_text(f"✅ {safe_name} has no warnings to remove.", parse_mode=enums.ParseMode.HTML)
        return

    if remove_all:
        db.reset_warns(target_id, chat_id)
        await message.reply_text(f"✅ All warnings removed for <b>{safe_name}</b>!", parse_mode=enums.ParseMode.HTML)
    elif warn_index >= 0 and warn_index < len(warns):
        db.remove_warn(target_id, chat_id, warn_index)
        await message.reply_text(f"✅ Warning #{warn_index+1} removed for <b>{safe_name}</b>!", parse_mode=enums.ParseMode.HTML)
    else:
        db.remove_warn(target_id, chat_id, -1)
        await message.reply_text(f"✅ Last warning removed for <b>{safe_name}</b>!", parse_mode=enums.ParseMode.HTML)
        
async def pin_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    if not message.reply_to_message:
        await message.reply_text("Please reply to a message to pin it.")
        return

    chat_id = message.chat.id
    message_id = message.reply_to_message.id
    args = message.command[1:] if len(message.command) > 1 else []

    try:
        silent = False
        if args and args[0].lower() in ['silent', 'quiet', 'loud']:
            silent = args[0].lower() == 'silent'

        await client.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=silent
        )

        db.pin_message(chat_id, message_id)
        await message.reply_text("📌 Message pinned successfully!")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def unpin_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id
    args = message.command[1:] if len(message.command) > 1 else []

    try:
        message_id = None
        if args:
            try:
                message_id = int(args[0])
            except ValueError:
                pass

        if message_id:
            await client.unpin_chat_message(chat_id, message_id)
        else:
            await client.unpin_all_chat_messages(chat_id)

        db.unpin_message(chat_id)
        await message.reply_text("📎 Message unpinned successfully!")
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def history_command(client: Client, message: Message):
    await send_typing_action(client, message)

    chat_id = message.chat.id

    # Use extract_target to get target user
    target_id, target_name, error = await extract_target(client, message)

    if not target_id:
        # Fall back to sender if no target
        target_id = message.from_user.id
        target_name = message.from_user.first_name

    all_messages = []
    for msg in db.data['message_history'].get(chat_id, []):
        if msg['user_id'] == target_id:
            all_messages.append(msg)

    if not all_messages:
        safe_name = html.escape(target_name or "User")
        await message.reply_text(f"{safe_name} has no recent message history in this chat.", parse_mode=enums.ParseMode.HTML)
        return

    recent_messages = all_messages[-10:]

    safe_name = html.escape(target_name or "User")
    history_text = f"<b>📜 Message History for {safe_name}:</b>\n\n"

    for i, msg in enumerate(recent_messages, 1):
        try:
            msg_time = datetime.fromisoformat(msg['time']).strftime("%H:%M")
            msg_preview = msg['message'][:50] + "..." if len(msg['message']) > 50 else msg['message']
            safe_msg = html.escape(msg_preview)
            history_text += f"{i}. [{msg_time}] {safe_msg}\n"
        except Exception:
            continue

    history_text += f"\nTotal messages in history: {len(all_messages)}"
    await message.reply_text(history_text, parse_mode=enums.ParseMode.HTML)

async def atag_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 Only admins can use this command.")
        return

    chat_id = message.chat.id
    reply_to_id = message.reply_to_message.id if message.reply_to_message else None

    status_msg = await message.reply_text("⏳ Tagging admins...")

    try:
        admin_tags = []
        seen_users = set()  # Keeps track of who we already tagged
        
        async for admin in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            user = admin.user
            
            # 1. Skip if it is a bot, deleted account, or already in our seen list
            if user.is_bot or user.is_deleted or user.id in seen_users:
                continue
                
            seen_users.add(user.id)

            # Format the tag
            if user.username:
                admin_tags.append(f"@{user.username}")
            else:
                import html
                name = html.escape(user.first_name or "Admin")
                admin_tags.append(f'<a href="tg://user?id={user.id}">{name}</a>')

        if not admin_tags:
            await status_msg.edit_text("❌ No real admins found to tag.")
            return

        await status_msg.delete()

        # Custom text if provided (e.g., /atag Read this!)
        args = message.command[1:] if len(message.command) > 1 else []
        custom_text = ' '.join(args) if args else "Admins Attention!"
        import html
        safe_custom_text = html.escape(custom_text)

        # 2. Chunk the list into groups of 5
        chunk_size = 5
        for i in range(0, len(admin_tags), chunk_size):
            chunk = admin_tags[i:i + chunk_size]
            
            tag_message = f"<b>👑 {safe_custom_text}</b>\n\n" + "\n".join(chunk)
            
            # Send the message
            if reply_to_id:
                await client.send_message(
                    chat_id, 
                    tag_message, 
                    reply_to_message_id=reply_to_id, 
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                await client.send_message(
                    chat_id, 
                    tag_message, 
                    parse_mode=enums.ParseMode.HTML
                )
            
            # 3. Small delay to prevent flood errors when sending multiple chunks
            import asyncio
            await asyncio.sleep(2.0)

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error in atag: {e}")
        await message.reply_text("❌ Error in tagging admins.")
        
async def utag_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 Only admins can use this command.")
        return

    chat_id = message.chat.id

    # 1. Check karein ki kisi message par reply kiya gaya hai ya nahi
    reply_to_id = message.reply_to_message.id if message.reply_to_message else None

    # Custom text nikalna
    args = message.command[1:] if len(message.command) > 1 else []
    input_text = ' '.join(args) if args else "Attention!"

    status_msg = await message.reply_text("⏳ Tagging started...")
    user_tags = []

    try:
        # Pyrogram ka direct method to fetch all members!
        async for member in client.get_chat_members(chat_id):
            # 👇 YAHAN CHANGE KIYA HAI: is_bot lagaya hai taaki bots tag na ho 👇
            if member.user.is_bot or member.user.is_deleted:
                continue
            
            name = member.user.first_name or "User"
            user_tags.append(f'<a href="tg://user?id={member.user.id}">{html.escape(name)}</a>')

        if not user_tags:
            await status_msg.edit_text("❌ No users found to tag.")
            return

        await status_msg.delete()

        # Database me session start karein taaki /canceltagging kaam kare
        db.start_tagging_session(chat_id, "utag", message.id)

        # 5 users ek baar me tag honge
        chunk_size = 5 
        for i in range(0, len(user_tags), chunk_size):
            
            # Check karein ki kisi admin ne /canceltagging toh nahi chalaya
            if not db.get_tagging_session(chat_id):
                await message.reply_text("🛑 Tagging stopped by admin!")
                break

            chunk = user_tags[i:i + chunk_size]
            tag_message = f"<b>{html.escape(input_text)}</b>\n" + "\n".join(chunk)
            
            # 2. Agar reply me command di gayi thi, toh target message ko reply karke tag karega
            if reply_to_id:
                await client.send_message(
                    chat_id, 
                    tag_message, 
                    reply_to_message_id=reply_to_id, 
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                await client.send_message(
                    chat_id, 
                    tag_message, 
                    parse_mode=enums.ParseMode.HTML
                )
            
            await asyncio.sleep(2.5) # Flood error se bachne ke liye delay

    except Exception as e:
        logger.error(f"Error in utag: {e}")
        await message.reply_text("❌ Error in tagging. Make sure I have admin rights.")
    finally:
        # Session end karein
        db.stop_tagging_session(chat_id)

async def canceltagging_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id
    if db.stop_tagging_session(chat_id):
        await message.reply_text("✅ Tagging session cancelled!")
    else:
        await message.reply_text("No active tagging session found.")

async def ban_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return
    if not await is_bot_admin(client, message):
        await message.reply_text("🚫 I need to be an admin to ban users.")
        return

    target_id, target_name, reason = await extract_target(client, message)

    if not target_id:
        await message.reply_text(reason)
        return

    chat_id = message.chat.id

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("🚫 I cannot take action against an admin.")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    safe_name = html.escape(target_name or "User")
    safe_reason = html.escape(reason)

    await message.reply_text(
        f"Are you sure you want to ban {safe_name}?\nReason: {safe_reason}",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=get_confirmation_keyboard("ban", target_id)
    )

async def kick_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return
    if not await is_bot_admin(client, message):
        await message.reply_text("🚫 I need to be an admin to kick users.")
        return

    target_id, target_name, reason = await extract_target(client, message)
    if not target_id:
        await message.reply_text(reason)
        return

    chat_id = message.chat.id

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot kick or ban a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    safe_name = html.escape(target_name or "User")
    safe_reason = html.escape(reason)

    await message.reply_text(
        f"Are you sure you want to kick {safe_name}?\nReason: {safe_reason}",
        parse_mode=enums.ParseMode.HTML,
        reply_markup=get_confirmation_keyboard("kick", target_id)
    )

async def dban_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return
    if not await is_bot_admin(client, message):
        await message.reply_text("🚫 I need to be an admin to ban users.")
        return

    if not message.reply_to_message:
        await message.reply_text("Please reply to a user's message to delete and ban.")
        return

    chat_id = message.chat.id
    target_user = message.reply_to_message.from_user

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_user.id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot kick or ban a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    args = message.command[1:] if len(message.command) > 1 else []
    reason = ' '.join(args) if args else "No reason"

    try:
        await message.reply_to_message.delete()
        await client.ban_chat_member(chat_id, target_user.id)
        db.ban_user(target_user.id, chat_id)

        safe_name = html.escape(target_user.first_name or "User")
        safe_reason = html.escape(reason)

        await message.reply_text(
            f"🗑️🚫 Message deleted and {safe_name} has been banned!\nReason: {safe_reason}",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def dkick_command(client: Client, message: Message):
    if not await is_admin(client, message):
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to a user to kick.")
        return

    target = message.reply_to_message.from_user
    chat_id = message.chat.id

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target.id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot kick or ban a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    args = message.command[1:] if len(message.command) > 1 else []
    reason = " ".join(args) if args else "No reason"

    import html
    safe_reason = html.escape(reason)

    try:
        await message.reply_to_message.delete()
    except Exception:
        pass

    try:
        await client.ban_chat_member(chat_id, target.id)
        await client.unban_chat_member(chat_id, target.id)
    except Exception as e:
        await message.reply_text(f"Error kicking user: {e}")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await client.send_message(
        chat_id,
        f"🚫 {target.mention} kicked!\nReason: {safe_reason}",
        parse_mode=enums.ParseMode.HTML
    )

async def mute_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return
    if not await is_bot_admin(client, message):
        await message.reply_text("🚫 I need to be an admin to mute users.")
        return

    target_id, target_name, args_text = await extract_target(client, message)
    if not target_id:
        await message.reply_text(args_text)
        return

    chat_id = message.chat.id

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot mute a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    duration_minutes = None
    if args_text and args_text != "No reason":
        first_token = args_text.split()[0].lower()
        try:
            if first_token.isdigit():
                duration_minutes = int(first_token)
            elif first_token.endswith('m') and first_token[:-1].isdigit():
                duration_minutes = int(first_token[:-1])
            elif first_token.endswith('h') and first_token[:-1].isdigit():
                duration_minutes = int(first_token[:-1]) * 60
            elif first_token.endswith('d') and first_token[:-1].isdigit():
                duration_minutes = int(first_token[:-1]) * 1440
        except Exception:
            duration_minutes = None

    mute_until = datetime.now(pytz.utc) + timedelta(minutes=duration_minutes) if duration_minutes else None

    try:
        restrict_kwargs = {
            "chat_id": chat_id,
            "user_id": target_id,
            "permissions": ChatPermissions(can_send_messages=False)
        }

        # NOTE: Pass until_date only for timed mutes.
        # Passing None can break on some Pyrogram/Telegram combinations
        # with errors like: 'NoneType' object has no attribute 'to_bytes'.
        if mute_until is not None:
            restrict_kwargs["until_date"] = mute_until

        await client.restrict_chat_member(**restrict_kwargs)

        if duration_minutes:
            db.mute_user(target_id, chat_id, mute_until)
        else:
            db.mute_user(target_id, chat_id, datetime.now(pytz.utc) + timedelta(days=3650))

        safe_name = html.escape(target_name or "User")
        time_txt = f"for {duration_minutes} minutes" if duration_minutes else "permanently"
        await message.reply_text(f"🔇 {safe_name} has been muted {time_txt}.", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await message.reply_text(f"Error: {e}")

async def dmute_command(client: Client, message: Message):
    if not await is_admin(client, message):
        return

    if not message.reply_to_message:
        await message.reply_text("Reply to a user to mute.")
        return

    target = message.reply_to_message.from_user
    chat_id = message.chat.id

    # 👇 ADMIN PROTECTION CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target.id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text("what a hell i cannot mute a admin")
            return
    except Exception:
        pass
    # 👆 ADMIN PROTECTION CHECK 👆

    args = message.command[1:] if len(message.command) > 1 else []
    reason = " ".join(args) if args else "No reason"

    import html
    safe_reason = html.escape(reason)

    try:
        await message.reply_to_message.delete()
    except Exception:
        pass

    await client.restrict_chat_member(
        chat_id,
        target.id,
        permissions=ChatPermissions(can_send_messages=False)
    )

    await message.delete()
    await client.send_message(
        chat_id,
        f"🔇 {target.mention} muted!\nReason: {safe_reason}",
        parse_mode=enums.ParseMode.HTML
    )
    
async def unmute_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    chat_id = message.chat.id
    target_id, target_name, _ = await extract_target(client, message)

    if not target_id:
        await message.reply_text("❗ Reply to a user to unmute.")
        return

    import html
    safe_name = html.escape(target_name or "User")

    # 👇 ADMIN CHECK 👇
    try:
        target_member = await client.get_chat_member(chat_id, target_id)
        if target_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            await message.reply_text(f"🛡️ <b>{safe_name}</b> is an Admin. Admins cannot be muted anyway!", parse_mode=enums.ParseMode.HTML)
            return
    except Exception:
        pass
    # 👆 ADMIN CHECK 👆

    try:
        # Pyrogram-supported permissions
        await client.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False
            )
        )

        db.unmute_user(target_id, chat_id)
        db.reset_warns(target_id, chat_id)

        await message.reply_text(
            f"🔊 <b>{safe_name}</b> has been unmuted.\n"
            f"⚠️ Warnings have been reset to 0.",
            parse_mode=enums.ParseMode.HTML
        )

    except Exception as e:
        await message.reply_text(f"⚠️ Failed to unmute user. Error: {e}")

async def report_command(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply_text("❗ Kisi bad message par reply karke `/report` likhein.")
        return

    chat_id = message.chat.id
    reporter = message.from_user.mention
    target_user = message.reply_to_message.from_user.mention
    
    # Admins fetch karke tag banana
    admin_tags = []
    async for admin in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        if not admin.user.is_bot:
            admin_tags.append(f"<a href='tg://user?id={admin.user.id}'>\u200b</a>")
    
    tag_string = "".join(admin_tags)
    
    await message.reply_text(
        f"🚨 <b>Report Received!</b>\n\n"
        f"👤 <b>Reporter:</b> {reporter}\n"
        f"👤 <b>Target:</b> {target_user}\n"
        f"📩 <b>Admins:</b> Check this message! {tag_string}",
        parse_mode=enums.ParseMode.HTML
    )

async def speak_command(client: Client, message: Message):
    VOICES = {
        "female": "hi-IN-SwaraNeural",
        "male": "hi-IN-MadhurNeural",
        "child": "en-US-AnaNeural",
        "robot": "en-GB-RyanNeural"
    }

    args = message.command[1:] if len(message.command) > 1 else []
    voice_type = "female"
    text = ""

    if args and args[0].lower() in VOICES:
        voice_type = args[0].lower()
        text = " ".join(args[1:])
    else:
        text = " ".join(args)

    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text

    if not text:
        help_text = (
            "❗ **Kripya text likhein ya reply karein.**\n\n"
            "**Available Voices:** `female`, `male`, `child`, `robot`\n\n"
            "**Examples:**\n"
            "🗣️ `/speak female Kaise ho aap sab?`\n"
            "👦 `/speak male Main Megabot hoon!`\n"
            "👶 `/speak child Hello everyone!`"
        )
        await message.reply_text(help_text)
        return

    await client.send_chat_action(chat_id=message.chat.id, action=enums.ChatAction.RECORD_AUDIO)
    status_msg = await message.reply_text(f"🎙️ Generating {voice_type} voice...")

    db.update_bot_stats("command")

    output_file = f"voice_{message.from_user.id}.mp3"
    selected_voice = VOICES[voice_type]

    try:
        if voice_type == "robot":
            communicate = edge_tts.Communicate(text, selected_voice, rate="+10%", pitch="-25Hz")
        else:
            communicate = edge_tts.Communicate(text, selected_voice)

        await communicate.save(output_file)

        await message.reply_voice(voice=output_file, caption=f"🗣️ Voice: **{voice_type.capitalize()}**")

        os.remove(output_file)
        await status_msg.delete()

    except Exception as e:
        logger.error(f"TTS Error: {e}")
        await status_msg.edit_text("❌ Voice generate karne mein error aayi.")
        if os.path.exists(output_file):
            os.remove(output_file)

async def dice_command(client: Client, message: Message):
    logger.info(f"Dice command used by {message.from_user.id}")
    await send_typing_action(client, message)
    await client.send_dice(message.chat.id)

async def dart_command(client: Client, message: Message):
    await send_typing_action(client, message)
    await client.send_dice(message.chat.id, emoji="🎯")

async def joke_command(client: Client, message: Message):
    await send_typing_action(client, message)

    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "Why did the scarecrow win an award? He was outstanding in his field!",
        "What do you call a fish wearing a bowtie? Sofishticated!",
        "Why don't eggs tell jokes? They'd crack each other up!",
        "What do you call a bear with no teeth? A gummy bear!"
    ]

    joke = random.choice(jokes)
    await message.reply_text(f"<b>😂 Joke:</b>\n\n{joke}", parse_mode=enums.ParseMode.HTML)

async def reload_command(client: Client, message: Message):
    if not await is_admin(client, message):
        await message.reply_text("🚫 You need to be an admin to use this command.")
        return

    await message.reply_text("🔄 Reloading bot configuration...")

    try:
        # Re-apply the bot commands to Telegram
        await set_bot_commands(client)
        await message.reply_text("✅ Bot configuration reloaded successfully and commands updated!")
    except Exception as e:
        await message.reply_text(f"Error during reload: {e}")

async def quote_command(client: Client, message: Message):
    await send_typing_action(client, message)

    quotes = [
        "The only way to do great work is to love what you do. - Steve Jobs",
        "Life is what happens when you're busy making other plans. - John Lennon",
        "The future belongs to those who believe in the beauty of their dreams. - Eleanor Roosevelt",
        "It is during our darkest moments that we must focus to see the light. - Aristotle",
        "Whoever is happy will make others happy too. - Anne Frank"
    ]

    quote = random.choice(quotes)
    await message.reply_text(f"<b>💭 Quote of the day:</b>\n\n{quote}", parse_mode=enums.ParseMode.HTML)

# ============== APPROVED USERS DATABASE HELPERS ==============
async def is_approved(chat_id, user_id):
    if user_id == OWNER_ID:
        return True
    cache_key = f"approved:{chat_id}:{user_id}"
    cached = approved_cache.get(cache_key)
    if cached is not None:
        return cached
    chat = await chats_col.find_one({"chat_id": chat_id, "approved": user_id})
    result = True if chat else False
    approved_cache.set(cache_key, result)
    return result

async def add_approve(chat_id, user_id):
    await chats_col.update_one({"chat_id": chat_id}, {"$addToSet": {"approved": user_id}}, upsert=True)
    approved_cache.delete(f"approved:{chat_id}:{user_id}")

async def remove_approve(chat_id, user_id):
    await chats_col.update_one({"chat_id": chat_id}, {"$pull": {"approved": user_id}})
    approved_cache.delete(f"approved:{chat_id}:{user_id}")

async def get_approved_users(chat_id):
    chat = await chats_col.find_one({"chat_id": chat_id})
    return chat.get("approved", []) if chat else []

# ============== FILTERS DATABASE HELPERS ==============
async def add_filter_db(chat_id, keyword, content):
    await chats_col.update_one({"chat_id": chat_id}, {"$set": {f"filters.{keyword}": content}}, upsert=True)
    filters_cache.delete(f"filters:{chat_id}")

async def del_filter_db(chat_id, keyword):
    await chats_col.update_one({"chat_id": chat_id}, {"$unset": {f"filters.{keyword}": ""}})
    filters_cache.delete(f"filters:{chat_id}")

async def get_all_filters(chat_id):
    cache_key = f"filters:{chat_id}"
    cached = filters_cache.get(cache_key)
    if cached is not None:
        return cached
    chat = await chats_col.find_one({"chat_id": chat_id})
    result = chat.get("filters", {}) if chat else {}
    filters_cache.set(cache_key, result)
    return result 

# ============== LOCKS DATABASE HELPERS ==============
async def set_lock(chat_id: int, lock_type: str, status: bool):
    """Save lock status to database"""
    await chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {f"locks.{lock_type}": status}},
        upsert=True
    )
    # Clear cache so it fetches fresh data next time
    locks_cache.delete(f"locks:{chat_id}")

async def get_all_locks(chat_id: int):
    """Get all locks from database with caching"""
    cache_key = f"locks:{chat_id}"
    cached = locks_cache.get(cache_key)
    if cached is not None:
        return cached
    
    chat = await chats_col.find_one({"chat_id": chat_id})
    locks = chat.get("locks", {}) if chat else {}
    
    locks_cache.set(cache_key, locks)
    return locks

# ============== NIGHT MODE DATABASE HELPERS ==============
async def set_night_config(chat_id: int, start: int, end: int):
    """Save night mode timings to database"""
    await chats_col.update_one(
        {"chat_id": chat_id},
        {"$set": {"night_mode": {"start": start, "end": end}}},
        upsert=True
    )
    # Cache clear karein taaki fresh data load ho
    night_cache.delete(f"night:{chat_id}")

async def get_night_config(chat_id: int):
    """Get night mode timings from database"""
    cache_key = f"night:{chat_id}"
    cached = night_cache.get(cache_key)
    if cached is not None:
        return cached
    
    chat = await chats_col.find_one({"chat_id": chat_id})
    config = chat.get("night_mode") if chat else None
    
    night_cache.set(cache_key, config)
    return config

# ============== BLOCKED WORDS & STICKERS HELPERS ==============
async def get_bwords(chat_id):
    cache_key = f"bwords:{chat_id}"
    cached = bwords_cache.get(cache_key)
    if cached is not None: return cached
    chat = await chats_col.find_one({"chat_id": chat_id})
    res = chat.get("bwords", []) if chat else []
    bwords_cache.set(cache_key, res)
    return res

async def add_bword(chat_id, word):
    await chats_col.update_one({"chat_id": chat_id}, {"$addToSet": {"bwords": word.lower()}}, upsert=True)
    bwords_cache.delete(f"bwords:{chat_id}")

async def rm_bword(chat_id, word):
    await chats_col.update_one({"chat_id": chat_id}, {"$pull": {"bwords": word.lower()}})
    bwords_cache.delete(f"bwords:{chat_id}")

async def get_bspacks(chat_id):
    cache_key = f"bspacks:{chat_id}"
    cached = bspacks_cache.get(cache_key)
    if cached is not None: return cached
    chat = await chats_col.find_one({"chat_id": chat_id})
    res = chat.get("bspacks", []) if chat else []
    bspacks_cache.set(cache_key, res)
    return res

async def add_bspack(chat_id, pack_name):
    await chats_col.update_one({"chat_id": chat_id}, {"$addToSet": {"bspacks": pack_name}}, upsert=True)
    bspacks_cache.delete(f"bspacks:{chat_id}")

async def rm_bspack(chat_id, pack_name):
    await chats_col.update_one({"chat_id": chat_id}, {"$pull": {"bspacks": pack_name}})
    bspacks_cache.delete(f"bspacks:{chat_id}")       

# ==========================================
#           AUTOMATED HANDLERS
# ==========================================

from PIL import Image, ImageDraw, ImageFont
import os

async def generate_welcome_image(client, user, chat_title):

    assets_dir = Path(__file__).resolve().parent
    bg = Image.open(assets_dir / "welcome_bg.png").convert("RGBA")
    draw = ImageDraw.Draw(bg)

    def load_font(size):
        font_candidates = [
            str(assets_dir / "fonts" / "Poppins-Bold.ttf"),
            str(assets_dir / "fonts" / "Montserrat-Bold.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        for font_path in font_candidates:
            if os.path.exists(font_path):
                try:
                    return ImageFont.truetype(font_path, size=size)
                except OSError:
                    continue
        return ImageFont.load_default()

    def draw_centered_text(text, box, size, fill="Red"):
        # Auto shrink text so long names/usernames fit in template box.
        x1, y1, x2, y2 = box
        text = (text or "-").strip() or "-"
        font_size = size

        while font_size >= 42:
            font = load_font(font_size)
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
            width, height = right - left, bottom - top
            if width <= (x2 - x1):
                x = x1 + ((x2 - x1) - width) // 2
                y = y1 + ((y2 - y1) - height) // 2
                draw.text((x, y), text, fill=fill, font=font, stroke_width=2, stroke_fill="black")
                return
            font_size -= 2

        draw.text((x1, y1), text, fill=fill, font=load_font(42), stroke_width=2, stroke_fill="black")

    name = user.first_name
    user_id = str(user.id)
    username = f"@{user.username}" if user.username else "No Username"
    group = chat_title

    # Match actual label rows in the background template.
    draw_centered_text(name, (300, 320, 920, 390), 92)
    draw_centered_text(user_id, (300, 400, 920, 490), 82)
    draw_centered_text(username, (300, 485, 920, 595), 74)
    draw_centered_text(group, (300, 545, 920, 700), 74)

    # USER PROFILE PHOTO
    photo = None
    async for p in client.get_chat_photos(user.id, limit=1):
        photo = await client.download_media(p.file_id)

    if photo and os.path.exists(photo):
        pfp = Image.open(photo).convert("RGBA").resize((420, 420))
    else:
        # Fallback avatar when user has no profile photo.
        pfp = Image.new("RGBA", (420, 420), (75, 85, 99, 255))
        fallback_draw = ImageDraw.Draw(pfp)
        initial = (name or "?").strip()[:1].upper() or "?"
        initial_font = load_font(180)
        l, t, r, b = fallback_draw.textbbox((0, 0), initial, font=initial_font)
        fallback_draw.text(((420 - (r - l)) // 2, (420 - (b - t)) // 2), initial, fill="white", font=initial_font)
    
    # CIRCLE MASK
    mask = Image.new("L", (420, 420), 0)
    draw_mask = ImageDraw.Draw(mask)
    draw_mask.ellipse((0, 0, 420, 420), fill=255)

    pfp.putalpha(mask)

    # PROFILE POSITION (circle frame ke andar)
    bg.paste(pfp, (980, 250), pfp)

    output = f"welcome_{user.id}.png"
    bg.save(output)

    return output


import asyncio
from pyrogram import Client
from pyrogram.types import Message

async def welcome_handler(client: Client, message: Message):
    chat = message.chat

    # Sirf group / supergroup me chale
    if chat.type.value not in ["group", "supergroup"]:
        return

    chat_id = chat.id
    chat_title = chat.title

    # Welcome enabled check (agar tum use kar rahe ho)
    if not await get_welcome_enabled(chat_id):
        return

    bot_id = (await client.get_me()).id

    for member in message.new_chat_members:

        # Bot ko welcome mat bhejo
        if member.id == bot_id:
            continue

        try:
            # Image generate karo
            photo_path = await generate_welcome_image(client, member, chat_title)

            # Sirf image bhejo (NO caption)
            sent = await client.send_photo(
                chat_id,
                photo_path
            )

            # 5 min baad auto delete (optional)
            asyncio.create_task(
                delete_msg_later(client, chat_id, sent.id, 300)
            )

        except Exception as e:
            print(f"Welcome Error: {e}")

async def goodbye_handler(client: Client, message: Message):
    chat = message.chat
    if chat.type.value not in ['group', 'supergroup']:
        return

    chat_id = chat.id
    chat_title = chat.title
    member = message.left_chat_member

    if not member:
        return

    bot_id = (await client.get_me()).id
    if member.id == bot_id:
        return

    if not await get_goodbye_enabled(chat_id):
        return

    greet = await get_greet(chat_id, "goodbye")
    if not greet:
        greet = {
            'type': 'text',
            'text': "Goodbye {name}! 👋"
        }

    try:
        sent = None
        if greet['type'] == 'text':
            text = format_text(greet['text'], member, chat_title)
            sent = await client.send_message(chat_id, text, parse_mode=enums.ParseMode.MARKDOWN)
        elif greet['type'] == 'photo':
            caption = format_text(greet.get('caption', ''), member, chat_title)
            sent = await client.send_photo(chat_id, greet['file_id'], caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        elif greet['type'] == 'video':
            caption = format_text(greet.get('caption', ''), member, chat_title)
            sent = await client.send_video(chat_id, greet['file_id'], caption=caption, parse_mode=enums.ParseMode.MARKDOWN)
        elif greet['type'] == 'sticker':
            sent = await client.send_sticker(chat_id, greet['file_id'])

        if sent:
            asyncio.create_task(delete_msg_later(client, chat_id, sent.id, 300))
    except Exception as e:
        print(f"Goodbye Error: {e}")

# ================== UPDATED FILTER WATCHER ==================
async def filter_watcher(client: Client, message: Message):
    if not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        return
        
    chat_id = message.chat.id
    filters_data = await get_all_filters(chat_id)
    if not filters_data:
        return

    # Searchable text: message text/caption + sender's first_name, last_name, username
    search_text = ""
    if message.text:
        search_text += message.text.lower() + " "
    if message.caption:
        search_text += message.caption.lower() + " "
    
    user = message.from_user
    if user.first_name:
        search_text += user.first_name.lower() + " "
    if getattr(user, "last_name", None):
        search_text += user.last_name.lower() + " "
    if getattr(user, "username", None):
        search_text += user.username.lower() + " "

    import re
    for kw, data in filters_data.items():
        pattern = rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])"

        if re.search(pattern, search_text):
            try:
                if data['type'] == 'text':
                    await message.reply_text(data['text'])
                elif data['type'] == 'photo':
                    await message.reply_photo(data['file_id'], caption=data.get('caption', ""))
                elif data['type'] == 'video':
                    await message.reply_video(data['file_id'], caption=data.get('caption', ""))
                elif data['type'] == 'sticker':
                    await message.reply_sticker(data['file_id'])
                break
            except Exception as e:
                print(f"Filter error: {e}")
# ===========================================================

async def handle_delete_callback(client: Client, callback_query: CallbackQuery):
    try:
        creator_id = int(callback_query.data.split("|")[1])
    except (IndexError, ValueError):
        await callback_query.answer("Invalid callback data", show_alert=True)
        return

    clicking_user_id = callback_query.from_user.id
    chat_id = callback_query.message.chat.id

    is_admin = False
    try:
        member = await client.get_chat_member(chat_id, clicking_user_id)
        if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            is_admin = True
    except Exception:
        pass

    if clicking_user_id == creator_id or is_admin:
        try:
            await callback_query.message.delete()
            await callback_query.answer("✅ Deleted!")
        except Exception as e:
            await callback_query.answer(f"❌ Delete failed: {e}", show_alert=True)
    else:
        await callback_query.answer(
            "⚠️ Bhai, ye tere liye nahi hai! Sirf Admin ya owner hi delete kar sakte hain.",
            show_alert=True
        )

async def security_enforcer(client: Client, message: Message):
    # 👇 ADD THESE TWO LINES AT THE VERY TOP 👇
    if not message.from_user:
        return
            
    if message.text and message.text.startswith(("/", "!")):
        return

    chat = message.chat
    if chat.type.value not in ['group', 'supergroup']:
        return

    chat_id = chat.id
    user_id = message.from_user.id

    if await is_approved(chat_id, user_id):
        return
    try:
        member = await client.get_chat_member(chat_id, user_id)
        if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return
    except Exception:
        pass

    locks = await get_all_locks(chat_id)
    if not locks:
        return 

    msg_text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []

    if locks.get("link", False):
        has_link = False
        for ent in entities:
            if ent.type in [enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK]:
                has_link = True
                break

        if not has_link and re.search(r"(https?://\S+|www\.\S+|\b\w+\.(com|org|net|in|me|xyz|co)\b)", msg_text.lower()):
            has_link = True

        if has_link:
            try:
                await message.delete()
                raise StopPropagation 
            except Exception:
                pass

    if locks.get("media", False):
        if any([message.photo, message.video, message.audio,
                message.voice, message.document, message.animation,
                message.video_note]):
            try:
                await message.delete()
                raise StopPropagation 
            except Exception:
                pass

    if locks.get("sticker", False) and message.sticker:
        try:
            await message.delete()
            raise StopPropagation 
        except Exception:
            pass

    if locks.get("poll", False) and message.poll:
        try:
            await message.delete()
            raise StopPropagation 
        except Exception:
            pass

    if locks.get("emoji", False):
        emoji_pattern = r"[\U0001f300-\U0001f64f\U0001f680-\U0001f6ff\u2600-\u27bf\U0001f900-\U0001f9ff\U0001fa70-\U0001faff]"
        has_emoji = bool(re.search(emoji_pattern, msg_text))

        if not has_emoji:
            for ent in entities:
                if ent.type == enums.MessageEntityType.CUSTOM_EMOJI:
                    has_emoji = True
                    break

        if has_emoji:
            try:
                await message.delete()
                raise StopPropagation 
            except Exception:
                pass

    if locks.get("text", False):
        if message.text:
            try:
                await message.delete()
                raise StopPropagation 
            except Exception:
                pass

# ============== CALLBACK QUERY HANDLER ==============
async def button_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()

    data = callback_query.data
    chat = callback_query.message.chat
    chat_id = chat.id

    # Handle main menu callbacks
    if data == "main_menu":
        await callback_query.edit_message_text(
            "<b>📱 Interactive Menu</b>\n\nSelect a category below:",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=get_main_menu_keyboard()
        )

    elif data == "menu_security":
        text = (
            "<b>🛡️ Security Features</b>\n\n"
            "• <b>Anti-Spam & Flood:</b> Automatically mutes or deletes fast/spammy messages.\n\n"
            "• <b>Night Mode:</b> Silences the group during specified hours (<code>/setnight</code>).\n\n"
            "• <b>Content Locks:</b> Lock links, media, stickers, text, emojis, or polls (<code>/lock all</code>, <code>/unlock</code>).\n\n"
            "• <b>Blocked Content:</b> Block specific words (<code>/addword</code>) or sticker packs (<code>/addspack</code>).\n\n"
            "• <b>Bypass Security:</b> Approve users to bypass filters (<code>/approve</code>, <code>/free</code>)."
        )
        await callback_query.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=get_back_keyboard())

    elif data == "menu_moderation":
        text = (
            "<b>⚖️ Moderation Commands</b>\n\n"
            "<b>Admin Commands:</b>\n"
            "• <code>/ban</code> - Ban a user\n"
            "• <code>/unban</code> - Unban a user\n"
            "• <code>/dban</code> - Delete & Ban a user\n"
            "• <code>/kick</code> - Kick a user\n"
            "• <code>/dkick</code> - Delete & Kick a user\n"
            "• <code>/mute</code> - Mute a user\n"
            "• <code>/unmute</code> - Unmute a user\n"
            "• <code>/dmute</code> - Delete & Mute a user\n"
            "• <code>/warn</code> - Warn a user\n"
            "• <code>/unwarn</code> - Unwarn a user\n"
            "•<code>/dwarn</code> - Delete & Warn a user\n"
            "•<code>/warns</code> - Check current user's warnings\n"
            "• <code>/purge</code> - Delete multiple messages\n"
            "• <code>/promote</code> - Promote a user to admin\n"
            "• <code>/demote</code> - Demote an admin to user\n"
            "• <code>/pin</code> - Pin a message\n"
            "• <code>/unpin</code> - Unpin a message\n"
            "• <code>/utag</code> - Tag all users in the group\n"
            "• <code>/atag</code> - Tag all admins in the group\n"
            "• <code>/canceltagging</code> - Cancel tagging\n"
            "• <code>/setrules</code> - Set group rules\n"
            "• <code>/filter</code> - Manage filters\n"
            "• <code>/stop</code> - Stop filters\n\n"
            "<b>User Commands:</b>\n"
            "• <code>/info</code> - Get user info\n"
            "• <code>/info chat </code> - Get chat info\n"
            "• <code>/rule</code> - Get chat rules\n"
            "• <code>/ranking</code> - View user ranking\n"
            "• <code>/adminlist</code> - Show Adminlist."
        )
        await callback_query.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=get_back_keyboard())

    elif data == "menu_fun":
        text = (
            "<b>🎮 Fun & Games</b>\n\n"
            "• <code>/dice</code> - Roll a 6-sided dice 🎲\n"
            "• <code>/dart</code> - Throw a dart 🎯\n"
            "• <code>/joke</code> - Get a random text joke 😂\n"
            "• <code>/quote</code> - Get an inspiring quote 💭\n"
            "• <code>/speak</code> (or <code>/tts</code>) - Convert your text to real human/AI voices 🗣️\n"
            "• <code>/ai</code> - Ask Gemini AI a question 🧠"
        )
        await callback_query.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=get_back_keyboard())

    elif data in ["menu_stats", "bot_stats"]:
        status_text = await generate_bot_status_text()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_botstatus")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ])
        await callback_query.edit_message_text(status_text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)

    elif data == "menu_about":
        text = (
            "<b>ℹ️ About & Extra Features</b>\n\n"
            "<b>Version:</b> 3.0 Merged Edition (PTB + PyroBot)\n\n"
            "<b>Features:</b>\n"
            "• <b>Welcome/Goodbye:</b> Customizable greetings with media.\n"
            "• <b>Welcome/Goodbye On/Off:</b> Toggle welcome messages on/off for the group.\n"
            "• <b>setwelcome/setgoodbye:</b> Set custom welcome/goodbye messages with text or media.\n"
            "• <b>delwelcome/delgoodbye:</b> Delete custom welcome/goodbye messages.\n"
            "• <b>Ranking System:</b> Tracks daily, weekly, and overall active users.\n"
            "• <b>Tagging System:</b> Tag all admins (<code>/atag</code>) or everyone (<code>/utag</code>).\n"
            "• <b>Filter System:</b> Create and manage custom filters for your group.\n"
            "• <b>Bad Word Filter:</b> Automatically filter inappropriate words.\n"
            "• <b>Sticker Blocker:</b> Globally Block specific sticker packs.\n"
            "• <b>VC Monitor:</b> Notifies when users are invited to Voice Chats.\n\n"
        )
        await callback_query.edit_message_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=get_back_keyboard())

    elif data == "refresh_botstatus":
        await refresh_botstatus_callback(client, callback_query)

    elif data == "close_menu":
        await callback_query.message.delete()

    # ========== Confirmation Handlers ==========
    elif data.startswith("confirm_"):
        parts = data.split("_")
        if len(parts) >= 3:
            action = parts[1]
            target_id = int(parts[2])

            if action == "ban":
                try:
                    await client.ban_chat_member(chat.id, target_id)
                    db.ban_user(target_id, chat.id)
                    await callback_query.edit_message_text("✅ User has been banned!")
                except Exception as e:
                    await callback_query.edit_message_text(f"Error: {e}")

            elif action == "kick":
                try:
                    await client.ban_chat_member(chat.id, target_id)
                    await client.unban_chat_member(chat.id, target_id)
                    await callback_query.edit_message_text("✅ User has been kicked!")
                except Exception as e:
                    await callback_query.edit_message_text(f"Error: {e}")

    elif data.startswith("cancel_"):
        await callback_query.edit_message_text("❌ Action cancelled.")

    elif data == "main_help":
        help_text = (
            "<b>🤖 Complete Bot Help Menu</b>\n\n"
            "Here is a quick overview of my commands. Click <b>Open Menu</b> below for detailed explanations of all my features.\n\n"
            "<b>🛡️ Security:</b> <code>/lock</code>, <code>/setnight</code>, <code>/addword</code>\n\n"
            "<b>⚖️ Moderation:</b> <code>/ban</code>, <code>/mute</code>, <code>/kick</code>, <code>/warn</code>, <code>/purge</code>, <code>/promote</code>\n\n"
            "<b>🎮 Fun:</b> <code>/dice</code>, <code>/speak</code>, <code>/ai</code>, <code>/joke</code>\n\n"
            "<b>ℹ️ Info:</b> <code>/id</code>, <code>/info</code>, <code>/botstatus</code>, <code>/ranking</code>\n\n"
            "<b>📜</b> Select a catogery below\n"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Open Menu", callback_data="main_menu")],
            [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
        ])
        await callback_query.edit_message_text(help_text, parse_mode=enums.ParseMode.HTML, reply_markup=keyboard)

    elif data == "start_menu":
        me = await client.get_me()
        safe_name = html.escape(callback_query.from_user.first_name)
        
        welcome_text = (
            f"✨ <b>Welcome {safe_name}!</b> ✨\n\n"
            "I'm a complete multi-purpose Telegram bot with advanced features for both groups and channels.\n\n"
            "<b>🤖 Advanced Features:</b>\n"
            "• Complete Group Management\n"
            "• Security System (Spam/Flood/Media/Night Controls)\n"
            "• Edit Monitoring\n"
            "• Sticker Blocker\n"
            "• Abuse Blocker\n"
            "• Bot Status Monitoring\n"
            "• And much more!\n\n"
            "<b>📜</b> Select a catogery below\n"
        )
        
        buttons = [
            [InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{me.username}?startgroup=true")],
            [
                InlineKeyboardButton("📖 Help Menu", callback_data="main_help"),
                InlineKeyboardButton("🆘 Support", url="https://t.me/+rjE5xZlIK4U3ODA1") 
            ]
        ]
        
        await callback_query.edit_message_text(
            welcome_text, 
            parse_mode=enums.ParseMode.HTML, 
            reply_markup=InlineKeyboardMarkup(buttons)
        )
# ============== CALLBACK HELPER FUNCTIONS ==============
async def botstatus_command_callback(client: Client, callback_query: CallbackQuery):
    await botstatus_command(client, callback_query.message)
    await callback_query.message.delete()

async def refresh_botstatus_callback(client: Client, callback_query: CallbackQuery):
    """Handle refresh bot status callback"""
    await callback_query.answer("🔄 Refreshing status...")
    status_text = await generate_bot_status_text()

    await callback_query.edit_message_text(
        status_text,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_botstatus")]
        ])
    )

async def adminlist_command_callback(client: Client, callback_query: CallbackQuery):
    await adminlist_command(client, callback_query.message)
    await callback_query.message.delete()

async def refresh_adminlist_callback(client: Client, callback_query: CallbackQuery):
    """Handle refresh adminlist callback"""
    await callback_query.answer("🔄 Refreshing admin list...")
    chat_id = callback_query.message.chat.id

    try:
        admins_with_status = await get_admins_list_with_status(client, callback_query.message)

        if not admins_with_status:
            await callback_query.edit_message_text("No admins found in this group.", parse_mode=enums.ParseMode.HTML)
            return

        owner = None
        other_admins = []

        for admin, status in admins_with_status:
            if status == enums.ChatMemberStatus.OWNER.value:
                owner = admin
            else:
                other_admins.append((admin, status))

        admin_list_text = "<b>👑 Group Administrators 👑</b>\n\n"

        if owner:
            if owner.username:
                owner_mention = f"@{owner.username}"
            else:
                safe_name = html.escape(owner.first_name or "User")
                owner_mention = f'<a href="tg://user?id={owner.id}">{safe_name}</a>'
            admin_list_text += f"👑 <b>Owner:</b> {owner_mention}\n\n"

        if other_admins:
            admin_list_text += "<b>📋 Admins:</b>\n"
            for i, (admin, status) in enumerate(other_admins, 1):
                if admin.username:
                    admin_mention = f"@{admin.username}"
                else:
                    safe_name = html.escape(admin.first_name or "User")
                    admin_mention = f'<a href="tg://user?id={admin.id}">{safe_name}</a>'
                admin_list_text += f"{i}. {admin_mention}\n"

        await callback_query.edit_message_text(
            admin_list_text,
            parse_mode=enums.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_adminlist")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in refresh adminlist: {e}")
        await callback_query.edit_message_text(f"Error getting admin list: {e}")

async def reload_command_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer("🔄 Reloading...")
    await callback_query.edit_message_text("🔄 Reloading bot configuration...")
    await asyncio.sleep(1)
    await callback_query.edit_message_text("✅ Bot configuration reloaded successfully!")

async def canceltagging_callback(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    if db.stop_tagging_session(chat_id):
        await callback_query.edit_message_text("✅ Tagging session cancelled!")
    else:
        await callback_query.edit_message_text("No active tagging session found.")

# ============== MESSAGE HANDLERS ==============
async def handle_message(client: Client, message: Message):
    """Handle all messages, save users to memory, and track history"""
    if not message.from_user:
        return

    user = message.from_user
    chat = message.chat

    # 1. Save user to the "members" dictionary for tagging (/utag)
    members = db.get_group_data(chat.id, 'members', {})
    members[str(user.id)] = user.first_name
    db.save_group_data(chat.id, 'members', members)

    # 2. Track message history for /history command
    if message.text and not message.text.startswith('/'):
        db.add_message_to_history(chat.id, user.id, user.first_name, message.text)

        # Security: Check if user is muted or banned
        if db.is_muted(user.id, chat.id):
            try:
                await message.delete()
            except Exception:
                pass

        if db.is_banned(user.id, chat.id):
            try:
                await client.ban_chat_member(chat.id, user.id)
            except Exception:
                pass
            
import traceback
from datetime import datetime
from collections import defaultdict
from pymongo import UpdateOne

import pyrogram
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message, CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, 
    InlineKeyboardButton, BotCommand
)
from pyrogram.handlers import (
    MessageHandler, CallbackQueryHandler, ChatMemberUpdatedHandler, EditedMessageHandler
)
from pyrogram import StopPropagation

# ============== SET BOT COMMANDS ==============
async def set_bot_commands(client: Client):
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("status", "Show bot & system status"), 
        BotCommand("help", "Show help menu"),
        BotCommand("rules", "Show group rules"),
        BotCommand("setrules", "Set group rules (Admin)"),
        BotCommand("ranking", "Show group leaderboard"),
        BotCommand("warn", "Warn a user (Admin)"),
        BotCommand("warns", "Check warnings"),
        BotCommand("unwarn", "Remove warnings (Admin)"),
        BotCommand("ban", "Ban a user (Admin)"),
        BotCommand("unban", "Unban a user (Admin)"),
        BotCommand("dban", "Delete and ban (Admin)"),
        BotCommand("kick", "Kick a user (Admin)"),
        BotCommand("mute", "Mute a user (Admin)"),
        BotCommand("unmute", "Unmute a user (Admin)"),
        BotCommand("pin", "Pin a message (Admin)"),
        BotCommand("unpin", "Unpin a message (Admin)"),
        BotCommand("atag", "Tag all admins (Admin)"),
        BotCommand("utag", "Tag all users (Admin)"),
        BotCommand("canceltagging", "Cancel tagging (Admin)"),
        BotCommand("adminlist", "List all admins"),
        BotCommand("botstatus", "Show bot status"),
        BotCommand("reload", "Reload bot config (Admin)"),
        BotCommand("promote", "Promote user (Admin)"),
        BotCommand("demote", "Demote user (Admin)"),
        BotCommand("purge", "Delete messages (Admin)"),
        BotCommand("dice", "Roll a dice"),
        BotCommand("dart", "Throw a dart"),
        BotCommand("joke", "Get a random joke"),
        BotCommand("quote", "Get inspirational quote"),
        BotCommand("ai", "Ask the AI a question"),
        BotCommand("about", "About this bot"),
        BotCommand("contact", "Contact admin"),
        BotCommand("notify", "Notification settings"),
        BotCommand("speak", "Text to speech"),
    ]

    await client.set_bot_commands(commands)


# ============== OPTIMIZED RANKING SYSTEM ==============
# Global cache to store message counts temporarily
message_counts_cache = defaultdict(lambda: defaultdict(int))
user_names_cache = {}

async def message_tracker(client: Client, message: Message):
    """Tracks messages in memory (cache) instead of hitting the database instantly."""
    if not message.from_user:
        return
    chat = message.chat
    if chat.type.value not in ['group', 'supergroup']:
        return
        
    user = message.from_user
    
    # Just add +1 in the temporary memory (Super Fast ⚡)
    message_counts_cache[chat.id][user.id] += 1
    user_names_cache[user.id] = user.first_name

async def flush_message_counts():
    """Runs every 60 seconds to push all cached messages to MongoDB at once."""
    global message_counts_cache, user_names_cache
    
    while True:
        await asyncio.sleep(60)
        
        if not message_counts_cache:
            continue

        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        week = now.strftime("%Y-%V")

        # Copy data safely and clear the original cache immediately
        cache_copy = message_counts_cache.copy()
        names_copy = user_names_cache.copy()
        message_counts_cache.clear()
        user_names_cache.clear()

        bulk_ops = []

        # Prepare bulk operations for the database
        for chat_id, users in cache_copy.items():
            for user_id, count in users.items():
                name = names_copy.get(user_id, "Unknown")
                bulk_ops.append(
                    UpdateOne(
                        {"uid": user_id, "chat_id": chat_id},
                        {
                            "$inc": {f"counts.{today}": count, f"weeks.{week}": count, "overall": count},
                            "$set": {"name": name}
                        },
                        upsert=True
                    )
                )

        # Execute one single database call for everyone
        if bulk_ops:
            try:
                await stats_col.bulk_write(bulk_ops)
            except Exception as e:
                logger.error(f"Bulk write error in tracker: {e}")

async def ranking_command(client: Client, message: Message):
    buttons = [
        [
            InlineKeyboardButton("📅 Daily", callback_data="rank_daily"),
            InlineKeyboardButton("🗓️ Weekly", callback_data="rank_weekly"),
            InlineKeyboardButton("🏆 Overall", callback_data="rank_overall")
        ]
    ]
    await message.reply_text(
        "📊 **Group Leaderboard**\nSelect ranking type:", 
        reply_markup=InlineKeyboardMarkup(buttons), 
        parse_mode=enums.ParseMode.MARKDOWN
    )

async def ranking_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer()
    rank_type = callback_query.data.split("_")[1]
    chat_id = callback_query.message.chat.id
    now = datetime.now(IST)

    if rank_type == "daily":
        key = f"counts.{now.strftime('%Y-%m-%d')}"
        title = "📅 Daily Top 10"
    elif rank_type == "weekly":
        key = f"weeks.{now.strftime('%Y-%V')}"
        title = "🗓️ Weekly Top 10"
    else:
        key = "overall"
        title = "🏆 Overall Top 10"

    pipeline = [
        {"$match": {"chat_id": chat_id, key: {"$exists": True}}},
        {"$project": {"name": 1, "count": f"${key}"}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]

    try:
        cursor = stats_col.aggregate(pipeline)
        results = await cursor.to_list(length=10)
        
        if not results:
            await callback_query.edit_message_text("No data found yet!")
            return
            
        text = f"**{title}**\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, user in enumerate(results, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += f"{medal} **{user['name']}** — `{user['count']}` msgs\n"
        text += "━━━━━━━━━━━━━━━━━━━━"
        
        buttons = [[
            InlineKeyboardButton("📅 Daily", callback_data="rank_daily"),
            InlineKeyboardButton("🗓️ Weekly", callback_data="rank_weekly"),
            InlineKeyboardButton("🏆 Overall", callback_data="rank_overall")
        ]]
        await callback_query.edit_message_text(
            text, 
            reply_markup=InlineKeyboardMarkup(buttons), 
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Ranking Error: {e}")


# ============== BLOCKED CONTENT HANDLER ==============
async def blocked_content_handler(client: Client, message: Message):
    chat = message.chat
    if chat.type.value not in ['group', 'supergroup']:
        return

    # 1. Check Sticker Pack
    if message.sticker and message.sticker.set_name:
        bspacks = await get_bspacks(chat.id)
        if message.sticker.set_name in bspacks:
            try:
                await message.delete()
                warn_msg = await client.send_message(
                    chat.id, 
                    f"🚫 {message.from_user.mention} used a blocked sticker pack.", 
                    parse_mode=enums.ParseMode.HTML
                )
                asyncio.create_task(delete_msg_later(client, chat.id, warn_msg.id, 5))
                raise StopPropagation 
            except StopPropagation:
                raise
            except Exception:
                pass

    # 2. Check Words
    text = message.text or message.caption or ""
    if text:
        text_lower = text.lower()
        bwords = await get_bwords(chat.id)
        for w in bwords:
            if w in text_lower:
                try:
                    await message.delete()
                    warn_text = f"🚫 {message.from_user.mention} used abusive word: <b>{html.escape(w)}</b>"
                    warn_msg = await client.send_message(chat.id, warn_text, parse_mode=enums.ParseMode.HTML)
                    asyncio.create_task(delete_msg_later(client, chat.id, warn_msg.id, 5))
                    raise StopPropagation
                except StopPropagation:
                    raise
                except Exception:
                    pass

async def track_bot_status(client: Client, chat_member_updated: ChatMemberUpdated):
    """Automatically updates the database when the bot is added or kicked from a group."""
    # 1. Safely grab the member object (fallback to old_chat_member if new is missing)
    member = chat_member_updated.new_chat_member or chat_member_updated.old_chat_member
    
    # 2. If neither exists, or the user object is missing, ignore the update
    if not member or not member.user:
        return
        
    bot_id = (await client.get_me()).id
    
    # 3. Trigger only if the status update is about the bot itself
    if member.user.id != bot_id:
        return
        
    # 4. We specifically need the new status for the logic below
    if not chat_member_updated.new_chat_member:
        return
        
    chat = chat_member_updated.chat
    new_status = chat_member_updated.new_chat_member.status

    if new_status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR]:
        await chats_col.update_one(
            {"chat_id": chat.id},
            {"$set": {"title": chat.title, "type": chat.type.value, "active": True}},
            upsert=True
        )
    elif new_status in [enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED]:
        await chats_col.update_one(
            {"chat_id": chat.id},
            {"$set": {"active": False}}
        )

# ============== MAIN STARTUP FUNCTION ==============
async def main_async():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set your bot token!")
        print("1. Go to @BotFather on Telegram")
        print("2. Create a new bot with /newbot")
        print("3. Copy the token and replace 'YOUR_BOT_TOKEN_HERE'")
        sys.exit(1)

    app = Client(
        "merged_bot", 
        bot_token=BOT_TOKEN, 
        api_id=API_ID,       # Make sure API_ID and API_HASH are defined in your configs
        api_hash=API_HASH
    )

    # 👇 YAHI EK LINE ADD KARNI HAI 👇
    await db.load_from_mongo()

    # 👇 DEBUG LOGGER 👇
    @app.on_message(group=-10)
    async def debug_logger(client, message):
        if message.from_user:
            print(f"📩 DEBUG: Message received from {message.from_user.first_name} - Text: {message.text}")
    # 👆 DEBUG LOGGER 👆

    # Note: Pyrogram handlers execute according to their `group` attribute (lower number = earlier).
    
    # ================= 🛡️ GROUP -2: EXTREME PRIORITY (SECURITY) =================
    app.add_handler(MessageHandler(blocked_content_handler, filters.group), group=-2)
    
    # ================= 🛡️ GROUP -1: SPAM & FLOOD ENFORCER =================
    app.add_handler(MessageHandler(unified_security_handler, filters.group), group=-1)

    # ================= ⚙️ GROUP 0: STANDARD COMMANDS =================
    commands = {
        "start": start_command, "help": help_command, "menu": menu_command,
        "rules": rules_command, "setrules": set_rules_command, "warn": warn_command,
        "unwarn": unwarn_command, "ban": ban_command,
        "unban": unban_command, "dban": dban_command, "dmute": dmute_command,
        "dkick": dkick_command, "dwarn": dwarn_command, "kick": kick_command,
        "mute": mute_command, "unmute": unmute_command, "pin": pin_command,
        "unpin": unpin_command, "info": info_command, "ranking": ranking_command,
        "atag": atag_command, "utag": utag_command, "canceltagging": canceltagging_command,
        "adminlist": adminlist_command, "reload": reload_command, "promote": promote_command,
        "demote": demote_command, "purge": purge_command,
        "filters": list_filters_cmd, "setnight": set_night_cmd,
        "nightoff": night_off_cmd, "welcome": welcome_cmd, "goodbye": goodbye_cmd,
        "approve": approve_cmd, "unapprove": unapprove_cmd, "free": free_cmd,
        "unfree": unfree_cmd, "filter": add_filter_cmd, "stop": stop_filter_cmd,
        "setwelcome": set_welcome_cmd, "setgoodbye": set_goodbye_cmd, "delwelcome": del_welcome_cmd,
        "delgoodbye": del_goodbye_cmd, "about": about_command, "contact": contact_command,
        "notify": notify_command, "dice": dice_command, "dart": dart_command,"report": report_command, 
        "joke": joke_command, "quote": quote_command, "ai": ai_command, 
        "addsudo": addsudo_cmd, "rmsudo": rmsudo_cmd, "sudolist": sudolist_cmd, 
        "addword": addword_cmd, "rmword": rmword_cmd, "bwordlist": bwordlist_cmd, 
        "addspack": addspack_cmd, "rmspack": rmspack_cmd, "stickerlist": stickerlist_cmd,
        "bounty": bounty_command, "hunters": hunters_command, "grouplist": grouplist_cmd,
        "getlink": getlink_command, "gmsg": gmsg_command, "broadcast": broadcast_command
    }
    for cmd, func in commands.items():
        app.add_handler(MessageHandler(func, filters.command(cmd)), group=0)

    # Multi-aliases
    app.add_handler(MessageHandler(botstatus_command, filters.command(["botstatus", "status"])), group=0)
    app.add_handler(MessageHandler(speak_command, filters.command(["speak", "tts"])), group=0)
    app.add_handler(MessageHandler(lock_unlock_handler, filters.command(["lock", "unlock"])), group=0)


    # ================= 👁️ GROUP 1: WATCHERS & AUTOMATION =================
    app.add_handler(ChatMemberUpdatedHandler(track_bot_status), group=1)
    app.add_handler(MessageHandler(welcome_handler, filters.new_chat_members), group=1)
    app.add_handler(MessageHandler(goodbye_handler, filters.left_chat_member), group=1)
    app.add_handler(MessageHandler(vc_invite_handler, filters.video_chat_members_invited), group=1)
    app.add_handler(MessageHandler(filter_watcher, filters.text), group=1)

    # 👇 YE DONO NAYI LINES YAHAN PASTE KAREIN 👇
    app.add_handler(MessageHandler(vc_start_handler, filters.video_chat_started), group=1)
    app.add_handler(MessageHandler(vc_end_handler, filters.video_chat_ended), group=1)

    # ================= 📈 GROUP 3: MESSAGE TRACKING =================
    app.add_handler(MessageHandler(message_tracker, filters.text & filters.group), group=3)
    
    # ================= 🔒 GROUP 4: LOCKS ENFORCER =================
    app.add_handler(MessageHandler(security_enforcer, filters.group), group=4)
    
    # ================= 📝 GROUP 6: GENERAL MESSAGE HANDLER =================
    # Must be last in priority for text
    app.add_handler(MessageHandler(handle_message, filters.text), group=6)


    # ================= 🕹️ CALLBACK QUERIES =================
    app.add_handler(CallbackQueryHandler(permission_callback, filters.regex(r"^perm_")))
    app.add_handler(CallbackQueryHandler(handle_delete_callback, filters.regex(r"^del_msg\|")))
    app.add_handler(CallbackQueryHandler(ranking_callback, filters.regex(r"^rank_")))
    app.add_handler(CallbackQueryHandler(button_callback)) # Fallback

    # Start the app
    await app.start()
    await set_bot_commands(app)
    
    print("=" * 50)
    print("🤖 MERGED BOT STARTING (Pyrogram Features)...")
    print("=" * 50)
    print("✨ Features Included:")
    print("  • Complete Group Management")
    print("  • Ranking System (Daily/Weekly/Overall)")
    print("  • Admin & User Tagging")
    print("  • Message History")
    print("  • Bot Status Monitoring")
    print("  • Advanced Moderation Tools")
    print("  • Interactive Menu System")
    print("  • 🎤 VC Invite Monitor")
    print("  • ⬆️⬇️ Promote/Demote Commands")
    print("  • 🧹 Purge Command")
    print("=" * 50)
    print("📱 Use /start to begin")
    print("🔄 Press Ctrl+C to stop")
    print("=" * 50)

    # Spin up background tasks
    asyncio.create_task(flush_message_counts())

    # Idle keeps the script running
    await pyrogram.idle()
    
    # Cleanup on exit
    await app.stop()

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Error starting bot: {e}")
        logger.error(f"Failed to start bot: {e}")

if __name__ == '__main__':
    main()
