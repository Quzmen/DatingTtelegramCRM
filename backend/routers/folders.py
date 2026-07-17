from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, schemas, models
from ..database import get_db
from ..auth import get_current_user

router = APIRouter(prefix="/api/folders", tags=["folders"])


@router.get("", response_model=List[schemas.FolderOut])
def list_folders(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return crud.list_folders(db, current_user.id)


@router.post("", response_model=schemas.FolderOut)
def create_folder(
    data: schemas.FolderCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    return crud.create_folder(db, current_user.id, data)


@router.patch("/{folder_id}", response_model=schemas.FolderOut)
def update_folder(
    folder_id: int, data: schemas.FolderUpdate,
    db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    folder = crud.get_folder(db, current_user.id, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    return crud.update_folder(db, folder, data)


@router.delete("/{folder_id}", status_code=204)
def delete_folder(
    folder_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    folder = crud.get_folder(db, current_user.id, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    crud.delete_folder(db, folder)


@router.post("/reorder", response_model=List[schemas.FolderOut])
def reorder_folders(
    data: schemas.FolderReorderIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    return crud.reorder_folders(db, current_user.id, data.ordered_ids)


@router.post("/assign")
def assign_dialogs(
    data: schemas.FolderAssignIn, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user),
):
    if data.folder_id is not None and crud.get_folder(db, current_user.id, data.folder_id) is None:
        raise HTTPException(status_code=404, detail="Папка не найдена")
    moved = crud.assign_dialogs_to_folder(db, current_user.id, data.telegram_ids, data.folder_id)
    return {"moved": moved}
