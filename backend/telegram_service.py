"""
Обёртка над Telethon: один клиент на всё приложение, авторизованный
под тем Telegram-аккаунтом, из которого пользователь входит через
экран "Telegram" в интерфейсе.

CRM работает только с одним авторизованным аккаунтом одновременно —
это не мультиаккаунтный сервис.

Сессия Telethon хранится не в файле на диске (файловая система
контейнера на Render эфемерна и сбрасывается при каждом
рестарте/редеплое), а как StringSession в таблице telegram_settings
той же БД, что и остальные данные CRM (см. crud.py,
database.get_db/SessionLocal). После успешного входа переавторизация
не требуется, пока пользователь сам не нажмёт "выйти" — сессия
переживает перезапуски и пересборку контейнера.

Помимо базовой авторизации и отправки текста, сервис умеет то, что
нужно полноценному мессенджеру поверх Telegram: диалоги, вложения
(фото/голосовые/документы), ответы, пересылку, редактирование,
удаление, закрепление и "живые" статусы (онлайн / последний визит /
печатает…) через фоновый обработчик событий Telethon.
"""
import asyncio
import time
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
)
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.messages import GetPeerDialogsRequest
from telethon.tl.types import (
    InputDialogPeer,
    User,
    UserStatusOnline,
    UserStatusOffline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
)

from . import config, crud
from .database import SessionLocal


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


def _status_info(status) -> dict:
    """Приводит UserStatus* Telethon к простому и сериализуемому виду."""
    if isinstance(status, UserStatusOnline):
        return {"online": True, "last_seen": None, "last_seen_kind": "online"}
    if isinstance(status, UserStatusOffline):
        return {"online": False, "last_seen": status.was_online, "last_seen_kind": "exact"}
    if isinstance(status, UserStatusRecently):
        return {"online": False, "last_seen": None, "last_seen_kind": "recently"}
    if isinstance(status, UserStatusLastWeek):
        return {"online": False, "last_seen": None, "last_seen_kind": "last_week"}
    if isinstance(status, UserStatusLastMonth):
        return {"online": False, "last_seen": None, "last_seen_kind": "last_month"}
    return {"online": False, "last_seen": None, "last_seen_kind": "unknown"}


def _user_out(user: User) -> dict:
    name = " ".join(p for p in [user.first_name, user.last_name] if p) or user.username or str(user.id)
    return {
        "telegram_id": user.id,
        "name": name,
        "username": user.username,
        "phone": getattr(user, "phone", None),
    }


def _media_info(m) -> Optional[dict]:
    """Достаёт из Telethon-сообщения тип вложения и метаданные для UI.

    Порядок проверок важен: video_note и gif — это документы с особыми
    атрибутами, которые Telethon даёт через отдельные bool-шорткаты
    (m.video_note / m.gif), и их нужно проверить ДО общих m.video /
    m.document, иначе кружки и гифки определятся как обычное видео/файл.
    """
    if m.photo:
        return {"kind": "photo", "file_name": None, "size": None, "mime": "image/jpeg", "duration": None}
    if m.video_note:
        duration = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        return {"kind": "video_note", "file_name": None, "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "video/mp4", "duration": duration}
    if m.gif:
        return {"kind": "animation", "file_name": m.file.name if m.file else None,
                "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "video/mp4", "duration": None}
    if m.voice:
        duration = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        return {"kind": "voice", "file_name": None, "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "audio/ogg", "duration": duration}
    if m.audio:
        duration = None
        title = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        try:
            title = m.file.title or m.file.name
        except Exception:
            pass
        return {"kind": "audio", "file_name": title or "Аудио", "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "audio/mpeg", "duration": duration}
    if m.video:
        duration = None
        try:
            duration = m.file.duration
        except Exception:
            pass
        return {"kind": "video", "file_name": m.file.name if m.file else None,
                "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "video/mp4", "duration": duration}
    if m.sticker:
        mime = None
        try:
            mime = m.file.mime_type
        except Exception:
            pass
        return {"kind": "sticker", "file_name": None, "size": None, "mime": mime, "duration": None}
    if m.document:
        return {"kind": "document", "file_name": m.file.name if m.file else "Файл",
                "size": m.file.size if m.file else None,
                "mime": m.file.mime_type if m.file else "application/octet-stream", "duration": None}
    return None


class TelegramService:
    def __init__(self):
        self._client: Optional[TelegramClient] = None
        self._lock = asyncio.Lock()
        self._phone: Optional[str] = None
        self._phone_code_hash: Optional[str] = None

        # "Живые" статусы собеседников, наполняются фоновым обработчиком
        # событий Telethon (см. _register_handlers). Ключ -- telegram_id.
        self._presence: dict = {}
        self._handlers_registered = False

    @staticmethod
    def _load_session_string() -> str:
        """Читает сохранённую StringSession из БД. Пустая строка —
        как и пустой файл сессии раньше — означает "аккаунт ещё не
        авторизован", Telethon в этом случае создаёт новую сессию."""
        db = SessionLocal()
        try:
            return crud.get_telegram_session_string(db) or ""
        finally:
            db.close()

    def _persist_session(self) -> None:
        """Сохраняет текущую StringSession клиента в БД — вызывается
        сразу после успешного /api/telegram/sign-in, чтобы сессия
        пережила следующий рестарт/редеплой контейнера на Render."""
        if self._client is None:
            return
        session_string = self._client.session.save()
        if not session_string:
            return
        db = SessionLocal()
        try:
            crud.save_telegram_session_string(db, session_string)
        finally:
            db.close()

    @staticmethod
    def _clear_persisted_session() -> None:
        db = SessionLocal()
        try:
            crud.clear_telegram_session_string(db)
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
        """Подписывается на события "печатает"/"в сети" один раз за
        время жизни клиента, чтобы UI мог опрашивать /presence и
        получать актуальные статусы без постоянных запросов к Telegram.
        """
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
        pinned_id = await self._pinned_message_id(client, telegram_id)
        read_outbox_max_id = await self._read_outbox_max_id(client, telegram_id)

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

    async def send_file(
        self, telegram_id: int, file_path: str, caption: Optional[str] = None,
        reply_to: Optional[int] = None, voice_note: bool = False,
    ) -> dict:
        client = await self._get_client()
        if not await _with_timeout(client.is_user_authorized(), "проверка авторизации"):
            raise TelegramAuthError("Аккаунт не авторизован")
        kwargs = {}
        if caption:
            kwargs["caption"] = caption
        if reply_to:
            kwargs["reply_to"] = reply_to
        if voice_note:
            kwargs["voice_note"] = True
        try:
            msg = await _with_timeout(
                client.send_file(telegram_id, file_path, **kwargs),
                "отправка файла",
            )
        except ValueError:
            raise TelegramAuthError("Не удалось найти этого пользователя в Telegram")
        return {
            "id": msg.id, "dialog_id": telegram_id, "text": msg.message or "", "date": msg.date, "out": True,
            "status": "sent",
            "edited": False, "pinned": False,
            "reply_to": {"id": reply_to, "text": ""} if reply_to else None,
            "media": _media_info(msg) if msg.media else None,
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


telegram_service = TelegramService()
