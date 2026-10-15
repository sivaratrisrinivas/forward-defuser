"""Media handling (images, voice notes). Full implementation lands in M2."""

from defuser.channels.base import MediaRef
from defuser.models import CanonicalMessage, ImageContent


async def download_media(channel, media: MediaRef) -> bytes:
    return await channel.download(media)


class MediaProcessor:
    @classmethod
    def default(cls) -> "MediaProcessor":
        return cls()

    async def process(self, media: MediaRef, data: bytes, msg: CanonicalMessage) -> None:
        msg.image = ImageContent(error="media not supported yet")
