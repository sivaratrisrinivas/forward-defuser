"""Telegram Bot API adapter.

Verified against https://core.telegram.org/bots/api (Oct 2026):
- setWebhook `secret_token` is echoed in the `X-Telegram-Bot-Api-Secret-Token` header on every webhook call.
- Forwarded messages carry `forward_origin` (MessageOrigin: user | hidden_user | chat | channel).
- In privacy mode a bot in a group only receives commands meant for it and replies to its own messages
  (https://core.telegram.org/bots/features#privacy-mode). Group checks therefore use `/check` sent as a
  reply to the suspicious message; the bot then reads `reply_to_message`.
Not verified from docs in this environment (core.telegram.org was unreachable from the build sandbox for the
getFile section): the cloud Bot API download size limit (commonly documented as 20 MB) and the
`https://api.telegram.org/file/bot<token>/<file_path>` download URL format.
"""

import hmac
import re

import httpx

from defuser.channels.base import IncomingEvent, MediaRef

API = "https://api.telegram.org"
COMMANDS = {"check", "start", "help", "forget", "lang"}
_CMD_RE = re.compile(r"^/(?P<cmd>[a-zA-Z_]+)(?:@(?P<bot>[A-Za-z0-9_]+))?(?:\s+(?P<arg>.*))?$", re.S)


def verify_secret(header_value: str | None, expected: str | None) -> bool:
    if not expected:
        return False
    return header_value is not None and hmac.compare_digest(header_value, expected)


def _media_of(msg: dict) -> MediaRef | None:
    if msg.get("photo"):
        best = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        return MediaRef(
            kind="image", file_id=best["file_id"], size=best.get("file_size"), mime_type="image/jpeg"
        )
    doc = msg.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("image/"):
        return MediaRef(
            kind="image", file_id=doc["file_id"], size=doc.get("file_size"), mime_type=doc["mime_type"]
        )
    for key in ("voice", "audio"):
        if msg.get(key):
            a = msg[key]
            return MediaRef(
                kind="voice",
                file_id=a["file_id"],
                size=a.get("file_size"),
                mime_type=a.get("mime_type", "audio/ogg"),
                duration_s=a.get("duration"),
            )
    return None


def _content(msg: dict) -> tuple[str, MediaRef | None, bool]:
    text = msg.get("text") or msg.get("caption") or ""
    return (
        text,
        _media_of(msg),
        bool(msg.get("forward_origin") or msg.get("forward_from") or msg.get("forward_date")),
    )


def parse_update(update: dict, bot_username: str) -> IncomingEvent | None:
    """Return an event the bot should act on, or None. Group messages that don't address the bot are dropped
    here, before anything is stored."""
    msg = update.get("message")
    if not msg:
        return None  # edits, reactions, channel posts etc. are ignored
    chat = msg.get("chat", {})
    chat_type = "private" if chat.get("type") == "private" else "group"
    sender = msg.get("from") or {}
    text, media, forwarded = _content(msg)

    command, arg = None, ""
    m = _CMD_RE.match(text.strip()) if text.startswith("/") else None
    if m and m.group("cmd").lower() in COMMANDS:
        addressed_bot = m.group("bot")
        if addressed_bot and bot_username and addressed_bot.lower() != bot_username.lower():
            return None  # command for another bot
        command, arg = m.group("cmd").lower(), (m.group("arg") or "").strip()

    reply = msg.get("reply_to_message")
    mentioned = bool(bot_username) and f"@{bot_username.lower()}" in text.lower()

    if chat_type == "group":
        # Rule 6: in groups only process explicit requests.
        if command is None and not mentioned:
            return None
        if command not in (None, "check") and command not in ("forget", "help", "lang"):
            return None

    if command == "check" or (mentioned and command is None):
        if reply:
            text, media, forwarded = _content(reply)
            target_id = reply["message_id"]
        else:
            text = (
                arg
                if command == "check"
                else re.sub(f"@{re.escape(bot_username)}", "", text, flags=re.I).strip()
            )
            target_id = msg["message_id"]
        command = "check"
    else:
        target_id = msg["message_id"]

    if command is None and chat_type == "private" and not (text.strip() or media):
        return None

    return IncomingEvent(
        channel="telegram",
        chat_id=str(chat["id"]),
        chat_type=chat_type,
        message_id=str(msg["message_id"]),
        user_id=str(sender.get("id", chat.get("id"))),
        command=command or "check",  # a plain private message is a check request
        command_arg=arg,
        text=text,
        media=media,
        forwarded=forwarded,
        forwarded_many_times=False,  # Telegram does not expose a "forwarded many times" flag
        reply_to_message_id=str(target_id),
        lang_hint=sender.get("language_code"),
    )


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=15)

    def _url(self, method: str) -> str:
        return f"{API}/bot{self._token}/{method}"

    async def _call(self, method: str, payload: dict) -> dict:
        r = await self._client.post(self._url(method), json=payload)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(method, r.status_code, data.get("description", ""), data.get("parameters"))
        return data["result"]

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        payload: dict = {
            "chat_id": chat_id,
            "text": text[:4096],
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        res = await self._call("sendMessage", payload)
        return str(res["message_id"])

    async def typing(self, chat_id: str, message_id: str | None = None) -> None:
        try:
            await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    async def download(self, media: MediaRef) -> bytes:
        info = await self._call("getFile", {"file_id": media.file_id})
        r = await self._client.get(f"{API}/file/bot{self._token}/{info['file_path']}")
        r.raise_for_status()
        return r.content

    async def set_webhook(self, url: str, secret: str) -> dict:
        return await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            },
        )


class TelegramError(Exception):
    def __init__(self, method: str, status: int, description: str, parameters: dict | None):
        super().__init__(f"telegram {method} failed ({status}): {description}")
        self.status = status
        self.retry_after = (parameters or {}).get("retry_after")

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500
