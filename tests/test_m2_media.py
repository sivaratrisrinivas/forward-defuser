import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

from defuser.agent.tools import Toolbox
from defuser.checker import Checker
from defuser.media import MediaProcessor, MediaTooLarge, prepare_image
from defuser.models import ImageContent, Verdict
from tests.helpers import fixtures, msg


def png(w=400, h=300, text="RBI BANS 10 RUPEE COINS"):
    img = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(img).text((10, 10), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeVision:
    def __init__(self, ocr="RBI is banning all 10 rupee coins", pages=None, fail=False):
        self.ocr, self.pages, self.fail = ocr, pages or [], fail

    async def annotate(self, jpeg):
        if self.fail:
            raise RuntimeError("vision 503")
        return {"ocr_text": self.ocr, "matching_pages": self.pages, "web_entities": ["Chennai floods 2015"]}


class FakeSpeech:
    def __init__(self):
        self.chunks = 0

    async def transcribe_chunk(self, flac, lang_hint=None):
        self.chunks += 1
        return "RBI das rupaye ke sikke band kar raha hai"


async def test_image_ocr_and_phash():
    content = await MediaProcessor(FakeVision()).process_image(png())
    assert content.phash and len(content.phash) == 16 and "10 rupee" in content.ocr_text


async def test_similar_images_have_close_phash():
    import imagehash

    original = Image.open(io.BytesIO(png(800, 600)))
    ImageDraw.Draw(original).ellipse((200, 150, 600, 450), fill="red")
    buf = io.BytesIO()
    original.save(buf, "PNG")
    resent = io.BytesIO()
    original.resize((400, 300)).save(resent, "JPEG", quality=60)  # forwarded again: resized + recompressed
    _, a = prepare_image(buf.getvalue())
    _, b = prepare_image(resent.getvalue())
    _, c = prepare_image(png(800, 600, text="something else entirely"))
    assert imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b) <= 6
    assert imagehash.hex_to_hash(a) - imagehash.hex_to_hash(c) > 6


def test_huge_and_bomb_images_rejected():
    from defuser.config import get_settings

    with pytest.raises(MediaTooLarge):
        prepare_image(b"x" * (get_settings().max_image_bytes + 1))
    with pytest.raises(MediaTooLarge):
        prepare_image(png(9000, 9000))


async def test_corrupt_image_and_vision_outage_do_not_crash():
    assert (await MediaProcessor(FakeVision()).process_image(b"not an image")).error
    c = await MediaProcessor(FakeVision(fail=True)).process_image(png())
    assert c.error and c.phash


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
async def test_long_voice_note_truncated_and_chunked(tmp_path):
    out = tmp_path / "v.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=300:duration=330",
            "-c:a",
            "libopus",
            str(out),
        ],
        check=True,
    )
    speech = FakeSpeech()
    audio = await MediaProcessor(FakeVision(), speech).process_voice(out.read_bytes(), "hi")
    assert audio.truncated and audio.duration_s > 300 and speech.chunks == 6 and "sikke" in audio.transcript


async def test_reverse_image_search_builds_evidence():
    page_url = "https://www.boomlive.in/fact-check/old-chennai-airport-flood-photo"
    case = {
        "factchecks": [],
        "search": [],
        "pages": {
            page_url: "BOOM found that the photo is from Chennai airport during "
            "the 2015 floods and is not from Mumbai this week at all."
        },
    }
    backends, fetcher = fixtures(case)
    tb = Toolbox(backends, fetcher, image_backend=object())
    m = msg(
        image=ImageContent(
            phash="0" * 16,
            matching_pages=[
                {"url": page_url, "title": "Old photo"},
                {"url": "https://randomblog.example/x", "title": "x"},
            ],
        )
    )
    out = await tb.execute("reverse_image_search", {}, {"msg": m})
    tb.absorb(out)
    assert (
        out.evidence and out.evidence[0].kind == "image_match" and out.evidence[0].tier.value == "factchecker"
    )
    assert "reverse_image_search" in [t["name"] for t in tb.specs()]


async def test_image_only_message_checked_via_ocr():
    from tests.helpers import happy_script

    m = msg(
        text="",
        image=ImageContent(phash="1" * 16, ocr_text="RBI is banning all 10 rupee coins from next month"),
    )
    backends, fetcher = fixtures()
    res = await Checker(happy_script(), backends, fetcher).check(m)
    assert res.verdict == Verdict.FALSE


async def test_pipeline_photo_message(clean, tg):
    from defuser import pipeline
    from defuser.channels.base import MediaRef
    from defuser.ingest import ingest_event
    from defuser.llm.provider import set_llm
    from tests.conftest import drain_jobs
    from tests.helpers import event, happy_script

    tg.media["photo1"] = png()
    backends, fetcher = fixtures()
    pipeline.set_deps(backends=backends, fetcher=fetcher, media_processor=MediaProcessor(FakeVision()))
    set_llm(happy_script())
    try:
        await ingest_event(clean, event(text="", media=MediaRef(kind="image", file_id="photo1")))
        await drain_jobs(clean)
    finally:
        set_llm(None)
        pipeline.set_deps()
    assert tg.sent and tg.sent[0][1].startswith("❌")
