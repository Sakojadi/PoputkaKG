"""Pure classification of group chat messages: no Telegram or database I/O
except loading/saving the admin-editable soft word list.

`hard` verdicts are spam that should never be tolerated (porn bots, forwarded
link-button spam). `soft` verdicts are rule violations (profanity, drugs,
gambling, outside links) that get a warning ladder first.
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from bot.services.moderation import ALLOWED_LINK_PARTS
from bot.services.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

SOFT_WORDS_KEY = "group_soft_words"

# Phrases that only spam uses. Kept deliberately specific: a false positive here
# means a mute, then a ban.
HARD_PHRASES = [
    r"\b18\s*\+\s*only",
    r"\bhot\s+girls?",
    r"\bsquirt",
    r"\blive\s*cam",
    r"\bpov\s+sex",
    r"\bbj\s+vid",
    r"\bwatch\s+(all\s+)?videos?\s+free",
    r"\bfull\s+video",
    r"\bno\s+blur",
    r"\bonlyfans",
    r"\bporn",
    r"\bпорно",
    r"\bэскорт",
    r"\bпроститутк",
    r"\bсекс\s*видео",
    r"\bсекс\b",
    r"\bsex\b",
    r"\bnudes?\b",
    r"\bxxx\b",
    r"\bescort",
    r"\bинтим\s*услуг",
    r"\b18\s*\+\s*(video|видео|фото|photo)",
]

# Conservative defaults, hand-picked from the ad-flow list. Short or ambiguous
# stems that hit ordinary Russian/Kyrgyz words (член, соль, трав, меф, вирт,
# гидра...) are intentionally left out.
DEFAULT_SOFT_WORDS = [
    r"\bхуй",
    r"\bхуе",
    r"\bхуя",
    r"\bпизд",
    r"\bебат",
    r"\bебан",
    r"\bебал",
    r"\bблят",
    r"\bбляд",
    r"\bсука",
    r"\bсучк",
    r"\bмудак",
    r"\bгандон",
    r"\bпидор",
    r"\bпидар",
    r"нахуй",
    r"похуй",
    r"пиздец",
    r"еблан",
    r"долбоеб",
    r"долбоёб",
    r"заебал",
    r"блядина",
    r"\bсигейин",
    r"\bкотогум",
    r"\bkotok",
    r"\bhuy",
    r"\bpizd",
    r"\bblyat",
    r"\bsuka",
    r"\bfuck",
    r"\bbitch",
    r"героин",
    r"кокаин",
    r"амфетамин",
    r"мефедрон",
    r"гашиш",
    r"спайс",
    r"закладк",
    r"1xbet",
    r"казино",
    r"casino",
    r"пассивный\s*доход",
    r"легкий\s*заработок",
    # insults
    r"\bсволоч",
    r"\bурод",
    r"\bдебил",
    r"\bидиот",
    r"\bговн",
    r"\bдерьм",
    r"\bчмо\b",
    r"\bшлюх",
    r"\bлох\b",
    # drugs
    r"\bмарихуан",
    r"\bнаркот",
    # gambling
    r"\bбукмекер",
    r"\bрулетк",
    r"\b1win",
    r"\bmelbet",
    r"\bmostbet",
    r"\bpin-?up\b",
    r"\bставк\w*\s+на\s+спорт",
    # get-rich / loan scams
    r"\bработа\s+на\s+дому",
    r"\bзаработок\s+в\s+интернет",
    r"\bбыстрые\s+деньги",
    r"\bзайм\s+без\s+(отказ|проверк)",
    r"\bкриптовалют",
]

# Map and navigation links are everyday traffic in a taxi group.
MAP_LINK_PARTS = [
    "2gis.",
    "goo.gl/maps",
    "maps.app.goo.gl",
    "google.com/maps",
    "maps.google.",
    "yandex.ru/maps",
    "yandex.kg/maps",
    "yandex.com/maps",
]
_ALLOWED_LINK_PARTS = [*ALLOWED_LINK_PARTS, *MAP_LINK_PARTS]

# Stricter than the ad-flow URL pattern: a bare domain must start at a word
# boundary (not after "@", so e-mail addresses are not links), every label must
# contain a letter ("8:00.Info" is a time, not a site), and only unambiguous
# TLDs count ("Hello.To", "Ok.Come" are typos, not links).
_GROUP_URL_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|tg://\S+"
    r"|(?<![\w@.-])(?:(?=[a-z0-9-]*[a-z])[a-z0-9-]+\.)+"
    r"(?:kg|ru|com|org|net|xyz|info|biz|link|online|site|club)\b(?:/\S*)?)",
    re.IGNORECASE,
)

_HARD_PATTERNS = [re.compile(p, re.IGNORECASE) for p in HARD_PHRASES]
_soft_words: list[str] = list(DEFAULT_SOFT_WORDS)
_soft_patterns: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in DEFAULT_SOFT_WORDS]


@dataclass(frozen=True)
class Verdict:
    kind: str  # "hard" | "soft"
    rule: str  # "forwarded_link_buttons" | "adult_phrase" | "soft_word" | "disallowed_link"
    matched: str


def get_soft_words() -> list[str]:
    return list(_soft_words)


def _set_soft_words(words: list[str]) -> None:
    global _soft_words, _soft_patterns
    patterns = []
    for word in words:
        try:
            patterns.append(re.compile(word, re.IGNORECASE))
        except re.error:
            logger.warning("Skipping invalid soft-word pattern: %r", word)
    _soft_words = list(words)
    _soft_patterns = patterns


async def reload_soft_words() -> None:
    """Rebuild the live list from app_settings (defaults if never edited)."""
    raw = await get_setting(SOFT_WORDS_KEY)
    words = list(DEFAULT_SOFT_WORDS)
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list) and all(isinstance(w, str) for w in loaded):
                words = loaded
        except ValueError:
            logger.warning("group_soft_words setting is not valid JSON; using defaults")
    _set_soft_words(words)


async def save_soft_words(words: list[str]) -> None:
    await set_setting(SOFT_WORDS_KEY, json.dumps(words, ensure_ascii=False))
    _set_soft_words(words)


def find_group_link(text: str, urls: Sequence[str] = ()) -> str | None:
    """First link that is not a contact or map link: found in the text, or among
    `urls` (hyperlink targets and button URLs, which the text never shows)."""
    for url in [*_GROUP_URL_PATTERN.findall(text or ""), *urls]:
        if not any(part in url.lower() for part in _ALLOWED_LINK_PARTS):
            return url
    return None


def classify(
    text: str | None,
    *,
    forwarded: bool = False,
    buttons: Sequence[tuple[str, str | None]] = (),
    urls: Sequence[str] = (),
) -> Verdict | None:
    """Judge one message. `buttons` are the inline keyboard's (label, url) pairs:
    porn spam usually carries its giveaway words there, not in the text. `urls`
    are hidden hyperlink targets (text_link entities)."""
    text = text or ""

    if forwarded:
        for _label, url in buttons:
            if url:
                return Verdict("hard", "forwarded_link_buttons", url)

    haystack = "\n".join([text, *(label for label, _url in buttons)])

    for pattern in _HARD_PATTERNS:
        m = pattern.search(haystack)
        if m:
            return Verdict("hard", "adult_phrase", m.group(0))

    for pattern in _soft_patterns:
        m = pattern.search(haystack)
        if m:
            return Verdict("soft", "soft_word", m.group(0))

    link = find_group_link(text, [*urls, *(url for _label, url in buttons if url)])
    if link is not None:
        return Verdict("soft", "disallowed_link", link)

    return None
