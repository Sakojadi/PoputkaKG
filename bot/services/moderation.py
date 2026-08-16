import re
import io
import base64
import asyncio
import logging
import aiohttp
from typing import Tuple, Optional
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None

from bot.config import config

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(
    r'(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|tg://\S+|[a-zA-Z0-9_-]+\.(?:kg|ru|com|org|net|io|me|xyz|info|biz|co|app|cc|to|ly|link|online|site|club|top|pro)(?:/\S*)?)',
    re.IGNORECASE
)

MENTION_PATTERN = re.compile(r'(@[a-zA-Z0-9_]{4,})', re.IGNORECASE)

BANNED_WORDS = [
    r'секс', r'интим', r'эскорт', r'проститутк', r'шлюх', r'порно', r'член', r'минет', r'куни',
    r'трах', r'вирт', r'эротик', r'массаж\s+с\s+окончанием', r'боди\s*массаж', r'кыздар\s+керек',
    r'кыз\s+керек', r'жатакана', r'sex', r'intim', r'porn', r'xxx', r'escort', r'nude', r'onlyfans',
    r'prostitut', r'shlyuh', r'kyzdar\s+kerek', r'kyz\s+kerek',
    
    r'\bхуй', r'\bхуе', r'\bхуя', r'\bпизд', r'\bебат', r'\bебан', r'\bебал', r'\bбля', r'\bблят',
    r'\bсука', r'\bсучк', r'\bмудак', r'\bгандон', r'\bсике', r'\bсигейин', r'\bкоток', r'\bкотогум',
    r'\bам\b', r'\bамын', r'\bэненди', r'\bэнеңди', r'\bжалеп', r'\bдалбан',
    r'\bhuy', r'\bpizd', r'\bebat', r'\bblyat', r'\bsuka', r'\bkotok', r'\bsikeyin', r'\bfuck', r'\bbitch',
    
    r'нарко', r'меф', r'мефедрон', r'соль', r'соли', r'закладк', r'бошк', r'шишк', r'трав',
    r'гашиш', r'спайс', r'гидра', r'hydra', r'weed', r'drugs', r'mef', r'soli', r'zakladk',
    
    r'1xbet', r'казино', r'casino', r'ставка', r'ставк', r'пассивный\s*доход', r'легкий\s*заработок',
    r'stavk', r'zarabotok'
]

BANNED_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in BANNED_WORDS]

def moderate_text(text: str) -> Tuple[bool, str]:
    if not text:
        return True, ""

    if URL_PATTERN.search(text):
        return False, "links_not_allowed"

    if MENTION_PATTERN.search(text):
        return False, "links_not_allowed"

    for pattern in BANNED_PATTERNS:
        if pattern.search(text):
            return False, "banned_words_detected"

    return True, ""

def _extract_text_ocr(image_bytes: bytes) -> str:
    if pytesseract is None:
        return ""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.thumbnail((1200, 1200))
        return pytesseract.image_to_string(image)
    except Exception as e:
        logger.debug(f"OCR not available or failed: {e}")
        return ""

async def moderate_image_ocr(image_bytes: bytes) -> Tuple[bool, str]:
    try:
        extracted_text = await asyncio.wait_for(
            asyncio.to_thread(_extract_text_ocr, image_bytes),
            timeout=2.0
        )
        if extracted_text:
            return moderate_text(extracted_text)
    except Exception:
        pass
    return True, ""

async def moderate_image_openai(image_bytes: bytes) -> Tuple[bool, str]:
    if not config.openai_api_key:
        return True, ""

    try:
        b64_image = base64.b64encode(image_bytes).decode('utf-8')
        payload = {
            "model": "omni-moderation-latest",
            "input": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64_image}"
                    }
                }
            ]
        }
        headers = {
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/moderations",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=2.5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results and results[0].get("flagged"):
                        return False, "banned_words_detected"
    except Exception as e:
        logger.debug(f"OpenAI Moderation skipped or timed out: {e}")

    return True, ""

async def moderate_full_content(text: Optional[str], image_bytes: Optional[bytes]) -> Tuple[bool, str]:
    if text:
        valid, reason = moderate_text(text)
        if not valid:
            return False, reason

    if image_bytes:
        ocr_task = moderate_image_ocr(image_bytes)
        ai_task = moderate_image_openai(image_bytes)
        
        results = await asyncio.gather(ocr_task, ai_task, return_exceptions=True)
        for res in results:
            if isinstance(res, tuple) and not res[0]:
                return res

    return True, ""
