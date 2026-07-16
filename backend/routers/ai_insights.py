"""
Personal AI Operating System — API поверх ai_personal_engine.py.

Все эндпоинты работают только с данными самого пользователя (память,
паттерны, решения). Никаких оценок/предсказаний поведения контактов
здесь нет — это сознательно отдельно от Contact Intelligence
(см. routers/contacts.py, /analyze, /deep-report).
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import ai_personal_engine, crud, models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/ai", tags=["ai-insights"])


# ---------------------------------------------------------------
# AI Memory
# ---------------------------------------------------------------

@router.post("/memory/extract", response_model=schemas.AIMemoryExtractOut)
async def extract_memory(data: schemas.AIMemoryExtractIn, db: Session = Depends(get_db)):
    if data.contact_id is not None and not crud.get_contact(db, data.contact_id):
        raise HTTPException(status_code=404, detail="Контакт не найден")

    result = await ai_personal_engine.extract_memory(data.text, data.contact_id)
    saved_rows = []
    conflicts: List[str] = []
    for item in result["items"]:
        row = crud.create_memory_item(
            db, item, source=result["source"], contact_id=data.contact_id, source_text=data.text,
        )
        saved_rows.append(row)
        if item.get("related_at"):
            conflicts.extend(ai_personal_engine.find_time_conflicts(db, item["related_at"], exclude_id=row.id))

    return schemas.AIMemoryExtractOut(
        items=[_memory_out(r) for r in saved_rows],
        conflicts=conflicts,
        source=result["source"],
    )


@router.get("/memory", response_model=List[schemas.AIMemoryItemOut])
def list_memory(contact_id: Optional[int] = None, only_open: bool = False, db: Session = Depends(get_db)):
    rows = crud.list_memory_items(db, contact_id=contact_id, only_open=only_open)
    return [_memory_out(r) for r in rows]


@router.post("/memory", response_model=schemas.AIMemoryItemOut, status_code=201)
def create_memory_manual(data: schemas.AIMemoryItemCreate, db: Session = Depends(get_db)):
    if data.contact_id is not None and not crud.get_contact(db, data.contact_id):
        raise HTTPException(status_code=404, detail="Контакт не найден")
    row = crud.create_memory_item(
        db,
        {
            "kind": data.kind.value,
            "title": data.title,
            "details": data.details,
            "related_at": data.related_at,
            "importance": data.importance,
        },
        source="manual",
        contact_id=data.contact_id,
    )
    return _memory_out(row)


@router.patch("/memory/{item_id}", response_model=schemas.AIMemoryItemOut)
def update_memory(item_id: int, data: schemas.AIMemoryItemUpdate, db: Session = Depends(get_db)):
    row = crud.get_memory_item(db, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    crud.update_memory_item(db, row, data)
    return _memory_out(row)


@router.delete("/memory/{item_id}", status_code=204)
def delete_memory(item_id: int, db: Session = Depends(get_db)):
    row = crud.get_memory_item(db, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    crud.delete_memory_item(db, row)
    return None


# ---------------------------------------------------------------
# Personal Timeline
# ---------------------------------------------------------------

@router.get("/timeline", response_model=List[schemas.AITimelineEntryOut])
def timeline(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    rows = crud.list_memory_items(db, limit=300)
    entries = []
    for r in rows:
        at = r.related_at
        bucket = "future" if (at and at > now) else ("past" if (at and at < now) else "present")
        entries.append(schemas.AITimelineEntryOut(
            kind="memory",
            title=r.title,
            details=r.details,
            at=at,
            bucket=bucket,
            contact_id=r.contact_id,
            contact_name=r.contact.name if r.contact else None,
        ))
    entries.sort(key=lambda e: e.at or now)
    return entries


# ---------------------------------------------------------------
# Pattern Analyzer
# ---------------------------------------------------------------

@router.get("/patterns", response_model=List[schemas.AIPatternOut])
def get_patterns(db: Session = Depends(get_db)):
    return crud.list_patterns(db)


@router.post("/patterns/refresh", response_model=List[schemas.AIPatternOut])
async def refresh_patterns(db: Session = Depends(get_db)):
    patterns = await ai_personal_engine.analyze_patterns(db)
    rows = crud.save_patterns(db, patterns)
    return rows


# ---------------------------------------------------------------
# Decision Tree Engine
# ---------------------------------------------------------------

@router.post("/decisions", response_model=schemas.AIDecisionOut, status_code=201)
async def create_decision(data: schemas.AIDecisionIn, db: Session = Depends(get_db)):
    result = await ai_personal_engine.build_decision_tree(data.situation, data.options)
    row = crud.create_decision(db, data.situation, result["options"])
    return _decision_out(row, source=result["source"])


@router.get("/decisions", response_model=List[schemas.AIDecisionOut])
def list_decisions(db: Session = Depends(get_db)):
    return [_decision_out(r) for r in crud.list_decisions(db)]


@router.post("/decisions/{decision_id}/choose", response_model=schemas.AIDecisionOut)
def choose_decision(decision_id: int, data: schemas.AIDecisionChooseIn, db: Session = Depends(get_db)):
    row = crud.get_decision(db, decision_id)
    if not row:
        raise HTTPException(status_code=404, detail="Решение не найдено")
    crud.choose_decision_option(db, row, data.chosen_option)
    return _decision_out(row)


# ---------------------------------------------------------------
# AI Insights Dashboard (aggregate)
# ---------------------------------------------------------------

@router.get("/insights", response_model=schemas.AIInsightsOut)
def insights(db: Session = Depends(get_db)):
    recent_memory = crud.list_memory_items(db, limit=8)
    open_commitments = crud.list_memory_items(db, only_open=True, kind=models.AIMemoryKind.COMMITMENT.value)
    patterns = crud.list_patterns(db)
    decisions = crud.list_decisions(db, limit=5)

    recommendations = []
    if open_commitments:
        recommendations.append(
            f"У вас {len(open_commitments)} невыполненных договорённостей — стоит проверить раздел «Память»."
        )
    if not patterns:
        recommendations.append("Паттерны ещё не посчитаны — запустите анализ на вкладке «Паттерны».")

    return schemas.AIInsightsOut(
        memory_count=len(crud.list_memory_items(db, limit=10_000)),
        open_commitments=len(open_commitments),
        recent_memory=[_memory_out(r) for r in recent_memory],
        patterns=patterns,
        recent_decisions=[_decision_out(r) for r in decisions],
        recommendations=recommendations,
    )


# ---------------------------------------------------------------
# helpers
# ---------------------------------------------------------------

def _memory_out(row: models.AIMemoryItem) -> schemas.AIMemoryItemOut:
    return schemas.AIMemoryItemOut(
        id=row.id,
        kind=row.kind,
        title=row.title,
        details=row.details,
        contact_id=row.contact_id,
        contact_name=row.contact.name if row.contact else None,
        related_at=row.related_at,
        importance=row.importance,
        source=row.source,
        is_done=row.is_done,
        created_at=row.created_at,
    )


def _decision_out(row: models.AIDecision, source: str = "gemini") -> schemas.AIDecisionOut:
    return schemas.AIDecisionOut(
        id=row.id,
        situation=row.situation,
        options=[schemas.AIDecisionOptionOut(**o) for o in row.options],
        chosen_option=row.chosen_option,
        created_at=row.created_at,
        source=source,
    )
