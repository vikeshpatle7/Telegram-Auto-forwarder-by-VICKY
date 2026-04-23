#!/usr/bin/env python3
"""
TeleFeed Clone — Full MTProto Userbot for Telegram Auto-Forwarding
==================================================================
Run with Docker:  docker compose up -d
Run standalone:   python telefeed_clone.py

All data (SQLite DB + Telethon session files) is stored in ./data/
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# --------------- Config ---------------
API_ID = int(os.environ.get("TG_API_ID", 0))
API_HASH = os.environ.get("TG_API_HASH", "")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "telefeed.db"

# --------------- Database (SQLite) ---------------
import aiosqlite

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS redirections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            phone TEXT DEFAULT '',
            sources TEXT,
            destinations TEXT,
            settings TEXT,
            filters TEXT,
            cleaner TEXT,
            whitelist TEXT,
            blacklist TEXT,
            transformation TEXT,
            delay_secs INTEGER DEFAULT 0,
            translate_lang TEXT DEFAULT '',
            UNIQUE(user_id, name)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS seen_hashes (
            redir_id INTEGER,
            hash TEXT,
            ts REAL,
            UNIQUE(redir_id, hash)
        )""")
        await db.commit()

async def db_get_redir(user_id, name):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM redirections WHERE user_id=? AND name=?",
            (user_id, name)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def db_list_redirs(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM redirections WHERE user_id=?", (user_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def db_all_redirs():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM redirections") as cur:
            return [dict(r) for r in await cur.fetchall()]

async def db_add_redir(user_id, name, phone, sources, destinations):
    defaults = {
        "process_reply": False, "process_edit": True,
        "process_delete": False, "process_forward": False,
        "process_duplicates": True,
    }
    def_filters = {
        "audio": False, "video": False, "voicenote": False,
        "animation": False, "photo": False, "sticker": False,
        "document": False, "text": False, "caption": False,
        "forward": False, "reply": False,
    }
    def_cleaner = {
        "audio": False, "video": False, "voicenote": False,
        "animation": False, "photo": False, "sticker": False,
        "document": False, "text": False, "caption": False,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO redirections
            (user_id,name,phone,sources,destinations,settings,filters,cleaner,
             whitelist,blacklist,transformation,delay_secs,translate_lang)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, name, phone, json.dumps(sources), json.dumps(destinations),
             json.dumps(defaults), json.dumps(def_filters),
             json.dumps(def_cleaner), "[]", "[]", "{}", 0, "")
        )
        await db.commit()

async def db_del_redir(user_id, name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM redirections WHERE user_id=? AND name=?",
            (user_id, name)
        )
        await db.commit()

async def db_update_field(user_id, name, field, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE redirections SET {field}=? WHERE user_id=? AND name=?",
            (value if isinstance(value, (str, int)) else json.dumps(value),
             user_id, name)
        )
        await db.commit()

async def db_check_seen(redir_id, h):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_hashes WHERE redir_id=? AND hash=?",
            (redir_id, h)
        ) as cur:
            return await cur.fetchone() is not None

async def db_add_seen(redir_id, h):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_hashes (redir_id,hash,ts) VALUES (?,?,?)",
            (redir_id, h, time.time())
        )
        await db.execute(
            """DELETE FROM seen_hashes WHERE redir_id=? AND rowid NOT IN
            (SELECT rowid FROM seen_hashes WHERE redir_id=? ORDER BY ts DESC LIMIT 50000)""",
            (redir_id, redir_id)
        )
        await db.commit()

# --------------- Message Helpers ---------------

def detect_types(msg):
    t = set()
    if msg.text: t.add("text")
    if msg.audio: t.add("audio")
    if msg.video: t.add("video")
    if msg.voice: t.add("voicenote")
    if getattr(msg, "gif", None) or getattr(msg, "animation", None):
        t.add("animation")
    if msg.photo: t.add("photo")
    if msg.sticker: t.add("sticker")
    if msg.document and not msg.audio and not msg.video: t.add("document")
    if getattr(msg, "fwd_from", None): t.add("forward")
    if msg.reply_to: t.add("reply")
    return t

def msg_text(msg):
    return msg.text or ""

def msg_hash(msg):
    raw = msg_text(msg) + str(getattr(msg, "photo", "")) + str(getattr(msg, "video", ""))
    return hashlib.md5(raw.encode()).hexdigest()

def should_filter(msg, filters_json):
    fl = json.loads(filters_json) if isinstance(filters_json, str) else filters_json
    for t in detect_types(msg):
        if fl.get(t, False): return True
    return False

def matches_list(text, patterns_json):
    pats = json.loads(patterns_json) if isinstance(patterns_json, str) else patterns_json
    for p in pats:
        try:
            if re.search(p, text, re.I | re.M | re.S): return True
        except re.error:
            if p.strip('"').lower() in text.lower(): return True
    return False

def apply_format(text, fmt, msg):
    r = fmt.replace("[[Message.Text]]", text)
    sender = getattr(msg, "sender", None)
    if sender:
        r = r.replace("[[Message.Username]]", getattr(sender, "username", "") or "")
        r = r.replace("[[Message.First_Name]]", getattr(sender, "first_name", "") or "")
        r = r.replace("[[Message.Last_Name]]", getattr(sender, "last_name", "") or "")
    chat = getattr(msg, "chat", None)
    r = r.replace("[[Message.Group]]", getattr(chat, "title", "") or "")
    return r

def apply_power(text, rules):
    for rule in rules:
        rule = rule.strip()
        if not rule: continue
        m = re.match(r'^"(.*)"\s*,\s*"(.*)"$', rule)
        if m:
            text = text.replace(m.group(1), m.group(2))
            continue
        if "=" in rule:
            parts = rule.split("=", 1)
            try: text = re.sub(parts[0], parts[1], text, flags=re.M | re.S)
            except re.error: pass
    return text

def apply_remove_lines(text, keywords):
    lines = text.split("\n"); kept = []
    for line in lines:
        remove = False
        for kw_group in keywords:
            kws = [k.strip() for k in kw_group.split(",")]
            if all(k.lower() in line.lower() for k in kws if k):
                remove = True; break
        if not remove: kept.append(line)
    return "\n".join(kept)

async def translate_text(text, lang):
    if not OPENAI_API_KEY: return text
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        r = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"Translate to {lang}. Return only the translation."},
                {"role": "user", "content": text},
            ],
            max_tokens=2000,
        )
        return r.choices[0].message.content.strip()
    except Exception: return text

# --------------- Telethon Clients ---------------
from telethon import TelegramClient, events

user_clients: dict[str, TelegramClient] = {}
pending: dict[int, dict] = {}
active_handlers: dict[int, list] = {}

def session_path(phone: str) -> str:
    return str(DATA_DIR / f"session_{phone.replace('+','')}")

async def get_or_create_client(phone: str) -> TelegramClient:
    if phone in user_clients and user_clients[phone].is_connected():
        return user_clients[phone]
    client = TelegramClient(session_path(phone), API_ID, API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        user_clients[phone] = client
    return client

# Bot client (session stored in data/ too)
bot = TelegramClient(str(DATA_DIR / "bot_session"), API_ID, API_HASH)

# --------------- Forwarding Engine ---------------

async def setup_forwarding(client: TelegramClient, user_id: int):
    if user_id in active_handlers:
        for h in active_handlers[user_id]:
            try: client.remove_event_handler(h)
            except: pass
    active_handlers[user_id] = []

    redirs = await db_list_redirs(user_id)
    source_ids = set()
    for r in redirs:
        for sid in json.loads(r["sources"]):
            source_ids.add(sid)
    if not source_ids: return

    async def on_message(event):
        chat_id = event.chat_id
        msg = event.message
        redirs_now = await db_list_redirs(user_id)
        for rd in redirs_now:
            sources = json.loads(rd["sources"])
            if chat_id not in sources: continue
            settings = json.loads(rd["settings"])
            filt = json.loads(rd["filters"])
            wl = json.loads(rd["whitelist"] or "[]")
            bl = json.loads(rd["blacklist"] or "[]")
            tf = json.loads(rd["transformation"] or "{}")
            dests = json.loads(rd["destinations"])
            if should_filter(msg, filt): continue
            text = msg_text(msg)
            if wl and not matches_list(text, wl): continue
            if bl and matches_list(text, bl): continue
            if not settings.get("process_duplicates", True):
                h = msg_hash(msg)
                if await db_check_seen(rd["id"], h): continue
                await db_add_seen(rd["id"], h)
            tt = text
            if tf.get("removeLines"):
                tt = apply_remove_lines(tt, tf["removeLines"])
            if tf.get("power"):
                tt = apply_power(tt, tf["power"])
            if tf.get("format"):
                tt = apply_format(tt, tf["format"], msg)
            if rd["translate_lang"]:
                tt = await translate_text(tt, rd["translate_lang"])
            if rd["delay_secs"] and rd["delay_secs"] > 0:
                await asyncio.sleep(rd["delay_secs"])
            for dest in dests:
                try:
                    if settings.get("process_forward"):
                        await client.forward_messages(dest, msg)
                    elif tt != text:
                        await client.send_message(dest, tt)
                    else:
                        await client.send_message(dest, msg)
                except Exception as e:
                    print(f"[ERROR] Forward {rd['name']}→{dest}: {e}")

    handler = client.on(events.NewMessage(chats=list(source_ids)))(on_message)
    active_handlers[user_id] = [on_message]
    print(f"[INFO] Forwarding active for user {user_id} | {len(source_ids)} source(s)")

# --------------- Bot Commands ---------------

HELP = (
    "**TeleFeed Clone** — Commands:\n\n"
    "**Account:**\n"
    "`/connect <phone>` — Connect your Telegram account\n"
    "`/disconnect <phone>` — Disconnect account\n\n"
    "**Redirections:**\n"
    "`/redirection add <name> <phone>` — Create rule\n"
    "`/redirection remove <name>` — Delete rule\n"
    "`/redirection list` — Show all rules\n\n"
    "**Filters & Lists:**\n"
    "`/filters <name> [type]` — Toggle type filters\n"
    "`/whitelist add <name> <patterns>` — Whitelist\n"
    "`/blacklist add <name> <patterns>` — Blacklist\n\n"
    "**Transforms:**\n"
    "`/format <name> <template>` — Message format\n"
    "`/power <name> <rules>` — Regex replace\n"
    "`/removelines <name> <keywords>` — Remove lines\n\n"
    "**Other:**\n"
    "`/delay <name> <seconds>` — Delay forwarding\n"
    "`/translate <name> <lang|off>` — Translation\n"
    "`/settings <name> [setting]` — Toggle settings\n"
    "`/status` — Show all configs"
)

@bot.on(events.NewMessage(pattern=r"^/start"))
async def h_start(event):
    await event.respond(
        "👋 **Welcome to TeleFeed Clone!**\n\n"
        "I forward messages from ANY channel — including restricted ones.\n\n"
        "Start with `/connect +YourPhone` to link your account.\n"
        "Type `/help` for all commands."
    )

@bot.on(events.NewMessage(pattern=r"^/help"))
async def h_help(event):
    await event.respond(HELP)

@bot.on(events.NewMessage(pattern=r"^/connect\s+(\+?\d+)"))
async def h_connect(event):
    phone = event.pattern_match.group(1)
    uid = event.sender_id
    client = await get_or_create_client(phone)
    if await client.is_user_authorized():
        user_clients[phone] = client
        await setup_forwarding(client, uid)
        await event.respond(f"✅ Already connected with **{phone}**!")
        return
    try:
        await client.send_code_request(phone)
        pending[uid] = {"action": "code", "phone": phone, "client": client}
        await event.respond(
            f"📱 Code sent to **{phone}**.\n\n"
            "Reply with the code prefixed by `aa`.\n"
            "Example: code `52234` → send `aa52234`"
        )
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern=r"^aa(\d+)"))
async def h_code(event):
    uid = event.sender_id
    if uid not in pending or pending[uid].get("action") != "code": return
    code = event.pattern_match.group(1)
    p = pending.pop(uid)
    try:
        await p["client"].sign_in(p["phone"], code)
        user_clients[p["phone"]] = p["client"]
        await setup_forwarding(p["client"], uid)
        await event.respond(f"✅ Connected as **{p['phone']}**!")
    except Exception as e:
        if "2FA" in str(e) or "password" in str(e).lower():
            pending[uid] = {**p, "action": "2fa"}
            await event.respond("🔐 2FA enabled. Send your password:")
        else:
            await event.respond(f"❌ Error: {e}")

@bot.on(events.NewMessage(func=lambda e: e.sender_id in pending and pending.get(e.sender_id,{}).get("action")=="2fa"))
async def h_2fa(event):
    uid = event.sender_id
    p = pending.pop(uid)
    try:
        await p["client"].sign_in(password=event.text.strip())
        user_clients[p["phone"]] = p["client"]
        await setup_forwarding(p["client"], uid)
        await event.respond(f"✅ Connected with 2FA as **{p['phone']}**!")
    except Exception as e:
        await event.respond(f"❌ 2FA error: {e}")

@bot.on(events.NewMessage(pattern=r"^/redirection\s+add\s+(\S+)\s+(\+?\d+)"))
async def h_redir_add(event):
    name, phone = event.pattern_match.group(1), event.pattern_match.group(2)
    uid = event.sender_id
    pending[uid] = {"action": "redir_ids", "name": name, "phone": phone}
    await event.respond(
        f"Setting up **{name}** on **{phone}**.\n\n"
        "Send chat IDs: `SOURCE_ID - DEST_ID`\n"
        "Multiple: `-100111,-100222 - -100333`"
    )

@bot.on(events.NewMessage(func=lambda e: e.sender_id in pending and pending.get(e.sender_id,{}).get("action")=="redir_ids"))
async def h_redir_ids(event):
    uid = event.sender_id; text = event.text.strip()
    if "-" not in text:
        await event.respond("❌ Use: `SOURCE_ID - DEST_ID`"); return
    p = pending.pop(uid)
    parts = text.split("-", 1)
    try:
        src = [int(x.strip()) for x in parts[0].split(",") if x.strip()]
        dst = [int(x.strip()) for x in parts[1].split(",") if x.strip()]
    except ValueError:
        await event.respond("❌ IDs must be numbers."); return
    await db_add_redir(uid, p["name"], p["phone"], src, dst)
    await event.respond(f"✅ **{p['name']}** created!\nSource: {src}\nDest: {dst}")
    if p["phone"] in user_clients:
        await setup_forwarding(user_clients[p["phone"]], uid)

@bot.on(events.NewMessage(pattern=r"^/redirection\s+remove\s+(\S+)"))
async def h_redir_rm(event):
    await db_del_redir(event.sender_id, event.pattern_match.group(1))
    await event.respond(f"✅ Removed.")

@bot.on(events.NewMessage(pattern=r"^/redirection\s+list"))
async def h_redir_list(event):
    rs = await db_list_redirs(event.sender_id)
    if not rs: await event.respond("📋 No redirections."); return
    lines = ["📋 **Redirections:**\n"]
    for r in rs:
        lines.append(f"▸ **{r['name']}** ({r['phone']}): {json.loads(r['sources'])} → {json.loads(r['destinations'])}")
    await event.respond("\n".join(lines))

@bot.on(events.NewMessage(pattern=r"^/filters\s+(\S+)\s*(\S*)"))
async def h_filters(event):
    uid = event.sender_id
    name = event.pattern_match.group(1)
    toggle = event.pattern_match.group(2).lower() if event.pattern_match.group(2) else ""
    rd = await db_get_redir(uid, name)
    if not rd: await event.respond(f"❌ {name} not found."); return
    fl = json.loads(rd["filters"])
    if toggle and toggle in fl:
        fl[toggle] = not fl[toggle]
        await db_update_field(uid, name, "filters", fl)
        st = "🚫 BLOCKED" if fl[toggle] else "✅ ALLOWED"
        await event.respond(f"Filter **{toggle}** for **{name}**: {st}")
    else:
        lines = [f"🔍 **Filters for {name}:**\n"]
        for k, v in fl.items():
            lines.append(f"{'🚫' if v else '✅'} {k}: {'BLOCKED' if v else 'allowed'}")
        lines.append(f"\nToggle: `/filters {name} <type>`")
        await event.respond("\n".join(lines))

@bot.on(events.NewMessage(pattern=r"^/whitelist\s+add\s+(\S+)\s+(.+)", func=lambda e: e.is_private))
async def h_wl_add(event):
    uid, name = event.sender_id, event.pattern_match.group(1)
    pats = [p.strip().strip('"') for p in event.pattern_match.group(2).split("\n") if p.strip()]
    await db_update_field(uid, name, "whitelist", pats)
    await event.respond(f"✅ Whitelist for **{name}**: {len(pats)} pattern(s)")

@bot.on(events.NewMessage(pattern=r"^/whitelist\s+remove\s+(\S+)"))
async def h_wl_rm(event):
    await db_update_field(event.sender_id, event.pattern_match.group(1), "whitelist", [])
    await event.respond("✅ Whitelist removed.")

@bot.on(events.NewMessage(pattern=r"^/blacklist\s+add\s+(\S+)\s+(.+)", func=lambda e: e.is_private))
async def h_bl_add(event):
    uid, name = event.sender_id, event.pattern_match.group(1)
    pats = [p.strip().strip('"') for p in event.pattern_match.group(2).split("\n") if p.strip()]
    await db_update_field(uid, name, "blacklist", pats)
    await event.respond(f"✅ Blacklist for **{name}**: {len(pats)} pattern(s)")

@bot.on(events.NewMessage(pattern=r"^/blacklist\s+remove\s+(\S+)"))
async def h_bl_rm(event):
    await db_update_field(event.sender_id, event.pattern_match.group(1), "blacklist", [])
    await event.respond("✅ Blacklist removed.")

@bot.on(events.NewMessage(pattern=r"^/format\s+(\S+)\s+(.+)", func=lambda e: e.is_private))
async def h_format(event):
    uid, name = event.sender_id, event.pattern_match.group(1)
    template = event.pattern_match.group(2)
    rd = await db_get_redir(uid, name)
    if not rd: await event.respond(f"❌ {name} not found."); return
    tf = json.loads(rd["transformation"] or "{}")
    tf["format"] = template
    await db_update_field(uid, name, "transformation", tf)
    await event.respond(f"✅ Format set for **{name}**")

@bot.on(events.NewMessage(pattern=r"^/power\s+(\S+)\s+(.+)", func=lambda e: e.is_private))
async def h_power(event):
    uid, name = event.sender_id, event.pattern_match.group(1)
    rules = [r.strip() for r in event.pattern_match.group(2).split("\n") if r.strip()]
    rd = await db_get_redir(uid, name)
    if not rd: return
    tf = json.loads(rd["transformation"] or "{}")
    tf["power"] = rules
    await db_update_field(uid, name, "transformation", tf)
    await event.respond(f"✅ Power rules set for **{name}**: {len(rules)} rule(s)")

@bot.on(events.NewMessage(pattern=r"^/removelines\s+(\S+)\s+(.+)", func=lambda e: e.is_private))
async def h_rmlines(event):
    uid, name = event.sender_id, event.pattern_match.group(1)
    kws = [k.strip() for k in event.pattern_match.group(2).split("\n") if k.strip()]
    rd = await db_get_redir(uid, name)
    if not rd: return
    tf = json.loads(rd["transformation"] or "{}")
    tf["removeLines"] = kws
    await db_update_field(uid, name, "transformation", tf)
    await event.respond(f"✅ RemoveLines set for **{name}**: {len(kws)} group(s)")

@bot.on(events.NewMessage(pattern=r"^/delay\s+(\S+)\s+(\d+)"))
async def h_delay(event):
    name, secs = event.pattern_match.group(1), int(event.pattern_match.group(2))
    await db_update_field(event.sender_id, name, "delay_secs", secs)
    await event.respond(f"⏱️ Delay for **{name}**: {secs}s")

@bot.on(events.NewMessage(pattern=r"^/translate\s+(\S+)\s+(\S+)"))
async def h_translate(event):
    name, lang = event.pattern_match.group(1), event.pattern_match.group(2)
    if lang == "off": lang = ""
    await db_update_field(event.sender_id, name, "translate_lang", lang)
    await event.respond(f"🌐 Translate {'off' if not lang else lang} for **{name}**")

@bot.on(events.NewMessage(pattern=r"^/settings\s+(\S+)\s*(\S*)"))
async def h_settings(event):
    uid, name = event.sender_id, event.pattern_match.group(1)
    toggle = event.pattern_match.group(2).lower() if event.pattern_match.group(2) else ""
    rd = await db_get_redir(uid, name)
    if not rd: await event.respond(f"❌ {name} not found."); return
    st = json.loads(rd["settings"])
    if toggle and toggle in st:
        st[toggle] = not st[toggle]
        await db_update_field(uid, name, "settings", st)
        await event.respond(f"**{toggle}** for **{name}**: {'ON ✅' if st[toggle] else 'OFF ❌'}")
    else:
        lines = [f"⚙️ **Settings for {name}:**\n"]
        for k, v in st.items():
            lines.append(f"{'✅' if v else '❌'} {k}: {'ON' if v else 'OFF'}")
        lines.append(f"\nToggle: `/settings {name} <setting>`")
        await event.respond("\n".join(lines))

@bot.on(events.NewMessage(pattern=r"^/status"))
async def h_status(event):
    rs = await db_list_redirs(event.sender_id)
    if not rs: await event.respond("No redirections."); return
    lines = ["📊 **Status:**\n"]
    for r in rs:
        lines.append(f"\n▸ **{r['name']}** ({r['phone']})")
        lines.append(f"  {json.loads(r['sources'])} → {json.loads(r['destinations'])}")
        wl = json.loads(r["whitelist"] or "[]")
        bl = json.loads(r["blacklist"] or "[]")
        tf = json.loads(r["transformation"] or "{}")
        if wl: lines.append(f"  ✅ Whitelist: {len(wl)} patterns")
        if bl: lines.append(f"  🚷 Blacklist: {len(bl)} patterns")
        if tf: lines.append(f"  🔄 Transforms: {', '.join(tf.keys())}")
        if r["delay_secs"]: lines.append(f"  ⏱️ Delay: {r['delay_secs']}s")
        if r["translate_lang"]: lines.append(f"  🌐 Translate: {r['translate_lang']}")
    await event.respond("\n".join(lines))

# --------------- Startup ---------------

async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        print("=" * 50)
        print("ERROR: Missing configuration!")
        print("=" * 50)
        print()
        print("Copy .env.example to .env and fill in your values:")
        print("  cp .env.example .env")
        print("  nano .env")
        print()
        print("Required:")
        print(f"  TG_API_ID     = {'SET ✅' if API_ID else 'MISSING ❌'}")
        print(f"  TG_API_HASH   = {'SET ✅' if API_HASH else 'MISSING ❌'}")
        print(f"  TG_BOT_TOKEN  = {'SET ✅' if BOT_TOKEN else 'MISSING ❌'}")
        print(f"  OPENAI_API_KEY= {'SET ✅' if OPENAI_API_KEY else 'NOT SET (optional)'}")
        sys.exit(1)

    await init_db()

    # Reconnect any previously-connected user clients
    redirs = await db_all_redirs()
    phones_seen = set()
    for r in redirs:
        phone = r.get("phone", "")
        if phone and phone not in phones_seen:
            phones_seen.add(phone)
            try:
                client = await get_or_create_client(phone)
                if await client.is_user_authorized():
                    user_clients[phone] = client
                    await setup_forwarding(client, r["user_id"])
                    print(f"[INFO] Reconnected user client: {phone}")
            except Exception as e:
                print(f"[WARN] Could not reconnect {phone}: {e}")

    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"✅ Bot started as @{me.username}")
    print(f"📂 Data directory: {DATA_DIR.absolute()}")
    print("Send /start to the bot in Telegram!")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
