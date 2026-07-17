"""
Telegram Contacts CRM - FastAPI application.

Многопользовательский режим: единственный способ входа — Telegram
(см. routers/auth.py, /api/auth/*), после чего каждый запрос к
защищённым эндпоинтам проверяется зависимостью get_current_user по
cookie crm_session (см. auth.py). Отдельного Basic Auth перед всем
приложением больше нет — раньше он использовался как единственная
защита однопользовательской версии (переменная окружения APP_USERS),
но теперь у каждого пользователя CRM собственный логин через Telegram
и собственные данные, изолированные по user_id.

Runs fully locally: SQLite file on disk, static frontend served by
this same process. No external services except Telegram/Gemini.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from telethon.errors import AuthKeyUnregisteredError, FloodWaitError

from . import models
from .database import engine, run_migrations, SessionLocal
from .routers import contacts, dashboard, telegram, admin, folders, campaigns, media, ai_insights, auth as auth_router
from .telegram_service import get_telegram_service
from . import crud

logger = logging.getLogger("telegram-crm")

models.Base.metadata.create_all(bind=engine)
run_migrations()

app = FastAPI(title="Telegram Contacts CRM", version="2.0.0")

app.include_router(auth_router.router)
app.include_router(contacts.router)
app.include_router(dashboard.router)
app.include_router(telegram.router)
app.include_router(admin.router)
app.include_router(folders.router)
app.include_router(campaigns.router)
app.include_router(media.router)
app.include_router(ai_insights.router)


@app.on_event("startup")
async def _sync_telegram_on_startup() -> None:
    """Многопользовательский рестарт-синк: если у пользователя уже была
    авторизованная Telegram-сессия в прошлый раз (см.
    TelegramService._load_session_string / crud.get_telegram_session_string),
    поднимаем кэш его диалогов сразу при старте процесса, не дожидаясь
    первого запроса с фронтенда или первого нового сообщения — для
    КАЖДОГО такого пользователя по очереди, а не только для одного
    глобального аккаунта, как было в однопользовательском режиме.
    Best-effort на каждого пользователя отдельно: если для одного
    Telegram недоступен или сессия истекла, это не должно останавливать
    синхронизацию остальных и не должно ронять старт сервиса."""
    db = SessionLocal()
    try:
        user_ids = [row[0] for row in db.query(models.TelegramSettings.user_id)
                    .filter(models.TelegramSettings.user_id.isnot(None)).distinct().all()]
    finally:
        db.close()

    for user_id in user_ids:
        try:
            await get_telegram_service(user_id).sync_now()
        except Exception:
            logger.exception("Не удалось выполнить синхронизацию диалогов при старте (user_id=%s)", user_id)

    # Восстанавливать незавершённые кампании (см. СИНХРОНИЗАЦИЯ ТЗ):
    # если процесс упал/перезапустился, пока кампания была RUNNING,
    # она осталась бы в БД в этом статусе навсегда. Переводим такие
    # кампании в PAUSED -- не резюмируем автоматически, чтобы не
    # разослать сообщения повторно без ведома пользователя; из PAUSED
    # их можно осознанно продолжить через /resume, и cursor не потерян.
    # Работает сразу по всем пользователям -- list_stuck_running_campaigns
    # не фильтрует по user_id намеренно, это разовый служебный обход.
    db = SessionLocal()
    try:
        stuck = crud.list_stuck_running_campaigns(db)
        for campaign in stuck:
            campaign.status = models.CampaignStatus.PAUSED
        if stuck:
            db.commit()
            logger.info("Восстановлено %s незавершённых кампаний (переведены в паузу)", len(stuck))
    except Exception:
        logger.exception("Не удалось восстановить незавершённые кампании при старте")
    finally:
        db.close()


@app.exception_handler(FloodWaitError)
async def _handle_flood_wait(request: Request, exc: FloodWaitError):
    """Telegram сам просит подождать exc.seconds секунд, прежде чем
    делать этот же запрос повторно (обычно всплывает на эндпоинтах,
    которые опрашиваются с фронтенда каждые несколько секунд —
    /telegram/dialogs, /telegram/messages/{id}, /telegram/presence/{id}).
    Раньше это долетало до общего unhandled_exception_handler как 500
    без какой-либо информации о паузе, а фронтенд как ни в чём не
    бывало продолжал опрашивать тот же эндпоинт на следующем тике
    таймера — то есть ровно тогда, когда Telegram просит перестать
    стучаться, мы стучимся снова, и лимит только усугубляется.
    Отдаём 429 с заголовком Retry-After и тем же значением в теле,
    чтобы фронтенд (см. api.js) мог поставить именно этот запрос на
    паузу на нужное время вместо того, чтобы упасть в тост с ошибкой.
    """
    logger.warning("Telegram FloodWait на %s: подождать %sс", request.url.path, exc.seconds)
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(exc.seconds)},
        content={"detail": f"Telegram просит подождать {exc.seconds} сек.", "retry_after": exc.seconds},
    )


@app.exception_handler(AuthKeyUnregisteredError)
async def _handle_auth_key_unregistered(request: Request, exc: AuthKeyUnregisteredError):
    """Сохранённая StringSession больше не действительна на стороне
    Telegram (например, сессию завершили вручную в настройках Telegram,
    либо строка была скопирована из другого места окружения). Ловим это
    централизованно для всех /api/telegram/* эндпоинтов, чтобы вместо
    вечной 500-й пользователь увидел понятный 401 и экран повторного
    входа. Инвалидируем сессию ТОЛЬКО текущего пользователя запроса
    (по cookie crm_session), а не всех сразу -- у каждого свой
    независимый Telegram-аккаунт."""
    from .auth import get_optional_user
    from .database import SessionLocal as _SessionLocal

    db = _SessionLocal()
    try:
        user = await get_optional_user(request, db)
        if user is not None:
            await get_telegram_service(user.id).invalidate_session()
    finally:
        db.close()

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


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")


@app.get("/")
def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}
