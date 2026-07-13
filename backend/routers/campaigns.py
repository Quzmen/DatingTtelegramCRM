from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from .. import crud, schemas, models, config
from ..database import get_db
from .. import campaign_service

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

_EDITABLE_STATUSES = (models.CampaignStatus.DRAFT, models.CampaignStatus.READY)


def _get_or_404(db: Session, campaign_id: int) -> models.Campaign:
    campaign = crud.get_campaign(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Кампания не найдена")
    return campaign


@router.get("", response_model=List[schemas.CampaignOut])
def list_campaigns(db: Session = Depends(get_db)):
    return crud.list_campaigns(db)


@router.post("", response_model=schemas.CampaignOut)
def create_campaign(data: schemas.CampaignCreateIn, db: Session = Depends(get_db)):
    return crud.create_campaign(db, data)


@router.get("/{campaign_id}", response_model=schemas.CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    return crud.campaign_out(campaign)


@router.patch("/{campaign_id}", response_model=schemas.CampaignOut)
def update_campaign(campaign_id: int, data: schemas.CampaignUpdateIn, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status not in _EDITABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Кампанию можно менять только в статусе черновика")
    return crud.update_campaign(db, campaign, data)


@router.delete("/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status == models.CampaignStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Сначала поставьте выполняющуюся кампанию на паузу")
    if campaign.image_path:
        from pathlib import Path
        Path(campaign.image_path).unlink(missing_ok=True)
    crud.delete_campaign(db, campaign)


# ---------------------------------------------------------------
# Вложение — теперь всегда через единую медиатеку (см. МОДУЛЬ
# МЕДИАТЕКИ / ГАЛЕРЕЯ В КАМПАНИЯХ ТЗ: "Не создавать отдельную галерею
# для рассылок. Использовать единый интерфейс медиатеки во всей CRM").
# Прямая загрузка файла ниже тоже сохраняет его в медиатеку (а не
# только в саму кампанию), чтобы он сразу стал доступен для повторного
# использования — ровно так же, как выбор уже существующего файла.
# ---------------------------------------------------------------

@router.post("/{campaign_id}/media", response_model=schemas.CampaignOut)
def attach_campaign_media(campaign_id: int, data: schemas.CampaignMediaAttachIn, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status not in _EDITABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Кампанию можно менять только в статусе черновика")
    media = crud.get_media_file(db, data.media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Файл не найден в медиатеке")
    return crud.attach_campaign_media(db, campaign, media.id)


@router.post("/{campaign_id}/image", response_model=schemas.CampaignOut)
async def upload_campaign_image(campaign_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status not in _EDITABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Кампанию можно менять только в статусе черновика")
    contents = await file.read()
    media = crud.create_media_file(db, contents, file.filename or "image", file.content_type)
    return crud.attach_campaign_media(db, campaign, media.id)


@router.delete("/{campaign_id}/image", response_model=schemas.CampaignOut)
def remove_campaign_image(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.image_path:
        from pathlib import Path
        Path(campaign.image_path).unlink(missing_ok=True)
        campaign.image_path = None
        db.commit()
        db.refresh(campaign)
    if campaign.media_id:
        return crud.detach_campaign_media(db, campaign)
    return crud.campaign_out(campaign)


# ---------------------------------------------------------------
# Предпросмотр (раздел ПРЕДПРОСМОТР ТЗ)
# ---------------------------------------------------------------

@router.post("/{campaign_id}/preview", response_model=schemas.CampaignPreviewOut)
def preview_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    folder_ids = campaign.folder_ids
    filters = schemas.CampaignFiltersIn(**campaign.filters) if campaign.filters else schemas.CampaignFiltersIn()
    recipients, total_in_segments, excluded_reasons = crud.resolve_campaign_recipients(db, folder_ids, filters)
    media_out = crud.media_file_out(campaign.media) if campaign.media else None
    media_usage = crud.media_usage_bulk_check(db, campaign.media_id, recipients) if campaign.media_id and recipients else []
    return schemas.CampaignPreviewOut(
        folder_ids=folder_ids,
        total_dialogs_in_segments=total_in_segments,
        total_after_filters=len(recipients),
        excluded_count=total_in_segments - len(recipients),
        excluded_reasons=excluded_reasons,
        applied_filters=filters,
        message_text=campaign.message_text,
        has_image=bool(campaign.image_path or campaign.media_id),
        media=media_out,
        media_usage=media_usage,
    )


# ---------------------------------------------------------------
# Запуск / пауза / продолжение
# ---------------------------------------------------------------

@router.post("/{campaign_id}/start", response_model=schemas.CampaignOut)
def start_campaign(
    campaign_id: int, data: schemas.CampaignStartIn, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status not in _EDITABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Кампания уже запущена или завершена")
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Запуск требует подтверждения предпросмотра (confirm=true)")
    if not campaign.message_text.strip():
        raise HTTPException(status_code=400, detail="Текст сообщения пуст")

    import json
    filters = schemas.CampaignFiltersIn(**campaign.filters) if campaign.filters else schemas.CampaignFiltersIn()
    recipients, _, _ = crud.resolve_campaign_recipients(db, campaign.folder_ids, filters)
    if not recipients:
        raise HTTPException(status_code=400, detail="После применения фильтров получателей не осталось")

    campaign.recipient_ids_json = json.dumps(recipients)
    campaign.total_selected = len(recipients)
    campaign.cursor = 0
    campaign.processed_count = 0
    campaign.completed_count = 0
    campaign.skipped_count = 0
    campaign.error_count = 0
    campaign.status = models.CampaignStatus.RUNNING
    from datetime import datetime
    campaign.started_at = datetime.utcnow()
    campaign.finished_at = None
    db.commit()
    db.refresh(campaign)

    background_tasks.add_task(campaign_service.run_campaign, campaign_id)
    return crud.campaign_out(campaign)


@router.post("/{campaign_id}/pause", response_model=schemas.CampaignOut)
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status != models.CampaignStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Кампания сейчас не выполняется")
    campaign.status = models.CampaignStatus.PAUSED
    db.commit()
    db.refresh(campaign)
    return crud.campaign_out(campaign)


@router.post("/{campaign_id}/resume", response_model=schemas.CampaignOut)
def resume_campaign(campaign_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    campaign = _get_or_404(db, campaign_id)
    if campaign.status != models.CampaignStatus.PAUSED:
        raise HTTPException(status_code=409, detail="Кампания не на паузе")
    campaign.status = models.CampaignStatus.RUNNING
    db.commit()
    db.refresh(campaign)
    background_tasks.add_task(campaign_service.run_campaign, campaign_id)
    return crud.campaign_out(campaign)


# ---------------------------------------------------------------
# Журнал (раздел ЖУРНАЛ ТЗ)
# ---------------------------------------------------------------

@router.get("/{campaign_id}/logs", response_model=List[schemas.CampaignLogOut])
def get_campaign_logs(campaign_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, campaign_id)
    return crud.list_campaign_logs(db, campaign_id)
