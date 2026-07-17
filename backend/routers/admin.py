"""
Служебный роутер для первоначального переноса данных на сервер.

data/ (crm.db, telegram_session.session, media_cache/) никогда не
коммитится в git — там сессии Telegram (ключи доступа к аккаунтам
ВСЕХ пользователей CRM) и личная переписка. Чтобы перенести уже
существующие локальные данные на Render, используется этот эндпоинт:
он принимает zip-архив и распаковывает его прямо в DATA_DIR на
постоянном диске — то есть затрагивает данные СРАЗУ ВСЕХ
пользователей, а не одного, поэтому не может быть защищён обычной
пользовательской CRM-сессией (crm_session/get_current_user из auth.py) —
это операция уровня инфраструктуры, а не действие конкретного
пользователя внутри своих данных.

Раньше был защищён тем же Basic Auth, что и всё приложение (см.
main.py); теперь, когда общий Basic Auth перед приложением убран в
пользу входа через Telegram, этот эндпоинт защищён отдельным
секретом уровня инфраструктуры — переменной окружения ADMIN_TOKEN.
Если она не задана, эндпоинт полностью отключён (403) — раз он может
перезаписать данные всех пользователей сразу, безопасный дефолт здесь
"выключено", а не "открыто", в отличие от прежнего необязательного
APP_USERS.

Использование (локально, там где лежит папка data/):
    cd telegram-crm/data
    zip -r ../data_backup.zip . -x "upload_tmp/*"
    cd ..
    curl -H "X-Admin-Token: <ADMIN_TOKEN>" \
         -F "file=@data_backup.zip" \
         https://<your-app>.onrender.com/api/admin/import-data
"""
import os
import secrets
import shutil
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile

from .. import config

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin_token(x_admin_token: str = Header(default="")) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="Административные эндпоинты отключены (переменная окружения ADMIN_TOKEN не задана).",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий X-Admin-Token")


@router.post("/import-data")
async def import_data(file: UploadFile = File(...), _admin: None = Depends(require_admin_token)):
    """Распаковывает загруженный zip-архив в DATA_DIR, перезаписывая
    существующие файлы (crm.db, telegram_session.session, media_cache/)
    для ВСЕХ пользователей CRM сразу. Требует заголовок X-Admin-Token,
    см. module docstring."""
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
