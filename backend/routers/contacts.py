import time
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, schemas, models, analysis as ai, ai_gemini, ai_overview, config
from ..database import get_db
from ..telegram_service import telegram_service, TelegramAuthError

router = APIRouter(prefix="/api/contacts", tags=["contacts"])


def _get_or_404(db: Session, contact_id: int) -> models.Contact:
    contact = crud.get_contact(db, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Контакт не найден")
    return contact


@router.get("", response_model=List[schemas.ContactOut])
def list_contacts(
    search: Optional[str] = None,
    status: Optional[models.ContactStatus] = None,
    tag: Optional[str] = None,
    min_interest: Optional[int] = Query(None, ge=1, le=10),
    max_interest: Optional[int] = Query(None, ge=1, le=10),
    sort: str = "-updated_at",
    db: Session = Depends(get_db),
):
    return crud.list_contacts(db, search, status, tag, min_interest, max_interest, sort)


@router.post("", response_model=schemas.ContactDetailOut, status_code=201)
def create_contact(data: schemas.ContactCreate, db: Session = Depends(get_db)):
    contact = crud.create_contact(db, data)
    return crud.get_contact(db, contact.id)


@router.get("/by-telegram/{telegram_id}", response_model=schemas.ContactDetailOut)
def get_contact_by_telegram(telegram_id: int, db: Session = Depends(get_db)):
    contact = crud.get_contact_by_telegram_id(db, telegram_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Контакт не найден в CRM")
    return contact


@router.get("/{contact_id}", response_model=schemas.ContactDetailOut)
def get_contact(contact_id: int, db: Session = Depends(get_db)):
    return _get_or_404(db, contact_id)


@router.patch("/{contact_id}", response_model=schemas.ContactDetailOut)
def update_contact(contact_id: int, data: schemas.ContactUpdate, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)
    crud.update_contact(db, contact, data)
    return crud.get_contact(db, contact_id)


@router.patch("/{contact_id}/status", response_model=schemas.ContactDetailOut)
def update_status(contact_id: int, data: schemas.ContactStatusUpdate, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)
    crud.update_status(db, contact, data.status)
    return crud.get_contact(db, contact_id)


@router.delete("/{contact_id}", status_code=204)
def delete_contact(contact_id: int, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)
    crud.delete_contact(db, contact)


@router.post("/{contact_id}/interactions", response_model=schemas.InteractionOut, status_code=201)
def add_interaction(contact_id: int, data: schemas.InteractionCreate, db: Session = Depends(get_db)):
    _get_or_404(db, contact_id)
    return crud.add_interaction(db, contact_id, data)


@router.delete("/{contact_id}/interactions/{interaction_id}", status_code=204)
def delete_interaction(contact_id: int, interaction_id: int, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)
    interaction = next((i for i in contact.interactions if i.id == interaction_id), None)
    if not interaction:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    crud.delete_interaction(db, interaction)


# ---------- Contact Intelligence (Этап 9) ----------

# Простой in-memory троттлинг "живой" оценки: contact_id -> (monotonic-время, последний результат).
# Переживает только время жизни процесса — это ровно то, что нужно (не история,
# а просто "не пересчитывать чаще, чем LIVE_SCORE_MIN_INTERVAL, пока открыт диалог").
_live_score_cache: dict = {}


def _analysis_out(contact: models.Contact, result: dict) -> schemas.AnalysisOut:
    return schemas.AnalysisOut(
        contact_id=contact.id,
        interest_score=result["interest_score"],
        interest_category=result["interest_category"],
        current_status=contact.status,
        current_status_label=models.STATUS_LABELS.get(contact.status, contact.status),
        suggested_status=result["suggested_status"],
        suggested_status_label=models.STATUS_LABELS.get(result["suggested_status"], result["suggested_status"]),
        status_change_suggested=result["suggested_status"] != contact.status,
        next_action=result["next_action"],
        ai_summary=result["ai_summary"],
        suggested_reply=result.get("suggested_reply"),
        ai_source=result.get("ai_source", "local"),
        analyzed_at=datetime.utcnow(),
        messages_analyzed=result.get("messages_analyzed", 0),
        trend=result.get("trend"),
        signals=result.get("signals", {}),
    )


@router.post("/{contact_id}/analyze", response_model=schemas.AnalysisOut)
async def analyze_contact(contact_id: int, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)

    raw_messages: list = []
    if contact.telegram_id:
        try:
            raw_messages = await telegram_service.get_messages(contact.telegram_id, limit=300)
        except TelegramAuthError:
            # Telegram не подключён/не авторизован — анализируем по тому,
            # что уже есть в CRM, не роняя запрос ошибкой.
            raw_messages = []

    messages = ai.normalize_messages(raw_messages)
    result = ai.analyze(contact, messages)

    llm_result = None
    if config.AI_PROVIDER == "gemini":
        llm_result = await ai_gemini.enrich(contact, messages, result)

    if llm_result:
        result["ai_summary"] = llm_result["ai_summary"]
        result["next_action"] = llm_result["next_action"]
        result["suggested_reply"] = llm_result["suggested_reply"]
        result["ai_source"] = config.AI_PROVIDER

    crud.save_analysis(db, contact, result)

    # Оставляем след в таймлайне, чтобы было видно, когда и с каким
    # выводом запускался анализ.
    crud.add_interaction(
        db, contact.id,
        schemas.InteractionCreate(
            note=f"AI-анализ: {result['interest_category']} ({result['interest_score']}/100). {result['ai_summary']}",
            event_type="ai_analysis",
        ),
    )

    return _analysis_out(contact, result)


@router.post("/{contact_id}/live-score", response_model=schemas.LiveScoreOut)
def live_score(contact_id: int, data: schemas.LiveScoreIn, db: Session = Depends(get_db)):
    """Быстрая "живая" оценка интереса прямо во время просмотра переписки.

    В отличие от /analyze: считает ТОЛЬКО по сообщениям, которые фронтенд
    уже загрузил сам (тот же список, что рисуется в чате) — без похода в
    Telegram и без вызова LLM-провайдера, поэтому дёшево вызывать часто
    (chatview.js дёргает это в фоне вместе со своим обычным опросом новых
    сообщений). Ничего не пишет в БД и не трогает историю таймлайна — это
    просто индикатор, а не полноценный анализ.
    """
    contact = _get_or_404(db, contact_id)

    now = time.monotonic()
    cached = _live_score_cache.get(contact_id)
    if cached and now - cached[0] < config.LIVE_SCORE_MIN_INTERVAL:
        return cached[1]

    raw_messages = [m.model_dump() for m in data.messages]
    messages = ai.normalize_messages(raw_messages)
    result = ai.analyze(contact, messages)

    out = schemas.LiveScoreOut(
        contact_id=contact.id,
        interest_score=result["interest_score"],
        interest_category=result["interest_category"],
        suggested_status=result["suggested_status"],
        suggested_status_label=models.STATUS_LABELS.get(result["suggested_status"], result["suggested_status"]),
        status_change_suggested=result["suggested_status"] != contact.status,
        messages_analyzed=result.get("messages_analyzed", 0),
        trend=result.get("trend"),
    )
    _live_score_cache[contact_id] = (now, out)
    return out


def _deep_report_out(contact: models.Contact, result: dict, generated_at: datetime) -> schemas.DeepReportOut:
    return schemas.DeepReportOut(
        contact_id=contact.id,
        generated_at=generated_at,
        interest_score=result["interest_score"],
        category=result["category"],
        trend=result["trend"],
        meeting_probability=result["meeting_probability"],
        date_invite_probability=result["date_invite_probability"],
        ghost_probability=result["ghost_probability"],
        pressure_score=result["pressure_score"],
        initiative=schemas.PctSplitOut(me=result["initiative_me"], her=result["initiative_her"]),
        investment=schemas.PctSplitOut(me=result["investment_me"], her=result["investment_her"]),
        conversation_driver=schemas.PctSplitOut(me=result["driver_me"], her=result["driver_her"]),
        green_flags=result["green_flags"],
        red_flags=result["red_flags"],
        mistakes=result["mistakes"],
        improvements=result["improvements"],
        next_action=result["next_action"],
        reasoning=result["reasoning"],
    )


@router.post("/{contact_id}/deep-report", response_model=schemas.DeepReportOut)
async def generate_deep_report(contact_id: int, db: Session = Depends(get_db)):
    """Развёрнутый AI-разбор переписки (инициатива/вложенность/флирт/красные
    и зелёные флаги/ошибки/рекомендации) — только по явному запросу
    пользователя, требует настроенный Gemini. В отличие от /analyze, у
    этого отчёта нет локального аналога-заменителя: если Gemini недоступна,
    возвращаем понятную ошибку, а не молча урезанный результат.
    """
    contact = _get_or_404(db, contact_id)
    if config.AI_PROVIDER != "gemini" or not config.GEMINI_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Глубокий AI-отчёт требует настроенный Gemini (AI_PROVIDER=gemini и GEMINI_API_KEY в .env).",
        )

    raw_messages: list = []
    if contact.telegram_id:
        try:
            raw_messages = await telegram_service.get_messages(contact.telegram_id, limit=300)
        except TelegramAuthError:
            raw_messages = []

    messages = ai.normalize_messages(raw_messages)
    if not messages:
        raise HTTPException(status_code=400, detail="Недостаточно переписки для глубокого анализа.")

    local_result = ai.analyze(contact, messages)
    try:
        result = await ai_gemini.generate_deep_report(contact, messages, local_result)
    except ai_gemini.GeminiError as exc:
        raise HTTPException(status_code=502, detail=f"Gemini не ответила: {exc}")
    if result is None:
        raise HTTPException(status_code=400, detail="Недостаточно переписки для глубокого анализа.")

    crud.save_deep_report(db, contact, result)
    return _deep_report_out(contact, result, contact.deep_report_at)


@router.get("/{contact_id}/deep-report", response_model=schemas.DeepReportOut)
def get_deep_report(contact_id: int, db: Session = Depends(get_db)):
    """Возвращает последний сохранённый глубокий отчёт без пересчёта —
    для восстановления после перезагрузки страницы."""
    contact = _get_or_404(db, contact_id)
    result = contact.deep_report
    if result is None:
        raise HTTPException(status_code=404, detail="Глубокий отчёт ещё не запускался.")
    return _deep_report_out(contact, result, contact.deep_report_at)


def _overview_out(contact_id: int, result: dict, generated_at: datetime) -> schemas.AIOverviewOut:
    return schemas.AIOverviewOut(
        contact_id=contact_id,
        generated_at=generated_at,
        current_state=result["current_state"],
        key_factors=result.get("key_factors", []),
        scenarios=[schemas.AIOverviewScenarioOut(**s) for s in result.get("scenarios", [])],
        change_triggers=result.get("change_triggers", []),
        data_used=result.get("data_used", []),
        data_needed=result.get("data_needed", []),
        confidence=result.get("confidence"),
        risk_note=result.get("risk_note"),
        source=result.get("source", "gemini"),
    )


@router.post("/{contact_id}/overview", response_model=schemas.AIOverviewOut)
async def generate_overview(contact_id: int, db: Session = Depends(get_db)):
    """Строит новый снимок AI Overview: текущее состояние + дерево
    возможных сценариев (см. backend/ai_overview.py). В отличие от
    /deep-report не требует настроенного Gemini — при его отсутствии
    честно возвращает пустой сценарий с source="local", не 400/502,
    т.к. факты и события пользователя всё равно есть смысл увидеть."""
    contact = _get_or_404(db, contact_id)

    raw_messages: list = []
    if contact.telegram_id:
        try:
            raw_messages = await telegram_service.get_messages(contact.telegram_id, limit=300)
        except TelegramAuthError:
            raw_messages = []

    result = await ai_overview.build_overview(db, contact, raw_messages)
    row = crud.save_ai_overview(db, contact_id, result)
    return _overview_out(contact_id, result, row.created_at)


@router.get("/{contact_id}/overview", response_model=schemas.AIOverviewOut)
def get_overview(contact_id: int, db: Session = Depends(get_db)):
    """Возвращает последний сохранённый снимок AI Overview без пересчёта."""
    contact = _get_or_404(db, contact_id)
    row = crud.get_latest_ai_overview(db, contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="AI Overview ещё не запускался.")
    result = {
        "current_state": row.current_state,
        "key_factors": row.key_factors,
        "scenarios": row.scenarios,
        "change_triggers": row.change_triggers,
        "data_used": row.data_used,
        "data_needed": row.data_needed,
        "confidence": row.confidence,
        "risk_note": row.risk_note,
        "source": row.source,
    }
    return _overview_out(contact_id, result, row.created_at)


@router.post("/{contact_id}/apply-suggested-status", response_model=schemas.ContactDetailOut)
def apply_suggested_status(contact_id: int, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)
    crud.apply_suggested_status(db, contact)
    return crud.get_contact(db, contact_id)


@router.get("/{contact_id}/timeline", response_model=List[schemas.TimelineEventOut])
def get_timeline(contact_id: int, db: Session = Depends(get_db)):
    contact = _get_or_404(db, contact_id)
    return crud.get_timeline(db, contact)
