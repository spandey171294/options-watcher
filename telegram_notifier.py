import os
import requests


def send_telegram(message: str) -> None:

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        requests.post(
            url,
            json=payload,
            timeout=20,
        )

    except Exception as e:
        print(f"Telegram Error: {e}")
