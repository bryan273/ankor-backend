# Anker Hackathon — Track 4: Intelligent Services · Backend

> Anker 首届黑客松挑战赛 · 赛道 04 智能服务（真正听懂，真正解决）
> **Track 4 — Intelligent Services:** an AI-powered after-sales customer-service agent + a highly interactive front-end page for real after-sales scenarios.

## Mission (from the track brief)

Integrate user text, fault images, a technical knowledge base, and historical work-order information to perform **emotion recognition → product disambiguation → fault location → troubleshooting guidance → escalation**, orchestrated as a complete business flow with an LLM agent + rule constraints.

Key scenarios to prove:
- Angry user photos an error message before a party → address the emotion first, then guide step-by-step.
- "My S1 Pro isn't pumping anymore" → disambiguate **breast pump vs robot vacuum** before answering.
- Order not found in system → recognize dealer order, follow dealer list, decide warranty coverage and next action.
- Judge when to offer reassurance, guidance, or escalation — close the loop, don't just respond.

## Scope of this repo

Agent backend / orchestration:

- LLM agent with process orchestration + rule constraints (guardrails)
- Emotion recognition (text + image context)
- Product disambiguation against the product catalog
- Multimodal fault-image understanding
- Knowledge-base retrieval (technical KB)
- Work-order / order lookup (incl. dealer-order path)
- Escalation decisioning and service-loop state

Frontend lives in the sibling repo **`anker-hackathon-frontend`**.

## Planned stack

_(TBD — decide with the team, then document here, e.g. Python FastAPI + LangGraph, or Node.)_

## Dev setup

```bash
# e.g. python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:app --reload
```

_(adjust once the stack is fixed)_
