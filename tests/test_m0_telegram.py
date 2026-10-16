import httpx

from defuser.channels.telegram import parse_update


def upd(text, chat_type="private", mid=10, reply=None, chat_id=111, **extra):
    msg = {
        "message_id": mid,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 5, "language_code": "hi"},
        "text": text,
    }
    if reply:
        msg["reply_to_message"] = reply
    msg.update(extra)
    return {"update_id": mid, "message": msg}


def test_private_text_is_a_check():
    ev = parse_update(upd("Drinking hot water cures covid"), "DefuserBot")
    assert ev.command == "check" and ev.text.startswith("Drinking") and not ev.is_group


def test_group_chatter_is_dropped():
    assert parse_update(upd("good morning everyone", chat_type="supergroup"), "DefuserBot") is None


def test_group_check_reply_reads_target_message():
    target = {
        "message_id": 7,
        "text": "RBI is banning 10 rupee coins",
        "forward_origin": {"type": "hidden_user"},
    }
    ev = parse_update(upd("/check@DefuserBot", chat_type="group", reply=target), "DefuserBot")
    assert ev.command == "check" and ev.text == "RBI is banning 10 rupee coins"
    assert ev.forwarded and ev.reply_to_message_id == "7" and ev.is_group


def test_command_for_other_bot_ignored():
    assert parse_update(upd("/check@OtherBot", chat_type="group"), "DefuserBot") is None


def test_edits_ignored():
    assert parse_update({"update_id": 1, "edited_message": {"message_id": 1}}, "DefuserBot") is None


def test_photo_picks_largest():
    u = upd("", photo=[{"file_id": "s", "file_size": 10}, {"file_id": "L", "file_size": 999}])
    u["message"].pop("text")
    ev = parse_update(u, "DefuserBot")
    assert ev.media.kind == "image" and ev.media.file_id == "L"


async def test_webhook_rejects_bad_secret(clean):
    from defuser.app import create_app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
        r = await c.post(
            "/webhooks/telegram", json=upd("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}
        )
        assert r.status_code == 403
        r = await c.post(
            "/webhooks/telegram", json=upd("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
        )
        assert r.status_code == 200
    assert await clean.fetchval("SELECT count(*) FROM jobs") == 1
