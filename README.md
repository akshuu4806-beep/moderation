# Rose-style Telegram Moderation Bot (Pyrogram)

Is version me aapke requested features add hain: VC on/off + notify + invite notify, welcome/goodbye toggles and custom messages, aur lock types.

## Commands

### Admin moderation
- `/ban <reply|@username|id|mention> [reason]`
- `/unban <id|@username>`
- `/kick <reply|@username|id|mention> [reason]`
- `/dkick`
- `/mute <reply|@username|id|mention> [minutes]`
- `/unmute <reply|@username|id|mention>`
- `/dmute`
- `/warn <reply|@username|id|mention> [reason]`
- `/unwarn <reply|@username|id|mention> [count]`
- `/dwarn`
- `/warnings <reply|@username|id|mention>`
- `/filter <word>`
- `/stopfilter <word>` (alias: `/stop`)
- `/filters`

### Welcome / Goodbye
- `/welcomeon` / `/welcomeoff`
- `/goodbyeon` / `/goodbyeoff`
- `/setwelcome <message>`
- `/setgoodbye <message>`
- `/resetwelcome`
- `/resetgoodbye`

Template vars (welcome/goodbye message):
- `{mention}`, `{name}`, `{username}`, `{id}`, `{profile_link}`, `{chat_title}`

Default behavior: welcome ON rehta hai aur new user ke profile photo ke saath welcome card bhejne ki try hoti hai (fallback text message).

### VC controls
- `/vcon` / `/vcoff`
- `/vcnotifyon` / `/vcnotifyoff`
- `/vcinviteon` / `/vcinviteoff`

### Locks
- `/lock <link|sticker|gif|photo|video|document|audio|voice|poll|emoji|all>`
- `/unlock <type>`
- `/unlockall`
- `/locks`

### Free commands
- `/id`
- `/tag <reply|@username|id|mention>`
- `/utag <reply|@username|id|mention>`
- `/atag`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Required env vars

```bash
export TELEGRAM_BOT_TOKEN="<bot-token>"
export TELEGRAM_API_ID="<api-id>"
export TELEGRAM_API_HASH="<api-hash>"
```

## Run

```bash
python bot.py
```
