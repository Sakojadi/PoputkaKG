import json

from aiogram import Bot
from aiogram.methods import GetMe, GetUpdates

from bot.services.tolerant_session import TolerantSession

BOT = Bot(token="123:abc", session=TolerantSession())


def _msg(update_id, blocks=None):
    message = {
        "message_id": 1,
        "date": 0,
        "chat": {"id": -100, "type": "supergroup", "title": "g"},
        "text": "hi",
    }
    if blocks is not None:
        message["rich_message"] = {"blocks": blocks}
    return {"update_id": update_id, "message": message}


def _check(method, result):
    content = json.dumps({"ok": True, "result": result})
    return BOT.session.check_response(BOT, method, 200, content)


def test_unparseable_update_is_replaced_by_bare_update():
    good = _msg(1)
    bad = _msg(2, blocks=[{"type": "some_future_block_type"}])
    resp = _check(GetUpdates(), [good, bad])
    assert [u.update_id for u in resp.result] == [1, 2]
    assert resp.result[0].message is not None
    assert resp.result[1].message is None


def test_other_methods_still_raise():
    import pytest
    from aiogram.exceptions import ClientDecodeError

    with pytest.raises(ClientDecodeError):
        _check(GetMe(), {"bogus": 1})
