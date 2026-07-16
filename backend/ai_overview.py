"""
AI Overview — интеллектуальный анализ ситуации по конкретному контакту.

В отличие от Contact Intelligence (ai_gemini.py: interest_score, deep-report)
это не очередная метрика и не статистика активности CRM. AI Overview
собирает всё, что известно о ситуации с контактом — заметки и события
пользователя (AIMemoryItem/Interaction), историю переписки в Telegram и
предыдущие AI-анализы (deep_report, прошлые снимки AI Overview) — и строит
из этого структурированную картину: текущее состояние + несколько
возможных сценариев развития, каждый с признаками "за" и с тем, что могло
бы сценарий изменить.

Явно ЗАПРЕЩЕНО (см. системный промпт ниже):
- искать/использовать статистику активности CRM (когда создавались записи,
  сколько их, в какое время работает пользователь) — это не относится к
  задаче и не должно попадать в анализ;
- выдавать предположения как установленный факт;
- определять "истинные намерения" другого человека — только наблюдаемые
  признаки и то, как их можно объяснять;
- инструкции по контролю/манипуляции другим человеком — модуль
  аналитический, а не система управления поведением людей.

Отдельно от исходной идеи ТЗ: модуль НЕ подавляет этическую реакцию модели.
Если из самих фактов явно следует что-то тревожное (риск для пользователя
или признаки, которые стоит бережно назвать), модель должна это сказать —
запрещены только огульные, ничем не подкреплённые обвинительные ярлыки.
Разница в промпте: не "никогда не оценивай", а "не обвиняй без опоры на
факты, и если предупреждаешь — предупреждай на основании конкретных
признаков, а не с ходу".

Как и остальной AI-слой (ai_gemini.py, ai_personal_engine.py): любая ошибка
Gemini — тихий (но помеченный source="local") откат на упрощённый
локальный результат, ничего не падает.
"""
import json
import logging
from typing import Dict, List, Optional

from . import ai_common, ai_gemini, config, models

logger = logging.getLogger(__name__)


OVERVIEW_SYSTEM_PROMPT = """Ты — аналитический модуль AI Overview внутри \
локальной CRM для личных знакомств из Telegram. Твоя задача — не обычный \
чат и не подсчёт статистики CRM, а построение картины того, как развивается \
ситуация с конкретным контактом, на основе всех данных, которые тебе дали.

Тебе на вход дают до трёх источников:
1. Факты и события пользователя (заметки, ручные записи, хронология).
2. Хвост переписки в Telegram (если есть) — тон, инициатива, длина \
сообщений, скорость ответов, вопросы, эмоциональные изменения.
3. Предыдущие AI-анализы по этому контакту (если есть) — учитывай их как \
историю наблюдений: сравни прошлое состояние с текущим, отметь, что \
изменилось, а что нет. Не игнорируй их и не начинай анализ с нуля.

СТРОГО ЗАПРЕЩЕНО:
- Использовать в анализе даты/частоту создания записей в CRM, время работы \
пользователя с приложением или любую статистику активности — это не имеет \
отношения к задаче, анализируй только содержание.
- Утверждать что-либо о ситуации, чего нет в данных — если данных мало, \
так и скажи ("недостаточно данных для точного вывода"), не додумывай.
- Определять "истинные" намерения или мотивы другого человека — только то, \
что реально наблюдается (действия, слова), и как это можно объяснять; \
всегда допускай, что есть другое объяснение.
- Обвинительные, огульные формулировки о ЛЮБОЙ из сторон без опоры на \
конкретные факты (например "вы манипулируете" без единого подтверждающего \
признака в данных).
- Советы или формулировки, направленные на то, чтобы добиться нужной \
реакции от другого человека, надавить, вызвать ревность, создать ложное \
впечатление и т.п. Ты не помогаешь управлять поведением человека — только \
помогаешь пользователю понять картину.

Это НЕ значит, что нужно замалчивать реальные тревожные сигналы. Если из \
самих фактов явно следуют конкретные признаки риска для пользователя \
(например прямое давление, угрозы, признаки небезопасной ситуации) — назови \
это прямо и по существу, со ссылкой на конкретный факт, который на это \
указывает. Разница между этим и запрещённым выше: здесь вывод опирается на \
конкретный наблюдаемый факт, а не на общую оценку без опоры на данные.

Каждый содержательный вывод должен разделять:
- Факт: что реально есть в данных (коротко, дословно по сути).
- Интерпретация: как это можно объяснить (с оговоркой "один из вариантов").
- Уверенность: "высокая" / "средняя" / "низкая".

Используй нейтральные формулировки: "один из возможных сценариев...", "на \
основании доступных признаков...", "есть вероятность, что...", "другим \
возможным объяснением может быть...".

Построй от 2 до 4 возможных сценариев развития ситуации. Для каждого:
- label: короткое название сценария.
- probability: "высокая" / "средняя" / "низкая" (не число — это не точная \
статистика, а качественная оценка).
- signals: список конкретных признаков из данных, которые поддерживают этот \
сценарий (2-4 пункта).
- likely_next_step: возможный следующий этап, если сценарий будет \
развиваться (1 предложение, без категоричности).

Ответь СТРОГО валидным JSON без markdown, без пояснений до/после, вот такой \
структуры:
{
  "current_state": "2-4 предложения: в каком состоянии сейчас находится \
ситуация, с опорой на конкретику, без общих фраз",
  "key_factors": ["ключевой фактор 1", "ключевой фактор 2"],
  "scenarios": [
    {"label": "...", "probability": "средняя", "signals": ["...", "..."], \
"likely_next_step": "..."}
  ],
  "change_triggers": ["что могло бы изменить направление ситуации — новое \
событие, действие любой из сторон, и т.п."],
  "data_needed": ["какие данные повысили бы точность анализа, если их нет \
сейчас"],
  "confidence": "средняя",
  "risk_note": "заполняй ТОЛЬКО если из самих фактов прямо следует тревожный \
признак, требующий внимания пользователя (см. правило выше); иначе — пустая \
строка"
}"""


def _clamp_list(value, limit: int = 6) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def _parse_scenarios(raw) -> List[Dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:4]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        prob = str(item.get("probability") or "средняя").strip().lower()
        if prob not in {"высокая", "средняя", "низкая"}:
            prob = "средняя"
        out.append({
            "label": label[:150],
            "probability": prob,
            "signals": _clamp_list(item.get("signals"), 4),
            "likely_next_step": str(item.get("likely_next_step") or "").strip()[:300],
        })
    return out


def _gather_context(db, contact: models.Contact, transcript: str) -> Dict:
    """Собирает три источника данных, описанных в docstring модуля.
    Статистика активности CRM (когда создавались записи и т.п.)
    сознательно НЕ собирается и никуда не попадает."""
    from . import crud

    memory_items = crud.list_memory_items(db, contact_id=contact.id, limit=100)
    facts = [
        {"kind": (m.kind.value if hasattr(m.kind, "value") else m.kind), "title": m.title, "details": m.details}
        for m in memory_items
    ]

    interactions = list(contact.interactions or [])[:50]
    events = [
        {"note": i.note, "type": i.event_type, "when": i.occurred_at.strftime("%d.%m.%Y") if i.occurred_at else None}
        for i in interactions
    ]

    previous_snapshots = crud.list_ai_overview_snapshots(db, contact.id, limit=2)
    previous_overviews = [
        {
            "current_state": s.current_state,
            "scenarios": [sc.get("label") for sc in (s.scenarios or [])],
            "created_at": s.created_at.strftime("%d.%m.%Y"),
        }
        for s in previous_snapshots
    ]

    previous_deep_report = None
    if contact.deep_report:
        dr = contact.deep_report
        previous_deep_report = {
            "category": dr.get("category"),
            "trend": dr.get("trend"),
            "green_flags": dr.get("green_flags"),
            "red_flags": dr.get("red_flags"),
            "reasoning": dr.get("reasoning"),
        }

    return {
        "user_facts_and_events": facts + events,
        "chat_available": bool(transcript),
        "previous_ai_overviews": previous_overviews,
        "previous_deep_report": previous_deep_report,
    }


def _build_user_content(contact: models.Contact, context: Dict, transcript: str) -> str:
    parts = [
        f"Контакт: {contact.name}",
        "Пользовательские факты и события (JSON):",
        json.dumps(context["user_facts_and_events"], ensure_ascii=False),
    ]
    if context["previous_deep_report"]:
        parts.append("Предыдущий глубокий AI-анализ переписки (JSON, учти как историю наблюдений):")
        parts.append(json.dumps(context["previous_deep_report"], ensure_ascii=False))
    if context["previous_ai_overviews"]:
        parts.append("Предыдущие снимки AI Overview по этому контакту, от старых к новым (JSON):")
        parts.append(json.dumps(context["previous_ai_overviews"], ensure_ascii=False))
    if transcript:
        parts.append("Переписка (последние сообщения, старые сверху):")
        parts.append(transcript)
    else:
        parts.append("Переписка недоступна — анализируй только факты/события и предыдущие анализы.")
    return "\n\n".join(parts)


async def build_overview(db, contact: models.Contact, raw_messages: Optional[list] = None) -> Dict:
    """Строит один снимок AI Overview. raw_messages — уже полученные (не
    нормализованные) сообщения Telegram, если есть; функция сама их
    нормализует и обрежет до лимита. При недоступности Gemini — тихий
    откат на упрощённый локальный результат (source="local"), не падает."""
    transcript = ""
    if raw_messages:
        from . import analysis as ai
        messages = ai.normalize_messages(raw_messages)
        if messages:
            transcript = ai_common.build_transcript(messages, contact.name, config.AI_LLM_MAX_MESSAGES)

    context = _gather_context(db, contact, transcript)
    user_content = _build_user_content(contact, context, transcript)

    if config.AI_PROVIDER == "gemini" and config.GEMINI_API_KEY:
        try:
            parsed = await ai_gemini._call_gemini(OVERVIEW_SYSTEM_PROMPT, user_content)
            scenarios = _parse_scenarios(parsed.get("scenarios"))
            current_state = str(parsed.get("current_state") or "").strip()
            if current_state and scenarios:
                confidence = str(parsed.get("confidence") or "средняя").strip().lower()
                if confidence not in {"высокая", "средняя", "низкая"}:
                    confidence = "средняя"
                return {
                    "current_state": current_state,
                    "key_factors": _clamp_list(parsed.get("key_factors"), 6),
                    "scenarios": scenarios,
                    "change_triggers": _clamp_list(parsed.get("change_triggers"), 6),
                    "data_used": [k for k in (
                        "переписка" if context["chat_available"] else None,
                        "заметки и события пользователя" if context["user_facts_and_events"] else None,
                        "предыдущий глубокий AI-анализ" if context["previous_deep_report"] else None,
                        "предыдущие снимки AI Overview" if context["previous_ai_overviews"] else None,
                    ) if k],
                    "data_needed": _clamp_list(parsed.get("data_needed"), 4),
                    "confidence": confidence,
                    "risk_note": str(parsed.get("risk_note") or "").strip() or None,
                    "source": "gemini",
                }
        except ai_gemini.GeminiError as exc:
            logger.warning("AI Overview: откат на локальный результат (%s)", exc)

    # Локальный откат: честно и без выдумывания — просто фиксируем, что
    # данных недостаточно для сценариев, без интерпретации Gemini.
    return {
        "current_state": "Автоматический анализ недоступен (Gemini не настроена или ошибка запроса). "
                          "Показаны только собранные факты, без построения сценариев.",
        "key_factors": [],
        "scenarios": [],
        "change_triggers": [],
        "data_used": [],
        "data_needed": ["Настроенный Gemini (AI_PROVIDER=gemini и GEMINI_API_KEY)"],
        "confidence": None,
        "risk_note": None,
        "source": "local",
    }
