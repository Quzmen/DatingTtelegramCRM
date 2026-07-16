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
from datetime import datetime, timedelta
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
RETRY_DELAY_SECONDS = 8
# Отдельно для 429: одного повтора часто не хватает, лимит free-тарифа
# считается за минуту целиком — пробуем ещё раз с более длинной паузой,
# прежде чем сдаваться и откатываться на локальный результат. Если
# Google в ответе прислал свой retryDelay (см. _parse_quota_error) —
# используем его вместо этих чисел, они только фолбэк на случай, если
# сервис его не прислал.
RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_RETRY_DELAYS = (8, 20)

# ---------------------------------------------------------------
# Общий (на все поды/воркеры) лимитер запросов к Gemini.
#
# Реальная причина 429 из логов: "limit: 20, model: gemini-3.5-flash"
# на метрике generate_content_free_tier_requests — это ОБЩИЙ лимит на
# весь GEMINI_API_KEY, а не на один процесс. Сервис поднят в нескольких
# репликах (в логах видно 2-3 разных IP пода) — у каждой был свой
# процессный лимитер (asyncio.Lock + метка времени в памяти), который
# ничего не знал о запросах ДРУГИХ реплик, поэтому втроём они всё равно
# легко пробивали общие 20/мин, даже когда каждая по отдельности честно
# выдерживала паузу.
#
# Раз лимит общий на ключ — лимитер тоже должен быть общим, а не
# процессным. Общее у всех реплик — только база данных, поэтому лимитер
# здесь реализован через неё: перед каждым реальным HTTP-запросом к
# Gemini пишем метку времени в gemini_call_log (см. models.py) и
# считаем, сколько таких меток набралось за последние 60 секунд СО ВСЕХ
# ПОДОВ СРАЗУ. Если близко к лимиту — ждём и проверяем снова, вместо
# того чтобы стрелять запросом, который почти наверняка словит 429.
# GEMINI_SAFE_RPM (см. config.py) берётся с запасом ниже реального
# лимита (20), чтобы пережить неточность тайминга между подами и не
# считать впритык.
# ---------------------------------------------------------------
QUOTA_WINDOW_SECONDS = 60
QUOTA_POLL_INTERVAL = 2.0
QUOTA_MAX_WAIT_SECONDS = 90  # не ждать бесконечно — после этого просто пробуем (и, если что, ловим 429 честно)


class GeminiError(Exception):
    """Причина, по которой Gemini не ответила — человекочитаемая, чтобы
    generate_deep_report() мог показать её прямо в интерфейсе (см. 502 в
    routers/contacts.py), а не заставлять лезть в терминал backend'а."""


async def _wait_for_quota_slot() -> None:
    """Блокирует выполнение (в отдельном потоке, чтобы не морозить event
    loop синхронными запросами к БД), пока в общем окне за последние 60
    секунд не появится свободное место под ещё один запрос к Gemini —
    считая запросы СО ВСЕХ ПОДОВ, а не только текущего процесса."""
    from .database import SessionLocal

    def _try_reserve_slot() -> bool:
        db = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=QUOTA_WINDOW_SECONDS)
            # Чистим старые метки — иначе таблица растёт бесконечно.
            db.query(models.GeminiCallLog).filter(models.GeminiCallLog.created_at < cutoff).delete()
            count = db.query(models.GeminiCallLog).filter(models.GeminiCallLog.created_at >= cutoff).count()
            if count >= config.GEMINI_SAFE_RPM:
                db.commit()
                return False
            db.add(models.GeminiCallLog(created_at=datetime.utcnow()))
            db.commit()
            return True
        finally:
            db.close()

    waited = 0.0
    while waited < QUOTA_MAX_WAIT_SECONDS:
        got_slot = await asyncio.to_thread(_try_reserve_slot)
        if got_slot:
            return
        logger.info("Gemini: общий лимит (%s/мин на все поды) занят, жду %sс…",
                    config.GEMINI_SAFE_RPM, QUOTA_POLL_INTERVAL)
        await asyncio.sleep(QUOTA_POLL_INTERVAL)
        waited += QUOTA_POLL_INTERVAL
    # Не дождались за отведённое время — пробуем всё равно; если лимит
    # правда занят, получим честный 429 и обработаем его как обычно.
    logger.warning("Gemini: не дождались свободного места в лимите за %sс, пробуем как есть", QUOTA_MAX_WAIT_SECONDS)


async def _call_gemini(system_prompt: str, user_content: str, *, _retry_count: int = 0) -> dict:
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

    # Гейтим КАЖДУЮ попытку, включая повторы — ретрай тоже реальный HTTP-
    # запрос, который расходует общий лимит наравне с первой попыткой.
    await _wait_for_quota_slot()

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
        quota_detail, retry_delay_hint = _parse_quota_error(response)
        if _retry_count < RATE_LIMIT_MAX_RETRIES:
            delay = retry_delay_hint or RATE_LIMIT_RETRY_DELAYS[min(_retry_count, len(RATE_LIMIT_RETRY_DELAYS) - 1)]
            logger.info("Gemini: превышен лимит запросов (429: %s), повтор через %sс (попытка %s/%s)",
                        quota_detail or "причина не распознана", delay, _retry_count + 1, RATE_LIMIT_MAX_RETRIES)
            await asyncio.sleep(delay)
            return await _call_gemini(system_prompt, user_content, _retry_count=_retry_count + 1)
        raise GeminiError(
            f"превышен лимит запросов Gemini (429): {quota_detail}" if quota_detail else
            "превышен лимит запросов Gemini (429) — причина не распознана, см. ответ API в логах"
        )
    if response.status_code == 503 and _retry_count == 0:
        logger.info("Gemini: сервис временно перегружен (503), повтор через %sс", RETRY_DELAY_SECONDS)
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        return await _call_gemini(system_prompt, user_content, _retry_count=_retry_count + 1)
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



def _parse_quota_error(response) -> tuple:
    """Google на 429 обычно присылает тело вида:
    {"error": {"message": "...", "status": "RESOURCE_EXHAUSTED",
     "details": [{"@type": ".../QuotaFailure", "violations": [
        {"quotaMetric": "...", "quotaId": "...", ...}]},
        {"@type": ".../RetryInfo", "retryDelay": "23s"}]}}

    Раньше мы это тело вообще не читали и просто предполагали "лимит в
    минуту" — из-за чего "подождите минуту" могло быть неверным советом,
    если на самом деле упёрлись в ДНЕВНОЙ лимит модели или в лимит по
    конкретной метрике, которую минутным ожиданием не обойти. Возвращает
    (человекочитаемое_описание | None, retry_delay_seconds | None)."""
    try:
        body = response.json()
        error = body.get("error", {})
    except Exception:
        return None, None

    quota_metric = None
    retry_delay = None
    for detail in error.get("details", []):
        type_ = detail.get("@type", "")
        if type_.endswith("QuotaFailure"):
            violations = detail.get("violations") or []
            if violations:
                quota_metric = violations[0].get("quotaMetric") or violations[0].get("quotaId")
        elif type_.endswith("RetryInfo"):
            raw_delay = detail.get("retryDelay", "")
            if raw_delay.endswith("s"):
                try:
                    retry_delay = float(raw_delay[:-1])
                except ValueError:
                    pass

    message = error.get("message", "").strip()
    parts = []
    if quota_metric:
        parts.append(f"метрика квоты: {quota_metric}")
    if message:
        parts.append(message)
    description = "; ".join(parts) if parts else None
    return description, retry_delay


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
