"""Builds channel clients from settings. Tests swap entries in `_overrides`."""

from defuser.channels.base import Channel
from defuser.config import get_settings

_overrides: dict[str, Channel] = {}
_cache: dict[str, Channel] = {}


def set_channel(name: str, channel: Channel | None) -> None:
    if channel is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = channel


def get_channel(name: str) -> Channel:
    if name in _overrides:
        return _overrides[name]
    if name not in _cache:
        s = get_settings()
        if name == "telegram":
            from defuser.channels.telegram import TelegramChannel

            if not s.telegram_bot_token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
            _cache[name] = TelegramChannel(s.telegram_bot_token.get_secret_value())
        elif name == "whatsapp":
            from defuser.channels.whatsapp import WhatsAppChannel

            if not (s.whatsapp_access_token and s.whatsapp_phone_number_id):
                raise RuntimeError("WhatsApp is not configured")
            _cache[name] = WhatsAppChannel(
                s.whatsapp_access_token.get_secret_value(),
                s.whatsapp_phone_number_id,
                s.whatsapp_graph_version,
            )
        else:
            raise KeyError(name)
    return _cache[name]
