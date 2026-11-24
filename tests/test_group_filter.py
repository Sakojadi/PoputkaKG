import pytest

from bot.services import group_filter
from bot.services.group_filter import Verdict, classify

SPAM = "FREE HOT GIRL VIDEOS Full video · No blur · Watch now 18+ only — tap button"


@pytest.fixture(autouse=True)
def _restore_soft_words():
    yield
    group_filter._set_soft_words(list(group_filter.DEFAULT_SOFT_WORDS))


def test_screenshot_spam_text_is_hard_spam():
    verdict = classify(SPAM)
    assert verdict is not None
    assert (verdict.kind, verdict.rule) == ("hard", "adult_phrase")


def test_adult_words_in_button_labels_are_hard_spam():
    verdict = classify("Watch", buttons=[("POV SEX", None), ("LIVE CAM", None)])
    assert verdict is not None
    assert (verdict.kind, verdict.rule) == ("hard", "adult_phrase")


def test_forwarded_message_with_link_button_is_hard_spam():
    verdict = classify("Watch", forwarded=True, buttons=[("Open", "https://x.example")])
    assert verdict == Verdict("hard", "forwarded_link_buttons", "https://x.example")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"forwarded": True},
        {"forwarded": True, "buttons": [("Reply", None)]},
        {"forwarded": False, "buttons": [("Open", "https://t.me/driver_osh")]},
    ],
)
def test_legitimate_forwards_and_buttons_are_not_flagged(kwargs):
    assert classify("Бишкектен Ошко жолго чыгам", **kwargs) is None


@pytest.mark.parametrize("text", [None, ""])
def test_empty_text_is_clean(text):
    assert classify(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Нужен член семьи на заднее сиденье",
        "соль не везём",
        "трава на обочине",
        "Бишкек Ош жеңил машина 0555123456",
        "Звоните wa.me/996555123456",
        "Бишкектен ошко жолго чыгабыз машина к5 2000 сомдон 0504331331",
        "Ok.Come to Alamedin at 5",
        "Hello.Today going to Osh",
        "Hello.To going to Osh",
        "отправка в 8:00.Info",
        "Пишите на почту driver@mail.ru",
        "Точка сбора: https://2gis.kg/bishkek/geo/70000001",
        "Вот точка https://maps.app.goo.gl/abc123",
        "Едем от https://www.google.com/maps/place/Osh",
        "olive cam going to Osh",
    ],
)
def test_ordinary_chat_is_not_flagged(text):
    assert classify(text) is None


def test_profanity_is_a_soft_violation():
    verdict = classify("ты сука")
    assert verdict is not None
    assert (verdict.kind, verdict.rule) == ("soft", "soft_word")


def test_disallowed_link_is_a_soft_violation():
    verdict = classify("смотри instagram.com/foo")
    assert verdict is not None
    assert (verdict.kind, verdict.rule) == ("soft", "disallowed_link")


def test_hard_beats_soft():
    assert classify("сука porn").kind == "hard"


async def test_soft_list_edits_persist_and_take_effect(db):
    await group_filter.save_soft_words(["\\bплохо"])
    group_filter._set_soft_words([])
    await group_filter.reload_soft_words()
    assert group_filter.get_soft_words() == ["\\bплохо"]
    assert classify("это плохо").rule == "soft_word"
    assert classify("ты сука") is None


def test_invalid_soft_pattern_is_skipped_not_fatal():
    group_filter._set_soft_words(["(unclosed", "плохо"])
    assert classify("это плохо").rule == "soft_word"
    assert classify("всё хорошо") is None


def test_link_hidden_behind_a_button_or_text_link_is_a_soft_violation():
    by_button = classify("Free gift", buttons=[("Claim", "https://scam.xyz")])
    assert by_button is not None
    assert (by_button.kind, by_button.rule) == ("soft", "disallowed_link")

    hidden = classify("Нажми сюда", urls=["https://scam.xyz"])
    assert hidden is not None
    assert (hidden.kind, hidden.rule) == ("soft", "disallowed_link")


def test_contact_links_behind_buttons_are_fine():
    assert classify("Звоните", buttons=[("Написать", "https://t.me/driver_osh")]) is None
    assert classify("Звоните", urls=["https://wa.me/996555123456"]) is None


@pytest.mark.parametrize(
    "text",
    [
        "Секс знакомства",
        "free sex chat",
        "send nudes",
        "XXX videos",
        "Escort service Bishkek",
        "интим услуги Бишкек",
        "смотри 18+ видео",
    ],
)
def test_more_adult_spam_is_hard(text):
    verdict = classify(text)
    assert verdict is not None
    assert verdict.kind == "hard", text


@pytest.mark.parametrize(
    "text",
    [
        "ты сволочь",
        "дебил на дороге",
        "идиот за рулём",
        "тут говно",
        "ты чмо",
        "какой лох",
        "урод",
        "дерьмо",
        "букмекер и 1win",
        "melbet промокод",
        "mostbet бонус",
        "ставки на спорт",
        "рулетка",
        "работа на дому 5000 в день",
        "заработок в интернете",
        "быстрые деньги",
        "займ без отказа",
        "марихуана",
        "наркотики",
        "криптовалюта",
    ],
)
def test_more_rule_violations_are_soft(text):
    verdict = classify(text)
    assert verdict is not None, text
    assert verdict.kind == "soft", text


@pytest.mark.parametrize(
    "text",
    [
        "Sussex going to Osh",
        "Essex road",
        "сексуальный голос по радио",
        "Лохматый пёс в машине",
        "урожай яблок везём",
        "Уроки вождения сегодня",
        "дерево упало на дорогу",
        "говорим по телефону",
        "чмок всем",
        "работа на дороге",
        "ставки по кредиту выросли",
        "рулевое управление сломалось",
        "идиоматические выражения",
    ],
)
def test_ordinary_words_near_the_new_stems_are_not_flagged(text):
    assert classify(text) is None, text


def test_default_lists_compile_and_have_no_duplicates():
    import re

    for words in (group_filter.HARD_PHRASES, group_filter.DEFAULT_SOFT_WORDS):
        assert len(words) == len(set(words))
        for word in words:
            re.compile(word)
