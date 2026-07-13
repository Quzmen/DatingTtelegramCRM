"""
Data-access layer. All direct DB queries live here so routers stay
thin and the query logic is reusable / testable in one place.
"""
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from . import models, schemas

ATTENTION_THRESHOLD_DAYS = 7


# ---------------------------------------------------------------
# Telegram StringSession (см. models.TelegramSettings) — одна строка
# на всё приложение (один Telegram-аккаунт на CRM).
# ---------------------------------------------------------------

def get_telegram_session_string(db: Session) -> Optional[str]:
    row = db.query(models.TelegramSettings).order_by(models.TelegramSettings.id).first()
    return row.session_string if row else None


def save_telegram_session_string(db: Session, session_string: str) -> None:
    row = db.query(models.TelegramSettings).order_by(models.TelegramSettings.id).first()
    if row:
        row.session_string = session_string
    else:
        row = models.TelegramSettings(session_string=session_string)
        db.add(row)
    db.commit()


def clear_telegram_session_string(db: Session) -> None:
    db.query(models.TelegramSettings).delete()
    db.commit()


def _get_or_create_tags(db: Session, names: List[str]) -> List[models.Tag]:
    tags = []
    for raw in names:
        clean = raw.strip().lstrip("#")
        if not clean:
            continue
        tag = db.query(models.Tag).filter(models.Tag.name == clean).first()
        if not tag:
            tag = models.Tag(name=clean)
            db.add(tag)
            db.flush()
        tags.append(tag)
    return tags


def create_contact(db: Session, data: schemas.ContactCreate) -> models.Contact:
    payload = data.model_dump(exclude={"tags"})
    contact = models.Contact(**payload)
    if data.tags:
        contact.tags = _get_or_create_tags(db, data.tags)
    db.add(contact)
    db.commit()
    db.refresh(contact)

    # seed the history with a "contact created" event
    add_interaction(
        db, contact.id,
        schemas.InteractionCreate(note="Контакт добавлен в CRM", event_type="status_change"),
    )
    return contact


def get_contact(db: Session, contact_id: int) -> Optional[models.Contact]:
    return (
        db.query(models.Contact)
        .options(joinedload(models.Contact.tags), joinedload(models.Contact.interactions))
        .filter(models.Contact.id == contact_id)
        .first()
    )


def get_contact_by_telegram_id(db: Session, telegram_id: int) -> Optional[models.Contact]:
    return (
        db.query(models.Contact)
        .options(joinedload(models.Contact.tags), joinedload(models.Contact.interactions))
        .filter(models.Contact.telegram_id == telegram_id)
        .first()
    )


def list_contacts(
    db: Session,
    search: Optional[str] = None,
    status: Optional[models.ContactStatus] = None,
    tag: Optional[str] = None,
    min_interest: Optional[int] = None,
    max_interest: Optional[int] = None,
    sort: str = "-updated_at",
) -> List[models.Contact]:
    query = db.query(models.Contact).options(joinedload(models.Contact.tags))

    if search:
        like = f"%{search}%"
        query = query.filter(or_(models.Contact.name.ilike(like), models.Contact.username.ilike(like)))

    if status:
        query = query.filter(models.Contact.status == status)

    if tag:
        query = query.join(models.Contact.tags).filter(models.Tag.name == tag)

    if min_interest is not None:
        query = query.filter(models.Contact.interest_level >= min_interest)
    if max_interest is not None:
        query = query.filter(models.Contact.interest_level <= max_interest)

    sort_field = sort.lstrip("-")
    column = getattr(models.Contact, sort_field, models.Contact.updated_at)
    query = query.order_by(column.desc() if sort.startswith("-") else column.asc())

    return query.all()


def update_contact(db: Session, contact: models.Contact, data: schemas.ContactUpdate) -> models.Contact:
    payload = data.model_dump(exclude_unset=True, exclude={"tags"})

    old_status = contact.status
    for field, value in payload.items():
        setattr(contact, field, value)

    if data.tags is not None:
        contact.tags = _get_or_create_tags(db, data.tags)

    db.commit()
    db.refresh(contact)

    if "status" in payload and payload["status"] != old_status:
        label = models.STATUS_LABELS.get(models.ContactStatus(payload["status"]), payload["status"])
        add_interaction(
            db, contact.id,
            schemas.InteractionCreate(note=f"Статус изменён на «{label}»", event_type="status_change"),
        )

    return contact


def update_status(db: Session, contact: models.Contact, status: models.ContactStatus) -> models.Contact:
    return update_contact(db, contact, schemas.ContactUpdate(status=status))


def delete_contact(db: Session, contact: models.Contact) -> None:
    db.delete(contact)
    db.commit()


def add_interaction(db: Session, contact_id: int, data: schemas.InteractionCreate) -> models.Interaction:
    interaction = models.Interaction(
        contact_id=contact_id,
        note=data.note,
        event_type=data.event_type,
        occurred_at=data.occurred_at or datetime.utcnow(),
    )
    db.add(interaction)

    contact = db.query(models.Contact).filter(models.Contact.id == contact_id).first()
    if contact and data.event_type not in ("status_change", "ai_analysis"):
        contact.last_contact_at = interaction.occurred_at

    db.commit()
    db.refresh(interaction)
    return interaction


def delete_interaction(db: Session, interaction: models.Interaction) -> None:
    db.delete(interaction)
    db.commit()


def get_all_tags(db: Session) -> List[models.Tag]:
    return db.query(models.Tag).order_by(models.Tag.name).all()


def import_telegram_contacts(
    db: Session, tg_contacts: List[dict], default_status: models.ContactStatus, tags: List[str]
) -> schemas.TelegramImportResultOut:
    """Создаёт CRM-контакты из выбранных контактов Telegram-аккаунта.

    Контакты с telegram_id, которые уже есть в базе, пропускаются --
    повторный импорт безопасен и не плодит дубликаты.
    """
    imported = 0
    skipped = 0
    for tg in tg_contacts:
        exists = (
            db.query(models.Contact)
            .filter(models.Contact.telegram_id == tg["telegram_id"])
            .first()
        )
        if exists:
            skipped += 1
            continue

        contact = models.Contact(
            name=tg["name"],
            username=tg.get("username"),
            telegram_id=tg["telegram_id"],
            source="Импорт из Telegram",
            status=default_status,
        )
        if tags:
            contact.tags = _get_or_create_tags(db, tags)
        db.add(contact)
        db.flush()
        imported += 1

        add_interaction(
            db, contact.id,
            schemas.InteractionCreate(note="Контакт импортирован из Telegram", event_type="status_change"),
        )

    db.commit()
    return schemas.TelegramImportResultOut(imported=imported, skipped=skipped)


def contacts_needing_attention(db: Session) -> List[models.Contact]:
    threshold = datetime.utcnow() - timedelta(days=ATTENTION_THRESHOLD_DAYS)
    return (
        db.query(models.Contact)
        .filter(models.Contact.status != models.ContactStatus.ARCHIVE)
        .filter(
            or_(
                models.Contact.last_contact_at.is_(None),
                models.Contact.last_contact_at < threshold,
            )
        )
        .order_by(models.Contact.last_contact_at.asc().nullsfirst())
        .all()
    )


# ---------- Contact Intelligence (Этап 9) ----------

def save_analysis(db: Session, contact: models.Contact, result: dict) -> models.Contact:
    contact.interest_score = result["interest_score"]
    contact.interest_category = result["interest_category"]
    contact.suggested_status = result["suggested_status"]
    contact.next_action = result["next_action"]
    contact.ai_summary = result["ai_summary"]
    contact.suggested_reply = result.get("suggested_reply")
    contact.ai_source = result.get("ai_source", "local")
    trend = result.get("trend") or {}
    contact.trend_direction = trend.get("direction")
    contact.trend_label = trend.get("label")
    contact.trend_delta = trend.get("delta")
    contact.analyzed_at = datetime.utcnow()
    db.commit()
    db.refresh(contact)
    return contact


def save_deep_report(db: Session, contact: models.Contact, result: dict) -> models.Contact:
    """Сохраняет результат ai_gemini.generate_deep_report() одним JSON-блобом
    (см. models.Contact.deep_report_json/deep_report property)."""
    import json

    contact.deep_report_json = json.dumps(result, ensure_ascii=False)
    contact.deep_report_at = datetime.utcnow()
    db.commit()
    db.refresh(contact)
    return contact


def apply_suggested_status(db: Session, contact: models.Contact) -> models.Contact:
    if not contact.suggested_status or contact.suggested_status == contact.status:
        return contact
    return update_status(db, contact, contact.suggested_status)


_EVENT_TITLES = {
    "note": "Заметка",
    "message": "Переписка",
    "meeting": "Встреча",
    "status_change": "Изменение статуса",
    "ai_analysis": "AI-анализ",
}


def get_timeline(db: Session, contact: models.Contact) -> List[dict]:
    events = [{
        "id": f"created-{contact.id}",
        "kind": "created",
        "occurred_at": contact.created_at,
        "title": "Контакт добавлен в CRM",
        "note": contact.source or "",
    }]
    for i in contact.interactions:
        events.append({
            "id": f"interaction-{i.id}",
            "kind": i.event_type,
            "occurred_at": i.occurred_at,
            "title": _EVENT_TITLES.get(i.event_type, "Запись"),
            "note": i.note,
        })
    events.sort(key=lambda e: e["occurred_at"], reverse=True)
    return events


def get_reminders(db: Session) -> List[dict]:
    """Follow-up напоминания: контакты, которые давно не отвечали, но
    не в архиве. Используется список contacts_needing_attention как
    основа, поверх него формируется человекочитаемый текст."""
    reminders = []
    for c in contacts_needing_attention(db):
        days = (datetime.utcnow() - c.last_contact_at).days if c.last_contact_at else None
        if days is None:
            text = f"{c.name}: переписки ещё не было — напишите первым."
        else:
            tone = "разговор был позитивным" if (c.interest_score or 0) >= 51 else "стоит уточнить, актуально ли общение"
            text = f"{c.name} не отвечал(а) {days} дн. Последний {tone}."
        reminders.append({
            "contact_id": c.id,
            "name": c.name,
            "photo_url": c.photo_url,
            "status": c.status,
            "status_label": models.STATUS_LABELS.get(c.status, c.status),
            "days_since_contact": days,
            "text": text,
        })
    return reminders


def get_dashboard(db: Session) -> schemas.DashboardOut:
    total = db.query(models.Contact).count()

    week_ago = datetime.utcnow() - timedelta(days=7)
    new_this_week = db.query(models.Contact).filter(models.Contact.created_at >= week_ago).count()

    active_statuses = [
        models.ContactStatus.WARM,
        models.ContactStatus.IN_PROGRESS,
        models.ContactStatus.MEETING_SCHEDULED,
    ]
    active_dialogues = (
        db.query(models.Contact).filter(models.Contact.status.in_(active_statuses)).count()
    )

    needs_attention = len(contacts_needing_attention(db))

    by_status = []
    for status in models.STATUS_ORDER:
        count = db.query(models.Contact).filter(models.Contact.status == status).count()
        by_status.append(
            schemas.StatusCount(status=status, label=models.STATUS_LABELS[status], count=count)
        )

    return schemas.DashboardOut(
        total_contacts=total,
        new_this_week=new_this_week,
        active_dialogues=active_dialogues,
        needs_attention=needs_attention,
        by_status=by_status,
    )
