import importlib

import pytest
from starlette.requests import Request

from bot.services import group_enforcement as ge
from bot.services import group_filter
from bot.services.group_enforcement import Action

# `bot.web.app` the attribute is the FastAPI instance; we want the module.
web = importlib.import_module("bot.web.app")


@pytest.fixture(autouse=True)
def _restore_soft_words():
    yield
    group_filter._set_soft_words(list(group_filter.DEFAULT_SOFT_WORDS))


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/moderation",
            "query_string": b"",
            "headers": [],
            "session": {},
        }
    )


def _location(response) -> str:
    return response.headers["location"]


async def test_mode_switch_persists(db):
    response = await web.moderation_mode_action(mode="enforce", _=True)
    assert await ge.get_mode() == "enforce"
    assert "msg=" in _location(response)


async def test_unknown_mode_is_rejected_and_mode_unchanged(db):
    response = await web.moderation_mode_action(mode="bogus", _=True)
    assert await ge.get_mode() == "log"
    assert "err=" in _location(response)


async def test_add_word_persists(db):
    response = await web.moderation_words_add(word="\\bплохо", _=True)
    assert "\\bплохо" in group_filter.get_soft_words()
    assert "msg=" in _location(response)
    assert group_filter.classify("это плохо").rule == "soft_word"


async def test_invalid_regex_is_rejected(db):
    before = group_filter.get_soft_words()
    response = await web.moderation_words_add(word="(unclosed", _=True)
    assert group_filter.get_soft_words() == before
    assert "err=" in _location(response)


async def test_duplicate_word_is_rejected(db):
    existing = group_filter.get_soft_words()[0]
    response = await web.moderation_words_add(word=existing, _=True)
    assert group_filter.get_soft_words().count(existing) == 1
    assert "err=" in _location(response)


async def test_delete_word(db):
    word = group_filter.get_soft_words()[0]
    await web.moderation_words_delete(word=word, _=True)
    assert word not in group_filter.get_soft_words()


async def test_reset_restores_defaults(db):
    await group_filter.save_soft_words(["\\bплохо"])
    await web.moderation_words_reset(_=True)
    assert group_filter.get_soft_words() == group_filter.DEFAULT_SOFT_WORDS


async def test_page_shows_mode_words_and_recent_events(db):
    await ge.record_event(
        user_id=777,
        chat_id=-100,
        message_id=1,
        kind="hard",
        rule="adult_phrase",
        action=Action.MUTE,
        enforced=False,
        snippet="FREE HOT GIRL VIDEOS",
    )
    response = await web.moderation_page(_request(), _=True)
    body = response.body.decode()
    assert response.status_code == 200
    assert "FREE HOT GIRL VIDEOS" in body
    assert "adult_phrase" in body
    assert group_filter.get_soft_words()[0] in body
