"""
Pydantic schemas used for request validation and API responses.
Kept separate from the ORM models so the API contract can evolve
independently of the storage layer.
"""
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field, ConfigDict

from .models import ContactStatus, CampaignStatus, MediaKind


# ---------- Tags ----------

class TagOut(BaseModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


# ---------- Folders (сегменты диалогов) ----------

class FolderBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    color: str = Field(default="#6C8EF5", max_length=20)
    icon: Optional[str] = Field(default=None, max_length=16)


class FolderCreate(FolderBase):
    pass


class FolderUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    color: Optional[str] = Field(None, max_length=20)
    icon: Optional[str] = Field(None, max_length=16)


class FolderOut(FolderBase):
    id: int
    position: int
    dialog_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class FolderReorderIn(BaseModel):
    ordered_ids: List[int] = Field(..., min_length=1)


class FolderAssignIn(BaseModel):
    telegram_ids: List[int] = Field(..., min_length=1)
    folder_id: Optional[int] = None  # None -> убрать диалог(и) из папки


# ---------- Interactions ----------

class InteractionBase(BaseModel):
    note: str = Field(..., min_length=1)
    event_type: str = Field(default="note")
    occurred_at: Optional[datetime] = None


class InteractionCreate(InteractionBase):
    pass


class InteractionOut(InteractionBase):
    id: int
    contact_id: int
    occurred_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Contacts ----------

class ContactBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    username: Optional[str] = None
    telegram_id: Optional[int] = None
    photo_url: Optional[str] = None
    source: Optional[str] = None
    status: ContactStatus = ContactStatus.NEW
    interest_level: int = Field(default=5, ge=1, le=10)
    notes: Optional[str] = None
    next_task: Optional[str] = None
    next_task_date: Optional[datetime] = None
    last_contact_at: Optional[datetime] = None


class ContactCreate(ContactBase):
    tags: Optional[List[str]] = []


class ContactUpdate(BaseModel):
    """All fields optional -- used for PATCH-style partial updates."""
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    username: Optional[str] = None
    telegram_id: Optional[int] = None
    photo_url: Optional[str] = None
    source: Optional[str] = None
    status: Optional[ContactStatus] = None
    interest_level: Optional[int] = Field(None, ge=1, le=10)
    notes: Optional[str] = None
    next_task: Optional[str] = None
    next_task_date: Optional[datetime] = None
    last_contact_at: Optional[datetime] = None
    tags: Optional[List[str]] = None


class ContactStatusUpdate(BaseModel):
    status: ContactStatus


class TrendOut(BaseModel):
    direction: str  # "up" | "flat" | "down" | "unknown"
    label: str
    delta: Optional[float] = None


class ContactOut(ContactBase):
    id: int
    created_at: datetime
    updated_at: datetime
    tags: List[TagOut] = []

    # Contact Intelligence (Этап 9) — только для чтения, обновляется
    # исключительно через POST /contacts/{id}/analyze.
    interest_score: int = 0
    interest_category: Optional[str] = None
    suggested_status: Optional[ContactStatus] = None
    next_action: Optional[str] = None
    ai_summary: Optional[str] = None
    suggested_reply: Optional[str] = None
    ai_source: Optional[str] = None
    trend: Optional[TrendOut] = None
    analyzed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ContactDetailOut(ContactOut):
    interactions: List[InteractionOut] = []

    model_config = ConfigDict(from_attributes=True)


# ---------- Contact Intelligence (AI-анализ) ----------

class AnalysisSignals(BaseModel):
    their_initiative_ratio: Optional[float] = None
    avg_response_minutes: Optional[float] = None
    avg_message_length: Optional[float] = None
    question_ratio: Optional[float] = None
    meeting_mentions: Optional[int] = None


class AnalysisOut(BaseModel):
    contact_id: int
    interest_score: int
    interest_category: str
    current_status: ContactStatus
    current_status_label: str
    suggested_status: ContactStatus
    suggested_status_label: str
    status_change_suggested: bool
    next_action: Optional[str] = None
    ai_summary: Optional[str] = None
    suggested_reply: Optional[str] = None
    ai_source: Optional[str] = None
    analyzed_at: datetime
    messages_analyzed: int = 0
    trend: Optional[TrendOut] = None
    signals: AnalysisSignals


# Лёгкий вход/выход для "живой" оценки во время просмотра переписки
# (см. /live-score в routers/contacts.py). В отличие от AnalysisOut,
# считается только по уже загруженному на клиенте списку сообщений —
# без похода в Telegram и без вызова LLM-провайдера — и не пишется в БД.
class LiveScoreMessageIn(BaseModel):
    text: str = ""
    date: Optional[datetime] = None
    out: bool = False


class LiveScoreIn(BaseModel):
    messages: List[LiveScoreMessageIn] = Field(default_factory=list)


class LiveScoreOut(BaseModel):
    contact_id: int
    interest_score: int
    interest_category: str
    suggested_status: ContactStatus
    suggested_status_label: str
    status_change_suggested: bool
    messages_analyzed: int = 0
    trend: Optional[TrendOut] = None


# ---- Глубокий AI-отчёт (см. ai_gemini.generate_deep_report) ----
# Требует Gemini — локального аналога у этого отчёта нет (в отличие от
# AnalysisOut/LiveScoreOut, где число всегда есть за счёт local-эвристики).

class PctSplitOut(BaseModel):
    me: int
    her: int


class DeepReportOut(BaseModel):
    contact_id: int
    generated_at: datetime
    interest_score: int
    category: str
    trend: str
    meeting_probability: int
    date_invite_probability: int
    ghost_probability: int
    pressure_score: int
    initiative: PctSplitOut
    investment: PctSplitOut
    conversation_driver: PctSplitOut
    green_flags: List[str] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)
    mistakes: List[str] = Field(default_factory=list)
    improvements: List[str] = Field(default_factory=list)
    next_action: str
    reasoning: str


class TimelineEventOut(BaseModel):
    id: str
    kind: str
    occurred_at: datetime
    title: str
    note: str = ""


class ReminderOut(BaseModel):
    contact_id: int
    name: str
    photo_url: Optional[str] = None
    status: ContactStatus
    status_label: str
    days_since_contact: Optional[int] = None
    text: str


# ---------- Dashboard ----------

class StatusCount(BaseModel):
    status: ContactStatus
    label: str
    count: int


class DashboardOut(BaseModel):
    total_contacts: int
    new_this_week: int
    active_dialogues: int          # warm + in_progress + meeting_scheduled
    needs_attention: int           # no contact in > 7 days, not archived
    by_status: List[StatusCount]


# ---------- Telegram ----------

class TelegramUserOut(BaseModel):
    telegram_id: int
    name: str
    username: Optional[str] = None
    phone: Optional[str] = None


class TelegramStatusOut(BaseModel):
    authorized: bool
    needs_password: bool = False
    user: Optional[TelegramUserOut] = None


class TelegramSendCodeIn(BaseModel):
    phone: str = Field(..., min_length=5)


class TelegramSignInIn(BaseModel):
    phone: str = Field(..., min_length=5)
    code: Optional[str] = None
    password: Optional[str] = None


class TelegramContactOut(TelegramUserOut):
    already_imported: bool = False


class TelegramImportIn(BaseModel):
    telegram_ids: List[int] = Field(..., min_length=1)
    default_status: ContactStatus = ContactStatus.NEW
    tags: Optional[List[str]] = []


class TelegramImportResultOut(BaseModel):
    imported: int
    skipped: int


class TelegramReplyRefOut(BaseModel):
    id: int
    text: str = ""


class TelegramMediaOut(BaseModel):
    kind: str  # photo | video | video_note | animation | voice | audio | document | sticker
    file_name: Optional[str] = None
    size: Optional[int] = None
    mime: Optional[str] = None
    duration: Optional[float] = None


class TelegramMessageOut(BaseModel):
    id: int
    dialog_id: int
    text: str
    date: Optional[datetime] = None
    out: bool
    status: Optional[str] = None  # "sent" | "read" — только для исходящих (out=True)
    edited: bool = False
    pinned: bool = False
    reply_to: Optional[TelegramReplyRefOut] = None
    media: Optional[TelegramMediaOut] = None


class TelegramSendMessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    reply_to: Optional[int] = None


class TelegramBulkSendIn(BaseModel):
    """Минимальный вход для массовой отправки: список диалогов + текст.
    Без сегментов/фильтров/шаблонов — это делает модуль кампаний позже."""
    telegram_ids: List[int] = Field(..., min_items=1, max_items=500)
    text: str = Field(..., min_length=1, max_length=4000)


class TelegramBulkSendOut(BaseModel):
    queued: int
    delay_seconds: int



class TelegramEditMessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class TelegramForwardIn(BaseModel):
    to_telegram_id: int


class TelegramResolveIn(BaseModel):
    username: str = Field(..., min_length=1)


class TelegramDialogOut(BaseModel):
    telegram_id: int
    name: str
    username: Optional[str] = None
    phone: Optional[str] = None
    has_photo: bool = False
    last_message_text: Optional[str] = None
    last_message_kind: str = "text"
    last_message_date: Optional[datetime] = None
    last_message_out: bool = False
    unread_count: int = 0
    pinned: bool = False
    online: bool = False
    last_seen: Optional[datetime] = None
    last_seen_kind: str = "unknown"
    typing: bool = False
    folder_id: Optional[int] = None


class TelegramPresenceOut(BaseModel):
    online: bool = False
    last_seen: Optional[datetime] = None
    last_seen_kind: str = "unknown"
    typing: bool = False


# ---------- Медиатека (см. МОДУЛЬ МЕДИАТЕКИ ТЗ) ----------

class MediaFileOut(BaseModel):
    id: int
    original_name: str
    kind: MediaKind
    mime: Optional[str] = None
    size_bytes: int
    width: Optional[int] = None
    height: Optional[int] = None
    has_thumb: bool = False
    folder_id: Optional[int] = None
    send_count: int = 0
    last_sent_at: Optional[datetime] = None  # заполняется в crud.list_media_files — дата последней отправки по всем диалогам
    created_at: datetime
    updated_at: datetime
    url: str = ""        # заполняется в crud.media_file_out — прямая ссылка на файл
    thumb_url: str = ""  # заполняется в crud.media_file_out — ссылка на превью (пусто, если превью нет)

    model_config = ConfigDict(from_attributes=True)


class MediaListOut(BaseModel):
    items: List[MediaFileOut]
    total_count: int
    total_size_bytes: int


class MediaRenameIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)


# ---------- Папки медиатеки (раздел СТРУКТУРА МЕДИАТЕКИ ТЗ) ----------
# Отдельная от FolderBase/FolderOut иерархия — те описывают папки
# диалогов, эти — папки внутри встроенной галереи медиафайлов.

class MediaFolderBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    color: str = Field(default="#6C8EF5", max_length=20)
    icon: Optional[str] = Field(default=None, max_length=16)


class MediaFolderCreate(MediaFolderBase):
    pass


class MediaFolderUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    color: Optional[str] = Field(None, max_length=20)
    icon: Optional[str] = Field(None, max_length=16)


class MediaFolderOut(MediaFolderBase):
    id: int
    position: int
    file_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class MediaFolderReorderIn(BaseModel):
    ordered_ids: List[int] = Field(..., min_length=1)


class MediaMoveIn(BaseModel):
    """Массовый перенос файлов медиатеки в папку (или изъятие из
    папки, если folder_id не задан) — раздел ИНТЕРФЕЙС ТЗ: drag & drop
    и массовый выбор."""
    media_ids: List[int] = Field(..., min_length=1)
    folder_id: Optional[int] = None


class MediaBulkDeleteIn(BaseModel):
    media_ids: List[int] = Field(..., min_length=1)


class MediaUsageStatusOut(BaseModel):
    """Ответ на "уже отправлялось этому получателю?" — и для одного
    диалога в чате (раздел ПРОВЕРКА В ЧАТЕ), и построчно для
    получателей кампании (раздел ПРОВЕРКА ПРИ КАМПАНИЯХ)."""
    telegram_id: int
    sent: bool
    send_count: int = 0
    last_sent_at: Optional[datetime] = None


class MediaUsageBulkCheckIn(BaseModel):
    telegram_ids: List[int] = Field(..., min_length=1)


class MediaDialogUsageOut(BaseModel):
    """Обратная форма MediaUsageStatusOut: один диалог, много файлов —
    нужна галерее в чате, чтобы отметить "Уже отправлялось" сразу на
    всех миниатюрах, а не делать по запросу на каждую (раздел
    ПРОВЕРКА В ЧАТЕ ТЗ)."""
    media_id: int
    sent: bool
    send_count: int = 0
    last_sent_at: Optional[datetime] = None


class MediaDialogUsageCheckIn(BaseModel):
    telegram_id: int
    media_ids: List[int] = Field(..., min_length=1)


class MediaSendIn(BaseModel):
    caption: Optional[str] = None
    reply_to: Optional[int] = None


# ---------- Кампании массовых рассылок ----------

class CampaignFiltersIn(BaseModel):
    """Все поля опциональны — фильтр применяется, только если задан.
    Соответствует разделу ФИЛЬТРЫ ПОЛУЧАТЕЛЕЙ ТЗ."""
    not_replied_days: Optional[int] = Field(None, ge=0, description="не отвечал X дней")
    active_only: bool = False              # только активные диалоги (есть синхронизированная переписка)
    exclude_archived: bool = False         # исключить архив (Contact.status == ARCHIVE)
    exclude_deleted: bool = False          # исключить удалённые диалоги
    crm_stage: Optional[ContactStatus] = None  # только выбранная стадия CRM
    tag_ids: Optional[List[int]] = None        # только выбранные теги


class CampaignCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    message_text: str = Field(default="", max_length=4000)
    folder_ids: List[int] = Field(default_factory=list)
    filters: CampaignFiltersIn = Field(default_factory=CampaignFiltersIn)


class CampaignUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    message_text: Optional[str] = Field(None, max_length=4000)
    folder_ids: Optional[List[int]] = None
    filters: Optional[CampaignFiltersIn] = None


class CampaignPreviewOut(BaseModel):
    """Ответ на предпросмотр: соответствует разделу ПРЕДПРОСМОТР ТЗ."""
    folder_ids: List[int]
    total_dialogs_in_segments: int          # до фильтрации
    total_after_filters: int                # после фильтрации -- это и пойдёт в рассылку
    excluded_count: int
    excluded_reasons: dict                  # {"not_replied_days": 3, "exclude_archived": 5, ...}
    applied_filters: CampaignFiltersIn
    message_text: str
    has_image: bool
    media: Optional[MediaFileOut] = None
    media_usage: List[MediaUsageStatusOut] = Field(default_factory=list)  # раздел ПРОВЕРКА ПРИ КАМПАНИЯХ — по каждому получателю


class CampaignOut(BaseModel):
    id: int
    name: str
    status: CampaignStatus
    message_text: str
    has_image: bool = False
    media_id: Optional[int] = None
    media: Optional[MediaFileOut] = None
    folder_ids: List[int] = Field(default_factory=list)
    filters: CampaignFiltersIn = Field(default_factory=CampaignFiltersIn)

    total_selected: int = 0
    processed_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0

    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class CampaignMediaAttachIn(BaseModel):
    media_id: int


class CampaignStartIn(BaseModel):
    """Запуск возможен только после подтверждения пользователем (см.
    ПРЕДПРОСМОТР ТЗ) -- confirm должен быть явно True, иначе эндпоинт
    отклоняет запрос вместо того чтобы молча считать отсутствие
    параметра согласием."""
    confirm: bool = Field(..., description="Пользователь подтвердил предпросмотр и запуск")


class CampaignLogOut(BaseModel):
    id: int
    telegram_id: int
    processed_at: datetime
    result: str
    error_text: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)
