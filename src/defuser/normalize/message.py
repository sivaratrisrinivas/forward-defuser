"""Builds a CanonicalMessage from a channel event: text cleanup, link fetching, image and voice handling."""

import asyncio
import logging

from defuser.channels.base import IncomingEvent
from defuser.config import get_settings
from defuser.models import CanonicalMessage, ChatRef, LinkContent
from defuser.normalize.fetch import Fetcher, extract_text
from defuser.normalize.text import clean, extract_urls, guess_lang
from defuser.privacy import hash_id

log = logging.getLogger(__name__)
MAX_LINKS = 3


async def fetch_links(fetcher: Fetcher, urls: list[str]) -> list[LinkContent]:
    async def one(u: str) -> LinkContent:
        res = await fetcher.fetch(u)
        if not res.ok:
            return LinkContent(
                url=u, final_url=res.final_url, redirect_chain=res.redirect_chain, error=res.error
            )
        title, text = extract_text(res.body, res.content_type, res.final_url)
        return LinkContent(
            url=u,
            final_url=res.final_url,
            redirect_chain=res.redirect_chain,
            title=title,
            text=(text or "")[:20_000],
        )

    return list(await asyncio.gather(*[one(u) for u in urls[:MAX_LINKS]]))


async def normalize_event(
    ev: IncomingEvent, chat: ChatRef, fetcher: Fetcher, media_bytes: bytes | None = None, media_processor=None
) -> CanonicalMessage:
    s = get_settings()
    text, truncated = clean(ev.text, s.max_text_chars)
    msg = CanonicalMessage(
        channel=ev.channel,
        message_id=ev.message_id,
        user_hash=hash_id(ev.channel, ev.user_id),
        chat=chat,
        text=text,
        forwarded=ev.forwarded,
        forwarded_many_times=ev.forwarded_many_times,
        truncated=truncated,
    )
    if ev.media and media_processor is not None and media_bytes is not None:
        await media_processor.process(ev.media, media_bytes, msg)
    msg.links = await fetch_links(fetcher, extract_urls(msg.combined_text()))
    msg.lang = guess_lang(msg.combined_text(), ev.lang_hint)
    return msg
