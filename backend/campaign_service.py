"""
Выполнение кампаний массовых рассылок (см. КАМПАНИИ СООБЩЕНИЙ,
ШАБЛОНЫ, ПРЕДПРОСМОТР, ЖУРНАЛ, СИНХРОНИЗАЦИЯ в ТЗ).

Логика запуска/паузы намеренно предельно простая: одна фоновая
корутина на кампанию (см. routers/campaigns.start_campaign,
BackgroundTasks), которая идёт по заранее посчитанному списку
telegram_id (Campaign.recipient_ids_json, зафиксирован в момент
запуска — см. models.Campaign) начиная с Campaign.cursor. Пауза —
это просто флаг в БД (Campaign.status == PAUSED), который цикл
проверяет перед каждой отправкой; никакого отдельного планировщика
или очереди задач нет, это соответствует масштабу остального проекта
(один аккаунт, работа в одном процессе).
"""
import asyncio
import logging
import string
from typing import Optional

from sqlalchemy.orm import Session

from . import crud, models, media_manager
from .database import SessionLocal
from .telegram_service import telegram_service, TelegramAuthError, BULK_SEND_DELAY_SECONDS

logger = logging.getLogger("telegram-crm")

# Архитектура должна позволять без изменений ядра добавлять новые
# переменные (см. ШАБЛОНЫ ТЗ) — единственное место, которое нужно
# трогать, чтобы добавить новую переменную вида {что-то}, это
# TelegramService.get_entity_vars (какие данные доступны) и, при
# необходимости, список поддерживаемых имён ниже для документации/UI.
SUPPORTED_TEMPLATE_VARS = ["name", "username", "first_name"]


class _SafeDict(dict):
    """Позволяет render_template не падать на переменной, которой нет
    в данных получателя (например, get_entity_vars не смог резолвить
    username) — оставляет {var} как есть вместо KeyError."""
    def __missing__(self, key):
        return "{" + key + "}"


def render_template(text: str, variables: dict) -> str:
    return string.Formatter().vformat(text, (), _SafeDict(**variables))


async def run_campaign(campaign_id: int) -> None:
    db: Session = SessionLocal()
    try:
        campaign = crud.get_campaign(db, campaign_id)
        if campaign is None:
            return
        recipient_ids = campaign.recipient_ids
        total = len(recipient_ids)

        while campaign.cursor < total:
            db.refresh(campaign)
            if campaign.status != models.CampaignStatus.RUNNING:
                # Кто-то поставил на паузу (или кампанию удалили) —
                # выходим, ничего не завершая. cursor уже сохранён с
                # прошлой итерации, так что resume продолжит отсюда же.
                return

            telegram_id = recipient_ids[campaign.cursor]
            result = "sent"
            error_text: Optional[str] = None
            try:
                variables = await telegram_service.get_entity_vars(telegram_id)
                text = render_template(campaign.message_text, variables)
                if campaign.media_id and campaign.media:
                    path = media_manager.file_path(campaign.media.stored_name)
                    await telegram_service.send_file(
                        telegram_id, str(path), caption=text, kind=campaign.media.kind.value,
                    )
                    crud.record_media_usage(db, campaign.media_id, telegram_id, context="campaign")
                elif campaign.image_path:
                    # Вложения, загруженные до появления медиатеки — отправляем
                    # как раньше, определяя фото/видео по расширению файла.
                    kind = media_manager.classify_kind(campaign.image_path).value
                    await telegram_service.send_file(telegram_id, campaign.image_path, caption=text, kind=kind)
                else:
                    await telegram_service.send_message(telegram_id, text)
                campaign.completed_count += 1
            except TelegramAuthError as e:
                result, error_text = "error", str(e)
                campaign.error_count += 1
            except Exception as e:
                logger.exception("campaign %s: ошибка отправки на %s", campaign_id, telegram_id)
                result, error_text = "error", str(e)
                campaign.error_count += 1

            campaign.processed_count += 1
            campaign.cursor += 1
            db.commit()
            crud.create_campaign_log(db, campaign_id, telegram_id, result, error_text)

            if campaign.cursor < total:
                await asyncio.sleep(BULK_SEND_DELAY_SECONDS)

        db.refresh(campaign)
        if campaign.status == models.CampaignStatus.RUNNING:
            campaign.status = (
                models.CampaignStatus.COMPLETED_WITH_ERRORS
                if campaign.error_count > 0
                else models.CampaignStatus.COMPLETED
            )
            from datetime import datetime
            campaign.finished_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
