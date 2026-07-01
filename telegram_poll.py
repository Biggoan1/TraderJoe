import os
import requests
from dotenv import load_dotenv

load_dotenv(".env")

token = os.getenv("TELEGRAM_BOT_TOKEN")

url = f"https://api.telegram.org/bot{token}/getUpdates"

data = requests.get(url).json()

updates = data.get("result", [])

def parse_reply(text):
    text = text.strip().lower()

    if text == "y":
        return True, 500

    if text == "n":
        return False, 0

    try:
        amount = float(text)

        if amount > 0:
            return True, amount

    except:
        pass

    return None, None
    



if not updates:
    print("No messages")
    raise SystemExit()

latest = updates[-1]

chat_id = latest["message"]["chat"]["id"]
text = latest["message"]["text"]

print("CHAT:", chat_id)
print("TEXT:", text)

print(parse_reply("y"))
print(parse_reply("1000"))
print(parse_reply("n"))