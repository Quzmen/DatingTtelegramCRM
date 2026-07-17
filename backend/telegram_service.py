"""
Обёртка над Telethon.

ВАЖНО (переход на многопользовательский режим, см. routers/auth.py):
теперь один экземпляр TelegramService = один Telegram-клиент ОДНОГО
пользователя CRM, а не общий на всё приложение. За выдачу и
переиспользование экземпляров отвечает get_telegram_service(user_id)
внизу файла — она держит по одному живому TelegramService на каждого
залогиненного пользователя (пока процесс жив; сама сессия Telethon
переживает рестарт через БД, см. ниже).

Сессия Telethon хранится не в файле на диске (файловая система
контейнера на Render эфемерна и сбрасывается при каждом
рестарте/редеплое), а как StringSession в таблице telegram_settings
той же БД, что и остальные данные CRM (см. crud.py,
database.get_db/SessionLocal) — теперь по одной строке НА
ПОЛЬЗОВАТЕЛЯ (telegram_settings.user_id), а не одной строке на всё
приложение. После успешного входа переавторизация не требуется, пока
пользователь сам не нажмёт "выйти" — сессия переживает перезапуски и
пересборку контейнера.

Помимо базовой авторизации и отправки текста, сервис умеет то, что
нужно полноценному мессенджеру поверх Telegram: диалоги, вложения
(фото/голосовые/документы), ответы, пересылку, редактирование,
удаление, закрепление и "живые" статусы (онлайн / последний визит /
печатает…) через фоновый обработчик событий Telethon.

Модуль по-прежнему экспортирует module-level `telegram_service`
(user_id=None, "ничья" сессия) — это временная подпорка для
эндпоинтов, ещё не переведённых на per-user клиент через
get_telegram_service(current_user.id) (см. TODO в routers/telegram.py
и остальных роутерах, которые пока используют глобальный объект;
это следующий этап миграции на многопользовательский режим, а не
постоянное решение).
"""
import asyncio
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
    AuthKeyUnregisteredError,
    FileReferenceExpiredError,
)
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import InputDialogPeer, User, InputPhoto, InputDocument

from . import config, crud
from . import sync_service
from .database import SessionLocal
from .telegram_utils import _status_info, _user_out, _media_info, _cache_file_id

logger = logging.getLogger("telegram-crm")

BULK_SEND_DELAY_SECONDS = 15


class TelegramAuthError(Exception):
    """Ожидаемая ошибка авторизации -- показывается пользователю как есть."""


async def _with_timeout(coro, action: str):
    try:
        return await asyncio.wait_for(coro, timeout=config.CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        raise TelegramAuthError(
            f"Telegram не ответил за {config.CONNECT_TIMEOUT} сек. ({action}). "
            "Проверьте интернет-соединение или настройте прокси (TG_PROXY_HOST)."
        )


class TelegramService:
    def __init__(self, user_id: Optional[int] = None):
        # user_id=None — временный режим совместимости для кода,
        # ещё не переведённого на get_telegram_service(current_user.id)
        # (см. module docstring выше). Читает/пишет "ничью" строку
        # telegram_settings с user_id IS NULL.
        self.user_id = user_id
        self._client: Optional[TelegramClient] = None
        self._lock = asyncio.Lock()
        self._phone: Optional[str] = None
        self._phone_code_hash: Optional[str] = None

        # "Живые" статусы собеседников, наполняются фоновым обработчиком
        # событий Telethon (см. _register_handlers). Ключ -- telegram_id.
        self._presence: dict = {}
        self._handlers_registered = False

    def _load_session_string(self) -> str:
        """Читает сохранённую StringSession этого пользователя из БД.
        Пустая строка — как и пустой файл сессии раньше — означает
        "аккаунт ещё не авторизован", Telethon в этом случае создаёт
        новую сессию."""
        db = SessionLocal()
        try:
            return crud.get_telegram_session_string(db, self.user_id) or ""
        finally:
            db.close()

    def _persist_session(self) -> None:
        """Сохраняет текущую StringSession клиента в БД — вызывается
        сразу после успешного входа, чтобы сессия пережила следующий
        рестарт/редеплой контейнера на Render."""
        if self._client is None:
            return
        session_string = self._client.session.save()
        if not session_string:
            return
        db = SessionLocal()
        try:
            crud.save_telegram_session_string(db, self.user_id, session_string)
        finally:
            db.close()

    def _clear_persisted_session(self) -> None:
        db = SessionLocal()
        try:
            crud.clear_telegram_session_string(db, self.user_id)
        finally:
            db.close()

    async def invalidate_session(self) -> None:
        """Вызывается, когда Telegram сообщает, что сохранённая сессия
        больше не действительна (AuthKeyUnregisteredError — например,
        аккаунт вышел из этого сеанса вручную через настройки Telegram).
        Сбрасывает и локальный клиент, и запись в БД, чтобы пользователь
        увидел экран входа вместо повторяющейся 500-й ошибки."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._handlers_registered = False
        self._phone = None
        self._phone_code_hash = None
        self._clear_persisted_session()

    async def _get_client(self) -> TelegramClient:
        if self._client is None:
            kwargs = {}
            if config.PROXY:
                kwargs["proxy"] = config.PROXY
            session_string = self._load_session_string()
            self._client = TelegramClient(StringSession(session_string), config.API_ID, config.API_HASH, **kwargs)
            # Обработчики событий Telethon (см. sync_service.py) получают
            # только event, а не self -- у event есть event.client, поэтому
            # кладём user_id прямо на клиент, чтобы обработчик знал, чью
            # запись обновлять в Dialog/Message (см. models.py: обе таблицы
            # теперь ключуются user_id, а не только telegram_id).
            self._client.crm_user_id = self.user_id
        if not self._client.is_connected():
            try:
                await asyncio.wait_for(self._client.connect(), timeout=config.CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise TelegramAuthError(
                    "Не удалось подключиться к серверам Telegram за "
                    f"{config.CONNECT_TIMEOUT} сек. Проверьте интернет-соединение; "
                    "если Telegram заблокирован в вашей сети, настройте прокси "
                    "через переменные окружения TG_PROXY_HOST/TG_PROXY_PORT."
                )
            except OSError as e:
                raise TelegramAuthError(f"Ошибка сети при подключении к Telegram: {e}")
        await self._register_handlers(self._client)
        return self._client

    async def _register_handlers(self, client: TelegramClient) -> None:
        """Подписывается на события "печатает"/"в сети" и на события
        сообщений (см. sync_service.register_message_handlers) один раз
        за время жизни клиента."""
        if self._handlers_registered:
            return

        @client.on(events.UserUpdate)
        async def _on_user_update(event):
            uid = event.user_id
            entry = self._presence.setdefault(uid, {})
            try:
                if event.typing:
                    entry["typing_at"] = time.time()
            except Exception:
                pass
            status = getattr(event, "status", None)
            if status is not None:
                entry.update(_status_info(status))

        sync_service.register_message_handlers(client)

        self._handlers_registered = True

    # ---------------------------------------------------------------
    # Авторизация
    # ---------------------------------------------------------------

    async def status(self) -> dict:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            return {"authorized": False, "user": None}
        me = await _with_timeout(client.get_me(), "получение профиля")
        return {"authorized": True, "user": _user_out(me)}

    async def sync_now(self) -> bool:
        """Полная синхронизация кэша диалогов, без предварительного
        входа -- используется при старте приложения (main.py), если
        сессия уже сохранена в БД с прошлого раза. Тихо ничего не
        делает, если аккаунт не авторизован (например, первый запуск,
        когда ещё никто не логинился) или Telegram недоступен."""
        try:
            client = await self._get_client()
            if not await client.is_user_authorized():
                return False
            await sync_service.full_sync(client)
            return True
        except Exception:
            return False

    async def send_code(self, phone: str) -> None:
        client = await self._get_client()
        try:
            sent = await _with_timeout(client.send_code_request(phone), "отправка кода")
        except PhoneNumberInvalidError:
            raise TelegramAuthError("Некорректный номер телефона")
        except FloodWaitError as e:
            raise TelegramAuthError(f"Слишком много попыток, подождите {e.seconds} сек.")
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash

    async def sign_in(self, phone: str, code: str, password: Optional[str] = None) -> dict:
        client = await self._get_client()

        if password:
            try:
                await _with_timeout(client.sign_in(password=password), "вход по паролю")
            except TelegramAuthError:
                raise
            except Exception:
                raise TelegramAuthError("Неверный пароль двухфакторной аутентификации")
        else:
            if not self._phone_code_hash or self._phone != phone:
                raise TelegramAuthError("Сначала запросите код для этого номера")
            try:
                await _with_timeout(
                    client.sign_in(phone=phone, code=code, phone_code_hash=self._phone_code_hash),
                    "вход по коду",
                )
            except SessionPasswordNeededError:
                return {"authorized": False, "needs_password": True, "user": None}
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                raise TelegramAuthError("Неверный или устаревший код")

        me = await _with_timeout(client.get_me(), "получение профиля")
        self._phone_code_hash = None
        self._persist_session()
        await sync_service.full_sync(client)
        return {"authorized": True, "needs_password": False, "user": _user_out(me)}

    async def logout(self) -> None:
        client = await self._get_client()
        if await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            await _with_timeout(client.log_out(), "выход из аккаунта")
        self._phone = None
        self._phone_code_hash = None
        self._clear_persisted_session()
        # log_out() уже инвалидировал ключ на стороне Telegram — пересоздаём
        # клиента с чистой StringSession, чтобы следующий вход начинался с нуля.
        self._client = None
        self._handlers_registered = False

    # ---------------------------------------------------------------
    # Контакты
    # ---------------------------------------------------------------

    async def fetch_contacts(self) -> list:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")
        result = await _with_timeout(client(GetContactsRequest(hash=0)), "загрузка контактов")
        users = [u for u in getattr(result, "users", []) if isinstance(u, User) and not u.bot]
        return [_user_out(u) for u in users]

    async def resolve_username(self, username: str) -> dict:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")
        clean = username.lstrip("@").strip()
        if not clean:
            raise TelegramAuthError("Укажите username")
        try:
            entity = await _with_timeout(client.get_entity(clean), "поиск пользователя")
        except (ValueError, TypeError):
            raise TelegramAuthError("Пользователь с таким username не найден")
        if not isinstance(entity, User):
            raise TelegramAuthError("Это не пользователь Telegram")
        return _user_out(entity)

    # ---------------------------------------------------------------
    # Диалоги (список чатов слева)
    # ---------------------------------------------------------------

    async def list_dialogs(self, limit: int = 100) -> list:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")

        out = []
        dialogs = await _with_timeout(client.get_dialogs(limit=limit), "загрузка диалогов")
        for d in dialogs:
            entity = d.entity
            if not isinstance(entity, User) or entity.bot or entity.is_self:
                continue

            info = _status_info(entity.status) if entity.status else {"online": False, "last_seen": None, "last_seen_kind": "unknown"}
            cached = self._presence.setdefault(entity.id, {})
            merged = {**info, **{k: v for k, v in cached.items() if k in ("online", "last_seen", "last_seen_kind")}}
            typing = (time.time() - cached.get("typing_at", 0)) < config.TYPING_TIMEOUT

            last_message = d.message
            preview = None
            preview_kind = "text"
            if last_message:
                media = _media_info(last_message) if last_message.media else None
                preview_kind = media["kind"] if media else "text"
                preview = last_message.message or ""

            out.append({
                "telegram_id": entity.id,
                "name": _user_out(entity)["name"],
                "username": entity.username,
                "phone": getattr(entity, "phone", None),
                "has_photo": bool(entity.photo),
                "last_message_text": preview,
                "last_message_kind": preview_kind,
                "last_message_date": last_message.date if last_message else None,
                "last_message_out": bool(last_message.out) if last_message else False,
                "unread_count": d.unread_count,
                "pinned": d.pinned,
                "online": merged.get("online", False),
                "last_seen": merged.get("last_seen"),
                "last_seen_kind": merged.get("last_seen_kind", "unknown"),
                "typing": typing,
            })
        return out

    async def get_presence(self, telegram_id: int) -> dict:
        await self._get_client()
        cached = self._presence.get(telegram_id, {})
        typing = (time.time() - cached.get("typing_at", 0)) < config.TYPING_TIMEOUT
        if "online" not in cached:
            try:
                client = self._client
                entity = await client.get_entity(telegram_id)
                if entity.status:
                    cached.update(_status_info(entity.status))
            except Exception:
                pass
        return {
            "online": cached.get("online", False),
            "last_seen": cached.get("last_seen"),
            "last_seen_kind": cached.get("last_seen_kind", "unknown"),
            "typing": typing,
        }

    # ---------------------------------------------------------------
    # Аватары
    # ---------------------------------------------------------------

    async def get_avatar_path(self, telegram_id: int) -> Optional[Path]:
        client = await self._get_client()
        existing = list(config.MEDIA_DIR.glob(f"avatar_{telegram_id}.*"))
        if existing:
            return existing[0]
        try:
            path = await client.download_profile_photo(telegram_id, file=str(config.MEDIA_DIR / f"avatar_{telegram_id}"))
        except Exception:
            return None
        return Path(path) if path else None

    # ---------------------------------------------------------------
    # Сообщения
    # ---------------------------------------------------------------

    async def _pinned_message_id(self, client, telegram_id: int) -> Optional[int]:
        try:
            full = await client(GetFullUserRequest(telegram_id))
            return getattr(full.full_user, "pinned_msg_id", None)
        except Exception:
            return None

    async def _read_outbox_max_id(self, client, telegram_id: int) -> Optional[int]:
        """ID последнего НАШЕГО сообщения, которое собеседник уже прочитал.

        Нужен, чтобы честно показывать ✓ (отправлено) vs ✓✓ синим (прочитано) —
        раньше фронтенд рисовал двойную галочку "прочитано" для всех исходящих
        сообщений без исключения, из-за чего статус никогда не менялся.
        """
        try:
            entity = await client.get_entity(telegram_id)
            result = await _with_timeout(
                client(GetPeerDialogsRequest(peers=[InputDialogPeer(entity)])),
                "статус прочтения",
            )
            if result.dialogs:
                return result.dialogs[0].read_outbox_max_id
        except Exception:
            pass
        return None

    async def get_messages(self, telegram_id: int, limit: int = 50) -> list:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")
        try:
            messages = await _with_timeout(client.get_messages(telegram_id, limit=limit), "загрузка переписки")
        except ValueError:
            raise TelegramAuthError("Не удалось найти диалог с этим пользователем в Telegram")

        by_id = {m.id: m for m in messages}
        # Закреплённое сообщение и статус прочтения не зависят друг от друга —
        # раньше шли последовательно (await, потом ещё await), и на медленном
        # канале до Telegram задержки складывались одна к другой, заметно
        # растягивая открытие диалога. Запускаем оба запроса параллельно.
        pinned_id, read_outbox_max_id = await asyncio.gather(
            self._pinned_message_id(client, telegram_id),
            self._read_outbox_max_id(client, telegram_id),
        )

        out = []
        for m in reversed(list(messages)):
            if not m.message and not m.media and not m.action:
                continue
            reply = None
            if m.reply_to_msg_id:
                ref = by_id.get(m.reply_to_msg_id)
                reply = {
                    "id": m.reply_to_msg_id,
                    "text": (ref.message[:80] if ref and ref.message else ("Вложение" if ref and ref.media else "Сообщение")),
                }
            status = None
            if m.out:
                status = "read" if (read_outbox_max_id is not None and m.id <= read_outbox_max_id) else "sent"
            out.append({
                "id": m.id,
                "dialog_id": telegram_id,
                "text": m.message or "",
                "date": m.date,
                "out": bool(m.out),
                "status": status,
                "edited": bool(m.edit_date),
                "pinned": m.id == pinned_id,
                "reply_to": reply,
                "media": _media_info(m) if m.media else None,
            })
        return out

    async def send_message(self, telegram_id: int, text: str, reply_to: Optional[int] = None) -> dict:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")
        try:
            kwargs = {}
            if reply_to:
                kwargs["reply_to"] = reply_to
            msg = await _with_timeout(client.send_message(telegram_id, text, **kwargs), "отправка сообщения")
        except ValueError:
            raise TelegramAuthError("Не удалось найти этого пользователя в Telegram")
        return {
            "id": msg.id, "dialog_id": telegram_id, "text": msg.message or text, "date": msg.date, "out": True,
            "status": "sent",
            "edited": False, "pinned": False,
            "reply_to": {"id": reply_to, "text": ""} if reply_to else None,
            "media": None,
        }

    async def get_entity_vars(self, telegram_id: int) -> dict:
        """Возвращает {name, username, first_name} для подстановки в
        шаблон кампании (см. campaign_service.render_template). При
        любой ошибке резолва отдаёт пустые строки -- рассылка не должна
        падать из-за одного проблемного получателя, это дело
        campaign_service (там ошибка/пропуск логируется по получателю)."""
        try:
            client = await self._get_client()
            entity = await _with_timeout(client.get_entity(telegram_id), "поиск получателя")
            info = _user_out(entity)
            return {
                "name": info["name"] or "",
                "username": info["username"] or "",
                "first_name": getattr(entity, "first_name", None) or "",
            }
        except Exception:
            return {"name": "", "username": "", "first_name": ""}

    async def bulk_send(self, telegram_ids: list[int], text: str) -> None:
        """Отправляет один и тот же текст по списку диалогов с паузой
        BULK_SEND_DELAY_SECONDS между сообщениями. Синхронный обход
        (не asyncio.gather) — пауза должна выдерживаться между каждой
        парой отправок, а не просто ограничивать конкурентность.

        Ошибка на одном диалоге не останавливает рассылку остальных;
        FloodWaitError уважается отдельной паузой сверх обычной задержки,
        чтобы не подставлять аккаунт под более жёсткий бан от Telegram.
        Ничего не пишет в БД и не отслеживает статус — вызывающий код
        (background task) сам решает, что делать с логами.
        """
        total = len(telegram_ids)
        for i, telegram_id in enumerate(telegram_ids, start=1):
            try:
                await self.send_message(telegram_id, text)
                logger.info("bulk_send: отправлено %s/%s -> %s", i, total, telegram_id)
            except FloodWaitError as e:
                logger.warning("bulk_send: FloodWait %sс на %s, ждём и пропускаем", e.seconds, telegram_id)
                await asyncio.sleep(e.seconds)
                continue
            except Exception:
                logger.exception("bulk_send: ошибка отправки на %s", telegram_id)
            if i < total:
                await asyncio.sleep(BULK_SEND_DELAY_SECONDS)

    @staticmethod
    def _build_input_media(cached_file_id: str):
        """Разбирает строку из telegram_utils._cache_file_id обратно в
        InputPhoto/InputDocument, которые Telethon может отправить
        напрямую -- без чтения файла с диска и повторной загрузки его
        байтов на сервер Telegram (раздел ИСТОРИЯ ИСПОЛЬЗОВАНИЯ /
        "использовать telegram_file_id для повторной отправки без
        повторной загрузки файла" ТЗ)."""
        kind, id_, access_hash, file_ref_hex, dc_id = cached_file_id.split(":")
        file_reference = bytes.fromhex(file_ref_hex)
        if kind == "photo":
            return InputPhoto(id=int(id_), access_hash=int(access_hash), file_reference=file_reference)
        return InputDocument(id=int(id_), access_hash=int(access_hash), file_reference=file_reference)

    async def send_file(
        self, telegram_id: int, file_path: str, caption: Optional[str] = None,
        reply_to: Optional[int] = None, voice_note: bool = False, kind: Optional[str] = None,
        cached_file_id: Optional[str] = None,
    ) -> dict:
        """Отправляет вложение корректным методом Telegram API в
        зависимости от типа (раздел ОТПРАВКА ФОТО И ВИДЕО ТЗ):
          - photo -> отправляется как Telegram Photo (сжатое превью,
            открывается во весь экран одним тапом);
          - video/gif -> отправляется как Telegram Video/анимация с
            поддержкой потокового воспроизведения, а не как файл-документ;
          - всё остальное -> обычный документ.

        `kind` — необязательная подсказка ("photo"/"video"/"gif"/
        "document"), обычно приходящая из медиатеки (см.
        media_manager.classify_kind), которая знает тип файла точнее,
        чем расширение само по себе. Если kind не передан, тип
        определяется по расширению файла — так же ведёт себя и
        media_manager, поэтому поведение одинаковое что для файлов из
        галереи, что для вложений "на лету" (обычный чат, вставка из
        буфера, кампании). Единая точка входа для ВСЕХ мест приложения,
        отправляющих вложения — обычных чатов, быстрых сообщений,
        кампаний и очереди отправки (раздел АРХИТЕКТУРА ТЗ).

        `cached_file_id` — необязательный слепок Telethon
        InputPhoto/InputDocument от предыдущей отправки этого же файла
        (см. crud.get_latest_media_file_id / models.MediaUsage.
        telegram_file_id), собранный telegram_utils._cache_file_id.
        Если он передан, файл отправляется этим объектом напрямую, без
        чтения file_path и повторной загрузки байтов на сервер
        Telegram. file_reference в нём живёт около суток — если он уже
        истёк, Telegram ответит FileReferenceExpiredError, и метод
        сам подстрахуется обычной отправкой с диска."""
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")

        if kind is None and not voice_note:
            from . import media_manager
            kind = media_manager.classify_kind(Path(file_path).name).value

        kwargs = {}
        if caption:
            kwargs["caption"] = caption
        if reply_to:
            kwargs["reply_to"] = reply_to
        if voice_note:
            kwargs["voice_note"] = True
        elif kind == "photo":
            kwargs["force_document"] = False
        elif kind == "video":
            kwargs["force_document"] = False
            kwargs["supports_streaming"] = True
        elif kind == "gif":
            from telethon.tl.types import DocumentAttributeAnimated
            kwargs["force_document"] = False
            kwargs["attributes"] = [DocumentAttributeAnimated()]
        elif kind == "document":
            kwargs["force_document"] = True

        file_arg = file_path
        used_cache = False
        normalized_path: Optional[Path] = None
        if cached_file_id:
            try:
                file_arg = self._build_input_media(cached_file_id)
                used_cache = True
            except Exception:
                logger.warning("send_file: не удалось разобрать cached_file_id %r, отправляю с диска", cached_file_id)
                file_arg = file_path

        if not used_cache and kind == "photo" and Path(file_path).suffix.lower() not in (".jpg", ".jpeg", ".png"):
            # Telethon решает "фото или документ" не только по нашему
            # force_document=False, а ЕЩЁ И по собственной проверке
            # расширения файла (utils.is_image) — со своим списком,
            # который уже нашего PHOTO_EXTS в media_manager.py и не
            # знает про .jfif/.webp/.heic/.bmp. Если расширение не из
            # её списка, Telethon отправит файл как обычный документ,
            # ДАЖЕ ПРИ force_document=False — снаружи (в Telegram)
            # это выглядит один в один как исходный баг: карточка
            # файла вместо фото, хотя kind у нас определился верно.
            # Подстраховываемся временной копией с ".jpg" — байты не
            # трогаем, Telegram сам разбирает реальное содержимое при
            # обработке фото на сервере, расширение тут используется
            # только Telethon-ом как локальная эвристика на клиенте.
            try:
                normalized_path = config.UPLOAD_TMP_DIR / f"tg_photo_{uuid.uuid4().hex}.jpg"
                try:
                    os.link(file_path, normalized_path)
                except OSError:
                    shutil.copy2(file_path, normalized_path)
                file_arg = str(normalized_path)
            except Exception:
                logger.warning("send_file: не удалось подготовить временную копию с .jpg для %s", file_path)
                file_arg = file_path
                normalized_path = None

        try:
            try:
                msg = await _with_timeout(
                    client.send_file(telegram_id, file_arg, **kwargs),
                    "отправка файла",
                )
            except FileReferenceExpiredError:
                if not used_cache:
                    raise
                logger.info("send_file: file_reference устарел, повторяю отправку с диска")
                msg = await _with_timeout(
                    client.send_file(telegram_id, file_path, **kwargs),
                    "отправка файла (повторно, с диска)",
                )
        except ValueError:
            raise TelegramAuthError("Не удалось найти этого пользователя в Telegram")
        finally:
            if normalized_path is not None:
                normalized_path.unlink(missing_ok=True)
        return {
            "id": msg.id, "dialog_id": telegram_id, "text": msg.message or "", "date": msg.date, "out": True,
            "status": "sent",
            "edited": False, "pinned": False,
            "reply_to": {"id": reply_to, "text": ""} if reply_to else None,
            "media": _media_info(msg) if msg.media else None,
            "cache_file_id": _cache_file_id(msg) if msg.media else None,
        }

    async def edit_message(self, telegram_id: int, message_id: int, text: str) -> dict:
        client = await self._get_client()
        try:
            msg = await _with_timeout(client.edit_message(telegram_id, message_id, text), "редактирование сообщения")
        except ValueError:
            raise TelegramAuthError("Сообщение не найдено или его нельзя редактировать")
        return {"id": msg.id, "dialog_id": telegram_id, "text": msg.message or text, "date": msg.date, "out": True, "edited": True}

    async def delete_message(self, telegram_id: int, message_id: int, revoke: bool = True) -> None:
        client = await self._get_client()
        await _with_timeout(client.delete_messages(telegram_id, [message_id], revoke=revoke), "удаление сообщения")

    async def pin_message(self, telegram_id: int, message_id: int) -> None:
        client = await self._get_client()
        await _with_timeout(client.pin_message(telegram_id, message_id, notify=False), "закрепление сообщения")

    async def unpin_message(self, telegram_id: int, message_id: Optional[int] = None) -> None:
        client = await self._get_client()
        await _with_timeout(client.unpin_message(telegram_id, message_id), "открепление сообщения")

    async def forward_message(self, from_telegram_id: int, message_id: int, to_telegram_id: int) -> dict:
        client = await self._get_client()
        msgs = await _with_timeout(
            client.forward_messages(to_telegram_id, message_id, from_telegram_id), "пересылка сообщения"
        )
        msg = msgs[0] if isinstance(msgs, list) else msgs
        return {"id": msg.id, "dialog_id": to_telegram_id, "text": msg.message or "", "date": msg.date, "out": True}

    async def mark_read(self, telegram_id: int) -> None:
        client = await self._get_client()
        try:
            await _with_timeout(client.send_read_acknowledge(telegram_id), "отметка о прочтении")
        except Exception:
            pass
        db = SessionLocal()
        try:
            crud.upsert_dialog(db, self.user_id, telegram_id, unread_count=0)
        finally:
            db.close()

    async def download_media(self, telegram_id: int, message_id: int) -> Path:
        client = await self._get_client()
        existing = list(config.MEDIA_DIR.glob(f"{telegram_id}_{message_id}.*"))
        if existing:
            return existing[0]
        msg = await client.get_messages(telegram_id, ids=message_id)
        if not msg or not msg.media:
            raise TelegramAuthError("Сообщение не найдено или не содержит вложения")
        path = await client.download_media(msg, file=str(config.MEDIA_DIR / f"{telegram_id}_{message_id}"))
        if not path:
            raise TelegramAuthError("Не удалось скачать вложение")
        return Path(path)


# ---------------------------------------------------------------
# Реестр экземпляров по пользователям (многопользовательский режим).
#
# По одному живому TelegramService на каждого залогиненного
# пользователя, пока жив процесс — сама Telegram-сессия при этом не
# теряется между процессами/рестартами, потому что персистится в БД
# (см. TelegramService._load_session_string/_persist_session выше).
# Не asyncio.Lock на весь реестр -- одновременный вызов на два разных
# user_id не должен блокировать друг друга, а гонка на один и тот же
# user_id (например, два вкладки одного пользователя одновременно)
# просто создаст, в редком худшем случае, два экземпляра подряд —
# не страшно, второй перезапишет первый в словаре, а следующий вызов
# получит единственный актуальный.
# ---------------------------------------------------------------
_service_registry: dict[int, "TelegramService"] = {}


def get_telegram_service(user_id: int) -> "TelegramService":
    """Возвращает (создавая при необходимости) TelegramService для
    конкретного пользователя CRM. Это основной способ получить сервис
    для нового кода — см. routers/auth.py и Depends(get_current_user)
    в остальных роутерах (миграция роутеров на этот способ — следующий
    этап, см. module docstring)."""
    service = _service_registry.get(user_id)
    if service is None:
        service = TelegramService(user_id)
        _service_registry[user_id] = service
    return service


async def drop_telegram_service(user_id: int) -> None:
    """Отключает и убирает из реестра клиента пользователя — вызывается
    при выходе (logout), чтобы следующий вход начинался с чистого
    клиента, а не с уже разлогиненного объекта в памяти."""
    service = _service_registry.pop(user_id, None)
    if service is not None and service._client is not None:
        try:
            await service._client.disconnect()
        except Exception:
            pass


# Временная подпорка для эндпоинтов, ещё не переведённых на
# get_telegram_service(current_user.id) — см. module docstring выше.
# user_id=None означает "ничью" строку telegram_settings, которая
# существовала до перехода на многопользовательский режим.
telegram_service = TelegramService()
