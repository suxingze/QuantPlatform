from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .auth import authenticate, create_token, get_current_user
from .db import DB_PATH, dict_row, dict_rows, get_conn, init_db, now_ms
from .demo_scenarios import seed_all_demo_scenarios
from .exchange import SimulatedExchange
from .instruments import available_instruments
from .models import (
    CancelOrderRequest,
    ExchangeEvent,
    ExchangeOrderRequest,
    InstructionStatus,
    LoginRequest,
    PlaceOrderRequest,
    ReviewRequest,
    TERMINAL_STATUSES,
)
from .risk import RiskEngine
from .state_machine import OrderStateManager

app = FastAPI(title="Quant Risk Platform")
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

conn = get_conn(DB_PATH)
init_db(conn)
event_queue: asyncio.Queue[ExchangeEvent] = asyncio.Queue()
exchange = SimulatedExchange(event_queue)
clients: set[WebSocket] = set()
exchange_clients: set[WebSocket] = set()


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(exchange_event_worker())


async def exchange_event_worker() -> None:
    manager = OrderStateManager(conn)
    while True:
        event = await event_queue.get()
        try:
            order, inserted = manager.apply_event(event)
            if inserted:
                await broadcast_exchange({"type": "exchange_report", "event": event.model_dump(mode="json")})
                await broadcast({"type": "order_event", "event": event.model_dump(mode="json"), "order": order})
                await broadcast({"type": "positions", "positions": get_positions_data()})
        finally:
            event_queue.task_done()


async def broadcast(payload: dict[str, Any]) -> None:
    disconnected: list[WebSocket] = []
    for ws in list(clients):
        try:
            await ws.send_json(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        clients.discard(ws)


async def broadcast_exchange(payload: dict[str, Any]) -> None:
    disconnected: list[WebSocket] = []
    for ws in list(exchange_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        exchange_clients.discard(ws)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.post("/orders")
async def exchange_place_order(request: ExchangeOrderRequest) -> dict[str, Any]:
    """Direct simulated-exchange HTTP entrypoint.

    The reviewed trading flow uses /instructions/orders. This endpoint documents
    and exposes the exchange-facing HTTP contract required by the assignment.
    """
    order_id = request.order_id or f"ord-{uuid.uuid4().hex[:12]}"
    if get_order_data(order_id):
        raise HTTPException(status_code=409, detail="order_id already exists")

    manager = OrderStateManager(conn)
    order = manager.create_order(
        order_id=order_id,
        client_order_id=request.client_order_id or f"direct-{uuid.uuid4().hex[:12]}",
        instruction_id=None,
        instrument_id=request.instrument_id,
        side=request.side.value,
        price=request.price,
        volume=request.volume,
        account_id=request.account_id,
    )
    await exchange.place_order(order)
    await broadcast({"type": "order", "order": order})
    return order


@app.post("/cancel")
async def exchange_cancel_order(request: CancelOrderRequest) -> dict[str, Any]:
    order = get_order_data(request.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order["status"] in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="终态订单不允许撤单")
    await exchange.cancel_order(order)
    return {"ok": True, "order_id": request.order_id}


@app.post("/auth/login")
async def login(request: LoginRequest) -> dict[str, Any]:
    user = authenticate(conn, request.username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"access_token": create_token(user), "token_type": "bearer", "user": redact_user(user)}


@app.get("/instruments")
async def list_instruments(user: dict[str, Any] = Depends(get_current_user)) -> list[str]:
    return available_instruments()


@app.post("/instructions/orders")
async def submit_order_instruction(
    request: PlaceOrderRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    ts = now_ms()
    cur = conn.execute(
        """
        INSERT INTO instructions(
            type, status, submitter_id, instrument_id, side, price, volume, created_at, updated_at
        )
        VALUES ('order', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            InstructionStatus.PENDING.value,
            user["id"],
            request.instrument_id,
            request.side.value,
            request.price,
            request.volume,
            ts,
            ts,
        ),
    )
    conn.commit()
    instruction = get_instruction(cur.lastrowid)
    await broadcast({"type": "instruction", "instruction": instruction})
    return instruction


@app.post("/instructions/cancel")
async def submit_cancel_instruction(
    request: CancelOrderRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    order = get_order_data(request.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    latest_cancel = conn.execute(
        """
        SELECT *
        FROM instructions
        WHERE type = 'cancel'
          AND order_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (request.order_id,),
    ).fetchone()
    if latest_cancel and latest_cancel["status"] in {
        InstructionStatus.PENDING.value,
        InstructionStatus.APPROVED.value,
        InstructionStatus.SENT.value,
    }:
        return dict_row(latest_cancel)

    ts = now_ms()
    cur = conn.execute(
        """
        INSERT INTO instructions(type, status, submitter_id, order_id, created_at, updated_at)
        VALUES ('cancel', ?, ?, ?, ?, ?)
        """,
        (InstructionStatus.PENDING.value, user["id"], request.order_id, ts, ts),
    )
    conn.commit()
    instruction = get_instruction(cur.lastrowid)
    await broadcast({"type": "instruction", "instruction": instruction})
    return instruction


@app.get("/instructions/pending")
async def pending_instructions(user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return dict_rows(
        conn.execute(
            """
            SELECT i.*, u.username AS submitter_username
            FROM instructions i
            JOIN users u ON u.id = i.submitter_id
            WHERE i.status = ? AND i.submitter_id <> ?
            ORDER BY i.created_at ASC
            """,
            (InstructionStatus.PENDING.value, user["id"]),
        ).fetchall()
    )


@app.get("/instructions")
async def list_instructions(user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return list_instructions_data()


@app.post("/demo/scenarios")
async def seed_demo_scenarios(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    summary = seed_all_demo_scenarios(conn, reset=True)
    await broadcast({"type": "demo_scenarios", "summary": summary})
    await broadcast({"type": "positions", "positions": get_positions_data()})
    return summary


@app.post("/instructions/{instruction_id}/review")
async def review_instruction(
    instruction_id: int,
    request: ReviewRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    instruction = get_instruction(instruction_id)
    if not instruction:
        raise HTTPException(status_code=404, detail="指令不存在")
    if instruction["status"] != InstructionStatus.PENDING.value:
        raise HTTPException(status_code=409, detail="指令已审核")
    if int(instruction["submitter_id"]) == int(user["id"]):
        raise HTTPException(status_code=403, detail="提交人不能审核自己的指令")

    ts = now_ms()
    if request.decision == InstructionStatus.REJECTED.value:
        conn.execute(
            """
            UPDATE instructions
            SET status = ?, reviewer_id = ?, review_time = ?, review_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (InstructionStatus.REJECTED.value, user["id"], ts, request.reason, ts, instruction_id),
        )
        conn.commit()
        result = get_instruction(instruction_id)
        await broadcast({"type": "instruction", "instruction": result})
        return result

    conn.execute(
        """
        UPDATE instructions
        SET status = ?, reviewer_id = ?, review_time = ?, review_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (InstructionStatus.APPROVED.value, user["id"], ts, request.reason, ts, instruction_id),
    )
    conn.commit()

    if instruction["type"] == "order":
        result = await execute_order_instruction(instruction_id)
    else:
        result = await execute_cancel_instruction(instruction_id)
    await broadcast({"type": "instruction", "instruction": result})
    return result


async def execute_order_instruction(instruction_id: int) -> dict[str, Any]:
    instruction = get_instruction(instruction_id)
    risk = RiskEngine(conn).check_place_order(
        instrument_id=instruction["instrument_id"],
        price=float(instruction["price"]),
    )
    ts = now_ms()
    if not risk.ok:
        conn.execute(
            """
            UPDATE instructions
            SET status = ?, risk_error_code = ?, risk_error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (InstructionStatus.RISK_REJECTED.value, risk.code, risk.message, ts, instruction_id),
        )
        conn.commit()
        return get_instruction(instruction_id)

    order_id = f"ord-{uuid.uuid4().hex[:12]}"
    client_order_id = f"cli-{instruction_id}"
    manager = OrderStateManager(conn)
    order = manager.create_order(
        order_id=order_id,
        client_order_id=client_order_id,
        instruction_id=instruction_id,
        instrument_id=instruction["instrument_id"],
        side=instruction["side"],
        price=float(instruction["price"]),
        volume=int(instruction["volume"]),
    )
    conn.execute(
        "UPDATE instructions SET status = ?, order_id = ?, updated_at = ? WHERE id = ?",
        (InstructionStatus.SENT.value, order_id, ts, instruction_id),
    )
    conn.commit()
    await exchange.place_order(order)
    await broadcast({"type": "order", "order": order})
    return get_instruction(instruction_id)


async def execute_cancel_instruction(instruction_id: int) -> dict[str, Any]:
    instruction = get_instruction(instruction_id)
    order_id = instruction["order_id"]
    risk = RiskEngine(conn).check_cancel(order_id)
    ts = now_ms()
    if not risk.ok:
        conn.execute(
            """
            UPDATE instructions
            SET status = ?, risk_error_code = ?, risk_error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (InstructionStatus.RISK_REJECTED.value, risk.code, risk.message, ts, instruction_id),
        )
        conn.commit()
        return get_instruction(instruction_id)

    conn.execute(
        "UPDATE instructions SET status = ?, updated_at = ? WHERE id = ?",
        (InstructionStatus.SENT.value, ts, instruction_id),
    )
    conn.commit()
    await exchange.cancel_order(get_order_data(order_id))
    return get_instruction(instruction_id)


@app.get("/orders")
async def list_orders(user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return list_orders_data()


@app.get("/orders/{order_id}")
async def get_order(order_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    order = get_order_data(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@app.get("/positions")
async def positions(user: dict[str, Any] = Depends(get_current_user)) -> list[dict[str, Any]]:
    return get_positions_data()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_json({"type": "snapshot", "orders": list_orders_data(), "positions": get_positions_data()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


@app.websocket("/exchange/ws")
async def exchange_websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    exchange_clients.add(ws)
    try:
        await ws.send_json({"type": "exchange_snapshot", "events": list_order_events_data()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        exchange_clients.discard(ws)


def get_instruction(instruction_id: int) -> dict[str, Any] | None:
    return dict_row(conn.execute("SELECT * FROM instructions WHERE id = ?", (instruction_id,)).fetchone())


def get_order_data(order_id: str) -> dict[str, Any] | None:
    return dict_row(conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone())


def list_orders_data() -> list[dict[str, Any]]:
    return dict_rows(
        conn.execute(
            """
            SELECT
                o.*,
                (
                    SELECT i.id
                    FROM instructions i
                    WHERE i.type = 'cancel' AND i.order_id = o.order_id
                    ORDER BY i.created_at DESC, i.id DESC
                    LIMIT 1
                ) AS cancel_instruction_id,
                (
                    SELECT i.status
                    FROM instructions i
                    WHERE i.type = 'cancel' AND i.order_id = o.order_id
                    ORDER BY i.created_at DESC, i.id DESC
                    LIMIT 1
                ) AS cancel_instruction_status,
                (
                    SELECT i.risk_error_message
                    FROM instructions i
                    WHERE i.type = 'cancel' AND i.order_id = o.order_id
                    ORDER BY i.created_at DESC, i.id DESC
                    LIMIT 1
                ) AS cancel_risk_error_message
            FROM orders o
            ORDER BY o.created_at DESC
            """
        ).fetchall()
    )


def list_order_events_data(limit: int = 100) -> list[dict[str, Any]]:
    return dict_rows(
        conn.execute(
            """
            SELECT * FROM order_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def list_instructions_data(limit: int = 200) -> list[dict[str, Any]]:
    return dict_rows(
        conn.execute(
            """
            SELECT
                i.*,
                s.username AS submitter_username,
                r.username AS reviewer_username
            FROM instructions i
            JOIN users s ON s.id = i.submitter_id
            LEFT JOIN users r ON r.id = i.reviewer_id
            ORDER BY i.created_at DESC, i.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def get_positions_data() -> list[dict[str, Any]]:
    return dict_rows(
        conn.execute(
            """
            SELECT
                instrument_id,
                SUM(CASE WHEN side = 'kBuy' THEN volume ELSE 0 END) AS buy_volume,
                SUM(CASE WHEN side = 'kSell' THEN volume ELSE 0 END) AS sell_volume,
                SUM(CASE WHEN side = 'kBuy' THEN volume ELSE -volume END) AS net_position,
                SUM(amount) AS turnover
            FROM trades
            GROUP BY instrument_id
            ORDER BY instrument_id
            """
        ).fetchall()
    )


def redact_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}
