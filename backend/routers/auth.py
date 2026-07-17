"""
Вход в CRM исключительно через Telegram — см. ТЗ "многопользовательский
режим". Отдельной регистрации/логина-пароля нет: первый успешный вход
через Telegram создаёт пользователя CRM, повторный — просто находит
существующего по telegram_id.

Пока номер телефона не подтверждён кодом, мы не знаем, какому
пользователю CRM (если он вообще уже существует) принадлежит этот
вход — поэтому send-code работает с временным Telegram-клиентом,
который живёт только в памяти этого процесса (_pending) и ничего не
пишет в БД. Сессия персистится в telegram_settings (см.
crud.save_telegram_session_string) только после успешного sign-in,
когда telegram_id уже известен.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
)

from .. import config, crud, schemas, sync_service
from ..database import get_db
from ..auth import COOKIE_NAME, COOKIE_SECURE, SESSION_MAX_AGE_SECONDS, create_user_session, get_current_user
from ..telegram_service import get_telegram_service, _with_timeout
from ..telegram_utils import _user_out

logger = logging.getLogger("telegram-crm")
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Вход в процессе: номер телефона -> {client, phone_code_hash}. Только
# в памяти этого процесса — см. модульный docstring выше. На Render с
# несколькими репликами это означает, что send-code и sign-in должны
# попасть в одну и ту же реплику; для одного текущего процесса (как и
# было раньше с единственным глобальным клиентом до многопользовательского
# режима) этого достаточно.
_pending: dict[str, dict] = {}


def _client_kwargs() -> dict:
    kwargs = {}
    if config.PROXY:
        kwargs["proxy"] = config.PROXY
    return kwargs


@router.post("/send-code")
async def send_code(data: schemas.TelegramSendCodeIn):
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH, **_client_kwargs())
    await _with_timeout(client.connect(), "подключение к Telegram")
    try:
        sent = await _with_timeout(client.send_code_request(data.phone), "отправка кода")
    except PhoneNumberInvalidError:
        await client.disconnect()
        raise HTTPException(status_code=400, detail="Некорректный номер телефона")
    except FloodWaitError as e:
        await client.disconnect()
        raise HTTPException(status_code=400, detail=f"Слишком много попыток, подождите {e.seconds} сек.")
    except Exception:
        await client.disconnect()
        raise

    old = _pending.pop(data.phone, None)
    if old is not None:
        try:
            await old["client"].disconnect()
        except Exception:
            pass
    _pending[data.phone] = {"client": client, "phone_code_hash": sent.phone_code_hash}
    return {"sent": True}


@router.post("/sign-in", response_model=schemas.TelegramStatusOut)
async def sign_in(data: schemas.TelegramSignInIn, response: Response, db: Session = Depends(get_db)):
    pending = _pending.get(data.phone)
    if not pending:
        raise HTTPException(status_code=400, detail="Сначала запросите код для этого номера")
    client: TelegramClient = pending["client"]

    if data.password:
        try:
            await _with_timeout(client.sign_in(password=data.password), "вход по паролю")
        except Exception:
            raise HTTPException(status_code=400, detail="Неверный пароль двухфакторной аутентификации")
    else:
        try:
            await _with_timeout(
                client.sign_in(phone=data.phone, code=data.code, phone_code_hash=pending["phone_code_hash"]),
                "вход по коду",
            )
        except SessionPasswordNeededError:
            return schemas.TelegramStatusOut(authorized=False, needs_password=True, user=None)
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            raise HTTPException(status_code=400, detail="Неверный или устаревший код")

    me = await _with_timeout(client.get_me(), "получение профиля")
    session_string = client.session.save()
    _pending.pop(data.phone, None)

    user_out = _user_out(me)
    user = crud.get_or_create_user_by_telegram_id(
        db, telegram_id=user_out["telegram_id"], name=user_out["name"],
        username=user_out["username"], phone=user_out["phone"] or data.phone,
    )
    crud.save_telegram_session_string(db, user.id, session_string)

    token = create_user_session(db, user.id)
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )

    # Прогреваем кэш диалогов сразу после входа, как и раньше делал
    # sign_in() в TelegramService — переиспользуем уже подключённый
    # и авторизованный клиент, вместо того чтобы коннектиться заново.
    service = get_telegram_service(user.id)
    client.crm_user_id = user.id
    service._client = client
    try:
        await sync_service.full_sync(client)
    except Exception:
        logger.exception("Не удалось синхронизировать диалоги сразу после входа")

    return schemas.TelegramStatusOut(authorized=True, needs_password=False, user=schemas.TelegramUserOut(**user_out))


@router.get("/me")
async def me(current_user=Depends(get_current_user)):
    return {
        "id": current_user.id,
        "telegram_id": current_user.telegram_id,
        "name": current_user.name,
        "username": current_user.username,
        "phone": current_user.phone,
    }


@router.post("/logout")
async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        crud.delete_user_session(db, token)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
