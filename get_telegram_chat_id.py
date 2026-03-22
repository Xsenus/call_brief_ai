#!/usr/bin/env python3
import json
import os
import sys

import requests
from dotenv import load_dotenv


load_dotenv()


def main() -> int:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1

    drop_webhook = os.getenv("TELEGRAM_DROP_WEBHOOK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    base_url = f"https://api.telegram.org/bot{bot_token}"

    if drop_webhook:
        response = requests.post(
            f"{base_url}/deleteWebhook",
            json={"drop_pending_updates": False},
            timeout=30,
        )
        response.raise_for_status()
        print("Webhook removed:")
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        print()

    response = requests.get(f"{base_url}/getUpdates", timeout=30)
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
