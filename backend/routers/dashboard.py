from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import crud, schemas, models
from ..database import get_db

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard", response_model=schemas.DashboardOut)
def get_dashboard(db: Session = Depends(get_db)):
    return crud.get_dashboard(db)


@router.get("/attention", response_model=List[schemas.ContactOut])
def get_attention_list(db: Session = Depends(get_db)):
    return crud.contacts_needing_attention(db)


@router.get("/reminders", response_model=List[schemas.ReminderOut])
def get_reminders(db: Session = Depends(get_db)):
    """Follow-up напоминания (Этап 9, п.5) — контакты, которым пора
    напомнить о себе, с человекочитаемым текстом подсказки."""
    return crud.get_reminders(db)


@router.get("/tags", response_model=List[schemas.TagOut])
def get_tags(db: Session = Depends(get_db)):
    return crud.get_all_tags(db)


@router.get("/statuses")
def get_statuses():
    """Ordered list of statuses with labels -- powers the kanban columns."""
    return [
        {"value": status.value, "label": models.STATUS_LABELS[status]}
        for status in models.STATUS_ORDER
    ]
