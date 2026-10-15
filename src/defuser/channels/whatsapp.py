"""WhatsApp Cloud API adapter.

Not verified against Meta's docs in this build: developers.facebook.com was unreachable from the build sandbox.
The shapes below were cross-checked against the open-source `pywa` client (4.5.0) and should be confirmed against
https://developers.facebook.com/docs/whatsapp/cloud-api before production:
- Webhook verification: GET with `hub.mode=subscribe`, `hub.verify_token`, `hub.challenge`; reply with the
  challenge as plain text.
- Every POST is signed: `X-Hub-Signature-256: sha256=<hex HMAC-SHA256(app secret, raw body)>`.
- Payload: entry[].changes[].value.{metadata, contacts[], messages[], statuses[]}; a message has `from`, `id`,
  `type` (text | image | audio | ...), `text.body`, `image.{id, mime_type, caption}`, `audio.{id, mime_type,
  voice}`, `context.{forwarded, frequently_forwarded, id}` and `group_id` when sent in a group.
- Send: POST /{version}/{phone_number_id}/messages with messaging_product=whatsapp, to, type=text,
  text.{body, preview_url}, context.message_id (reply). Error 131047 means the 24-hour customer-service window has
  closed, so free-form follow-ups to that person are no longer allowed (a pre-approved template would be needed).
- Media: GET /{version}/{media_id} -> {url, mime_type, file_size}; then GET url with the same bearer token.
- Typing: POST .../messages with status=read, message_id and typing_indicator.type=text (marks the message read).

Groups: the WhatsApp Groups API is limited (business-created, invite-only groups). In a group the bot only acts on
messages that start with `/check` or `check` followed by the text; a reply-to-message check cannot work because the
webhook carries only the replied message's id, never its content, and the bot does not store group chatter.
"""

import hashlib
import hmac
import re

import httpx

from defuser.channels.base import IncomingEvent, MediaRef

GRAPH = "https://graph.facebook.com"
COMMANDS = {"check", "start", "help", "forget", "lang"}
_CMD_RE = re.compile(r"^/?(?P<cmd>check|start|help|forget|lang)\b[:\s]*(?P<arg>.*)$", re.I | re.S)


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str | None) -> bool:
    if not app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.removeprefix("sha256="))


def verify_handshake(params: dict, verify_token: str | None) -> str | None:
    """Returns the challenge to echo, or None to reject."""
    if not verify_token or params.get("hub.mode") != "subscribe":
        return None
    if not hmac.compare_digest(params.get("hub.verify_token", ""), verify_token):
        return None
    return params.get("hub.challenge", "")


def _media(m: dict) -> MediaRef | None:
    if m.get("type") == "image" and m.get("image"):
        return MediaRef(kind="image", file_id=m["image"]["id"], mime_type=m["image"].get("mime_type"))
    if m.get("type") == "document" and str(m.get("document", {}).get("mime_type", "")).startswith("image/"):
        d = m["document"]
        return MediaRef(kind="image", file_id=d["id"], mime_type=d["mime_type"])
    if m.get("type") == "audio" and m.get("audio"):
        return MediaRef(kind="voice", file_id=m["audio"]["id"], mime_type=m["audio"].get("mime_type"))
    return None


def parse_message(m: dict) -> IncomingEvent | None:
    if m.get("type") not in ("text", "image", "audio", "document"):
        return None  # reactions, stickers, locations, interactive replies, system messages
    text = (
        (m.get("text") or {}).get("body", "")
        if m["type"] == "text"
        else (m.get(m["type"]) or {}).get("caption", "")
    )
    media = _media(m)
    ctx = m.get("context") or {}
    in_group = "group_id" in m
    cmd = _CMD_RE.match(text.strip()) if text else None
    command, arg = (cmd.group("cmd").lower(), cmd.group("arg").strip()) if cmd else (None, "")
    if in_group:
        if command is None:
            return None  # rule 6: ignore group chatter
        if command == "check":
            text = arg
            if not text and not media:
                command = "help"  # a bare reply-check: we can't see the target, so explain how to forward it
    elif command == "check":
        text = arg
    if command is None:
        if not (text.strip() or media):
            return None
        command = "check"
    return IncomingEvent(
        channel="whatsapp",
        chat_id=f"group:{m['group_id']}" if in_group else m["from"],
        chat_type="group" if in_group else "private",
        message_id=m["id"],
        user_id=m["from"],
        command=command,
        command_arg=arg,
        text=text,
        media=media,
        forwarded=bool(ctx.get("forwarded") or ctx.get("frequently_forwarded")),
        forwarded_many_times=bool(ctx.get("frequently_forwarded")),
        reply_to_message_id=m["id"],
        lang_hint=None,  # WhatsApp does not send a client language
    )


def parse_webhook(payload: dict) -> list[IncomingEvent]:
    events = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") not in (None, "messages"):
                continue
            for m in (change.get("value") or {}).get("messages") or []:
                try:
                    ev = parse_message(m)
                except (KeyError, TypeError, AttributeError):
                    ev = None  # malformed message: drop it, don't fail the batch
                if ev is not None:
                    events.append(ev)
    return events


class WhatsAppError(Exception):
    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"whatsapp send failed ({status}, code {code}): {message}")
        self.status, self.code = status, code

    @property
    def retryable(self) -> bool:
        return self.code != 131047 and (self.status == 429 or self.status >= 500)

    @property
    def window_closed(self) -> bool:
        return self.code == 131047


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(
        self, token: str, phone_number_id: str, version: str, client: httpx.AsyncClient | None = None
    ):
        self._token, self._phone, self._v = token, phone_number_id, version
        self._client = client or httpx.AsyncClient(timeout=15)

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(self, payload: dict) -> dict:
        r = await self._client.post(
            f"{GRAPH}/{self._v}/{self._phone}/messages", json=payload, headers=self._auth
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error") or {}
            raise WhatsAppError(r.status_code, err.get("code"), err.get("message", r.text[:200]))
        return data

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        is_group = chat_id.startswith("group:")
        payload: dict = {
            "messaging_product": "whatsapp",
            "recipient_type": "group" if is_group else "individual",
            "to": chat_id.removeprefix("group:"),
            "type": "text",
            "text": {"body": text[:4096], "preview_url": False},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        data = await self._post(payload)
        return (data.get("messages") or [{}])[0].get("id", "")

    async def typing(self, chat_id: str, message_id: str | None = None) -> None:
        if not message_id:
            return  # the typing indicator is tied to a received message id
        try:
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                    "typing_indicator": {"type": "text"},
                }
            )
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    async def download(self, media: MediaRef) -> bytes:
        r = await self._client.get(f"{GRAPH}/{self._v}/{media.file_id}", headers=self._auth)
        r.raise_for_status()
        info = r.json()
        size = info.get("file_size")
        if size and int(size) > 25_000_000:
            from defuser.media import MediaTooLarge

            raise MediaTooLarge(f"{size} bytes")
        f = await self._client.get(info["url"], headers=self._auth)
        f.raise_for_status()
        return f.content
