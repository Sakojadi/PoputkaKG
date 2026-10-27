import pytest

from bot.services import moderation


@pytest.fixture(autouse=True)
def _restore_words():
    yield
    moderation._set_banned_words(list(moderation.DEFAULT_BANNED_WORDS))


def test_blocked_word_is_reported_as_whole_word():
    assert moderation.moderate_text("Откуда: Оштон, Озгондон, Манастан") == (
        False,
        "banned_words_detected",
        "Озгондон",
        "гондон",
    )


def test_clean_text_passes():
    assert moderation.moderate_text("Бишкек - Ош, сегодня") == (True, "", "", "")


async def test_edits_persist_and_take_effect(db):
    await moderation.save_banned_words(["\\bгондон"])
    assert moderation.moderate_text("Озгондон")[0] is True
    assert moderation.moderate_text("гондон")[0] is False

    moderation._set_banned_words([])
    await moderation.reload_banned_words()
    assert moderation.BANNED_WORDS == ["\\bгондон"]


def test_invalid_pattern_is_skipped_not_fatal():
    moderation._set_banned_words(["(unclosed", "плохо"])
    assert moderation.moderate_text("это плохо")[0] is False
    assert moderation.moderate_text("всё хорошо")[0] is True
