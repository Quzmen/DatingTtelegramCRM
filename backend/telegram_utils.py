"""
Общие хелперы для представления сущностей Telethon (статус
пользователя, профиль, метаданные вложений) в простом сериализуемом
виде. Вынесены из telegram_service.py в отдельный модуль, чтобы их
могли использовать и telegram_service.py (живые REST-эндпоинты), и
sync_service.py (фоновые event-хендлеры) без циклического импорта
друг друга.
"""
from typing import Optional

from telethon.tl.types import (
    User,
    UserStatusOnline,
    UserStatusOffline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
)


def _status_info(status) -> dict:
    """Приводит UserStatus* Telethon к простому и сериализуемому виду."""
    if isinstance(status, UserStatusOnline):
        return {"online": True, "last_seen": None, "last_seen_kind": "online"}
    if isinstance(status, UserStatusOffline):
        return {"online": False, "last_seen": status.was_online, "last_seen_kind": "exact"}
    if isinstance(status, UserStatusRecently):
        return {"online": False, "last_seen": None, "last_seen_kind": "recently"}
    if isinstance(status, UserStatusLastWeek):
        return {"online": False, "last_seen": None, "last_seen_kind": "last_week"}
    if isinstance(status, UserStatusLastMonth):
        return {"online": False, "last_seen": None, "last_seen_kind": "last_month"}
    return {"online": False, "last_seen": None, "last_seen_kind": "unknown"}


def _user_out(user: User) -> dict:
    name = " ".join(p for p in [user.first_name, user.last_name] if p) or user.username or str(user.id)
    return {
        "telegram_id": user.id,
        "name": name,
        "username": user.username,
        "phone": getattr(user, "phone", None),
    }


def _media_info(m) -> Optional[dict]:
    """Достаёт из Telethon-сообщения тип вложения и метаданные для UI.

    Порядок проверок важен: video_note и gif — это документы с особыми
    атрибутами, которые Telethon даёт через отдельные bool-шорткаты
    (m.video_note / m.gif), и их нужно проверить ДО общих m.video /
    m.document, иначе кружки и гифки определятся как обычное видео/файл.
    """
    if m.photo:
        return {"kind": "photo", "file_name": None, "size": None, "mime": "image/jpeg", "duration": None}
    if m.video_note:
        duration = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        return {"kind": "video_note", "file_name": None, "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "video/mp4", "duration": duration}
    if m.gif:
        return {"kind": "animation", "file_name": m.file.name if m.file else None,
                "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "video/mp4", "duration": None}
    if m.voice:
        duration = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        return {"kind": "voice", "file_name": None, "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "audio/ogg", "duration": duration}
    if m.audio:
        duration = None
        title = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        try:
            title = m.file.title or m.file.name
        except Exception:
            pass
        return {"kind": "audio", "file_name": title or "Аудио", "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "audio/mpeg", "duration": duration}
    if m.video:
        duration = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        return {"kind": "video", "file_name": m.file.name if m.file else None,
                "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "video/mp4", "duration": duration}
    if m.sticker:
        mime = None
        try:
            mime = m.file.mime_type
        except Exception:
            pass
        return {"kind": "sticker", "file_name": None, "size": None, "mime": mime, "duration": None}
    if m.document:
        return {"kind": "document", "file_name": m.file.name if m.file else "Файл",
                "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "application/octet-stream", "duration": None}
    return None
