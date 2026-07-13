"""
Служебный роутер для первоначального переноса данных на сервер.

data/ (crm.db, telegram_session.session, media_cache/) никогда не
коммитится в git — там сессия Telegram (фактически ключ доступа к
аккаунту) и личная переписка. Чтобы перенести уже существующие
локальные данные на Render, используется этот эндпоинт: он принимает
zip-архив и распаковывает его прямо в DATA_DIR на постоянном диске.

Защищён тем же Basic Auth, что и всё приложение (см. main.py) —
отдельной проверки не требует, но эндпоинт всё равно стоит
использовать один раз, сразу после первого деплоя.

Использование (локально, там где лежит папка data/):
    cd telegram-crm/data
    zip -r ../data_backup.zip . -x "upload_tmp/*"
    cd ..
    curl -u <login>:<password> \
         -F "file=@data_backup.zip" \
         https://<your-app>.onrender.com/api/admin/import-data
"""
import shutil
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, File, HTTPException, UploadFile

from .. import config

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/import-data")
async def import_data(file: UploadFile = File(...)):
    """Распаковывает загруженный zip-архив в DATA_DIR, перезаписывая
    существующие файлы (crm.db, telegram_session.session, media_cache/)."""
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Ожидается .zip архив")

    with NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(config.DATA_DIR)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Файл повреждён или не является zip-архивом")
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"status": "ok", "extracted_to": str(config.DATA_DIR)}
