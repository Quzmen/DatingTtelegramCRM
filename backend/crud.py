"""
Data-access layer. All direct DB queries live here so routers stay
thin and the query logic is reusable / testable in one place.
"""
from datetime import datetime, timedelta
import json
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


# ---------------------------------------------------------------
# Кэш диалогов/сообщений (Dialog/Message) — наполняется sync_service
# из событий Telethon в реальном времени. Это источник правды для
# "последнего реального события в чате", на который дальше опирается
# и Dashboard, и статус "Требуют внимания".
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Папки (сегменты) диалогов
# ---------------------------------------------------------------

def _folder_with_count(db: Session, folder: models.Folder) -> schemas.FolderOut:
    count = db.query(models.Dialog).filter(models.Dialog.folder_id == folder.id).count()
    out = schemas.FolderOut.model_validate(folder)
    out.dialog_count = count
    return out


def list_folders(db: Session) -> List[schemas.FolderOut]:
    folders = db.query(models.Folder).order_by(models.Folder.position, models.Folder.id).all()
    return [_folder_with_count(db, f) for f in folders]


def create_folder(db: Session, data: schemas.FolderCreate) -> schemas.FolderOut:
    max_position = db.query(models.Folder).count()
    folder = models.Folder(
        name=data.name.strip(),
        color=data.color or "#6C8EF5",
        icon=data.icon,
        position=max_position,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _folder_with_count(db, folder)


def get_folder(db: Session, folder_id: int) -> Optional[models.Folder]:
    return db.query(models.Folder).filter(models.Folder.id == folder_id).first()


def update_folder(db: Session, folder: models.Folder, data: schemas.FolderUpdate) -> schemas.FolderOut:
    if data.name is not None:
        folder.name = data.name.strip()
    if data.color is not None:
        folder.color = data.color
    if data.icon is not None:
        folder.icon = data.icon
    db.commit()
    db.refresh(folder)
    return _folder_with_count(db, folder)


def delete_folder(db: Session, folder: models.Folder) -> None:
    # Диалоги, лежавшие в папке, не удаляются -- просто теряют
    # привязку (folder_id -> NULL), см. ondelete="SET NULL" в models.
    db.query(models.Dialog).filter(models.Dialog.folder_id == folder.id).update({"folder_id": None})
    db.delete(folder)
    db.commit()


def reorder_folders(db: Session, ordered_ids: List[int]) -> List[schemas.FolderOut]:
    folders = {f.id: f for f in db.query(models.Folder).all()}
    for position, folder_id in enumerate(ordered_ids):
        folder = folders.get(folder_id)
        if folder is not None:
            folder.position = position
    db.commit()
    return list_folders(db)


def assign_dialogs_to_folder(db: Session, telegram_ids: List[int], folder_id: Optional[int]) -> int:
    """Перекладывает один или несколько диалогов в папку (или убирает
    из папки, если folder_id is None). Если для какого-то telegram_id
    ещё нет строки Dialog (диалог не попадал в полную/живую синхронизацию),
    создаёт её -- иначе перенос в папку "потерялся" бы молча."""
    moved = 0
    for tg_id in telegram_ids:
        dialog = get_dialog_by_telegram_id(db, tg_id)
        if dialog is None:
            dialog = models.Dialog(telegram_id=tg_id)
            db.add(dialog)
        dialog.folder_id = folder_id
        moved += 1
    db.commit()
    return moved


def get_dialog_by_telegram_id(db: Session, telegram_id: int) -> Optional[models.Dialog]:
    return db.query(models.Dialog).filter(models.Dialog.telegram_id == telegram_id).first()


def get_folder_ids_by_telegram_id(db: Session) -> dict:
    """telegram_id -> folder_id для всех диалогов, у которых есть
    папка. Один запрос вместо похода в БД на каждый диалог из
    списка, который каждый раз приходит напрямую из Telegram API
    (см. routers/telegram.list_dialogs)."""
    rows = (
        db.query(models.Dialog.telegram_id, models.Dialog.folder_id)
        .filter(models.Dialog.folder_id.isnot(None))
        .all()
    )
    return {tg_id: folder_id for tg_id, folder_id in rows}


def touch_contact_last_contact(db: Session, telegram_id: int, when: datetime) -> None:
    """Двигает Contact.last_contact_at вперёд при реальном событии в
    Telegram (входящее/исходящее сообщение). Обновляет только если
    новое событие свежее того, что уже сохранено — так поздние
    ретро-синки (full_sync) не затирают более свежие живые события."""
    contact = (
        db.query(models.Contact)
        .filter(models.Contact.telegram_id == telegram_id)
        .first()
    )
    if contact and (contact.last_contact_at is None or contact.last_contact_at < when):
        contact.last_contact_at = when
        db.commit()


def upsert_dialog(
    db: Session,
    telegram_id: int,
    *,
    last_message_id: Optional[int] = None,
    last_message_text: Optional[str] = None,
    last_message_kind: Optional[str] = None,
    last_message_date: Optional[datetime] = None,
    last_message_out: Optional[bool] = None,
    unread_count: Optional[int] = None,
    pinned: Optional[bool] = None,
) -> models.Dialog:
    """Upsert по telegram_id. Поля, не переданные явно (None), не
    трогают уже сохранённое значение — так частичные обновления
    (например, только unread_count из события прочтения) не затирают
    последний текст сообщения нулями."""
    dialog = get_dialog_by_telegram_id(db, telegram_id)
    if dialog is None:
        dialog = models.Dialog(telegram_id=telegram_id)
        db.add(dialog)

    contact = (
        db.query(models.Contact)
        .filter(models.Contact.telegram_id == telegram_id)
        .first()
    )
    if contact is not None:
        dialog.contact_id = contact.id

    # Не перетираем более свежее сообщение более старым — full_sync и
    # живые события могут прийти в любом порядке относительно друг друга.
    is_newer = (
        last_message_date is not None
        and (dialog.last_message_date is None or last_message_date >= dialog.last_message_date)
    )
    if is_newer:
        if last_message_id is not None:
            dialog.last_message_id = last_message_id
        if last_message_text is not None:
            dialog.last_message_text = last_message_text
        if last_message_kind is not None:
            dialog.last_message_kind = last_message_kind
        dialog.last_message_date = last_message_date
        if last_message_out is not None:
            dialog.last_message_out = last_message_out

    if unread_count is not None:
        dialog.unread_count = unread_count
    if pinned is not None:
        dialog.pinned = pinned

    db.commit()
    db.refresh(dialog)
    return dialog


def upsert_message(
    db: Session,
    dialog_telegram_id: int,
    message_id: int,
    *,
    text: Optional[str],
    date: datetime,
    out: bool,
    kind: str = "text",
    duration: Optional[int] = None,
    status: Optional[str] = None,
    edited: bool = False,
) -> models.Message:
    """Upsert по (dialog_telegram_id, message_id) — источник дедупликации
    (Баг №2: "возможны дубли"). Повторное событие с тем же id (например,
    Telethon иногда присылает апдейт дважды при нестабильной сети)
    обновляет существующую строку вместо создания второй."""
    msg = (
        db.query(models.Message)
        .filter(
            models.Message.dialog_telegram_id == dialog_telegram_id,
            models.Message.message_id == message_id,
        )
        .first()
    )
    if msg is None:
        msg = models.Message(dialog_telegram_id=dialog_telegram_id, message_id=message_id)
        db.add(msg)

    msg.text = text
    msg.date = date
    msg.out = out
    msg.kind = kind
    msg.duration = duration
    if status is not None:
        msg.status = status
    msg.edited = edited
    msg.deleted = False

    db.commit()
    db.refresh(msg)
    return msg


def mark_message_deleted(db: Session, dialog_telegram_id: int, message_id: int) -> None:
    msg = (
        db.query(models.Message)
        .filter(
            models.Message.dialog_telegram_id == dialog_telegram_id,
            models.Message.message_id == message_id,
        )
        .first()
    )
    if msg is not None:
        msg.deleted = True
        db.commit()


def mark_outbox_read(db: Session, dialog_telegram_id: int, max_id: int) -> None:
    """Событие Telethon events.MessageRead для исходящих: собеседник
    прочитал все наши сообщения с id <= max_id. Раньше статус
    ✓/✓✓ пересчитывался только при следующем открытии диалога
    (get_messages -> _read_outbox_max_id), теперь — сразу по событию."""
    (
        db.query(models.Message)
        .filter(
            models.Message.dialog_telegram_id == dialog_telegram_id,
            models.Message.out.is_(True),
            models.Message.message_id <= max_id,
            models.Message.status != "read",
        )
        .update({"status": "read"}, synchronize_session=False)
    )
    db.commit()


# ---------------------------------------------------------------
# Кампании массовых рассылок
# ---------------------------------------------------------------

def campaign_out(campaign: models.Campaign) -> schemas.CampaignOut:
    out = schemas.CampaignOut.model_validate(campaign)
    out.has_image = bool(campaign.image_path)
    out.folder_ids = campaign.folder_ids
    out.filters = schemas.CampaignFiltersIn(**campaign.filters) if campaign.filters else schemas.CampaignFiltersIn()
    return out


def create_campaign(db: Session, data: schemas.CampaignCreateIn) -> schemas.CampaignOut:
    status = (
        models.CampaignStatus.READY
        if data.message_text.strip() and data.folder_ids
        else models.CampaignStatus.DRAFT
    )
    campaign = models.Campaign(
        name=data.name.strip(),
        message_text=data.message_text,
        folder_ids_json=json.dumps(data.folder_ids),
        filters_json=json.dumps(data.filters.model_dump()),
        status=status,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign_out(campaign)


def list_campaigns(db: Session) -> List[schemas.CampaignOut]:
    campaigns = db.query(models.Campaign).order_by(models.Campaign.created_at.desc()).all()
    return [campaign_out(c) for c in campaigns]


def get_campaign(db: Session, campaign_id: int) -> Optional[models.Campaign]:
    return db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()


def update_campaign(db: Session, campaign: models.Campaign, data: schemas.CampaignUpdateIn) -> schemas.CampaignOut:
    if data.name is not None:
        campaign.name = data.name.strip()
    if data.message_text is not None:
        campaign.message_text = data.message_text
    if data.folder_ids is not None:
        campaign.folder_ids_json = json.dumps(data.folder_ids)
    if data.filters is not None:
        campaign.filters_json = json.dumps(data.filters.model_dump())
    if campaign.status in (models.CampaignStatus.DRAFT, models.CampaignStatus.READY):
        campaign.status = (
            models.CampaignStatus.READY
            if campaign.message_text.strip() and campaign.folder_ids
            else models.CampaignStatus.DRAFT
        )
    db.commit()
    db.refresh(campaign)
    return campaign_out(campaign)


def delete_campaign(db: Session, campaign: models.Campaign) -> None:
    db.delete(campaign)
    db.commit()


def set_campaign_status(db: Session, campaign: models.Campaign, status: "models.CampaignStatus") -> None:
    campaign.status = status
    db.commit()


def resolve_campaign_recipients(
    db: Session, folder_ids: List[int], filters: schemas.CampaignFiltersIn,
) -> tuple[List[int], int, dict]:
    """Применяет фильтры получателей (раздел ФИЛЬТРЫ ПОЛУЧАТЕЛЕЙ ТЗ) к
    диалогам из выбранных папок. Возвращает (итоговый список telegram_id,
    количество диалогов в сегментах до фильтрации, разбивку исключённых
    по причине) -- второе и третье нужны для предпросмотра.

    Честно про два фильтра, которые локальная модель данных CRM не
    поддерживает напрямую:
    - exclude_deleted: у Dialog нет флага "диалог удалён в Telegram"
      (см. models.Dialog) -- фильтр не исключает ничего, пока такой
      флаг не появится в синхронизации.
    - active_only: трактуется как "есть хотя бы одно синхронизированное
      сообщение" (last_message_date IS NOT NULL), а не как признак
      "диалог не заброшен" в каком-то ином, более точном смысле.
    """
    query = db.query(models.Dialog)
    if folder_ids:
        query = query.filter(models.Dialog.folder_id.in_(folder_ids))
    dialogs = query.options(joinedload(models.Dialog.contact)).all()
    total_in_segments = len(dialogs)

    excluded_reasons: dict = {}
    kept: List[models.Dialog] = []

    for d in dialogs:
        reason = None
        if filters.active_only and d.last_message_date is None:
            reason = "not_active"
        elif filters.not_replied_days is not None:
            cutoff = datetime.utcnow() - timedelta(days=filters.not_replied_days)
            # "не отвечал X дней": последнее сообщение в диалоге -- наше,
            # и с тех пор прошло меньше X дней -> собеседник ещё не
            # "молчал" нужный срок, исключаем из рассылки.
            if not (d.last_message_out and d.last_message_date and d.last_message_date <= cutoff):
                reason = "not_replied_days"
        if reason is None and filters.exclude_archived:
            if d.contact and d.contact.status == models.ContactStatus.ARCHIVE:
                reason = "exclude_archived"
        if reason is None and filters.crm_stage is not None:
            if not d.contact or d.contact.status != filters.crm_stage:
                reason = "crm_stage"
        if reason is None and filters.tag_ids:
            contact_tag_ids = {t.id for t in d.contact.tags} if d.contact else set()
            if not contact_tag_ids.intersection(filters.tag_ids):
                reason = "tag_ids"

        if reason is None:
            kept.append(d)
        else:
            excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1

    return [d.telegram_id for d in kept], total_in_segments, excluded_reasons


def create_campaign_log(
    db: Session, campaign_id: int, telegram_id: int, result: str, error_text: Optional[str] = None,
) -> None:
    log = models.CampaignLog(
        campaign_id=campaign_id, telegram_id=telegram_id, result=result, error_text=error_text,
    )
    db.add(log)
    db.commit()


def list_campaign_logs(db: Session, campaign_id: int, limit: int = 200) -> List[models.CampaignLog]:
    return (
        db.query(models.CampaignLog)
        .filter(models.CampaignLog.campaign_id == campaign_id)
        .order_by(models.CampaignLog.processed_at.desc())
        .limit(limit)
        .all()
    )


def list_stuck_running_campaigns(db: Session) -> List[models.Campaign]:
    """Кампании, которые остались в статусе RUNNING на диске -- то есть
    процесс упал/перезапустился посреди рассылки (см. СИНХРОНИЗАЦИЯ ТЗ,
    \"восстанавливать незавершённые кампании\"). Не резюмируются
    автоматически (чтобы не отправить сообщения повторно/непредсказуемо
    без ведома пользователя) -- переводятся в PAUSED, откуда их можно
    осознанно продолжить."""
    return db.query(models.Campaign).filter(models.Campaign.status == models.CampaignStatus.RUNNING).all()
