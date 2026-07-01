import os
import requests
from dotenv import load_dotenv

load_dotenv(".env")

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")

if not token:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")

if not chat_id:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    print("No TELEGRAM_CHAT_ID yet.")
    print("Open this URL after messaging your bot:")
    print(url)
    raise SystemExit()

url = f"https://api.telegram.org/bot{token}/sendMessage"

message = """
💎 Trader Joe is online.

Hermes backend: ✅
Paper trading: ✅
AI analyst: ✅
Telegram: ✅

Joe is ready to responsibly make questionable financial decisions.
"""

response = requests.post(url, json={
    "chat_id": chat_id,
    "text": message
})

print(response.status_code)
print(response.text)