# TeleFeed Clone

Full Telegram auto-forwarding bot with MTProto.

## Quick Start (Docker)
```
cp .env.example .env
nano .env          # Fill in your credentials
docker compose up -d
docker compose logs -f
```

## Quick Start (No Docker)
```
pip install -r requirements.txt
cp .env.example .env
nano .env          # Fill in your credentials
python telefeed_clone.py
```

## Where to Get Credentials

| Credential | Where |
|---|---|
| TG_API_ID + TG_API_HASH | https://my.telegram.org |
| TG_BOT_TOKEN | @BotFather on Telegram |
| OPENAI_API_KEY | https://platform.openai.com (optional) |

## Usage in Telegram
1. /connect +YourPhone
2. Enter code with aa prefix: aa52234
3. /redirection add mytest +YourPhone
4. Send: SOURCE_ID - DEST_ID
5. Done! Messages auto-forward.
