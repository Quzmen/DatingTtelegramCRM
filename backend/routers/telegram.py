import mimetypes
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import crud, schemas, models, config
from ..database import get_db
from ..telegram_service import telegram_service, TelegramAuthError

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


def _err(e: TelegramAuthError, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


@router.get("/status", response_model=schemas.TelegramStatusOut)
async def get_status():
    return await telegram_service.status()


@router.post("/send-code")
async def send_code(data: schemas.TelegramSendCodeIn):
    try:
        await telegram_service.send_code(data.phone)
    except TelegramAuthError as e:
        _err(e)
    return {"sent": True}


@router.post("/sign-in", response_model=schemas.TelegramStatusOut)
async def sign_in(data: schemas.TelegramSignInIn):
    try:
        result = await telegram_service.sign_in(data.phone, data.code, data.password)
    except TelegramAuthError as e:
        _err(e)
    return result


@router.post("/logout")
async def logout():
    await telegram_service.logout()
    return {"authorized": False}


@router.get("/contacts", response_model=List[schemas.TelegramContactOut])
async def list_telegram_contacts(db: Session = Depends(get_db)):
    try:
        contacts = await telegram_service.fetch_contacts()
    except TelegramAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    existing_ids = {
        row[0] for row in db.query(models.Contact.telegram_id).filter(models.Contact.telegram_id.isnot(None)).all()
    }
    for c in contacts:
        c["already_imported"] = c["telegram_id"] in existing_ids
    return contacts


@router.post("/import", response_model=schemas.TelegramImportResultOut)
async def import_contacts(data: schemas.TelegramImportIn, db: Session = Depends(get_db)):
    try:
        all_contacts = await telegram_service.fetch_contacts()
    except TelegramAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    wanted = set(data.telegram_ids)
    selected = [c for c in all_contacts if c["telegram_id"] in wanted]
    if not selected:
        raise HTTPException(status_code=400, detail="Ни один из выбранных контактов не найден в Telegram")

    return crud.import_telegram_contacts(db, selected, data.default_status, data.tags or [])


@router.post("/resolve", response_model=schemas.TelegramUserOut)
async def resolve_username(data: schemas.TelegramResolveIn):
    try:
        return await telegram_service.resolve_username(data.username)
    except TelegramAuthError as e:
        _err(e)


# ---------------------------------------------------------------
# Диалоги (левая колонка мессенджера)
# ---------------------------------------------------------------

@router.get("/dialogs", response_model=List[schemas.TelegramDialogOut])
async def list_dialogs(limit: int = 100):
    try:
        return await telegram_service.list_dialogs(limit=limit)
    except TelegramAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/presence/{telegram_id}", response_model=schemas.TelegramPresenceOut)
async def get_presence(telegram_id: int):
    try:
        return await telegram_service.get_presence(telegram_id)
    except TelegramAuthError as e:
        _err(e)


@router.get("/avatar/{telegram_id}")
async def get_avatar(telegram_id: int):
    try:
        path = await telegram_service.get_avatar_path(telegram_id)
    except TelegramAuthError as e:
        _err(e)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Нет фото профиля")
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return FileResponse(path, media_type=mime)


@router.post("/read/{telegram_id}")
async def mark_read(telegram_id: int):
    await telegram_service.mark_read(telegram_id)
    return {"ok": True}


# ---------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------

@router.get("/messages/{telegram_id}", response_model=List[schemas.TelegramMessageOut])
async def get_messages(telegram_id: int, limit: int = 50):
    try:
        return await telegram_service.get_messages(telegram_id, limit=limit)
    except TelegramAuthError as e:
        _err(e)


@router.post("/messages/{telegram_id}", response_model=schemas.TelegramMessageOut)
async def send_message(telegram_id: int, data: schemas.TelegramSendMessageIn):
    try:
        return await telegram_service.send_message(telegram_id, data.text, reply_to=data.reply_to)
    except TelegramAuthError as e:
        _err(e)


@router.post("/messages/{telegram_id}/file", response_model=schemas.TelegramMessageOut)
async def send_file(
    telegram_id: int,
    file: UploadFile = File(...),
    caption: Optional[str] = Form(None),
    reply_to: Optional[int] = Form(None),
    voice: bool = Form(False),
):
    tmp_path = config.UPLOAD_TMP_DIR / f"{telegram_id}_{file.filename}"
    try:
        contents = await file.read()
        tmp_path.write_bytes(contents)
        result = await telegram_service.send_file(
            telegram_id, str(tmp_path), caption=caption, reply_to=reply_to, voice_note=voice,
        )
        return result
    except TelegramAuthError as e:
        _err(e)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@router.patch("/messages/{telegram_id}/{message_id}", response_model=schemas.TelegramMessageOut)
async def edit_message(telegram_id: int, message_id: int, data: schemas.TelegramEditMessageIn):
    try:
        return await telegram_service.edit_message(telegram_id, message_id, data.text)
    except TelegramAuthError as e:
        _err(e)


@router.delete("/messages/{telegram_id}/{message_id}", status_code=204)
async def delete_message(telegram_id: int, message_id: int):
    try:
        await telegram_service.delete_message(telegram_id, message_id)
    except TelegramAuthError as e:
        _err(e)


@router.post("/messages/{telegram_id}/{message_id}/pin")
async def pin_message(telegram_id: int, message_id: int):
    try:
        await telegram_service.pin_message(telegram_id, message_id)
    except TelegramAuthError as e:
        _err(e)
    return {"ok": True}


@router.post("/messages/{telegram_id}/unpin")
async def unpin_message(telegram_id: int, message_id: Optional[int] = None):
    try:
        await telegram_service.unpin_message(telegram_id, message_id)
    except TelegramAuthError as e:
        _err(e)
    return {"ok": True}


@router.post("/messages/{telegram_id}/{message_id}/forward", response_model=schemas.TelegramMessageOut)
async def forward_message(telegram_id: int, message_id: int, data: schemas.TelegramForwardIn):
    try:
        return await telegram_service.forward_message(telegram_id, message_id, data.to_telegram_id)
    except TelegramAuthError as e:
        _err(e)


@router.get("/media/{telegram_id}/{message_id}")
async def get_media(telegram_id: int, message_id: int):
    try:
        path = await telegram_service.download_media(telegram_id, message_id)
    except TelegramAuthError as e:
        _err(e)
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime, filename=Path(path).name)
