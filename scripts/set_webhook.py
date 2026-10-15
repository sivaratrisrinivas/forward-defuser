"""Register the Telegram webhook (with the secret token) for PUBLIC_BASE_URL, or show its status.

PUBLIC_BASE_URL=https://... uv run python scripts/set_webhook.py
uv run python scripts/set_webhook.py --info     # getWebhookInfo: pending updates, last error
"""

import asyncio
import sys

from defuser.channels.telegram import TelegramChannel
from defuser.config import get_settings


async def main() -> None:
    s = get_settings()
    assert s.telegram_bot_token, "set TELEGRAM_BOT_TOKEN"
    tg = TelegramChannel(s.telegram_bot_token.get_secret_value())
    if "--info" in sys.argv:
        print(await tg._call("getWebhookInfo", {}))
        return
    assert s.telegram_webhook_secret, "set TELEGRAM_WEBHOOK_SECRET"
    url = f"{s.public_base_url.rstrip('/')}/webhooks/telegram"
    print(await tg.set_webhook(url, s.telegram_webhook_secret.get_secret_value()), url)


asyncio.run(main())
