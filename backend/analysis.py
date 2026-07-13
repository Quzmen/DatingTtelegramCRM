"""
Contact Intelligence — эвристический локальный анализ диалога.

Всё в этом модуле считается локально, без сети и API-ключей, по уже
загруженной истории сообщений (или, если Telegram-переписки нет, по
заметкам/истории CRM). Это ядро остаётся источником истины для
interest_score и предлагаемого статуса даже если включён LLM-слой
(см. ai_gemini.py и config.AI_PROVIDER) — LLM умеет только красивее
переписать summary/next_action и предложить черновик ответа поверх
уже посчитанных здесь сигналов, но не саму оценку.

Главный принцип, зафиксированный в ТЗ: "не делать выводы только по
словам, основной вес — действия пользователя". Поэтому в скоринге
поведенческие сигналы (кто пишет первым, скорость ответа, длина
сообщений, вопросы, инициатива встречи) весят намного больше, чем
эмоциональный тон текста.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import models

# Слова-маркеры инициативы встречи. Список нарочно короткий и общий —
# это не текстовый детектор с претензией на точность, а грубый сигнал,
# который лишь чуть подправляет поведенческий скоринг.
MEETING_KEYWORDS = [
    "встрет", "увидимся", "погуля", "созвон", "кафе", "приезж",
    "приход", "выберемся", "заеду", "заедешь", "свидан", "погулять",
]

# Лёгкий сигнал тёплого тона: несколько распространённых эмодзи и
# восклицательный знак. Это НЕ детектор флирта по ключевым фразам —
# просто маленький второстепенный вес в общей формуле.
WARM_EMOJI = ["❤", "😍", "🥰", "😘", "🔥", "😊", "🙂", "😉", "💕", "💗"]

SCORE_CATEGORIES = [
    (20, "Холодная"),
    (50, "Тёплая"),
    (80, "Высокий интерес"),
    (100, "Очень высокий интерес"),
]

# ---- Тренд ("затухает"/"растёт") ----
#
# Переписка делится пополам по времени, для каждой половины считается
# упрощённый window-скор (см. _window_score), и сравнивается разница.
# Это НЕ то же самое, что основной interest_score (там часть сигналов,
# например их-инициатива по паузам, требует контекста всей переписки
# целиком и не может быть честно посчитана на половине) — это отдельная,
# более грубая метрика, нужная только для сравнения "было / стало".
TREND_MIN_INCOMING_PER_HALF = 3   # меньше — трендом не считаем, слишком шумно
TREND_FLAT_THRESHOLD = 8.0        # |delta| меньше этого — считаем "стабильно"


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Приводит любую дату (aware/naive) к naive UTC, чтобы её можно
    было безопасно сравнивать с datetime.utcnow(), которым пользуется
    остальной проект."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _category(score: int) -> str:
    for ceiling, label in SCORE_CATEGORIES:
        if score <= ceiling:
            return label
    return SCORE_CATEGORIES[-1][1]


def _window_score(window_messages: List[dict]) -> Optional[float]:
    """Упрощённая версия скоринга для одной половины переписки — только
    для сравнения "было / стало" в _trend(), не для показа пользователю
    напрямую. От основной формулы в analyze() отличается тем, что вместо
    их-инициативы по паузам (которая честно считается только на полном
    диалоге — ей нужен контекст соседних сообщений за его пределами)
    берётся более грубый прокси: доля входящих сообщений внутри самого
    окна."""
    incoming = [m for m in window_messages if not m["out"]]
    if not incoming:
        return None
    total = len(window_messages)

    their_share = len(incoming) / total if total else 0.0

    response_deltas = []
    for i in range(1, len(window_messages)):
        if (not window_messages[i]["out"]) and window_messages[i - 1]["out"]:
            delta = (window_messages[i]["date"] - window_messages[i - 1]["date"]).total_seconds()
            if 0 < delta < 48 * 3600:
                response_deltas.append(delta)
    avg_response_min = (sum(response_deltas) / len(response_deltas) / 60) if response_deltas else None

    avg_len = sum(len(m["text"]) for m in incoming) / len(incoming)
    question_ratio = sum(1 for m in incoming if "?" in m["text"]) / len(incoming)
    warm_ratio = sum(
        1 for m in incoming if any(e in m["text"] for e in WARM_EMOJI) or "!" in m["text"]
    ) / len(incoming)

    score = 0.0
    score += min(30.0, their_share * 60)                                 # доля входящих в окне — прокси инициативы
    if avg_response_min is not None:
        score += max(0.0, 20.0 - min(avg_response_min, 600) / 30)
    else:
        score += 8.0
    score += min(15.0, avg_len / 8)
    score += min(10.0, question_ratio * 20)
    score += min(10.0, warm_ratio * 15)

    return max(0.0, min(85.0, score))


def _trend(messages: List[dict]) -> Dict:
    """Сравнивает вторую половину переписки с первой и определяет
    направление: интерес растёт / стабилен / затухает. Возвращает
    direction ("up"/"flat"/"down"/"unknown"), человекочитаемый label и
    числовую delta (None, если данных недостаточно для вывода)."""
    mid = len(messages) // 2
    earlier, recent = messages[:mid], messages[mid:]

    earlier_incoming = sum(1 for m in earlier if not m["out"])
    recent_incoming = sum(1 for m in recent if not m["out"])
    if earlier_incoming < TREND_MIN_INCOMING_PER_HALF or recent_incoming < TREND_MIN_INCOMING_PER_HALF:
        return {"direction": "unknown", "label": "Пока мало данных для тренда", "delta": None}

    earlier_score = _window_score(earlier)
    recent_score = _window_score(recent)
    if earlier_score is None or recent_score is None:
        return {"direction": "unknown", "label": "Пока мало данных для тренда", "delta": None}

    delta = round(recent_score - earlier_score, 1)
    if delta >= TREND_FLAT_THRESHOLD:
        return {"direction": "up", "label": "Интерес растёт", "delta": delta}
    if delta <= -TREND_FLAT_THRESHOLD:
        return {"direction": "down", "label": "Похоже на затухание", "delta": delta}
    return {"direction": "flat", "label": "Стабильно", "delta": delta}


def normalize_messages(raw_messages: List[dict]) -> List[dict]:
    """Приводит сырые сообщения telegram_service.get_messages() к единому
    виду (text/date/out) и сортирует по времени. Вынесено отдельно, чтобы
    LLM-слой (ai_gemini.py) мог переиспользовать те же сообщения, что и
    локальная эвристика, без повторной логики нормализации."""
    messages = [
        {"text": m.get("text") or "", "date": _naive(m.get("date")), "out": bool(m.get("out"))}
        for m in raw_messages
        if m.get("date") is not None
    ]
    messages.sort(key=lambda m: m["date"])
    return messages


def analyze(contact: models.Contact, messages: List[dict]) -> Dict:
    """Считает интерес, предлагает статус, следующее действие и summary.

    messages — уже нормализованный список (см. normalize_messages), в
    хронологическом порядке.
    """
    if not messages:
        return _fallback_no_history(contact)

    incoming = [m for m in messages if not m["out"]]
    if not incoming:
        return _no_reply_result(contact, messages)

    total = len(messages)

    # 1. Кто чаще пишет первым: считаем "новый заход в разговор" как
    #    сообщение после паузы дольше 4 часов.
    GAP_SECONDS = 4 * 3600
    initiations = {"me": 0, "them": 0}
    prev = None
    for m in messages:
        if prev is None or (m["date"] - prev["date"]).total_seconds() > GAP_SECONDS:
            initiations["them" if not m["out"] else "me"] += 1
        prev = m
    total_init = initiations["me"] + initiations["them"]
    their_initiative_ratio = (initiations["them"] / total_init) if total_init else 0.0

    # 2. Скорость ответа контакта на наши сообщения (в минутах).
    response_deltas = []
    for i in range(1, len(messages)):
        if (not messages[i]["out"]) and messages[i - 1]["out"]:
            delta = (messages[i]["date"] - messages[i - 1]["date"]).total_seconds()
            if 0 < delta < 48 * 3600:
                response_deltas.append(delta)
    avg_response_min = (sum(response_deltas) / len(response_deltas) / 60) if response_deltas else None

    # 3. Средняя длина их сообщений.
    avg_len_them = sum(len(m["text"]) for m in incoming) / len(incoming)

    # 4. Доля сообщений с вопросами (встречный интерес).
    question_ratio = sum(1 for m in incoming if "?" in m["text"]) / len(incoming)

    # 5. Инициатива встречи — сколько раз контакт сам поднимал тему.
    meeting_mentions = sum(
        1 for m in incoming if any(k in m["text"].lower() for k in MEETING_KEYWORDS)
    )

    # 6. Тёплый тон — второстепенный, малый вес по ТЗ.
    warm_ratio = sum(
        1 for m in incoming if any(e in m["text"] for e in WARM_EMOJI) or "!" in m["text"]
    ) / len(incoming)

    # ---- Скоринг: поведение весит намного больше слов ----
    score = 0.0
    score += min(30.0, their_initiative_ratio * 30)                      # кто пишет первым — до 30
    if avg_response_min is not None:
        score += max(0.0, 20.0 - min(avg_response_min, 600) / 30)        # скорость ответа — до 20
    else:
        score += 8.0                                                     # нет данных — нейтрально
    score += min(15.0, avg_len_them / 8)                                  # длина сообщений — до 15
    score += min(10.0, question_ratio * 20)                               # вопросы — до 10
    score += min(15.0, meeting_mentions * 5)                              # инициатива встречи — до 15
    score += min(10.0, warm_ratio * 15)                                   # тон/эмодзи — до 10, второстепенно

    score_int = int(round(max(0.0, min(100.0, score))))
    category = _category(score_int)

    suggested_status = _suggest_status(contact, score_int, messages, meeting_mentions)
    next_action = _next_action(contact, messages, score_int)
    summary = _summary(contact, messages, meeting_mentions, avg_response_min)
    trend = _trend(messages)

    return {
        "interest_score": score_int,
        "interest_category": category,
        "suggested_status": suggested_status,
        "next_action": next_action,
        "ai_summary": summary,
        "suggested_reply": None,
        "ai_source": "local",
        "messages_analyzed": total,
        "trend": trend,
        "signals": {
            "their_initiative_ratio": round(their_initiative_ratio, 2),
            "avg_response_minutes": round(avg_response_min, 1) if avg_response_min is not None else None,
            "avg_message_length": round(avg_len_them, 1),
            "question_ratio": round(question_ratio, 2),
            "meeting_mentions": meeting_mentions,
        },
    }


def _suggest_status(contact, score: int, messages: List[dict], meeting_mentions: int) -> models.ContactStatus:
    # Финальные статусы (были на встрече / архив) — не трогаем автоматически,
    # это осознанные решения пользователя, а не то, что можно вывести из переписки.
    if contact.status in (models.ContactStatus.MET, models.ContactStatus.ARCHIVE):
        return contact.status

    if score >= 81 and meeting_mentions >= 2:
        return models.ContactStatus.MEETING_SCHEDULED
    if score >= 51:
        return models.ContactStatus.IN_PROGRESS
    if score >= 21:
        return models.ContactStatus.WARM
    return models.ContactStatus.NEW


def _next_action(contact, messages: List[dict], score: int) -> str:
    last = messages[-1]
    days_since = max(0, (datetime.utcnow() - last["date"]).days)

    if last["out"]:
        if days_since == 0:
            return "Подождать — последнее слово за вами"
        if days_since == 1:
            return "Подождать ответа ещё немного"
        return "Написать первым — давно нет ответа"

    # последнее сообщение от контакта
    if days_since == 0:
        return "Ответить на сообщение"
    if score >= 60:
        return "Предложить встречу"
    if days_since == 1:
        return "Написать первым завтра"
    return "Написать первым"


def _summary(contact, messages: List[dict], meeting_mentions: int, avg_response_min: Optional[float]) -> str:
    first_date = messages[0]["date"]
    last_date = messages[-1]["date"]
    days_span = max(1, (last_date - first_date).days)
    days_since_last = max(0, (datetime.utcnow() - last_date).days)

    parts = []
    parts.append("Общение началось сегодня." if days_span <= 1 else f"Общаетесь уже {days_span} дн.")

    if avg_response_min is not None:
        if avg_response_min < 30:
            parts.append("Отвечает быстро и стабильно.")
        elif avg_response_min < 240:
            parts.append("Отвечает в течение нескольких часов.")
        else:
            parts.append("Отвечает с заметной задержкой.")
    else:
        parts.append("Ответных сообщений пока мало для оценки скорости ответа.")

    if meeting_mentions:
        parts.append("Была инициатива встречи." if meeting_mentions == 1 else "Несколько раз поднимала(-ал) тему встречи.")

    who = "Вы" if messages[-1]["out"] else (contact.name or "Контакт")
    when = "сегодня" if days_since_last == 0 else ("вчера" if days_since_last == 1 else f"{days_since_last} дн. назад")
    parts.append(f"Последнее сообщение — {when}, писал(а) {who}.")

    return " ".join(parts)


def _no_reply_result(contact, messages: List[dict]) -> Dict:
    last = messages[-1]
    days_since = max(0, (datetime.utcnow() - last["date"]).days)
    return {
        "interest_score": 0,
        "interest_category": "Холодная",
        "suggested_status": contact.status,
        "next_action": "Подождать" if days_since < 3 else "Написать ещё раз или перевести в архив",
        "ai_summary": "Контакт пока ни разу не ответил(а) на сообщения.",
        "suggested_reply": None,
        "ai_source": "local",
        "trend": {"direction": "unknown", "label": "Пока мало данных для тренда", "delta": None},
        "messages_analyzed": len(messages),
        "signals": {
            "their_initiative_ratio": 0,
            "avg_response_minutes": None,
            "avg_message_length": 0,
            "question_ratio": 0,
            "meeting_mentions": 0,
        },
    }


def _fallback_no_history(contact) -> Dict:
    """Нет переписки Telegram вообще (контакт не привязан, либо ещё
    не открывали диалог). Даём консервативную оценку по тому, что уже
    есть в CRM, чтобы карточка не оставалась пустой."""
    score = max(0, min(100, (contact.interest_level or 0) * 10))
    return {
        "interest_score": score,
        "interest_category": _category(score),
        "suggested_status": contact.status,
        "next_action": contact.next_task or "Добавить первую запись или написать в Telegram",
        "ai_summary": "Переписка в Telegram недоступна — оценка приблизительная, по ручным данным CRM.",
        "suggested_reply": None,
        "ai_source": "local",
        "trend": {"direction": "unknown", "label": "Пока мало данных для тренда", "delta": None},
        "messages_analyzed": 0,
        "signals": {
            "their_initiative_ratio": None,
            "avg_response_minutes": None,
            "avg_message_length": None,
            "question_ratio": None,
            "meeting_mentions": None,
        },
    }
