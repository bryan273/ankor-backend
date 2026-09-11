"""Eval results — feeds the /admin/eval page that shows judges what was measured."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from app.clients import db
from app.deps import require_api_key

router = APIRouter(tags=["eval"])


@router.get("/eval/runs")
async def eval_runs(scenario: Optional[str] = None, limit: int = 100,
                    _: str = Depends(require_api_key)) -> Dict[str, Any]:
    if scenario:
        rows = await db.fetch(
            """
            select er.id::text as run_id, er.passed, er.score, er.ms, er.cost,
                   er.created_at, ec.name, ec.scenario
            from eval_runs er join eval_cases ec on ec.id = er.case_id
            where ec.scenario = %s order by er.created_at desc limit %s
            """,
            (scenario, limit),
        )
    else:
        rows = await db.fetch(
            """
            select er.id::text as run_id, er.passed, er.score, er.ms, er.cost,
                   er.created_at, ec.name, ec.scenario
            from eval_runs er join eval_cases ec on ec.id = er.case_id
            order by er.created_at desc limit %s
            """,
            (limit,),
        )
    passed = sum(1 for r in rows if r["passed"])
    by_scenario: Dict[str, Dict[str, int]] = {}
    for r in rows:
        bucket = by_scenario.setdefault(r["scenario"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r["passed"] else 0
    return {"runs": rows, "total": len(rows), "passed": passed,
            "pass_rate": round(passed / len(rows), 3) if rows else None,
            "by_scenario": by_scenario}


@router.get("/eval/cases")
async def eval_cases(_: str = Depends(require_api_key)) -> Dict[str, Any]:
    rows = await db.fetch(
        "select id::text as case_id, name, scenario, input, expect, enabled "
        "from eval_cases order by scenario, name")
    return {"cases": rows, "count": len(rows)}
