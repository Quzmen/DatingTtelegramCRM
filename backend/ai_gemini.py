"""
Contact Intelligence — LLM-слой поверх локальной эвристики (провайдер:
Google Gemini). Активен при AI_PROVIDER=gemini + заданном GEMINI_API_KEY.

Контракт простой: interest_score и статус
по-прежнему считает только локальная эвристика (analysis.py), Gemini
лишь переписывает summary/next_action и предлагает черновик ответа.
Любая ошибка (нет ключа, сеть, невалидный JSON) — тихий откат на
локальный результат, без падения /analyze.

Отдельно есть generate_deep_report() — развёрнутый разбор переписки по
многим осям (см. ai_common.DEEP_REPORT_SYSTEM_PROMPT), считается только
по явному запросу пользователя, а не автоматически с каждым /analyze.
"""
import asyncio
import logging
from typing import Dict, List, Optional

from . import ai_common, config, models

logger = logging.getLogger(__name__)

GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# После скольких секунд повторить один раз при временной ошибке (429 —
# превышен лимит запросов, 503 — сервис временно перегружен). Бесплатный
# тариф Gemini разрешает совсем немного запросов в минуту, поэтому это
# частая ситуация при анализе нескольких контактов подряд, а не баг.
RETRY_DELAY_SECONDS = 3


class GeminiError(Exception):
    """Причина, по которой Gemini не ответила — человекочитаемая, чтобы
    generate_deep_report() мог показать её прямо в интерфейсе (см. 502 в
    routers/contacts.py), а не заставлять лезть в терминал backend'а."""


async def _call_gemini(system_prompt: str, user_content: str, *, _retried: bool = False) -> dict:
    """Общий HTTP-вызов Gemini generateContent + разбор JSON-ответа.
    Либо возвращает распарсенный dict, либо кидает GeminiError с понятной
    причиной — вызывающий код сам решает, что делать (тихий откат в
    enrich(), показ причины пользователю в generate_deep_report())."""
    if not config.GEMINI_API_KEY:
        raise GeminiError("не задан GEMINI_API_KEY в .env")

    try:
        import httpx
    except ImportError:
        raise GeminiError("пакет httpx не установлен (pip install -r requirements.txt)")

    url = GEMINI_URL_TMPL.format(model=config.GEMINI_MODEL)
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": config.GEMINI_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=config.AI_LLM_TIMEOUT, proxy=config.HTTP_PROXY_URL) as client:
            response = await client.post(url, headers=headers, json=payload)
    except Exception as exc:  # DNS/прокси/таймаут соединения и т.п.
        raise GeminiError(f"сетевая ошибка ({type(exc).__name__}): {exc}") from exc

    if response.status_code == 429:
        if not _retried:
            logger.info("Gemini: превышен лимит запросов (429), повтор через %sс", RETRY_DELAY_SECONDS)
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            return await _call_gemini(system_prompt, user_content, _retried=True)
        raise GeminiError(
            "превышен лимит запросов Gemini (429) — на бесплатном тарифе это лимит в минуту, "
            "подождите немного и попробуйте снова"
        )
    if response.status_code == 503 and not _retried:
        logger.info("Gemini: сервис временно перегружен (503), повтор через %sс", RETRY_DELAY_SECONDS)
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        return await _call_gemini(system_prompt, user_content, _retried=True)
    if response.status_code == 401 or response.status_code == 403:
        raise GeminiError(f"HTTP {response.status_code} — похоже, неверный или отозванный GEMINI_API_KEY")
    if response.status_code >= 400:
        snippet = response.text[:300]
        raise GeminiError(f"HTTP {response.status_code}: {snippet}")

    try:
        data = response.json()
    except Exception as exc:
        raise GeminiError(f"не удалось разобрать ответ Gemini как JSON: {exc}") from exc

    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        # Частая причина -- ответ обрезан по safety-фильтрам Gemini, тогда
        # вместо "candidates[0].content" приходит candidates[0].finishReason.
        finish_reason = None
        try:
            finish_reason = data["candidates"][0].get("finishReason")
        except (KeyError, IndexError, TypeError):
            pass
        if finish_reason:
            raise GeminiError(f"Gemini не вернула текст (finishReason={finish_reason})")
        raise GeminiError("неожиданный формат ответа Gemini")

    try:
        return ai_common.parse_json_reply(raw_text)
    except (ValueError, TypeError) as exc:
        raise GeminiError(f"Gemini вернула невалидный JSON: {exc}") from exc



async def enrich(
    contact: models.Contact,
    messages: List[dict],
    local_result: Dict,
) -> Optional[Dict]:
    """Пытается дополнить local_result версией от Gemini. Возвращает dict
    с ключами ai_summary, next_action, suggested_reply, либо None."""
    if config.AI_PROVIDER != "gemini":
        return None
    if not messages:
        return None

    transcript = ai_common.build_transcript(messages, contact.name, config.AI_LLM_MAX_MESSAGES)
    if not transcript:
        return None

    user_content = ai_common.build_user_content(contact, local_result, transcript)
    try:
        parsed = await _call_gemini(ai_common.SYSTEM_PROMPT, user_content)
    except GeminiError as exc:
        logger.warning("Откат на локальный анализ: %s", exc)
        return None

    summary = str(parsed.get("summary") or "").strip()
    next_action = str(parsed.get("next_action") or "").strip()
    suggested_reply = str(parsed.get("suggested_reply") or "").strip()

    if not summary and not next_action:
        return None

    return {
        "ai_summary": summary or local_result.get("ai_summary"),
        "next_action": next_action or local_result.get("next_action"),
        "suggested_reply": suggested_reply,
    }


def _clamp_pct(value) -> int:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return 0


def _str_list(value, limit: int) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


async def generate_deep_report(
    contact: models.Contact,
    messages: List[dict],
    local_result: Dict,
) -> Optional[Dict]:
    """Развёрнутый AI-разбор переписки — инициатива/вложенность/флирт/
    красные и зелёные флаги/ошибки пользователя/рекомендации. В отличие
    от enrich(), у этой функции нет локального аналога-заменителя: если
    Gemini недоступна, кидает GeminiError с понятной причиной — роутер
    (см. /deep-report в contacts.py) превращает её в текст ошибки,
    который видно прямо в интерфейсе, без похода в терминал backend'а."""
    if not config.GEMINI_API_KEY:
        raise GeminiError("не задан GEMINI_API_KEY в .env")
    if not messages:
        return None

    transcript = ai_common.build_transcript(messages, contact.name, config.AI_LLM_MAX_MESSAGES)
    if not transcript:
        return None

    user_content = ai_common.build_deep_user_content(contact, local_result, transcript)
    parsed = await _call_gemini(ai_common.DEEP_REPORT_SYSTEM_PROMPT, user_content)

    initiative = parsed.get("initiative") or {}
    investment = parsed.get("investment") or {}
    driver = parsed.get("conversationDriver") or {}

    return {
        "interest_score": _clamp_pct(parsed.get("interestScore")),
        "category": str(parsed.get("category") or "").strip(),
        "trend": str(parsed.get("trend") or "").strip(),
        "meeting_probability": _clamp_pct(parsed.get("meetingProbability")),
        "date_invite_probability": _clamp_pct(parsed.get("dateInviteProbability")),
        "ghost_probability": _clamp_pct(parsed.get("ghostProbability")),
        "pressure_score": _clamp_pct(parsed.get("pressureScore")),
        "initiative_me": _clamp_pct(initiative.get("me")),
        "initiative_her": _clamp_pct(initiative.get("her")),
        "investment_me": _clamp_pct(investment.get("me")),
        "investment_her": _clamp_pct(investment.get("her")),
        "driver_me": _clamp_pct(driver.get("me")),
        "driver_her": _clamp_pct(driver.get("her")),
        "green_flags": _str_list(parsed.get("greenFlags"), 5),
        "red_flags": _str_list(parsed.get("redFlags"), 5),
        "mistakes": _str_list(parsed.get("mistakes"), 8),
        "improvements": _str_list(parsed.get("improvements"), 3),
        "next_action": str(parsed.get("nextAction") or "").strip(),
        "reasoning": str(parsed.get("reasoning") or "").strip(),
    }
