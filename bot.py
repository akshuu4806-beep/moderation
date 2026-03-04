 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/bot.py b/bot.py
new file mode 100644
index 0000000000000000000000000000000000000000..a7bd93dce40bf474efab1b6b6eac8b47ef3b1183
--- /dev/null
+++ b/bot.py
@@ -0,0 +1,666 @@
+import json
+import logging
+import os
+import re
+from datetime import datetime, timedelta, timezone
+from pathlib import Path
+from typing import Any
+
+from pyrogram import Client, filters
+from pyrogram.enums import ChatMemberStatus
+from pyrogram.errors import RPCError
+from pyrogram.types import ChatPermissions, Message, User
+
+logging.basicConfig(
+    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
+    level=logging.INFO,
+)
+logger = logging.getLogger(__name__)
+
+DATA_DIR = Path("data")
+STATE_FILE = DATA_DIR / "state.json"
+
+DEFAULT_WELCOME = "👋 Welcome {mention}\nName: {name}\nUsername: {username}\nID: {id}\nProfile: {profile_link}\nGroup: {chat_title}"
+DEFAULT_GOODBYE = "Goodbye {name}."
+LOCK_BASE_TYPES = {"link", "sticker", "gif", "photo", "video", "document", "audio", "voice", "poll", "emoji"}
+LOCK_TYPES = LOCK_BASE_TYPES | {"all"}
+
+
+def _default_state() -> dict[str, Any]:
+    return {
+        "warnings": {},
+        "filters": {},
+        "users": {},
+        "settings": {},
+        "locks": {},
+    }
+
+
+def load_state() -> dict[str, Any]:
+    DATA_DIR.mkdir(parents=True, exist_ok=True)
+    if not STATE_FILE.exists():
+        return _default_state()
+    try:
+        with STATE_FILE.open("r", encoding="utf-8") as f:
+            state = json.load(f)
+            base = _default_state()
+            base.update(state)
+            return base
+    except json.JSONDecodeError:
+        return _default_state()
+
+
+def save_state(state: dict[str, Any]) -> None:
+    DATA_DIR.mkdir(parents=True, exist_ok=True)
+    with STATE_FILE.open("w", encoding="utf-8") as f:
+        json.dump(state, f, indent=2)
+
+
+def _ck(chat_id: int) -> str:
+    return str(chat_id)
+
+
+def _uk(user_id: int) -> str:
+    return str(user_id)
+
+
+def chat_settings(chat_id: int) -> dict[str, Any]:
+    state = load_state()
+    ck = _ck(chat_id)
+    state["settings"].setdefault(
+        ck,
+        {
+            "welcome_enabled": True,
+            "goodbye_enabled": False,
+            "welcome_message": DEFAULT_WELCOME,
+            "goodbye_message": DEFAULT_GOODBYE,
+            "vc_enabled": False,
+            "vc_notify": False,
+            "vc_invite_notify": False,
+        },
+    )
+    save_state(state)
+    return state["settings"][ck]
+
+
+def update_setting(chat_id: int, key: str, value: Any) -> None:
+    state = load_state()
+    ck = _ck(chat_id)
+    state["settings"].setdefault(ck, {})
+    state["settings"][ck][key] = value
+    save_state(state)
+
+
+def set_locks(chat_id: int, locks: set[str]) -> None:
+    state = load_state()
+    state["locks"][_ck(chat_id)] = sorted(locks)
+    save_state(state)
+
+
+def get_locks(chat_id: int) -> set[str]:
+    state = load_state()
+    return set(state["locks"].get(_ck(chat_id), []))
+
+
+def remember_user(chat_id: int, user: User) -> None:
+    state = load_state()
+    ck = _ck(chat_id)
+    state["users"].setdefault(ck, {})
+    state["users"][ck][_uk(user.id)] = user.id
+    if user.username:
+        state["users"][ck][user.username.lower()] = user.id
+    save_state(state)
+
+
+async def is_admin(app: Client, chat_id: int, user_id: int) -> bool:
+    member = await app.get_chat_member(chat_id, user_id)
+    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}
+
+
+def parse_args(msg: Message) -> list[str]:
+    if not msg.text:
+        return []
+    parts = msg.text.split()
+    return parts[1:] if len(parts) > 1 else []
+
+
+async def resolve_target(app: Client, msg: Message) -> tuple[User | None, list[str]]:
+    args = parse_args(msg)
+
+    if msg.reply_to_message and msg.reply_to_message.from_user:
+        return msg.reply_to_message.from_user, args
+
+    if msg.entities:
+        for ent in msg.entities:
+            if ent.type == "text_mention" and ent.user:
+                return ent.user, args[1:] if args else []
+
+    if not args:
+        return None, []
+
+    raw = args[0].strip()
+    rest = args[1:]
+
+    if raw.lstrip("-").isdigit():
+        uid = int(raw)
+        try:
+            member = await app.get_chat_member(msg.chat.id, uid)
+            return member.user, rest
+        except RPCError:
+            return None, rest
+
+    uname = raw.lstrip("@").lower()
+    state = load_state()
+    cached = state.get("users", {}).get(_ck(msg.chat.id), {}).get(uname)
+    if cached:
+        try:
+            member = await app.get_chat_member(msg.chat.id, int(cached))
+            return member.user, rest
+        except RPCError:
+            return None, rest
+
+    return None, rest
+
+
+async def require_admin(app: Client, msg: Message) -> bool:
+    if not msg.from_user:
+        return False
+    if await is_admin(app, msg.chat.id, msg.from_user.id):
+        return True
+    await msg.reply_text("Yeh admin-only command hai.")
+    return False
+
+
+async def warn_user(chat_id: int, target_id: int, delta: int) -> int:
+    state = load_state()
+    ck = _ck(chat_id)
+    uk = _uk(target_id)
+    state["warnings"].setdefault(ck, {})
+    current = state["warnings"][ck].get(uk, 0)
+    new_count = max(0, current + delta)
+    state["warnings"][ck][uk] = new_count
+    save_state(state)
+    return new_count
+
+
+async def delete_replied(msg: Message) -> None:
+    if msg.reply_to_message:
+        try:
+            await msg.reply_to_message.delete()
+        except RPCError:
+            pass
+
+
+def fmt_template(template: str, user: User, chat_title: str) -> str:
+    return template.format(
+        mention=user.mention,
+        name=(user.first_name or "User"),
+        username=(f"@{user.username}" if user.username else "NoUsername"),
+        id=user.id,
+        profile_link=f"tg://user?id={user.id}",
+        chat_title=chat_title,
+    )
+
+
+
+
+def is_emoji_only_text(text: str) -> bool:
+    cleaned = text.strip()
+    if not cleaned:
+        return False
+    emoji_pattern = re.compile(
+        r"^[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF\u2600-\u26FF\u200d\ufe0f\s]+$"
+    )
+    return bool(emoji_pattern.match(cleaned))
+
+def message_locked(msg: Message, locks: set[str]) -> bool:
+    if "sticker" in locks and msg.sticker:
+        return True
+    if "gif" in locks and msg.animation:
+        return True
+    if "photo" in locks and msg.photo:
+        return True
+    if "video" in locks and msg.video:
+        return True
+    if "document" in locks and msg.document:
+        return True
+    if "audio" in locks and msg.audio:
+        return True
+    if "voice" in locks and msg.voice:
+        return True
+    if "poll" in locks and msg.poll:
+        return True
+    if "emoji" in locks and msg.text and is_emoji_only_text(msg.text):
+        return True
+    if "link" in locks and msg.text and re.search(r"https?://|t\.me/|telegram\.me/", msg.text.lower()):
+        return True
+
+    if "all" in locks:
+        if msg.service:
+            return False
+        if msg.text and msg.text.startswith("/"):
+            return False
+        return True
+
+    return False
+
+
+
+app = Client(
+    "rose_style_bot",
+    api_id=int(os.getenv("TELEGRAM_API_ID", "0")),
+    api_hash=os.getenv("TELEGRAM_API_HASH", ""),
+    bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
+)
+
+
+@app.on_message(filters.command(["start", "help"]) & filters.group)
+async def help_cmd(_: Client, msg: Message) -> None:
+    await msg.reply_text(
+        "Admin:\n"
+        "/ban /unban /kick /dkick\n"
+        "/mute /unmute /dmute\n"
+        "/warn /unwarn /dwarn /warnings\n"
+        "/filter /stopfilter(/stop) /filters\n"
+        "/welcomeon /goodbyeon /welcomeoff /goodbyeoff\n"
+        "/setwelcome <text> /setgoodbye <text>\n"
+        "/resetwelcome /resetgoodbye\n"
+        "/vcon /vcoff /vcnotifyon /vcnotifyoff /vcinviteon /vcinviteoff\n"
+        "/lock <type> /unlock <type> /unlockall /locks\n\n"
+        "Free:\n"
+        "/id /tag /utag /atag"
+    )
+
+
+@app.on_message(filters.command("id") & filters.group)
+async def id_cmd(_: Client, msg: Message) -> None:
+    if not msg.from_user:
+        return
+    text = f"Chat ID: {msg.chat.id}\nYour ID: {msg.from_user.id}"
+    if msg.reply_to_message and msg.reply_to_message.from_user:
+        text += f"\nReplied User ID: {msg.reply_to_message.from_user.id}"
+    await msg.reply_text(text)
+
+
+@app.on_message(filters.command(["tag", "utag"]) & filters.group)
+async def tag_cmd(client: Client, msg: Message) -> None:
+    user, _ = await resolve_target(client, msg)
+    if not user:
+        await msg.reply_text("Target user nahi mila. Reply ya @username/id do.")
+        return
+    await msg.reply_text(user.mention)
+
+
+@app.on_message(filters.command("atag") & filters.group)
+async def atag_cmd(client: Client, msg: Message) -> None:
+    mentions: list[str] = []
+    async for member in client.get_chat_members(msg.chat.id, filter=ChatMemberStatus.ADMINISTRATOR):
+        if member.user and not member.user.is_bot:
+            mentions.append(member.user.mention)
+    await msg.reply_text("Admins:\n" + (" ".join(mentions[:30]) if mentions else "Koi admin mila nahi."))
+
+
+@app.on_message(filters.command("warn") & filters.group)
+async def warn_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, rest = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /warn <reply|@username|id|mention> [reason]")
+        return
+    remember_user(msg.chat.id, target)
+    count = await warn_user(msg.chat.id, target.id, 1)
+    await msg.reply_text(f"{target.mention} warned. Total: {count}. Reason: {' '.join(rest) if rest else 'No reason'}")
+
+
+@app.on_message(filters.command("dwarn") & filters.group)
+async def dwarn_cmd(client: Client, msg: Message) -> None:
+    await delete_replied(msg)
+    await warn_cmd(client, msg)
+
+
+@app.on_message(filters.command("unwarn") & filters.group)
+async def unwarn_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, rest = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /unwarn <reply|@username|id|mention> [count]")
+        return
+    cnt = max(1, int(rest[0])) if rest and rest[0].isdigit() else 1
+    left = await warn_user(msg.chat.id, target.id, -cnt)
+    await msg.reply_text(f"{target.mention} unwarned. Current warnings: {left}")
+
+
+@app.on_message(filters.command("warnings") & filters.group)
+async def warnings_cmd(client: Client, msg: Message) -> None:
+    target, _ = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /warnings <reply|@username|id|mention>")
+        return
+    count = load_state()["warnings"].get(_ck(msg.chat.id), {}).get(_uk(target.id), 0)
+    await msg.reply_text(f"{target.mention} warnings: {count}")
+
+
+@app.on_message(filters.command("mute") & filters.group)
+async def mute_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, rest = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /mute <reply|@username|id|mention> [minutes]")
+        return
+    until_date = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(rest[0]))) if rest and rest[0].isdigit() else None
+    await client.restrict_chat_member(msg.chat.id, target.id, permissions=ChatPermissions(), until_date=until_date)
+    await msg.reply_text(f"Muted {target.mention}.")
+
+
+@app.on_message(filters.command("dmute") & filters.group)
+async def dmute_cmd(client: Client, msg: Message) -> None:
+    await delete_replied(msg)
+    await mute_cmd(client, msg)
+
+
+@app.on_message(filters.command("unmute") & filters.group)
+async def unmute_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, _ = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /unmute <reply|@username|id|mention>")
+        return
+    await client.restrict_chat_member(
+        msg.chat.id,
+        target.id,
+        permissions=ChatPermissions(
+            can_send_messages=True,
+            can_send_media_messages=True,
+            can_send_other_messages=True,
+            can_add_web_page_previews=True,
+        ),
+    )
+    await msg.reply_text(f"Unmuted {target.mention}.")
+
+
+@app.on_message(filters.command("ban") & filters.group)
+async def ban_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, rest = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /ban <reply|@username|id|mention> [reason]")
+        return
+    await client.ban_chat_member(msg.chat.id, target.id)
+    await msg.reply_text(f"Banned {target.mention}. Reason: {' '.join(rest) if rest else 'No reason'}")
+
+
+@app.on_message(filters.command("unban") & filters.group)
+async def unban_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, _ = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /unban <id|@username>")
+        return
+    await client.unban_chat_member(msg.chat.id, target.id)
+    await msg.reply_text(f"Unbanned {target.mention}.")
+
+
+@app.on_message(filters.command("kick") & filters.group)
+async def kick_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    target, rest = await resolve_target(client, msg)
+    if not target:
+        await msg.reply_text("Usage: /kick <reply|@username|id|mention> [reason]")
+        return
+    await client.ban_chat_member(msg.chat.id, target.id)
+    await client.unban_chat_member(msg.chat.id, target.id)
+    await msg.reply_text(f"Kicked {target.mention}. Reason: {' '.join(rest) if rest else 'No reason'}")
+
+
+@app.on_message(filters.command("dkick") & filters.group)
+async def dkick_cmd(client: Client, msg: Message) -> None:
+    await delete_replied(msg)
+    await kick_cmd(client, msg)
+
+
+@app.on_message(filters.command("filter") & filters.group)
+async def add_filter_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    args = parse_args(msg)
+    if not args:
+        await msg.reply_text("Usage: /filter <word>")
+        return
+    word = args[0].lower().strip()
+    state = load_state()
+    ck = _ck(msg.chat.id)
+    state["filters"].setdefault(ck, [])
+    if word not in state["filters"][ck]:
+        state["filters"][ck].append(word)
+        save_state(state)
+    await msg.reply_text(f"Filter added: {word}")
+
+
+@app.on_message(filters.command(["stopfilter", "stop"]) & filters.group)
+async def stop_filter_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    args = parse_args(msg)
+    if not args:
+        await msg.reply_text("Usage: /stopfilter <word>")
+        return
+    word = args[0].lower().strip()
+    state = load_state()
+    ck = _ck(msg.chat.id)
+    words = state["filters"].get(ck, [])
+    if word in words:
+        words.remove(word)
+        state["filters"][ck] = words
+        save_state(state)
+        await msg.reply_text(f"Filter removed: {word}")
+    else:
+        await msg.reply_text(f"Filter not found: {word}")
+
+
+@app.on_message(filters.command("filters") & filters.group)
+async def list_filter_cmd(_: Client, msg: Message) -> None:
+    words = load_state()["filters"].get(_ck(msg.chat.id), [])
+    await msg.reply_text("Filters:\n" + "\n".join(f"- {w}" for w in words) if words else "No filters set.")
+
+
+@app.on_message(filters.command("lock") & filters.group)
+async def lock_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    args = parse_args(msg)
+    if not args or args[0].lower() not in LOCK_TYPES:
+        await msg.reply_text(f"Usage: /lock <{'|'.join(sorted(LOCK_TYPES))}>")
+        return
+    locks = get_locks(msg.chat.id)
+    lock_type = args[0].lower()
+    if lock_type == "all":
+        locks.update(LOCK_BASE_TYPES)
+        locks.add("all")
+    else:
+        locks.add(lock_type)
+    set_locks(msg.chat.id, locks)
+    await msg.reply_text(f"Locked: {args[0].lower()}")
+
+
+@app.on_message(filters.command("unlock") & filters.group)
+async def unlock_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    args = parse_args(msg)
+    if not args or args[0].lower() not in LOCK_TYPES:
+        await msg.reply_text(f"Usage: /unlock <{'|'.join(sorted(LOCK_TYPES))}>")
+        return
+    locks = get_locks(msg.chat.id)
+    lock_type = args[0].lower()
+    if lock_type == "all":
+        locks.clear()
+    else:
+        locks.discard(lock_type)
+    set_locks(msg.chat.id, locks)
+    await msg.reply_text(f"Unlocked: {args[0].lower()}")
+
+
+
+
+@app.on_message(filters.command("unlockall") & filters.group)
+async def unlock_all_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    set_locks(msg.chat.id, set())
+    await msg.reply_text("All locks removed.")
+
+@app.on_message(filters.command("locks") & filters.group)
+async def locks_cmd(_: Client, msg: Message) -> None:
+    locks = sorted(get_locks(msg.chat.id))
+    await msg.reply_text("Locked types: " + (", ".join(locks) if locks else "none"))
+
+
+@app.on_message(filters.command(["welcomeon", "welcomeoff", "goodbyeon", "goodbyeoff"]) & filters.group)
+async def welcome_toggle_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    text = msg.text.split()[0].lstrip("/").split("@")[0].lower()
+    if text == "welcomeon":
+        update_setting(msg.chat.id, "welcome_enabled", True)
+        await msg.reply_text("Welcome ON")
+    elif text == "welcomeoff":
+        update_setting(msg.chat.id, "welcome_enabled", False)
+        await msg.reply_text("Welcome OFF")
+    elif text == "goodbyeon":
+        update_setting(msg.chat.id, "goodbye_enabled", True)
+        await msg.reply_text("Goodbye ON")
+    else:
+        update_setting(msg.chat.id, "goodbye_enabled", False)
+        await msg.reply_text("Goodbye OFF")
+
+
+@app.on_message(filters.command(["setwelcome", "setgoodbye", "resetwelcome", "resetgoodbye"]) & filters.group)
+async def welcome_message_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
+    args = parse_args(msg)
+    if cmd == "setwelcome":
+        if not args:
+            await msg.reply_text("Usage: /setwelcome <message>")
+            return
+        update_setting(msg.chat.id, "welcome_message", " ".join(args))
+        await msg.reply_text("Welcome message updated.")
+    elif cmd == "setgoodbye":
+        if not args:
+            await msg.reply_text("Usage: /setgoodbye <message>")
+            return
+        update_setting(msg.chat.id, "goodbye_message", " ".join(args))
+        await msg.reply_text("Goodbye message updated.")
+    elif cmd == "resetwelcome":
+        update_setting(msg.chat.id, "welcome_message", DEFAULT_WELCOME)
+        await msg.reply_text("Welcome message reset.")
+    else:
+        update_setting(msg.chat.id, "goodbye_message", DEFAULT_GOODBYE)
+        await msg.reply_text("Goodbye message reset.")
+
+
+@app.on_message(filters.command(["vcon", "vcoff", "vcnotifyon", "vcnotifyoff", "vcinviteon", "vcinviteoff"]) & filters.group)
+async def vc_toggle_cmd(client: Client, msg: Message) -> None:
+    if not await require_admin(client, msg):
+        return
+    cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
+    mapping = {
+        "vcon": ("vc_enabled", True, "VC status ON"),
+        "vcoff": ("vc_enabled", False, "VC status OFF"),
+        "vcnotifyon": ("vc_notify", True, "VC notify ON"),
+        "vcnotifyoff": ("vc_notify", False, "VC notify OFF"),
+        "vcinviteon": ("vc_invite_notify", True, "VC invite notify ON"),
+        "vcinviteoff": ("vc_invite_notify", False, "VC invite notify OFF"),
+    }
+    key, value, msg_text = mapping[cmd]
+    update_setting(msg.chat.id, key, value)
+    await msg.reply_text(msg_text)
+
+
+
+
+async def send_welcome_card(client: Client, msg: Message, user: User, settings: dict[str, Any]) -> None:
+    caption = fmt_template(settings.get("welcome_message", DEFAULT_WELCOME), user, msg.chat.title or "Group")
+    try:
+        async for photo in client.get_chat_photos(user.id, limit=1):
+            await client.send_photo(
+                chat_id=msg.chat.id,
+                photo=photo.file_id,
+                caption=caption,
+            )
+            return
+    except RPCError:
+        pass
+
+    await msg.reply_text(caption)
+
+@app.on_message(filters.group & filters.service)
+async def service_listener(client: Client, msg: Message) -> None:
+    settings = chat_settings(msg.chat.id)
+
+    if msg.new_chat_members and settings.get("welcome_enabled"):
+        for user in msg.new_chat_members:
+            remember_user(msg.chat.id, user)
+            await send_welcome_card(client, msg, user, settings)
+
+    if msg.left_chat_member and settings.get("goodbye_enabled"):
+        await msg.reply_text(
+            fmt_template(settings.get("goodbye_message", DEFAULT_GOODBYE), msg.left_chat_member, msg.chat.title or "Group")
+        )
+
+    if settings.get("vc_enabled") and settings.get("vc_notify"):
+        if msg.video_chat_started:
+            await msg.reply_text("Voice chat started.")
+        if msg.video_chat_ended:
+            await msg.reply_text("Voice chat ended.")
+
+    if settings.get("vc_enabled") and settings.get("vc_invite_notify") and msg.video_chat_members_invited:
+        await msg.reply_text("Voice chat invite event detected.")
+
+
+@app.on_message(filters.group)
+async def moderation_listener(client: Client, msg: Message) -> None:
+    if msg.from_user:
+        remember_user(msg.chat.id, msg.from_user)
+
+    locks = get_locks(msg.chat.id)
+
+    if msg.from_user:
+        try:
+            if await is_admin(client, msg.chat.id, msg.from_user.id):
+                return
+        except RPCError:
+            pass
+
+    if message_locked(msg, locks):
+        try:
+            await msg.delete()
+            return
+        except RPCError:
+            pass
+
+    if not msg.text or msg.text.startswith("/"):
+        return
+
+    blocked = set(load_state()["filters"].get(_ck(msg.chat.id), []))
+    blocked.update({"spamlink", "badword"})
+    if any(w in msg.text.lower() for w in blocked):
+        try:
+            await msg.delete()
+        except RPCError:
+            pass
+
+
+if __name__ == "__main__":
+    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_API_ID") or not os.getenv("TELEGRAM_API_HASH"):
+        raise RuntimeError("Set TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID and TELEGRAM_API_HASH env vars")
+    logger.info("Starting Pyrogram moderation bot...")
+    app.run()
 
EOF
)
