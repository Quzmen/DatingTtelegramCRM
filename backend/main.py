"""
Telegram Contacts CRM - FastAPI application.

Runs fully locally: SQLite file on disk, static frontend served by
this same process. No external services, no network calls.
"""
import base64
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from telethon.errors import AuthKeyUnregisteredError

from . import models
from .database import engine, run_migrations
from .routers import contacts, dashboard, telegram, admin, folders
from .telegram_service import telegram_service

logger = logging.getLogger("telegram-crm")

models.Base.metadata.create_all(bind=engine)
run_migrations()

app = FastAPI(title="Telegram Contacts CRM", version="1.0.0")

app.include_router(contacts.router)
app.include_router(dashboard.router)
app.include_router(telegram.router)
app.include_router(admin.router)
app.include_router(folders.router)


@app.on_event("startup")
async def _sync_telegram_on_startup() -> None:
    """Если аккаунт уже был авторизован в прошлый раз (сессия лежит в
    БД -- см. TelegramService._load_session_string), поднимаем кэш
    диалогов сразу при старте, не дожидаясь первого запроса из
    фронтенда или первого нового сообщения. Best-effort: если Telegram
    недоступен или аккаунт ещё не авторизован, просто тихо пропускаем --
    сервис при этом продолжает нормально стартовать."""
    try:
        await telegram_service.sync_now()
    except Exception:
        logger.exception("Не удалось выполнить синхронизацию диалогов при старте")


@app.exception_handler(AuthKeyUnregisteredError)
async def _handle_auth_key_unregistered(request: Request, exc: AuthKeyUnregisteredError):
    """Сохранённая StringSession больше не действительна на стороне
    Telegram (например, сессию завершили вручную в настройках Telegram,
    либо строка была скопирована из другого места окружения). Ловим это
    централизованно для всех /api/telegram/* эндпоинтов, чтобы вместо
    вечной 500-й пользователь увидел понятный 401 и экран повторного
    входа — без него именно так выглядела ошибка из отчёта:
    AuthKeyUnregisteredError после потери файла сессии при рестарте."""
    await telegram_service.invalidate_session()
    return JSONResponse(
        status_code=401,
        content={"detail": "Сессия Telegram недействительна, войдите заново"},
    )


# ---------------------------------------------------------------
# Устойчивость к обрывам соединения.
#
# Когда клиент закрывает вкладку, отменяет загрузку медиа или
# быстро переключает диалоги, соединение может оборваться прямо
# во время отправки ответа (на Windows это всплывает как
# ConnectionResetError: WinError 10054). Это ожидаемое поведение
# клиента, а не ошибка сервера — ловим её здесь, чтобы она не
# роняла обработку запроса и не засоряла логи трейсбеком.
# ---------------------------------------------------------------
@app.middleware("http")
async def guard_client_disconnects(request: Request, call_next):
    try:
        return await call_next(request)
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
        logger.info("Клиент разорвал соединение во время запроса %s: %s", request.url.path, e)
        return JSONResponse(status_code=499, content={"detail": "Соединение прервано клиентом"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        logger.info("Клиент разорвал соединение во время запроса %s: %s", request.url.path, exc)
        return JSONResponse(status_code=499, content={"detail": "Соединение прервано клиентом"})
    logger.exception("Необработанная ошибка при обработке %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера"})

# ---------------------------------------------------------------
# Basic Auth перед всем приложением.
#
# По умолчанию приложение не требует логина вообще (для запуска на
# localhost это ок). При деплое на Render сайт становится доступен
# по публичному URL, поэтому здесь добавлена простая защита: список
# логин:пароль задаётся переменной окружения APP_USERS
# (например "me:secret1,friend1:secret2,friend2:secret3" — по
# паре на каждого, кому нужен доступ к CRM). Если переменная не
# задана, доступ остаётся открытым — так проще для локальной
# разработки, но НЕ рекомендуется для публичного деплоя.
# ---------------------------------------------------------------
def _load_app_users() -> dict[str, str]:
    raw = os.environ.get("APP_USERS", "")
    users: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user, _, pwd = pair.partition(":")
        user, pwd = user.strip(), pwd.strip()
        if user and pwd:
            users[user] = pwd
    return users


_APP_USERS = _load_app_users()

if not _APP_USERS:
    logger.warning(
        "APP_USERS не задан — приложение работает БЕЗ пароля. "
        "При публичном деплое обязательно задайте переменную окружения "
        "APP_USERS (формат login:password,login2:password2)."
    )


@app.middleware("http")
async def basic_auth_guard(request: Request, call_next):
    if not _APP_USERS:
        return await call_next(request)
    if request.url.path == "/api/health":
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            user, pwd = "", ""
        expected = _APP_USERS.get(user)
        if expected is not None and secrets.compare_digest(pwd, expected):
            return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Telegram CRM"'},
        content="Требуется авторизация",
    )


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")


@app.get("/")
def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}
