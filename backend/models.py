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

    last_message_id = Column(Integer, nullable=True)
    last_message_text = Column(Text, nullable=True)
    last_message_kind = Column(String(30), nullable=True)          # text/photo/voice/video_note/...
    last_message_date = Column(DateTime, nullable=True)
    last_message_out = Column(Boolean, nullable=False, default=False)

    unread_count = Column(Integer, nullable=False, default=0)
    pinned = Column(Boolean, nullable=False, default=False)

    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    contact = relationship("Contact")


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