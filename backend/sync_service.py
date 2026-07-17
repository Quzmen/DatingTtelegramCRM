"""
Фундамент "живой" синхронизации CRM с Telegram.

Раньше вся работа с диалогами и сообщениями (TelegramService.list_dialogs /
get_messages) была request-driven: фронтенд опрашивал REST-эндпоинты, а
они каждый раз шли за данными напрямую в Telegram через Telethon.
Ничего не сохранялось локально, поэтому:

  - Contact.last_contact_at обновлялся только вручную (крестик "написал
    руками" во фронтенде / добавление Interaction) и был не связан с
    реальной перепиской -- отсюда баг "Требуют внимания" показывает
    диалоги, где переписка уже идёт.
  - Не было единого места, где можно было бы честно продедуплицировать
    сообщения -- дубли лечились уже на фронтенде, кустарно.
  - Статус прочтения пересчитывался только в момент открытия диалога
    (см. TelegramService._read_outbox_max_id), а не сразу, когда
    Telegram присылает событие о прочтении.

Этот модуль подписывается на события Telethon (новое сообщение, правка,
удаление, прочтение) и пишет их в локальные таблицы Dialog/Message
(backend/models.py) через crud.py -- независимо от того, открыт ли
сейчас этот диалог в UI.

МНОГОПОЛЬЗОВАТЕЛЬСКИЙ РЕЖИМ: один процесс обслуживает по клиенту
Telethon на каждого залогиненного пользователя CRM (см.
telegram_service.get_telegram_service), поэтому каждому обработчику
события нужно знать, ЧЕЙ это клиент, чтобы записать событие в Dialog/
Message правильного пользователя, а не перепутать с чужим диалогом.
Сам Telethon этого не передаёт (event несёт только сырые Telegram-id),
поэтому user_id прибит прямо к объекту клиента атрибутом
`crm_user_id` (см. TelegramService._get_client и routers/auth.sign_in)
и читается отсюда через event.client.crm_user_id.
"""
import logging
from typing import Optional

from telethon import events
from telethon.tl.types import User

from . import crud
from .database import SessionLocal
from .telegram_utils import _media_info

logger = logging.getLogger("telegram-crm.sync")


def _kind_and_duration(message) -> tuple[str, Optional[int]]:
    if not message.media:
        return "text", None
    info = _media_info(message)
    if not info:
        return "text", None
    return info["kind"], info.get("duration")


def _event_user_id(event) -> Optional[int]:
    user_id = getattr(event.client, "crm_user_id", None)
    if user_id is None:
        logger.warning(
            "Событие Telethon без crm_user_id на клиенте (chat_id=%s) -- "
            "пропускаем, чтобы не записать данные не в тот аккаунт.",
            getattr(event, "chat_id", None),
        )
    return user_id


async def _on_new_message(event) -> None:
    if not event.is_private:
        return
    user_id = _event_user_id(event)
    if user_id is None:
        return
    m = event.message
    telegram_id = event.chat_id
    kind, duration = _kind_and_duration(m)
    text = m.message or ""

    db = SessionLocal()
    try:
        crud.upsert_message(
            db, user_id, telegram_id, m.id,
            text=text, date=m.date, out=bool(m.out), kind=kind, duration=duration,
            status="sent" if m.out else None, edited=bool(m.edit_date),
        )
        crud.upsert_dialog(
            db, user_id, telegram_id,
            last_message_id=m.id, last_message_text=text, last_message_kind=kind,
            last_message_date=m.date, last_message_out=bool(m.out),
        )
        crud.touch_contact_last_contact(db, user_id, telegram_id, m.date)

        if not m.out:
            # Точный unread_count лучше всего знает сам Telegram (учитывает
            # прочтение с других устройств), но она не приходит вместе с
            # NewMessage. Инкремент здесь -- честная оценка "+1 новое", а
            # events.MessageRead (см. ниже) и следующий full_sync поправят
            # значение, если оно разошлось.
            dialog = crud.get_dialog_by_telegram_id(db, user_id, telegram_id)
            crud.upsert_dialog(db, user_id, telegram_id, unread_count=(dialog.unread_count or 0) + 1 if dialog else 1)
    except Exception:
        logger.exception("Не удалось сохранить входящее событие NewMessage (chat_id=%s)", telegram_id)
    finally:
        db.close()


async def _on_message_edited(event) -> None:
    if not event.is_private:
        return
    user_id = _event_user_id(event)
    if user_id is None:
        return
    m = event.message
    telegram_id = event.chat_id
    kind, duration = _kind_and_duration(m)
    text = m.message or ""

    db = SessionLocal()
    try:
        crud.upsert_message(
            db, user_id, telegram_id, m.id,
            text=text, date=m.date, out=bool(m.out), kind=kind, duration=duration,
            status="sent" if m.out else None, edited=True,
        )
        # upsert_dialog сам не перетрёт более свежее последнее сообщение
        # правкой старого -- см. проверку is_newer в crud.upsert_dialog.
        crud.upsert_dialog(
            db, user_id, telegram_id,
            last_message_id=m.id, last_message_text=text, last_message_kind=kind,
            last_message_date=m.date, last_message_out=bool(m.out),
        )
    except Exception:
        logger.exception("Не удалось сохранить событие MessageEdited (chat_id=%s)", telegram_id)
    finally:
        db.close()


async def _on_message_deleted(event) -> None:
    # ВАЖНО: для приватных чатов (в отличие от каналов/супергрупп)
    # Telegram-апдейт удаления (UpdateDeleteMessages) в принципе не несёт
    # информации о том, из какого диалога удалено сообщение -- только
    # список message_id. Telethon в этом случае отдаёт event.chat_id как
    # None, и однозначно определить диалог нельзя (id сообщения уникален
    # только в пределах одного чата, а не глобально, поэтому "угадывать"
    # чат небезопасно -- можно пометить удалённым чужое сообщение с тем
    # же id в другом диалоге). В этом случае запись останется в кэше как
    # есть и будет исправлена ближайшим full_sync/переоткрытием диалога.
    telegram_id = getattr(event, "chat_id", None)
    if telegram_id is None:
        return
    user_id = _event_user_id(event)
    if user_id is None:
        return
    db = SessionLocal()
    try:
        for message_id in event.deleted_ids:
            crud.mark_message_deleted(db, user_id, telegram_id, message_id)
    except Exception:
        logger.exception("Не удалось сохранить событие MessageDeleted (chat_id=%s)", telegram_id)
    finally:
        db.close()


async def _on_message_read(event) -> None:
    if not event.is_private:
        return
    user_id = _event_user_id(event)
    if user_id is None:
        return
    telegram_id = event.chat_id
    db = SessionLocal()
    try:
        if event.outbox:
            # Собеседник прочитал наши сообщения вплоть до max_id -- сразу
            # обновляем ✓ -> ✓✓, не дожидаясь следующего открытия диалога.
            crud.mark_outbox_read(db, user_id, telegram_id, event.max_id)
        if event.inbox:
            # Мы сами прочитали входящие (обычно вызвано mark_read при
            # открытии диалога) -- обнуляем счётчик непрочитанных.
            crud.upsert_dialog(db, user_id, telegram_id, unread_count=0)
    except Exception:
        logger.exception("Не удалось сохранить событие MessageRead (chat_id=%s)", telegram_id)
    finally:
        db.close()


def register_message_handlers(client) -> None:
    """Подписывает клиента на события сообщений. Вызывается один раз за
    время жизни клиента из TelegramService._register_handlers (там же
    есть защита от повторной регистрации)."""
    client.add_event_handler(_on_new_message, events.NewMessage())
    client.add_event_handler(_on_message_edited, events.MessageEdited())
    client.add_event_handler(_on_message_deleted, events.MessageDeleted())
    client.add_event_handler(_on_message_read, events.MessageRead())


async def full_sync(client, limit: int = 200) -> None:
    """Полная синхронизация кэша диалогов из Telegram: вызывается один
    раз при старте приложения на каждого уже авторизованного
    пользователя (и может быть вызвана повторно вручную). Поднимает
    Contact.last_contact_at и Dialog в консистентное состояние ещё до
    прихода первого живого события -- без этого после каждого
    перезапуска сервера кэш был бы пустым до первого нового сообщения
    в каждом диалоге."""
    user_id = getattr(client, "crm_user_id", None)
    if user_id is None:
        logger.warning("full_sync вызван для клиента без crm_user_id -- пропускаем")
        return

    dialogs = await client.get_dialogs(limit=limit)
    db = SessionLocal()
    try:
        for d in dialogs:
            entity = d.entity
            if not isinstance(entity, User) or entity.bot or entity.is_self:
                continue

            last_message = d.message
            kind, text, date, out = "text", "", None, False
            if last_message:
                date = last_message.date
                out = bool(last_message.out)
                text = last_message.message or ""
                kind, _duration = _kind_and_duration(last_message)

            crud.upsert_dialog(
                db, user_id, entity.id,
                last_message_id=last_message.id if last_message else None,
                last_message_text=text, last_message_kind=kind,
                last_message_date=date, last_message_out=out,
                unread_count=d.unread_count, pinned=d.pinned,
            )
            if date is not None:
                crud.touch_contact_last_contact(db, user_id, entity.id, date)
    except Exception:
        logger.exception("Не удалось выполнить full_sync диалогов (user_id=%s)", user_id)
    finally:
        db.close()
