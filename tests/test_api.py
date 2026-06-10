import os
import tempfile
import uuid
from pathlib import Path

import app.db

os.environ["QUANT_DB_PATH"] = tempfile.NamedTemporaryFile(delete=False, suffix=".db").name
app.db.DB_PATH = Path(os.environ["QUANT_DB_PATH"])

from fastapi.testclient import TestClient

from app.db import now_ms
from app.instruments import default_instrument
from app.main import app
from app.models import ExchangeEvent, OrderStatus
from app.state_machine import OrderStateManager
from app.main import conn


client = TestClient(app)
INSTRUMENT_ID = default_instrument()


def login(username="trader_a", password="password_a"):
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def test_auth_required_for_orders():
    resp = client.get("/orders")
    assert resp.status_code in (401, 403)


def test_instruments_endpoint_returns_current_if_ic_im_contracts():
    token = login()
    resp = client.get("/instruments", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    instruments = resp.json()
    assert len(instruments) == 12
    assert INSTRUMENT_ID in instruments
    assert {item[:2] for item in instruments} == {"IF", "IC", "IM"}


def test_submitter_cannot_review_own_instruction():
    token = login()
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/instructions/orders",
        json={"instrument_id": INSTRUMENT_ID, "side": "kBuy", "price": 3500, "volume": 1},
        headers=headers,
    )
    assert created.status_code == 200
    instruction_id = created.json()["id"]

    reviewed = client.post(
        f"/instructions/{instruction_id}/review",
        json={"decision": "Approved"},
        headers=headers,
    )
    assert reviewed.status_code == 403


def test_review_reject_does_not_create_order():
    token_a = login("trader_a", "password_a")
    token_b = login("trader_b", "password_b")
    created = client.post(
        "/instructions/orders",
        json={"instrument_id": INSTRUMENT_ID, "side": "kBuy", "price": 3500, "volume": 1},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    instruction_id = created.json()["id"]

    reviewed = client.post(
        f"/instructions/{instruction_id}/review",
        json={"decision": "Rejected", "reason": "manual reject"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "Rejected"

    orders = client.get("/orders", headers={"Authorization": f"Bearer {token_a}"}).json()
    assert all(order.get("instruction_id") != instruction_id for order in orders)


def test_exchange_http_order_entrypoint():
    resp = client.post(
        "/orders",
        json={"instrument_id": INSTRUMENT_ID, "side": "kBuy", "price": 3500, "volume": 1},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "kUnconfirmed"
    assert data["instruction_id"] is None


def test_cancel_click_is_visible_and_idempotent_in_order_list():
    manager = OrderStateManager(conn)
    order_id = f"ord-{uuid.uuid4().hex[:12]}"
    manager.create_order(
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        instruction_id=None,
        instrument_id=INSTRUMENT_ID,
        side="kBuy",
        price=3500,
        volume=10,
    )
    manager.apply_event(
        ExchangeEvent(
            event_id=f"evt-{uuid.uuid4().hex}",
            order_id=order_id,
            instrument_id=INSTRUMENT_ID,
            gen_time=now_ms(),
            status=OrderStatus.QUEUEING,
        )
    )

    token = login("trader_a", "password_a")
    headers = {"Authorization": f"Bearer {token}"}
    first = client.post("/instructions/cancel", json={"order_id": order_id}, headers=headers)
    second = client.post("/instructions/cancel", json={"order_id": order_id}, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    orders = client.get("/orders", headers=headers).json()
    order = next(item for item in orders if item["order_id"] == order_id)
    assert order["cancel_instruction_id"] == first.json()["id"]
    assert order["cancel_instruction_status"] == "PendingReview"


def test_retry_cancel_after_risk_rejected_becomes_latest_pending_review():
    manager = OrderStateManager(conn)
    order_id = f"ord-{uuid.uuid4().hex[:12]}"
    manager.create_order(
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        instruction_id=None,
        instrument_id=INSTRUMENT_ID,
        side="kBuy",
        price=3500,
        volume=10,
    )
    manager.apply_event(
        ExchangeEvent(
            event_id=f"evt-{uuid.uuid4().hex}",
            order_id=order_id,
            instrument_id=INSTRUMENT_ID,
            gen_time=now_ms(),
            status=OrderStatus.QUEUEING,
        )
    )
    ts = now_ms()
    conn.execute(
        """
        INSERT INTO instructions(
            type, status, submitter_id, reviewer_id, review_time,
            order_id, created_at, updated_at
        )
        VALUES ('cancel', 'Sent', 1, 2, ?, ?, ?, ?)
        """,
        (ts, order_id, ts, ts),
    )
    conn.execute(
        """
        INSERT INTO instructions(
            type, status, submitter_id, reviewer_id, review_time,
            order_id, risk_error_code, risk_error_message, created_at, updated_at
        )
        VALUES ('cancel', 'RiskRejected', 1, 2, ?, ?, 'CANCEL_DEBOUNCE', 'same order retry test', ?, ?)
        """,
        (ts, order_id, ts, ts),
    )
    conn.commit()

    token = login("trader_a", "password_a")
    headers = {"Authorization": f"Bearer {token}"}
    retried = client.post("/instructions/cancel", json={"order_id": order_id}, headers=headers)

    assert retried.status_code == 200
    assert retried.json()["status"] == "PendingReview"

    orders = client.get("/orders", headers=headers).json()
    order = next(item for item in orders if item["order_id"] == order_id)
    assert order["cancel_instruction_id"] == retried.json()["id"]
    assert order["cancel_instruction_status"] == "PendingReview"

    reviewer_token = login("trader_b", "password_b")
    pending = client.get("/instructions/pending", headers={"Authorization": f"Bearer {reviewer_token}"}).json()
    assert any(item["id"] == retried.json()["id"] for item in pending)


def test_demo_scenarios_are_visible_in_system_views():
    token = login("trader_a", "password_a")
    headers = {"Authorization": f"Bearer {token}"}

    seeded = client.post("/demo/scenarios", headers=headers)
    assert seeded.status_code == 200
    assert seeded.json()["orders"] >= 9
    assert seeded.json()["instructions"] >= 10

    orders = client.get("/orders", headers=headers).json()
    instructions = client.get("/instructions", headers=headers).json()
    positions = client.get("/positions", headers=headers).json()

    statuses = {order["status"] for order in orders}
    risk_codes = {item["risk_error_code"] for item in instructions if item["risk_error_code"]}
    order_ids = {order["order_id"] for order in orders}

    assert {"kRejected", "kQueueing", "kAllTraded", "kCanceled"} <= statuses
    assert "kUnconfirmed" not in statuses
    assert {
        "PRICE_INVALID",
        "PRICE_OUT_OF_RANGE",
        "PRICE_TOO_HIGH",
        "ORDER_RATE_SECOND",
        "ORDER_RATE_DAILY",
        "OPEN_ORDER_LIMIT",
        "TURNOVER_LIMIT",
        "ORDER_NOT_FOUND",
        "ORDER_NOT_CONFIRMED",
        "ORDER_TERMINAL",
        "CANCEL_DEBOUNCE",
    } <= risk_codes
    assert all(order_id.startswith("ord-") for order_id in order_ids)
    assert not any(order_id.startswith("demo-") for order_id in order_ids)
    assert any(order["cancel_instruction_status"] == "PendingReview" for order in orders)
    assert positions
