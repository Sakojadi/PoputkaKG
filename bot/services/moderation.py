import asyncio
import base64
import io
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None

from bot.config import config
from bot.services.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

BANNED_WORDS_KEY = "banned_words"

URL_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|tg://\S+|[a-zA-Z0-9_-]+\.(?:kg|ru|com|org|net|io|me|xyz|info|biz|co|app|cc|to|ly|link|online|site|club|top|pro)(?:/\S*)?)",
    re.IGNORECASE,
)

DEFAULT_BANNED_WORDS = [
    "секс",
    "интим",
    "эскорт",
    "проститутк",
    "шлюх",
    "порно",
    "член",
    "минет",
    "куни",
    "трах",
    "вирт",
    "эротик",
    "массаж\\s+с\\s+окончанием",
    "боди\\s*массаж",
    "кыздар\\s+керек",
    "кыз\\s+керек",
    "жатакана",
    "sex",
    "intim",
    "porn",
    "xxx",
    "escort",
    "nude",
    "onlyfans",
    "prostitut",
    "shlyuh",
    "kyzdar\\s+kerek",
    "kyz\\s+kerek",
    "\\bхуй",
    "\\bхуе",
    "\\bхуя",
    "\\bпизд",
    "\\bебат",
    "\\bебан",
    "\\bебал",
    "\\bбля",
    "\\bблят",
    "\\bсука",
    "\\bсучк",
    "\\bмудак",
    "\\bгандон",
    "\\bсике",
    "\\bсигейин",
    "\\bкоток",
    "\\bкотогум",
    "\\bам\\b",
    "\\bамын",
    "\\bэненди",
    "\\bэнеңди",
    "\\bжалеп",
    "\\bдалбан",
    "\\bhuy",
    "\\bpizd",
    "\\bebat",
    "\\bblyat",
    "\\bsuka",
    "\\bkotok",
    "\\bsikeyin",
    "\\bfuck",
    "\\bbitch",
    "нарко",
    "меф",
    "мефедрон",
    "соль",
    "соли",
    "закладк",
    "бошк",
    "шишк",
    "трав",
    "гашиш",
    "спайс",
    "гидра",
    "hydra",
    "weed",
    "drugs",
    "mef",
    "soli",
    "zakladk",
    "1xbet",
    "казино",
    "casino",
    "ставка",
    "ставк",
    "пассивный\\s*доход",
    "легкий\\s*заработок",
    "stavk",
    "zarabotok",
    "пидор",
    "пидорас",
    "пидар",
    "пидрила",
    "педик",
    "уебищ",
    "уебок",
    "уебан",
    "уёбищ",
    "уёбок",
    "уёбан",
    "\\bхуи",
    "нахуй",
    "похуй",
    "дохуя",
    "охуел",
    "охует",
    "хуесос",
    "хуила",
    "пиздец",
    "распиздяй",
    "пиздобол",
    "спиздил",
    "пиздюк",
    "пиздит",
    "\\bебуч",
    "еблан",
    "долбоеб",
    "долбоёб",
    "заебал",
    "выебал",
    "выебон",
    "\\bбляд",
    "блядина",
    "блядство",
    "сучар",
    "сучий",
    "гондон",
    "мудила",
    "потаскух",
    "шалав",
    "мразь",
    "залуп",
    "дрочить",
    "дрочил",
    "отсоси",
    "отсос\\b",
    "кунилингус",
    "жопа",
    "сиськи",
    "голая",
    "голый",
    "трахат",
    "котокбаш",
    "котогуңду",
    "котогумду",
    "коток\\s*же",
    "сигем",
    "сиктим",
    "сиккен",
    "сигиш",
    "сигишип",
    "сигишейли",
    "\\bсик\\b",
    "сигип",
    "амыңды",
    "амыңа",
    "амбаш",
    "амды",
    "энеңин",
    "эненду",
    "энеңдин",
    "энендин",
    "жалептер",
    "далбаеб",
    "далбайоб",
    "далбич",
    "көтүңдү",
    "көтүңө",
    "көтүн\\b",
    "көтүнө",
    "көтүң\\b",
    "котуң",
    "котуно",
    "эмчек",
    "түндө\\s+кыз",
    "тундо\\s+кыз",
    "акчага\\s+кыз",
    "pidor",
    "pidoras",
    "pidar",
    "pidrila",
    "pedik",
    "uebish",
    "uebok",
    "ueban",
    "uyobish",
    "hui",
    "hye",
    "nahuy",
    "pohuy",
    "ohuet",
    "huila",
    "huesos",
    "pizdec",
    "pizdetz",
    "pizdyuk",
    "spizdil",
    "eban",
    "eblan",
    "dolboeb",
    "dalbaeb",
    "zaebal",
    "blyad",
    "blad",
    "blyadina",
    "blyadstvo",
    "suchka",
    "gandon",
    "gondon",
    "mudak",
    "mudila",
    "zalupa",
    "mraz",
    "kotogum",
    "kotokbash",
    "sikem",
    "siktin",
    "sigish",
    "sikken",
    "amyn",
    "amyndy",
    "enengdi",
    "fucking",
    "cunt",
    "dick\\b",
    "pussy",
    "asshole",
    "whore",
    "slut",
    "cock\\b",
    "травка",
    "героин",
    "кокаин",
    "амфетамин",
    "экстази",
    "spice",
    "hashish",
    "gashish",
]

# Live list used by moderate_text. Admins edit it from the web panel; it is
# persisted in app_settings and rebuilt by reload_banned_words().
BANNED_WORDS: list[str] = list(DEFAULT_BANNED_WORDS)
BANNED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in BANNED_WORDS
]


def _set_banned_words(words: list[str]) -> None:
    global BANNED_WORDS, BANNED_PATTERNS
    patterns = []
    for word in words:
        try:
            patterns.append(re.compile(word, re.IGNORECASE))
        except re.error:
            logger.warning("Skipping invalid banned-word pattern: %r", word)
    BANNED_WORDS = list(words)
    BANNED_PATTERNS = patterns


async def reload_banned_words() -> None:
    """Rebuild the live list from app_settings (defaults if never edited)."""
    raw = await get_setting(BANNED_WORDS_KEY)
    words = list(DEFAULT_BANNED_WORDS)
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list) and all(isinstance(w, str) for w in loaded):
                words = loaded
        except ValueError:
            logger.warning("banned_words setting is not valid JSON; using defaults")
    _set_banned_words(words)


async def save_banned_words(words: list[str]) -> None:
    await set_setting(BANNED_WORDS_KEY, json.dumps(words, ensure_ascii=False))
    _set_banned_words(words)


def find_banned_word(text: str) -> tuple[str, str] | None:
    """Return (whole word as typed, the banned part inside it), or None if clean."""
    for pattern in BANNED_PATTERNS:
        m = pattern.search(text)
        if m:
            # The pattern often matches only a stem ("гондон" inside "Озгондон");
            # widen to the whole word so the user sees what to rewrite.
            start, end = m.start(), m.end()
            while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
                start -= 1
            while end < len(text) and (text[end].isalnum() or text[end] == "_"):
                end += 1
            return text[start:end], m.group(0)
    return None



def moderate_text(text: str) -> tuple[bool, str, str, str]:
    """Returns (ok, reason, word, part): `word` is the offending word as typed,
    `part` the banned fragment inside it (both empty when unknown)."""
    if not text:
        return True, "", "", ""

    urls = URL_PATTERN.findall(text)
    if urls:
        for url in urls:
            u_lower = url.lower()
            if not any(
                allowed in u_lower
                for allowed in ["wa.me", "whatsapp.com", "t.me", "telegram.me", "tg://"]
            ):
                return False, "links_not_allowed", url, url

    found = find_banned_word(text)
    if found is not None:
        return False, "banned_words_detected", found[0], found[1]

    return True, "", "", ""


MAX_OCR_IMAGE_BYTES = 8 * 1024 * 1024

# asyncio.wait_for cancels the future, not the thread: a slow tesseract run keeps
# going and keeps the decoded image alive. A small dedicated pool bounds how many
# of those can pile up at once, instead of the default 32-worker executor.
_ocr_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ocr")


def _extract_text_ocr(image_bytes: bytes) -> str:
    if pytesseract is None:
        return ""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.thumbnail((1200, 1200))
            return pytesseract.image_to_string(image)
    except Exception as e:
        logger.debug(f"OCR not available or failed: {e}")
        return ""


async def moderate_image_ocr(image_bytes: bytes) -> tuple[bool, str, str, str]:
    if len(image_bytes) > MAX_OCR_IMAGE_BYTES:
        logger.debug("Skipping OCR: image above %s bytes", MAX_OCR_IMAGE_BYTES)
        return True, "", "", ""

    try:
        loop = asyncio.get_running_loop()
        extracted_text = await asyncio.wait_for(
            loop.run_in_executor(_ocr_executor, _extract_text_ocr, image_bytes),
            timeout=2.0,
        )
        if extracted_text:
            return moderate_text(extracted_text)
    except Exception:
        pass
    return True, "", "", ""


async def moderate_image_openai(image_bytes: bytes) -> tuple[bool, str, str, str]:
    if not config.openai_api_key:
        return True, "", "", ""

    try:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "model": "omni-moderation-latest",
            "input": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        }

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                "https://api.openai.com/v1/moderations",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15.0),
            ) as resp,
        ):
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                if results and results[0].get("flagged"):
                    return False, "banned_words_detected", "", ""
            elif resp.status == 429:
                logger.warning(
                    "OpenAI Rate Limit hit (429). Skipping AI moderation and falling back to pure OCR."
                )
                return True, "", "", ""
            else:
                err_text = await resp.text()
                logger.error(
                    f"OpenAI Moderation API error {resp.status}: {err_text}"
                )
    except Exception as e:
        logger.error(f"OpenAI Moderation skipped or timed out: {e}")

    return True, "", "", ""


async def moderate_full_content(
    text: str | None, image_bytes: bytes | None
) -> tuple[bool, str, str, str]:
    if text:
        result = moderate_text(text)
        if not result[0]:
            return result

    if image_bytes:
        ocr_task = moderate_image_ocr(image_bytes)
        ai_task = moderate_image_openai(image_bytes)

        results = await asyncio.gather(ocr_task, ai_task, return_exceptions=True)
        for res in results:
            if isinstance(res, tuple) and not res[0]:
                return res

    return True, "", "", ""
