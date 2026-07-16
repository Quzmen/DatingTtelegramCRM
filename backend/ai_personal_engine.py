"""
Personal AI Operating System — интеллектуальный слой поверх CRM,
который анализирует ДАННЫЕ САМОГО ПОЛЬЗОВАТЕЛЯ: его заметки, задачи,
договорённости, повторяющиеся привычки и решения. Модуль сознательно
НЕ делает того, что делает Contact Intelligence (ai_gemini.py) —
не оценивает и не предсказывает поведение, интерес или реакцию
других людей. Если контакт упоминается в записи памяти, это только
контекст ("встреча с другом"), а не объект анализа.

Три сервиса:

  extract_memory()   — Event Extraction Service + AI Memory: из текста
                        пользователя достаёт факты о НЁМ (планы,
                        договорённости, предпочтения) + проверяет
                        конфликты по времени с уже сохранённой памятью.
  analyze_patterns()  — Pattern Analyzer: ищет закономерности в
                        собственной активности пользователя (когда
                        выполняет задачи, что чаще откладывает).
  build_decision_tree() — Decision Tree Engine: для ситуации выбора,
                        которую описал пользователь, и его же
                        вариантов действий — раскладывает вероятные
                        последствия ДЛЯ НЕГО САМОГО. Никогда не
                        оценивает, как поступит третье лицо.

Как и ai_gemini.py: любая ошибка Gemini (нет ключа, сеть, невалидный
JSON) — тихий откат на простой локальный результат, ничего не падает.
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import ai_gemini, config, models

logger = logging.getLogger(__name__)


MEMORY_SYSTEM_PROMPT = """Ты — модуль персональной памяти внутри CRM пользователя. \
Тебе дают свободный текст, который пользователь написал о СЕБЕ: заметку, \
описание задачи или дневниковую запись. Твоя задача — достать из него \
структурированные факты ТОЛЬКО о самом пользователе: его планы, \
договорённости, обещания, предпочтения, важные события. \

Строго соблюдай:
- Не додумывай ничего, чего нет в тексте.
- Не делай выводов о характере, мотивах или вероятном поведении других \
людей, упомянутых в тексте — если кто-то упомянут, это только контекст \
события (например "встреча с Иваном"), не объект анализа.
- Если в тексте нет ничего похожего на факт/событие/план — верни пустой \
список items.

Для каждого найденного факта укажи:
- kind: одно из "event" (разовое событие с датой), "commitment" \
(обещание/договорённость пользователя), "plan" (план на будущее без \
точной даты), "preference" (личное предпочтение), "fact" (прочее).
- title: короткая (до 80 символов) формулировка.
- details: 1 предложение с уточнением, если есть.
- related_at: дата/время события в формате ISO 8601, если есть в тексте \
(с учётом того, что сегодня {today}), иначе null.
- importance: число 0..1, насколько это важно запомнить.

Ответь СТРОГО валидным JSON без markdown, вот такой формы:
{"items": [{"kind": "...", "title": "...", "details": "...", \
"related_at": null, "importance": 0.5}]}"""


PATTERN_SYSTEM_PROMPT = """Ты — модуль анализа личных паттернов внутри CRM \
пользователя. Тебе дают агрегированные данные о СОБСТВЕННОЙ активности \
пользователя: когда он завершает задачи, какие задачи откладывает, когда \
создаёт события. Найди закономерности в ЕГО ЖЕ поведении — не в поведении \
других людей (в данных других людей нет и не будет).

Верни до 5 закономерностей. Для каждой:
- title: короткая формулировка ("Чаще завершает задачи по утрам").
- description: 1-2 предложения объяснения.
- confidence: 0..1, насколько уверенно закономерность подтверждается данными.
- evidence: список из 1-3 коротких фактов, на которых основан вывод.

Если данных мало для выводов — верни пустой список patterns, не выдумывай.

Ответь СТРОГО валидным JSON без markdown:
{"patterns": [{"title": "...", "description": "...", "confidence": 0.5, \
"evidence": ["..."]}]}"""


DECISION_SYSTEM_PROMPT = """Ты — модуль поддержки принятия решений внутри \
CRM пользователя. Пользователь описывает ситуацию выбора и несколько \
вариантов действий. Для КАЖДОГО варианта разложи вероятные плюсы, минусы и \
последствия ЛИЧНО ДЛЯ ПОЛЬЗОВАТЕЛЯ (его время, энергия, прогресс по его \
целям). Ты НЕ советуешь, какой вариант выбрать, и НЕ оцениваешь, как на \
это отреагируют другие люди — только раскладываешь последствия действия \
для самого пользователя.

Для каждого варианта:
- label: тот же текст варианта, что дал пользователь.
- pros: до 3 пунктов, что даёт этот вариант.
- cons: до 3 пунктов, чем приходится жертвовать.
- consequences: до 3 пунктов о вероятных последствиях в перспективе \
нескольких дней/недель (не про вероятности в процентах — просто трезвые \
наблюдения).

Ответь СТРОГО валидным JSON без markdown:
{"options": [{"label": "...", "pros": [], "cons": [], "consequences": []}]}"""


def _model_for(complexity: str) -> str:
    """Flash для быстрых операций (извлечение памяти, классификация),
    Pro для более сложных (паттерны, дерево решений) — см. п.3 ТЗ."""
    if complexity == "deep":
        return config.GEMINI_MODEL_PRO
    return config.GEMINI_MODEL


async def _call(system_prompt: str, user_content: str, complexity: str = "fast") -> Optional[dict]:
    if config.AI_PROVIDER != "gemini" or not config.GEMINI_API_KEY:
        return None
    try:
        import httpx
    except ImportError:
        return None

    # Переиспользуем URL-шаблон/парсинг ai_gemini, но с отдельным
    # вызовом: модель для "глубоких" задач отличается, а временно
    # подменять config.GEMINI_MODEL было бы не потокобезопасно.
    url = ai_gemini.GEMINI_URL_TMPL.format(model=_model_for(complexity))
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    headers = {"Content-Type": "application/json", "X-goog-api-key": config.GEMINI_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=config.AI_LLM_TIMEOUT, proxy=config.HTTP_PROXY_URL) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            logger.warning("Personal AI engine: Gemini HTTP %s", response.status_code)
            return None
        data = response.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _parse(raw_text)
    except Exception as exc:  # сеть/таймаут/невалидный JSON — тихий откат
        logger.warning("Personal AI engine: откат на локальный результат (%s)", exc)
        return None


def _parse(raw_text: str) -> dict:
    from . import ai_common
    return ai_common.parse_json_reply(raw_text)


def _parse_dt(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ---------------------------------------------------------------
# 1. AI Memory / Event Extraction Service
# ---------------------------------------------------------------

async def extract_memory(text: str, contact_id: Optional[int] = None) -> Dict:
    """Достаёт факты о пользователе из свободного текста. Всегда
    возвращает dict с items (может быть пустым) — при недоступности
    Gemini делает простой локальный откат (одна запись типа fact с
    исходным текстом целиком), а не падает."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    prompt = MEMORY_SYSTEM_PROMPT.format(today=today)
    parsed = await _call(prompt, text, complexity="fast")

    if parsed and isinstance(parsed.get("items"), list):
        items = []
        for raw in parsed["items"][:10]:
            kind = str(raw.get("kind") or "fact").strip()
            if kind not in {k.value for k in models.AIMemoryKind}:
                kind = "fact"
            title = str(raw.get("title") or "").strip()[:300]
            if not title:
                continue
            items.append({
                "kind": kind,
                "title": title,
                "details": (str(raw.get("details") or "").strip() or None),
                "related_at": _parse_dt(raw.get("related_at")),
                "importance": max(0.0, min(1.0, float(raw.get("importance") or 0.5))),
            })
        return {"items": items, "source": "gemini"}

    # Локальный откат: не пытаемся имитировать NLP-извлечение дат —
    # честно сохраняем текст одной записью, чтобы пользователь ничего
    # не потерял, и явно помечаем source="local".
    stripped = text.strip()
    if not stripped:
        return {"items": [], "source": "local"}
    return {
        "items": [{
            "kind": "fact",
            "title": stripped[:300],
            "details": None,
            "related_at": None,
            "importance": 0.5,
        }],
        "source": "local",
    }


def find_time_conflicts(db, related_at: datetime, exclude_id: Optional[int] = None,
                          window_minutes: int = 60) -> List[str]:
    """Локальная (не-AI) проверка: есть ли уже что-то запланированное
    в пределах window_minutes от related_at — п.6 ТЗ ("в это время уже
    существует договорённость"). Детерминированная, без Gemini."""
    if related_at is None:
        return []
    lo = related_at - timedelta(minutes=window_minutes)
    hi = related_at + timedelta(minutes=window_minutes)
    q = db.query(models.AIMemoryItem).filter(
        models.AIMemoryItem.related_at.isnot(None),
        models.AIMemoryItem.related_at >= lo,
        models.AIMemoryItem.related_at <= hi,
    )
    if exclude_id is not None:
        q = q.filter(models.AIMemoryItem.id != exclude_id)
    conflicts = []
    for item in q.all():
        when = item.related_at.strftime("%d.%m %H:%M")
        conflicts.append(f"На {when} уже есть запись «{item.title}» — возможно пересечение по времени.")
    return conflicts


# ---------------------------------------------------------------
# 2. Pattern Analyzer
# ---------------------------------------------------------------

async def analyze_patterns(db) -> List[Dict]:
    """Смотрит на собственную активность пользователя (память +
    задачи + напоминания контактов, т.к. это тоже действия
    пользователя) и ищет закономерности. Только агрегаты, без сырых
    личных текстов третьих лиц."""
    memory_items = db.query(models.AIMemoryItem).order_by(
        models.AIMemoryItem.created_at.desc()
    ).limit(200).all()

    if len(memory_items) < 5:
        return []

    by_hour = {}
    by_kind = {}
    done_delays = []
    for item in memory_items:
        hour = item.created_at.hour
        by_hour[hour] = by_hour.get(hour, 0) + 1
        by_kind[item.kind.value if hasattr(item.kind, "value") else item.kind] = \
            by_kind.get(item.kind.value if hasattr(item.kind, "value") else item.kind, 0) + 1
        if item.is_done and item.related_at:
            done_delays.append((item.updated_at - item.related_at).total_seconds())

    summary = {
        "total_items": len(memory_items),
        "by_hour_of_creation": by_hour,
        "by_kind": by_kind,
        "open_commitments": sum(
            1 for i in memory_items
            if i.kind == models.AIMemoryKind.COMMITMENT and not i.is_done
        ),
    }

    parsed = await _call(PATTERN_SYSTEM_PROMPT, json.dumps(summary, ensure_ascii=False), complexity="deep")
    if parsed and isinstance(parsed.get("patterns"), list):
        out = []
        for raw in parsed["patterns"][:5]:
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "title": title[:300],
                "description": (str(raw.get("description") or "").strip() or None),
                "confidence": max(0.0, min(1.0, float(raw.get("confidence") or 0.5))),
                "evidence": [str(e).strip() for e in (raw.get("evidence") or []) if str(e).strip()][:3],
            })
        return out

    # Локальный откат: одна простая закономерность по часу активности,
    # без Gemini-интерпретации — честно и без выдумывания причин.
    if by_hour:
        peak_hour = max(by_hour, key=by_hour.get)
        return [{
            "title": f"Больше всего записей появляется около {peak_hour}:00",
            "description": "Посчитано локально по времени создания записей памяти.",
            "confidence": 0.4,
            "evidence": [f"{by_hour[peak_hour]} записей создано в этот час за последнее время"],
        }]
    return []


# ---------------------------------------------------------------
# 3. Decision Tree Engine
# ---------------------------------------------------------------

async def build_decision_tree(situation: str, options: List[str]) -> Dict:
    """Раскладывает плюсы/минусы/последствия вариантов, которые сам
    пользователь предложил для своей же ситуации. Никогда не строит
    вероятности того, как поступит другой человек."""
    user_content = f"Ситуация: {situation}\n\nВарианты:\n" + "\n".join(f"- {o}" for o in options)
    parsed = await _call(DECISION_SYSTEM_PROMPT, user_content, complexity="deep")

    if parsed and isinstance(parsed.get("options"), list) and parsed["options"]:
        out = []
        by_label = {str(o.get("label") or "").strip(): o for o in parsed["options"]}
        for label in options:  # сохраняем порядок и формулировки пользователя
            raw = by_label.get(label, {})
            out.append({
                "label": label,
                "pros": [str(x).strip() for x in (raw.get("pros") or []) if str(x).strip()][:3],
                "cons": [str(x).strip() for x in (raw.get("cons") or []) if str(x).strip()][:3],
                "consequences": [str(x).strip() for x in (raw.get("consequences") or []) if str(x).strip()][:3],
            })
        return {"options": out, "source": "gemini"}

    # Локальный откат: пустые плюсы/минусы, просто сохраняем варианты
    # как есть, чтобы пользователь мог заполнить вручную.
    return {
        "options": [{"label": o, "pros": [], "cons": [], "consequences": []} for o in options],
        "source": "local",
    }
