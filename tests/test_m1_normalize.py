import pytest

from defuser.normalize.fetch import FetchBlocked, check_host
from defuser.normalize.text import clean, extract_urls, guess_lang
from defuser.sources import tier_for


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://example.com:8080/",
    ],
)
async def test_ssrf_guard_blocks(url):
    with pytest.raises(FetchBlocked):
        await check_host(url)


def test_extract_urls_and_bare_domains():
    assert extract_urls("see https://bit.ly/abc123, now!") == ["https://bit.ly/abc123"]
    assert extract_urls("Register at pm-free-recharge.in/apply today") == [
        "https://pm-free-recharge.in/apply"
    ]


def test_lang_guess():
    assert guess_lang("आरबीआई 10 रुपये के सिक्के बंद कर रहा है") == "hi"
    assert guess_lang("RBI 10 rupee ke sikke band kar raha hai, sabko forward karo") == "hinglish"
    assert guess_lang("RBI is banning ten rupee coins") == "en"
    assert guess_lang("ரிசர்வ் வங்கி பத்து ரூபாய் நாணயங்களை தடை செய்கிறது") == "ta"


def test_clean_strips_zero_width_and_truncates():
    text, truncated = clean("he​llo " * 10, 20)
    assert "​" not in text and truncated


def test_source_tiers_resist_lookalikes():
    assert tier_for("https://pib.gov.in/PressReleasePage.aspx?PRID=1") == "official"
    assert tier_for("https://pib-gov.in/fact") == "other"
    assert tier_for("https://pib.gov.in.verify-now.co/fact") == "other"
    assert tier_for("https://www.altnews.in/x") == "factchecker"
    assert tier_for("https://altnews-in.com/x") == "other"
    assert tier_for("https://www.indiatoday.in/fact-check/story/x") == "factchecker"
    assert tier_for("https://www.indiatoday.in/india/story/x") == "other"
    assert tier_for("https://www.fakingnews.com/x") == "satire"
