"""
Media Manager — единая точка входа для всего, что связано с медиатекой
CRM (раздел МОДУЛЬ МЕДИАТЕКИ / АРХИТЕКТУРА ТЗ).

Отвечает за:
  - хранение файлов на диске (config.MEDIA_LIBRARY_DIR);
  - генерацию и кэширование превью (thumbnails) для фото/GIF;
  - определение типа вложения (photo/video/gif/document);
  - подготовку файла к отправке в Telegram корректным методом API
    (см. telegram_service.send_file — он принимает kind, полученный
    отсюда, чтобы фото уходило как Photo, а видео — как Video);
  - историю использования (кто/когда получал файл).

Все части CRM (обычные чаты, быстрые сообщения, кампании, очередь
отправки) работают с медиатекой только через этот модуль и
routers/media.py — так исключено появление нескольких параллельных
реализаций отправки/хранения медиа (см. раздел АРХИТЕКТУРА ТЗ:
"Не должно существовать нескольких разных реализаций отправки медиа").

DB-запросы (сама история использования, список файлов и т.п.) живут в
crud.py — так же, как для остальных сущностей приложения (Contact,
Campaign...) — а этот модуль отвечает за файловую систему и
классификацию, и вызывается из routers/media.py вместе с crud.py.
"""
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

from . import config, models

logger = logging.getLogger("telegram-crm")

# ---------------------------------------------------------------
# Классификация типа файла
# ---------------------------------------------------------------

PHOTO_EXTS = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".heic"}
GIF_EXTS = {".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"}

THUMB_MAX_SIZE = (480, 480)
THUMB_JPEG_QUALITY = 78


def classify_kind(filename: str, mime: Optional[str] = None) -> models.MediaKind:
    ext = Path(filename or "").suffix.lower()
    mime = (mime or mimetypes.guess_type(filename or "")[0] or "").lower()

    if ext in GIF_EXTS or mime == "image/gif":
        return models.MediaKind.GIF
    if ext in PHOTO_EXTS or mime.startswith("image/"):
        return models.MediaKind.PHOTO
    if ext in VIDEO_EXTS or mime.startswith("video/"):
        return models.MediaKind.VIDEO
    return models.MediaKind.DOCUMENT


# ---------------------------------------------------------------
# Пути на диске
# ---------------------------------------------------------------

def file_path(stored_name: str) -> Path:
    return config.MEDIA_LIBRARY_DIR / stored_name


def thumb_path(stored_name: str) -> Path:
    return config.MEDIA_LIBRARY_THUMB_DIR / f"{Path(stored_name).stem}.jpg"


def _unique_stored_name(original_name: str) -> str:
    ext = Path(original_name or "").suffix.lower()
    return f"{uuid.uuid4().hex}{ext}"


# ---------------------------------------------------------------
# Превью
# ---------------------------------------------------------------

def _generate_thumbnail(src_path: Path, dest_path: Path, kind: models.MediaKind) -> bool:
    """Генерирует превью для фото/GIF через Pillow (если установлен).
    Для видео полноценное превью потребовало бы ffmpeg, которого в
    зависимостях проекта нет — в этом случае карточка в галерее просто
    показывает иконку по типу файла на фронтенде, ничего не ломая
    (см. раздел ПРОИЗВОДИТЕЛЬНОСТЬ: не должно быть повторной генерации
    и лишней работы там, где превью всё равно не помогает)."""
    if kind not in (models.MediaKind.PHOTO, models.MediaKind.GIF):
        return False
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow не установлен — превью для медиатеки не генерируются")
        return False
    try:
        with Image.open(src_path) as im:
            im = im.convert("RGB")
            im.thumbnail(THUMB_MAX_SIZE)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            im.save(dest_path, "JPEG", quality=THUMB_JPEG_QUALITY)
        return True
    except Exception:
        logger.exception("Не удалось сгенерировать превью для %s", src_path)
        return False


def _image_dimensions(src_path: Path, kind: models.MediaKind) -> tuple[Optional[int], Optional[int]]:
    if kind not in (models.MediaKind.PHOTO, models.MediaKind.GIF):
        return None, None
    try:
        from PIL import Image
        with Image.open(src_path) as im:
            return im.width, im.height
    except Exception:
        return None, None


# ---------------------------------------------------------------
# Загрузка / удаление
# ---------------------------------------------------------------

def store_upload(contents: bytes, original_name: str, mime: Optional[str] = None) -> dict:
    """Сохраняет загруженный файл на диск и возвращает набор полей,
    достаточный чтобы crud.create_media_file() создал строку в БД.
    Не трогает БД сама — только файловую систему, чтобы не завязывать
    media_manager на конкретную ORM-сессию."""
    original_name = (Path(original_name).name or "файл").strip() or "файл"
    kind = classify_kind(original_name, mime)
    stored_name = _unique_stored_name(original_name)
    dest = file_path(stored_name)
    dest.write_bytes(contents)

    width, height = _image_dimensions(dest, kind)
    has_thumb = _generate_thumbnail(dest, thumb_path(stored_name), kind)

    return {
        "original_name": original_name,
        "stored_name": stored_name,
        "kind": kind,
        "mime": mime or mimetypes.guess_type(original_name)[0],
        "size_bytes": len(contents),
        "width": width,
        "height": height,
        "has_thumb": has_thumb,
    }


def delete_files(stored_name: str) -> None:
    file_path(stored_name).unlink(missing_ok=True)
    thumb_path(stored_name).unlink(missing_ok=True)


def stats(media_files: list[models.MediaFile]) -> dict:
    return {
        "count": len(media_files),
        "total_size_bytes": sum(m.size_bytes for m in media_files),
    }
