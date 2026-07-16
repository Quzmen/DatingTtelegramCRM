"""
ORM models.

Contact      - a person you know / are getting to know on Telegram
Interaction  - a single logged event in the history of a contact
               (message, meeting, note, status change, etc.)
Tag          - a free-form label, many-to-many with Contact
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, DateTime, Float, Boolean,
    ForeignKey, Table, Enum, UniqueConstraint, func
)
from sqlalchemy.orm import relationship

from .database import Base


class ContactStatus(str, enum.Enum):
    NEW = "new"                        # Новый
    WARM = "warm"                      # Тёплый
    IN_PROGRESS = "in_progress"        # В работе
    MEETING_SCHEDULED = "meeting_scheduled"  # Назначена встреча
    MET = "met"                        # Встречались
    ARCHIVE = "archive"                # Архив


# Human-readable labels + kanban column order live here so both the
# API and the frontend can stay in sync with a single source of truth.
STATUS_LABELS = {
    ContactStatus.NEW: "Новый",
    ContactStatus.WARM: "Тёплый",
    ContactStatus.IN_PROGRESS: "В работе",
    ContactStatus.MEETING_SCHEDULED: "Назначена встреча",
    ContactStatus.MET: "Встречались",
    ContactStatus.ARCHIVE: "Архив",
}

STATUS_ORDER = [
    ContactStatus.NEW,
    ContactStatus.WARM,
    ContactStatus.IN_PROGRESS,
    ContactStatus.MEETING_SCHEDULED,
    ContactStatus.MET,
    ContactStatus.ARCHIVE,
]


contact_tags = Table(
    "contact_tags",
    Base.metadata,
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)

    contacts = relationship("Contact", secondary=contact_tags, back_populates="tags")


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False, index=True)
    username = Column(String(100), nullable=True, index=True)
    telegram_id = Column(BigInteger, nullable=True, unique=True, index=True)  # id из аккаунта Telegram, если контакт импортирован (может превышать 2^31, поэтому BigInteger)
    photo_url = Column(String(500), nullable=True)
    source = Column(String(200), nullable=True)          # источник знакомства
    status = Column(Enum(ContactStatus), default=ContactStatus.NEW, nullable=False, index=True)
    interest_level = Column(Integer, default=5)           # 1-10
    notes = Column(Text, nullable=True)

    next_task = Column(String(300), nullable=True)
    next_task_date = Column(DateTime, nullable=True)

    last_contact_at = Column(DateTime, nullable=True)

    # ---- Этап 9: Contact Intelligence (AI-анализ диалогов) ----
    # Всё это заполняется/обновляется только эндпоинтом /analyze,
    # никогда напрямую пользователем.
    interest_score = Column(Integer, default=0, nullable=False)     # 0-100
    interest_category = Column(String(50), nullable=True)           # Холодная/Тёплая/Высокий интерес/Очень высокий интерес
    suggested_status = Column(Enum(ContactStatus), nullable=True)   # предложенный статус, требует подтверждения
    next_action = Column(String(300), nullable=True)                # AI-подсказка следующего действия
    ai_summary = Column(Text, nullable=True)                        # краткое авто-описание диалога
    suggested_reply = Column(Text, nullable=True)                   # черновик ответа (только когда AI_PROVIDER=gemini)
    ai_source = Column(String(20), nullable=True)                   # "local" | "gemini" — чем именно посчитан анализ
    trend_direction = Column(String(10), nullable=True)             # "up" | "flat" | "down" | "unknown"
    trend_label = Column(String(64), nullable=True)
    trend_delta = Column(Float, nullable=True)
    analyzed_at = Column(DateTime, nullable=True)

    # Глубокий AI-отчёт (требует Gemini, считается только по явному запросу
    # пользователя) — хранится одним JSON-блобом, а не по колонке на поле,
    # чтобы не плодить миграцию на каждое новое измерение отчёта.
    deep_report_json = Column(Text, nullable=True)
    deep_report_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    tags = relationship("Tag", secondary=contact_tags, back_populates="contacts")
    interactions = relationship(
        "Interaction", back_populates="contact",
        cascade="all, delete-orphan", order_by="desc(Interaction.occurred_at)"
    )

    @property
    def trend(self):
        """Собирает три плоские колонки (trend_direction/trend_label/
        trend_delta) в один dict для schemas.ContactOut.trend — сами
        колонки существуют раздельно только ради простой SQLite-миграции
        в database.py, наружу удобнее отдавать одним вложенным объектом."""
        if self.trend_direction is None:
            return None
        return {
            "direction": self.trend_direction,
            "label": self.trend_label,
            "delta": self.trend_delta,
        }

    @property
    def deep_report(self):
        """Разбирает JSON-блоб deep_report_json обратно в dict для
        schemas.DeepReportOut. None, если отчёт ни разу не запускался."""
        if not self.deep_report_json:
            return None
        import json
        try:
            return json.loads(self.deep_report_json)
        except (ValueError, TypeError):
            return None


class Interaction(Base):
    __tablename__ = "interactions"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    note = Column(Text, nullable=False)
    event_type = Column(String(50), default="note")  # note, message, meeting, status_change

    contact = relationship("Contact", back_populates="interactions")


# ---------------------------------------------------------------
# Personal AI Operating System (см. ai_personal_engine.py)
#
# ВАЖНО: этот слой анализирует только САМОГО пользователя — его
# заметки, задачи, договорённости, повторяющиеся привычки, — а не
# пытается профилировать или предсказывать поведение других людей.
# AIMemoryItem может ссылаться на contact_id чисто как на "с кем
# связано событие" (например "встреча с другом"), но не хранит и не
# считает никаких оценок вроде "вероятность ответа" или "интерес" —
# для конкретного собеседника это уже отдельная, не связанная с этим
# модулем функциональность (Contact Intelligence, см. ai_gemini.py).
# ---------------------------------------------------------------

class AIMemoryKind(str, enum.Enum):
    EVENT = "event"              # разовое событие (встреча, звонок)
    COMMITMENT = "commitment"    # обещание/договорённость пользователя
    PLAN = "plan"                 # план на будущее
    PREFERENCE = "preference"    # личное предпочтение пользователя
    FACT = "fact"                 # прочий важный факт


class AIMemoryItem(Base):
    """Один факт из персональной долговременной памяти пользователя,
    извлечённый AI (или добавленный вручную) из заметки/задачи/текста.
    Не хранит никаких оценок поведения других людей — только то, что
    сам пользователь сказал о себе, своих планах и договорённостях."""
    __tablename__ = "ai_memory_items"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(Enum(AIMemoryKind), default=AIMemoryKind.FACT, nullable=False, index=True)
    title = Column(String(300), nullable=False)
    details = Column(Text, nullable=True)

    # С чем/кем связано — оба необязательны и служат только для
    # отображения контекста (например "встреча" + этот контакт), не
    # для анализа собеседника.
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    related_at = Column(DateTime, nullable=True, index=True)  # дата/время самого события (не создания записи)

    importance = Column(Float, default=0.5, nullable=False)  # 0..1, для сортировки на дашборде
    source = Column(String(30), default="manual", nullable=False)  # manual | ai_extracted
    source_text = Column(Text, nullable=True)  # исходный текст, из которого извлекли (для прозрачности)

    is_done = Column(Boolean, default=False, nullable=False)  # для commitment/plan — выполнено ли

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    contact = relationship("Contact")


class AIPattern(Base):
    """Закономерность в собственном поведении пользователя, найденная
    Pattern Analyzer'ом (например "чаще завершает задачи по утрам").
    Пересчитывается периодически, старые записи заменяются новыми —
    хранится история, чтобы видеть, как менялись выводы со временем."""
    __tablename__ = "ai_patterns"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    confidence = Column(Float, default=0.5, nullable=False)  # 0..1 — насколько AI уверен в закономерности
    evidence_json = Column(Text, nullable=True)  # на каких данных основан вывод (JSON-список коротких фактов)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def evidence(self):
        return _json_load_list(self.evidence_json)


class AIDecision(Base):
    """Дерево решений пользователя: одна конкретная ситуация выбора
    ("на что потратить 3 часа") + варианты действий, для каждого из
    которых AI показывает возможные последствия ЛИЧНО ДЛЯ
    ПОЛЬЗОВАТЕЛЯ (не для того, как отреагирует другой человек).
    AI не говорит, какой вариант выбрать — только раскладывает
    последствия по каждому, решение остаётся за пользователем."""
    __tablename__ = "ai_decisions"

    id = Column(Integer, primary_key=True, index=True)
    situation = Column(Text, nullable=False)          # описание ситуации/выбора от пользователя
    options_json = Column(Text, nullable=False)        # варианты + сгенерированные последствия, см. schemas.AIDecisionOption
    chosen_option = Column(String(300), nullable=True)  # что в итоге выбрал пользователь (опционально, для истории)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def options(self):
        return _json_load_list(self.options_json)


class TelegramSettings(Base):
    """Хранит StringSession Telethon в БД (а не в файле на диске), чтобы
    авторизация переживала перезапуски Render, редеплои и пересборку
    Docker-контейнера — файловая система контейнера эфемерна, а БД
    (Supabase Postgres) — нет. Приложение работает с одним Telegram-
    аккаунтом на CRM, поэтому строк здесь всегда 0 или 1 (см.
    crud.get_telegram_session_string / save_telegram_session_string)."""
    __tablename__ = "telegram_settings"

    id = Column(Integer, primary_key=True, index=True)
    session_string = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Folder(Base):
    """Пользовательская папка (сегмент) для организации диалогов —
    например 🔥 Приоритет, ⏳ Жду ответа. Ни к чему в Telegram не
    привязана, существует только внутри CRM: один диалог (Dialog)
    может лежать максимум в одной папке за раз (см. Dialog.folder_id),
    как в нативных папках Telegram Desktop."""
    __tablename__ = "folders"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(60), nullable=False)
    color = Column(String(20), nullable=False, default="#6C8EF5")   # hex-код акцентного цвета
    icon = Column(String(16), nullable=True)                        # emoji, напр. "🔥"
    position = Column(Integer, nullable=False, default=0, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    dialogs = relationship("Dialog", back_populates="folder")


class Dialog(Base):
    """Локальное зеркало списка диалогов Telegram (левая колонка
    мессенджера). Раньше этот список каждый раз запрашивался у Telegram
    напрямую (TelegramService.list_dialogs) — здесь же лежит кэш,
    который поддерживает в актуальном состоянии sync_service, слушая
    события Telethon (новое сообщение / правка / удаление / прочтение)
    в реальном времени, а не по запросу пользователя.

    Именно на это поле опирается автоматическое обновление
    Contact.last_contact_at и, в дальнейшем, статус "Требуют внимания"
    на Dashboard — раньше last_contact_at обновлялся только вручную
    (из crud.add_interaction или кликом во фронтенде) и был не связан
    с реальной перепиской в Telegram."""
    __tablename__ = "dialogs"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)  # id собеседника
    contact_id = Column(Integer, ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    folder_id = Column(Integer, ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True)

    last_message_id = Column(Integer, nullable=True)
    last_message_text = Column(Text, nullable=True)
    last_message_kind = Column(String(30), nullable=True)          # text/photo/voice/video_note/...
    last_message_date = Column(DateTime, nullable=True)
    last_message_out = Column(Boolean, nullable=False, default=False)

    unread_count = Column(Integer, nullable=False, default=0)
    pinned = Column(Boolean, nullable=False, default=False)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    contact = relationship("Contact")
    folder = relationship("Folder", back_populates="dialogs")


class Message(Base):
    """Локальный кэш отдельных сообщений по каждому диалогу.

    dialog_telegram_id + message_id уникальны в паре — это и есть
    защита от дублей (Баг №2: "возможны дубли"), потому что sync_service
    всегда делает upsert по этой паре, а не слепой INSERT."""
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("dialog_telegram_id", "message_id", name="uq_message_dialog_msgid"),
    )

    id = Column(Integer, primary_key=True, index=True)
    dialog_telegram_id = Column(BigInteger, nullable=False, index=True)
    message_id = Column(Integer, nullable=False)  # id сообщения внутри чата в Telegram

    text = Column(Text, nullable=True)
    date = Column(DateTime, nullable=False, index=True)
    out = Column(Boolean, nullable=False, default=False)
    kind = Column(String(30), nullable=False, default="text")
    duration = Column(Integer, nullable=True)          # для voice/video_note, секунды
    status = Column(String(10), nullable=True)          # sent | read (только для исходящих)
    edited = Column(Boolean, nullable=False, default=False)
    deleted = Column(Boolean, nullable=False, default=False)

# ---------------------------------------------------------------
# Кампании массовых рассылок
# ---------------------------------------------------------------

class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"                            # Черновик
    READY = "ready"                             # Готова к запуску
    RUNNING = "running"                          # Выполняется
    PAUSED = "paused"                            # Приостановлена
    COMPLETED = "completed"                      # Завершена
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # Завершена с ошибками


CAMPAIGN_STATUS_LABELS = {
    CampaignStatus.DRAFT: "Черновик",
    CampaignStatus.READY: "Готова к запуску",
    CampaignStatus.RUNNING: "Выполняется",
    CampaignStatus.PAUSED: "Приостановлена",
    CampaignStatus.COMPLETED: "Завершена",
    CampaignStatus.COMPLETED_WITH_ERRORS: "Завершена с ошибками",
}


class Campaign(Base):
    """Кампания массовой рассылки по сегментам (папкам) диалогов.

    Список получателей не хранится как отдельная таблица строк "на
    каждого получателя свою запись перед запуском" -- вместо этого
    recipient_ids (уже посчитанный на момент запуска, после всех
    фильтров, список telegram_id) кладётся одним JSON-массивом сюда,
    а сам прогресс/результат по каждому получателю живёт в CampaignLog.
    Это и есть snapshot получателей на момент запуска, на который
    ссылается ТЗ (папки/фильтры могут поменяться уже во время рассылки,
    но кампания идёт по списку, зафиксированному в момент старта)."""
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    status = Column(Enum(CampaignStatus), default=CampaignStatus.DRAFT, nullable=False, index=True)

    message_text = Column(Text, nullable=False, default="")
    image_path = Column(String(500), nullable=True)  # legacy: путь к вложению, загруженному до появления медиатеки
    media_id = Column(Integer, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, index=True)  # вложение кампании из единой медиатеки

    media = relationship("MediaFile")

    folder_ids_json = Column(Text, nullable=True)     # выбранные сегменты: JSON-массив id папок
    filters_json = Column(Text, nullable=True)        # применённые фильтры получателей: JSON-объект

    recipient_ids_json = Column(Text, nullable=True)  # snapshot telegram_id получателей после фильтрации на момент запуска
    cursor = Column(Integer, nullable=False, default=0)  # индекс в recipient_ids, на котором остановились (пауза/рестарт)

    total_selected = Column(Integer, nullable=False, default=0)
    processed_count = Column(Integer, nullable=False, default=0)
    completed_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    logs = relationship(
        "CampaignLog", back_populates="campaign",
        cascade="all, delete-orphan", order_by="desc(CampaignLog.processed_at)",
    )

    @property
    def folder_ids(self):
        return _json_load_list(self.folder_ids_json)

    @property
    def filters(self):
        return _json_load_dict(self.filters_json)

    @property
    def recipient_ids(self):
        return _json_load_list(self.recipient_ids_json)


class CampaignLog(Base):
    """Журнал выполнения: одна строка на каждый обработанный диалог
    кампании (см. раздел ЖУРНАЛ ТЗ)."""
    __tablename__ = "campaign_logs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    result = Column(String(20), nullable=False)   # sent | skipped | error
    error_text = Column(Text, nullable=True)

    campaign = relationship("Campaign", back_populates="logs")


class MediaKind(str, enum.Enum):
    PHOTO = "photo"
    VIDEO = "video"
    GIF = "gif"
    DOCUMENT = "document"


class MediaFolder(Base):
    """Папка внутри встроенной медиатеки (раздел СТРУКТУРА МЕДИАТЕКИ
    ТЗ) — например 📷 Фото, 🔥 Избранное, 🗂 Архив. Отдельная сущность
    от Folder (та — папки/сегменты диалогов в списке чатов): один
    файл медиатеки лежит максимум в одной папке одновременно, как и
    диалог в своей папке."""
    __tablename__ = "media_folders"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(60), nullable=False)
    color = Column(String(20), nullable=False, default="#6C8EF5")
    icon = Column(String(16), nullable=True)
    position = Column(Integer, nullable=False, default=0, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    files = relationship("MediaFile", back_populates="folder")


class MediaFile(Base):
    """Единый склад медиафайлов CRM (модуль медиатеки) — одно место
    хранения фото/видео/GIF/документов, переиспользуемых в обычных
    чатах, быстрых сообщениях и кампаниях. Все части CRM работают с
    этой таблицей только через media_manager.MediaManager, чтобы не
    плодить несколько независимых реализаций отправки/хранения медиа."""
    __tablename__ = "media_files"

    id = Column(Integer, primary_key=True, index=True)
    original_name = Column(String(300), nullable=False)   # как назывался файл при загрузке / текущее отображаемое имя
    stored_name = Column(String(300), nullable=False, unique=True)  # реальное имя файла на диске (уникальное, коллизии исключены)
    kind = Column(Enum(MediaKind), nullable=False, default=MediaKind.DOCUMENT, index=True)
    mime = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    has_thumb = Column(Boolean, nullable=False, default=False)
    folder_id = Column(Integer, ForeignKey("media_folders.id", ondelete="SET NULL"), nullable=True, index=True)

    send_count = Column(Integer, nullable=False, default=0)   # сколько раз файл был отправлен всего (по всем диалогам)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    folder = relationship("MediaFolder", back_populates="files")
    usages = relationship(
        "MediaUsage", back_populates="media", cascade="all, delete-orphan",
        order_by="desc(MediaUsage.sent_at)",
    )


class MediaUsage(Base):
    """История использования медиафайла: одна строка на каждую отправку
    в конкретный диалог (может повторяться для одного и того же
    telegram_id, если файл отправлялся ему несколько раз) — на этом
    строится и отметка "Уже отправлялось" в чате, и проверка перед
    запуском кампании."""
    __tablename__ = "media_usages"

    id = Column(Integer, primary_key=True, index=True)
    media_id = Column(Integer, ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    context = Column(String(20), nullable=False, default="chat")   # chat | campaign — откуда была отправка

    # Раздел СОХРАНЕНИЕ TELEGRAM ДАННЫХ ТЗ: что именно ушло в Telegram
    # для этой отправки. telegram_message_id — id сообщения в диалоге
    # получателя. telegram_file_id — компактный слепок Telethon
    # InputPhoto/InputDocument ("photo:<id>:<access_hash>:<file_reference
    # в hex>:<dc_id>" / аналогично для document), по которому
    # send_media_from_library повторно отправляет тот же файл без
    # повторной загрузки байтов на сервер Telegram (см.
    # telegram_service.send_file/_build_input_media). sent_kind — каким
    # методом Telegram фактически ушло вложение (photo/video/gif/document).
    telegram_message_id = Column(BigInteger, nullable=True)
    telegram_file_id = Column(String(500), nullable=True)
    sent_kind = Column(String(20), nullable=True)

    media = relationship("MediaFile", back_populates="usages")


def _json_load_list(raw):
    if not raw:
        return []
    import json
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _json_load_dict(raw):
    if not raw:
        return {}
    import json
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}
