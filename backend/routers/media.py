"""
Эндпоинты встроенной медиатеки (см. МОДУЛЬ МЕДИАТЕКИ / ВСТРОЕННАЯ
ГАЛЕРЕЯ / ИСТОРИЯ ИСПОЛЬЗОВАНИЯ ТЗ).

Всё, что связано с чтением/записью файлов на диск, идёт через
media_manager.py; сами DB-запросы — через crud.py, как и для
остальных сущностей приложения. Этот роутер — единственное место,
откуда фронтенд обращается к медиатеке, и используется одинаково из
обычного чата, кампаний и любых будущих модулей (см. АРХИТЕКТУРА ТЗ).
"""
import mimetypes
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import crud, schemas, media_manager
from ..database import get_db

router = APIRouter(prefix="/api/media", tags=["media"])


def _get_or_404(db: Session, media_id: int):
    media = crud.get_media_file(db, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Файл не найден в медиатеке")
    return media


# ---------------------------------------------------------------
# Список / поиск / сортировка / фильтрация (раздел ВСТРОЕННАЯ ГАЛЕРЕЯ)
# ---------------------------------------------------------------

@router.get("", response_model=schemas.MediaListOut)
def list_media(
    search: Optional[str] = None,
    kind: Optional[str] = None,
    sort: str = "date_desc",
    db: Session = Depends(get_db),
):
    return crud.list_media_files(db, search=search, kind=kind, sort=sort)


# ---------------------------------------------------------------
# Загрузка (несколько файлов за раз — тот же диалог "загрузить новые файлы")
# ---------------------------------------------------------------

@router.post("/upload", response_model=List[schemas.MediaFileOut])
async def upload_media(files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    out = []
    for file in files:
        contents = await file.read()
        if not contents:
            continue
        out.append(crud.create_media_file(db, contents, file.filename or "файл", file.content_type))
    if not out:
        raise HTTPException(status_code=400, detail="Не удалось прочитать ни один из файлов")
    return out


# ---------------------------------------------------------------
# Раздача файла / превью
# ---------------------------------------------------------------

@router.get("/{media_id}/file")
def get_media_file(media_id: int, db: Session = Depends(get_db)):
    media = _get_or_404(db, media_id)
    path = media_manager.file_path(media.stored_name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл отсутствует на диске")
    mime = media.mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime, filename=media.original_name)


@router.get("/{media_id}/thumb")
def get_media_thumb(media_id: int, db: Session = Depends(get_db)):
    media = _get_or_404(db, media_id)
    if not media.has_thumb:
        raise HTTPException(status_code=404, detail="У файла нет превью")
    path = media_manager.thumb_path(media.stored_name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Превью отсутствует на диске")
    return FileResponse(path, media_type="image/jpeg")


# ---------------------------------------------------------------
# Переименование / удаление
# ---------------------------------------------------------------

@router.patch("/{media_id}", response_model=schemas.MediaFileOut)
def rename_media(media_id: int, data: schemas.MediaRenameIn, db: Session = Depends(get_db)):
    media = _get_or_404(db, media_id)
    return crud.rename_media_file(db, media, data.name)


@router.delete("/{media_id}", status_code=204)
def delete_media(media_id: int, db: Session = Depends(get_db)):
    media = _get_or_404(db, media_id)
    crud.delete_media_file(db, media)


# ---------------------------------------------------------------
# История использования (разделы ПРОВЕРКА В ЧАТЕ / ПРОВЕРКА ПРИ КАМПАНИЯХ)
# ---------------------------------------------------------------

@router.get("/{media_id}/usage/{telegram_id}", response_model=schemas.MediaUsageStatusOut)
def check_media_usage(media_id: int, telegram_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, media_id)
    return crud.media_usage_status(db, media_id, telegram_id)


@router.post("/{media_id}/usage/check", response_model=List[schemas.MediaUsageStatusOut])
def bulk_check_media_usage(media_id: int, data: schemas.MediaUsageBulkCheckIn, db: Session = Depends(get_db)):
    _get_or_404(db, media_id)
    return crud.media_usage_bulk_check(db, media_id, data.telegram_ids)


@router.post("/usage-for-dialog", response_model=List[schemas.MediaDialogUsageOut])
def dialog_media_usage(data: schemas.MediaDialogUsageCheckIn, db: Session = Depends(get_db)):
    return crud.dialog_media_usage(db, data.telegram_id, data.media_ids)
