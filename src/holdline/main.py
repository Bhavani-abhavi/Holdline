"""FastAPI routes. One tool implementation, two front doors.

The browser agent and the phone platform both reach tools.py through the same
dispatch; `/api/voice/tool` is designed here in Phase 4 so the Phase 6 phone
path can never grow its own copy of the business logic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from holdline import clock, config, events, expiry, holds, inventory
from holdline.agent import Agent
from holdline.availability import sellable
from holdline.config import Settings, get_settings
from holdline.db import Database
from holdline.llm import AnthropicLLM, FakeLLM, LLMClient
from holdline.logging_setup import configure, get_logger
from holdline.notifications import acknowledge, open_alerts
from holdline.seed import LOCATION_ID, seed
from holdline.tools import TOOL_NAMES, ToolContext, dispatch

log = get_logger(__name__)
STATIC_DIR = Path(str(resources.files("holdline").joinpath("static")))


# --- Request bodies -----------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    channel: str = "browser"


class DeliveryRequest(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=10_000)
    note: str | None = None


class SimulateSalesRequest(BaseModel):
    minutes: int = Field(default=20, ge=1, le=600)


class VoiceToolRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(min_length=1, max_length=128)


class VoiceEventRequest(BaseModel):
    call_id: str
    event: str  # call_started | call_ended | transcript
    transcript: str | None = None


# --- App factory ---------------------------------------------------------------------


def build_llm(settings: Settings) -> LLMClient:
    """Pick the model client from settings. `fake` runs the app with no key."""
    if settings.llm_backend == "fake":
        return FakeLLM()
    return AnthropicLLM(settings.model, api_key=settings.anthropic_api_key)


def create_app(
    db: Database | None = None,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the application. Tests pass their own db and a FakeLLM."""
    settings = settings or get_settings()
    configure(settings.log_level)
    settings.validate(need_llm=llm is None and settings.llm_backend == "anthropic")
    database = db or Database(settings.db_path)
    database.apply_schema()
    with database.connection() as conn:
        has_location = conn.execute("SELECT 1 FROM locations LIMIT 1").fetchone()
    if not has_location:
        seed(database)
    agent = Agent(database, llm or build_llm(settings), location_id=LOCATION_ID)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        worker = asyncio.create_task(expiry.run_forever(database, stop))
        app.state.expiry_running = True
        try:
            yield
        finally:
            stop.set()
            await worker
            app.state.expiry_running = False

    app = FastAPI(title="HoldLine", version="0.4.0", lifespan=lifespan)
    app.state.db = database
    app.state.agent = agent
    app.state.settings = settings
    app.state.expiry_running = False
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- pages ----
    @app.get("/")
    async def customer_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/staff")
    async def staff_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "staff.html")

    @app.get("/analytics")
    async def analytics_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "analytics.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> JSONResponse:
        return JSONResponse(None, status_code=204)

    # ---- conversation ----
    @app.post("/api/chat")
    async def chat(body: ChatRequest) -> dict[str, Any]:
        channel = body.channel if body.channel in {"browser", "phone"} else "browser"
        result = await asyncio.to_thread(
            agent.handle_message, body.session_id, body.message, channel
        )
        return {
            "session_id": body.session_id,
            "reply": result.reply,
            "tool_calls": result.tool_calls,
            "violations": result.violations,
            "blocked_tier": result.blocked_tier,
            "escalated": result.escalated,
            "retried": result.retried,
        }

    # ---- dashboard ----
    @app.get("/api/dashboard")
    async def dashboard(after_id: int = 0) -> dict[str, Any]:
        return await asyncio.to_thread(_dashboard, database, after_id)

    @app.get("/api/menu")
    async def menu() -> dict[str, Any]:
        ctx = ToolContext(location_id=LOCATION_ID, call_id="menu-page", channel="browser")
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT id, name, category, price_cents FROM items ORDER BY category, name"
            ).fetchall()
        del ctx
        return {
            "items": [
                {
                    "item_id": r["id"],
                    "name": r["name"],
                    "category": r["category"],
                    "price": f"${r['price_cents'] / 100:.2f}",
                }
                for r in rows
            ]
        }

    # ---- staff ----
    @app.get("/api/staff/alerts")
    async def staff_alerts() -> dict[str, Any]:
        with database.connection() as conn:
            return {"alerts": open_alerts(conn, LOCATION_ID)}

    @app.post("/api/staff/alerts/{alert_id}/ack")
    async def ack_alert(alert_id: int) -> dict[str, Any]:
        with database.transaction(immediate=True) as conn:
            ok = acknowledge(conn, alert_id)
        if not ok:
            raise HTTPException(404, "no such alert")
        return {"ok": True}

    @app.post("/api/staff/holds/{hold_id}/approve")
    async def approve(hold_id: int) -> dict[str, Any]:
        return _ok_or_409(holds.approve_hold(database, hold_id), "hold is not pending approval")

    @app.post("/api/staff/holds/{hold_id}/decline")
    async def decline(hold_id: int) -> dict[str, Any]:
        return _ok_or_409(holds.decline_hold(database, hold_id), "hold is not pending approval")

    @app.post("/api/staff/holds/{hold_id}/collect")
    async def collect(hold_id: int) -> dict[str, Any]:
        return _ok_or_409(holds.collect_hold(database, hold_id), "hold is not active")

    @app.post("/api/staff/delivery")
    async def delivery(body: DeliveryRequest) -> dict[str, Any]:
        try:
            delivery_id = inventory.receive_delivery(
                database, LOCATION_ID, body.item_id, body.quantity, body.note
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a 404 below for unknown items
            from holdline.exceptions import UnknownItemError

            if isinstance(exc, UnknownItemError):
                raise HTTPException(404, "no such item") from exc
            raise
        return {"ok": True, "delivery_id": delivery_id}

    # ---- demo controls ----
    @app.post("/api/simulate/sales")
    async def simulate_sales(body: SimulateSalesRequest | None = None) -> dict[str, Any]:
        minutes = body.minutes if body else 20
        sold = await asyncio.to_thread(inventory.simulate_sales, database, LOCATION_ID, minutes)
        return {"sold": sold}

    @app.post("/api/simulate/stale")
    async def simulate_stale() -> dict[str, Any]:
        inventory.force_stale(database, LOCATION_ID)
        return {"ok": True}

    @app.post("/api/simulate/resync")
    async def simulate_resync() -> dict[str, Any]:
        inventory.resync(database, LOCATION_ID)
        return {"ok": True}

    @app.post("/api/reset")
    async def reset() -> dict[str, Any]:
        seed(database, reset_events=True)
        agent.conversations.clear()
        return {"ok": True}

    # ---- the second front door: a voice platform calling tools over HTTP ----
    @app.post("/api/voice/tool")
    async def voice_tool(body: VoiceToolRequest) -> dict[str, Any]:
        if body.tool_name not in TOOL_NAMES:
            raise HTTPException(400, f"unknown tool {body.tool_name!r}")
        ctx = ToolContext(location_id=LOCATION_ID, call_id=body.call_id, channel="phone")
        result = await asyncio.to_thread(dispatch, database, ctx, body.tool_name, body.arguments)
        return result.to_dict()

    @app.post("/api/voice/events")
    async def voice_events(body: VoiceEventRequest) -> dict[str, Any]:
        kind = (
            body.event
            if body.event in {"call_started", "call_ended", "transcript"}
            else "transcript"
        )
        with database.transaction(immediate=True) as conn:
            events.record(
                conn,
                kind,
                {"transcript": body.transcript} if body.transcript else {},
                location_id=LOCATION_ID,
                channel="phone",
                call_id=body.call_id,
            )
        return {"ok": True}

    # ---- liveness ----
    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        try:
            with database.connection() as conn:
                row = conn.execute(
                    "SELECT MIN(last_synced_at) AS oldest FROM stock WHERE location_id = ?",
                    (LOCATION_ID,),
                ).fetchone()
            age = (
                int((clock.now() - clock.parse(row["oldest"])).total_seconds())
                if row and row["oldest"]
                else None
            )
            db_ok = True
        except sqlite3.Error:
            age, db_ok = None, False
        status = "ok" if db_ok else "degraded"
        return JSONResponse(
            {
                "status": status,
                "db": db_ok,
                "last_pos_sync_age_seconds": age,
                "stale_threshold_seconds": config.STALE_THRESHOLD_SECONDS,
                "expiry_worker": bool(request.app.state.expiry_running),
                "llm_backend": settings.llm_backend,
                "model": settings.model,
            },
            status_code=200 if db_ok else 503,
        )

    return app


def _ok_or_409(ok: bool, detail: str) -> dict[str, Any]:
    if not ok:
        raise HTTPException(409, detail)
    return {"ok": True}


def _dashboard(database: Database, after_id: int) -> dict[str, Any]:
    """Everything the glass box and the staff page poll for, in one read."""
    with database.connection() as conn:
        loc = conn.execute(
            "SELECT id, name, hours FROM locations WHERE id = ?", (LOCATION_ID,)
        ).fetchone()
        items = conn.execute(
            "SELECT i.id, i.name, i.category, i.safety_buffer, s.on_hand FROM items i"
            " JOIN stock s ON s.item_id = i.id AND s.location_id = ? ORDER BY i.category, i.name",
            (LOCATION_ID,),
        ).fetchall()
        stock = []
        any_stale = False
        for it in items:
            avail = sellable(conn, LOCATION_ID, it["id"])
            any_stale = any_stale or avail.status == "unknown"
            stock.append(
                {
                    "item_id": it["id"],
                    "name": it["name"],
                    "category": it["category"],
                    "on_hand": it["on_hand"],
                    "safety_buffer": it["safety_buffer"],
                    "sellable": avail.quantity,
                    "status": avail.status,
                    "age_seconds": avail.age_seconds
                    if avail.status == "unknown"
                    else int((clock.now() - avail.as_of).total_seconds())
                    if avail.as_of
                    else None,
                }
            )
        live = holds.get_active_holds(conn, LOCATION_ID)
        today = clock.iso()[:10]
        counters: dict[str, dict[str, int]] = {}
        for row in stock:
            counters[row["item_id"]] = {
                "received": 0,
                "sold": 0,
                "held": 0,
                "sellable": row["sellable"] or 0,
            }
        for r in conn.execute(
            "SELECT item_id, SUM(quantity) AS q FROM deliveries"
            " WHERE substr(received_at, 1, 10) = ? GROUP BY item_id",
            (today,),
        ):
            counters.setdefault(r["item_id"], {}).update(received=int(r["q"]))
        for r in conn.execute(
            "SELECT detail FROM events WHERE kind = 'sale' AND substr(at, 1, 10) = ?", (today,)
        ):
            d = json.loads(r["detail"])
            if d.get("item_id") in counters:
                counters[d["item_id"]]["sold"] += int(d.get("quantity", 0))
        for h in live:
            if h["item_id"] in counters:
                counters[h["item_id"]]["held"] += int(h["quantity"])
        recent = events.recent(conn, limit=100, after_id=after_id)
    return {
        "location": dict(loc) if loc else None,
        "stale": any_stale,
        "stock": stock,
        "holds": live,
        "counters": counters,
        "events": recent,
    }
