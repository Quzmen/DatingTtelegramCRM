"""
Настройки приложения.

API_ID / API_HASH — это учётные данные приложения на my.telegram.org,
они привязаны к самому приложению (а не к какому-то конкретному
аккаунту) и нужны Telethon, чтобы вообще открыть соединение с Telegram.
Кто именно авторизуется через это приложение — решается уже на этапе
входа по номеру телефона (send-code / sign-in), см. telegram_service.py.

Значения можно переопределить переменными окружения TG_API_ID /
TG_API_HASH, если проект когда-нибудь будет разворачиваться не только
локально.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Необязательный .env рядом с run.py — удобно держать здесь свой
# GEMINI_API_KEY, не прописывая его в переменные окружения ОС.
# Если python-dotenv не установлен или файла нет — просто пропускаем,
# ничего не ломаем.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

API_ID = int(os.environ.get("TG_API_ID", "39300650"))
API_HASH = os.environ.get("TG_API_HASH", "ecfcd73c23ce5ec74713d7584ca9268e")

# В некоторых сетях/странах прямое соединение с серверами Telegram
# блокируется провайдером, и тогда TelegramClient.connect() зависает
# без внятной ошибки. Если у вас такая ситуация -- поднимите
# SOCKS5-прокси (например, локальный, через VPN-клиент, или купленный
# отдельно, с логином/паролем) и укажите его здесь через переменные
# окружения:
#   TG_PROXY_TYPE=socks5  TG_PROXY_HOST=1.2.3.4  TG_PROXY_PORT=1080
#   TG_PROXY_USERNAME=user  TG_PROXY_PASSWORD=pass   (логин/пароль — опционально)
_PROXY_HOST = os.environ.get("TG_PROXY_HOST")
_PROXY_TYPE = os.environ.get("TG_PROXY_TYPE", "socks5")
_PROXY_PORT = int(os.environ.get("TG_PROXY_PORT", "1080"))
_PROXY_USERNAME = os.environ.get("TG_PROXY_USERNAME") or None
_PROXY_PASSWORD = os.environ.get("TG_PROXY_PASSWORD") or None

PROXY = None
if _PROXY_HOST:
    if _PROXY_USERNAME:
        # Формат-кортеж PySocks/Telethon: (тип, хост, порт, rdns, логин, пароль).
        # rdns=True -- домены резолвятся на стороне прокси, а не локально.
        PROXY = (_PROXY_TYPE, _PROXY_HOST, _PROXY_PORT, True, _PROXY_USERNAME, _PROXY_PASSWORD)
    else:
        PROXY = (_PROXY_TYPE, _PROXY_HOST, _PROXY_PORT)

# Тот же прокси (обычно локальный SOCKS от VPN-клиента, либо купленный с
# логином/паролем) нужен и для вызовов Gemini API — иначе может оказаться,
# что Telegram через прокси работает, а "Обновить AI-анализ"/живая оценка
# всё равно падают с getaddrinfo failed, потому что httpx идёт мимо прокси
# напрямую. По умолчанию используем те же TG_PROXY_* переменные (один и тот
# же прокси для обоих), но если нужен другой — можно переопределить
# отдельно через HTTP_PROXY_URL.
HTTP_PROXY_URL = os.environ.get("HTTP_PROXY_URL")
if not HTTP_PROXY_URL and _PROXY_HOST:
    from urllib.parse import quote
    _proxy_scheme = "socks5" if _PROXY_TYPE.startswith("socks") else "http"
    _auth = f"{quote(_PROXY_USERNAME, safe='')}:{quote(_PROXY_PASSWORD or '', safe='')}@" if _PROXY_USERNAME else ""
    HTTP_PROXY_URL = f"{_proxy_scheme}://{_auth}{_PROXY_HOST}:{_PROXY_PORT}"

# Сколько секунд ждать подключение/ответ Telegram, прежде чем сдаться
# и показать пользователю понятную ошибку вместо бесконечной загрузки.
CONNECT_TIMEOUT = int(os.environ.get("TG_CONNECT_TIMEOUT", "20"))

# Сессия Telethon (после успешного входа) хранится не файлом на диске,
# а как StringSession в таблице telegram_settings в БД — см.
# telegram_service.py и crud.py. Так она переживает эфемерную файловую
# систему контейнера на Render (перезапуски, редеплои, пересборку
# Docker-образа), а не только локальный запуск.

# Локальный кэш скачанных вложений (фото, голосовые, документы, аватары)
# и временных файлов перед отправкой. Хранится на диске рядом с БД —
# ничего никуда не отправляется, кроме самого Telegram.
MEDIA_DIR = DATA_DIR / "media_cache"
MEDIA_DIR.mkdir(exist_ok=True)
UPLOAD_TMP_DIR = DATA_DIR / "upload_tmp"
UPLOAD_TMP_DIR.mkdir(exist_ok=True)

# Встроенная медиатека (см. media_manager.py) — отдельная от MEDIA_DIR
# (который является кэшем СКАЧАННЫХ из Telegram вложений). Здесь лежат
# файлы, которые пользователь сам загрузил в CRM для повторного
# использования в чатах и кампаниях.
MEDIA_LIBRARY_DIR = DATA_DIR / "media_library"
MEDIA_LIBRARY_DIR.mkdir(exist_ok=True)
MEDIA_LIBRARY_THUMB_DIR = MEDIA_LIBRARY_DIR / "thumbs"
MEDIA_LIBRARY_THUMB_DIR.mkdir(exist_ok=True)

# Через сколько секунд без нового события "печатает" считаем, что
# собеседник перестал печатать (Telegram не присылает явного события
# окончания набора текста, только повторяющиеся события во время ввода).
TYPING_TIMEOUT = 6

# ---- Contact Intelligence: LLM-слой поверх локальной эвристики ----
#
# Числовой скоринг (backend/analysis.py) всегда считается локально и
# детерминированно — он остаётся источником истины для interest_score
# и статуса, независимо от AI_PROVIDER. Провайдер лишь добавляет поверх
# него более живое summary, next_action и черновик ответа по уже
# посчитанным сигналам + тексту переписки.
#
# Ключ уходит только к выбранному провайдеру и не хранится в БД. Без
# ключа (или при любой ошибке сети/API) приложение автоматически
# откатывается на локальный расчёт — фича полностью опциональна.
# ---- Contact Intelligence: LLM-слой поверх локальной эвристики ----
#
# Числовой скоринг (backend/analysis.py) всегда считается локально и
# детерминированно — он остаётся источником истины для interest_score
# и статуса, независимо от AI_PROVIDER. Провайдер лишь добавляет поверх
# него более живое summary, next_action и черновик ответа по уже
# посчитанным сигналам + тексту переписки.
#
# Ключ уходит только в Google Gemini API и не хранится в БД. Без ключа
# (или при любой ошибке сети/API) приложение автоматически откатывается
# на локальный расчёт — фича полностью опциональна.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "local")  # "local" | "gemini"
AI_LLM_TIMEOUT = int(os.environ.get("AI_LLM_TIMEOUT", "20"))
AI_LLM_MAX_MESSAGES = int(os.environ.get("AI_LLM_MAX_MESSAGES", "60"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
LIVE_SCORE_MIN_INTERVAL = 60