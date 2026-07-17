"""
Сессии CRM после входа через Telegram.

Отдельного логина/пароля в CRM нет (см. routers/auth.py) — единственный
способ входа - авторизация в Telegram (send-code/sign-in). После
успешного входа выдаётся непрозрачный токен сессии CRM в httponly
cookie; get_current_user читает его и находит пользователя.

Токен нарочно непрозрачный (secrets.token_urlsafe), а не JWT — не нужно
ничего декодировать/проверять подпись, и его легко отозвать (просто
удалить строку из user_sessions), что не сделать с обычным
самодостаточным JWT без отдельного чёрного списка.
"""
import os
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import crud, models
from .database import get_db

COOKIE_NAME = "crm_session"
SESSION_MAX_AGE_SECONDS = 180 * 24 * 60 * 60  # 180 дней — тот же порядок, что и у сессии Telethon

# На Render (публичный HTTPS) cookie обязательно Secure, иначе браузер
# её не примет с SameSite=Lax через настоящий домен. Локально (http://
# localhost) Secure-cookie браузер тоже не отдаст обратно — поэтому
# включаем только когда явно указано, что деплой боевой.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def generate_session_token() -> str:
    return secrets.token_urlsafe(48)


def create_user_session(db: Session, user_id: int) -> str:
    token = generate_session_token()
    crud.create_user_session(db, user_id, token)
    return token


async def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    """FastAPI-зависимость: текущий пользователь CRM по cookie сессии.
    401, если cookie нет, токен неизвестен, либо пользователь этой
    сессии внезапно исчез (например, был удалён вручную из БД)."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизовано, войдите через Telegram")
    user = crud.get_user_by_session_token(db, token)
    if user is None:
        raise HTTPException(status_code=401, detail="Сессия недействительна, войдите заново")
    return user


async def get_optional_user(request: Request, db: Session = Depends(get_db)) -> "models.User | None":
    """Как get_current_user, но не кидает 401 — для эндпоинтов, которым
    нужно знать, авторизован ли кто-то, не обрывая запрос (например
    /api/auth/me на фронтенде при первой загрузке страницы)."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return crud.get_user_by_session_token(db, token)
