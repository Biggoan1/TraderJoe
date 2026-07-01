import time
import telegram_approvals

print("💎 Trader Joe Telegram Daemon Started")

while True:
    try:
        telegram_approvals.main()
    except Exception as e:
        print(f"Daemon Error: {type(e).__name__}")

    time.sleep(15)
