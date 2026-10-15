"""Channel-neutral view of an incoming webhook event and an outbound reply."""

from typing import Protocol

from pydantic import BaseModel


class MediaRef(BaseModel):
    kind: str  # image | voice
    file_id: str
    mime_type: str | None = None
    size: int | None = None
    duration_s: float | None = None


class IncomingEvent(BaseModel):
    channel: str
    chat_id: str
    chat_type: str  # private | group
    message_id: str
    user_id: str
    command: str | None = None  # check | start | help | forget | lang
    command_arg: str = ""
    text: str = ""
    media: MediaRef | None = None
    forwarded: bool = False
    forwarded_many_times: bool = False
    reply_to_message_id: str | None = None
    lang_hint: str | None = None  # client language code when the channel provides one

    @property
    def is_group(self) -> bool:
        return self.chat_type != "private"

    @property
    def kind(self) -> str:
        if self.command and self.command != "check":
            return "command"
        if self.media:
            return self.media.kind
        return "text"


class Channel(Protocol):
    name: str

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str: ...

    async def download(self, media: MediaRef) -> bytes: ...

    async def typing(self, chat_id: str) -> None: ...
