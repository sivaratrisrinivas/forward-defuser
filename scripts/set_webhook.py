"""Register the Telegram webhook (with the secret token) for PUBLIC_BASE_URL."""

import asyncio

from defuser.channels.telegram import TelegramChannel
from defuser.config import get_settings


async def main() -> None:
    s = get_settings()
    assert s.telegram_bot_token and s.telegram_webhook_secret, (
        "set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET"
    )
    tg = TelegramChannel(s.telegram_bot_token.get_secret_value())
    url = f"{s.public_base_url.rstrip('/')}/webhooks/telegram"
    print(await tg.set_webhook(url, s.telegram_webhook_secret.get_secret_value()), url)


asyncio.run(main())
