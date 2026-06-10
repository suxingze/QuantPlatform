from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .db import now_ms
from .instruments import default_instrument
from .models import ExchangeEvent, InstructionStatus, OrderStatus
from .state_machine import OrderStateManager

BUSINESS_TABLES = [
    "order_events",
    "trades",
    "cancel_requests",
    "risk_counters",
    "orders",
    "instructions",
]


def reset_business_data(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in BUSINESS_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('instructions', 'trades')")
        conn.execute("PRAGMA foreign_keys = ON")


def seed_all_demo_scenarios(conn: sqlite3.Connection, *, reset: bool = True) -> dict[str, Any]:
    if reset:
        reset_business_data(conn)

    manager = OrderStateManager(conn)
    created_orders: list[str] = []
    created_instructions: list[int] = []
    instrument_id = default_instrument()

    def create_order(volume: int = 10) -> str:
        order_id = f"ord-{uuid.uuid4().hex[:12]}"
        manager.create_order(
            order_id=order_id,
            client_order_id=f"cli-{uuid.uuid4().hex[:12]}",
            instruction_id=None,
            instrument_id=instrument_id,
            side="kBuy",
            price=3500,
            volume=volume,
        )
        created_orders.append(order_id)
        return order_id

    def event(order_id: str, status: OrderStatus, *, traded: int = 0, canceled: int = 0, suffix: str = "") -> ExchangeEvent:
        return ExchangeEvent(
            event_id=f"evt-{order_id}-{suffix or uuid.uuid4().hex[:8]}",
            order_id=order_id,
            instrument_id=instrument_id,
            gen_time=now_ms(),
            status=status,
            traded_volume=traded,
            canceled_volume=canceled,
            trade_price=3500 if traded else None,
            message=f"demo scenario {order_id}",
        )

    rejected = create_order()
    manager.apply_event(event(rejected, OrderStatus.REJECTED, suffix="reject"))

    partial = create_order()
    manager.apply_event(event(partial, OrderStatus.QUEUEING, suffix="queue"))
    manager.apply_event(event(partial, OrderStatus.QUEUEING, traded=4, suffix="partial"))

    all_traded = create_order()
    manager.apply_event(event(all_traded, OrderStatus.QUEUEING, suffix="queue"))
    manager.apply_event(event(all_traded, OrderStatus.QUEUEING, traded=4, suffix="partial"))
    manager.apply_event(event(all_traded, OrderStatus.ALL_TRADED, traded=10, suffix="filled"))

    duplicate = create_order()
    duplicate_event = event(duplicate, OrderStatus.QUEUEING, traded=3, suffix="same")
    manager.apply_event(duplicate_event)
    manager.apply_event(duplicate_event)

    out_of_order_trade = create_order()
    manager.apply_event(event(out_of_order_trade, OrderStatus.QUEUEING, traded=4, suffix="trade-first"))
    manager.apply_event(event(out_of_order_trade, OrderStatus.QUEUEING, suffix="late-queue"))

    cancel_before_trade = create_order()
    manager.apply_event(event(cancel_before_trade, OrderStatus.CANCELED, canceled=10, suffix="cancel-first"))
    manager.apply_event(event(cancel_before_trade, OrderStatus.QUEUEING, traded=3, suffix="late-trade"))

    filled_late_cancel = create_order()
    manager.apply_event(event(filled_late_cancel, OrderStatus.ALL_TRADED, traded=10, suffix="filled"))
    manager.apply_event(event(filled_late_cancel, OrderStatus.CANCELED, canceled=10, suffix="late-cancel"))

    oversized = create_order()
    manager.apply_event(event(oversized, OrderStatus.ALL_TRADED, traded=99, suffix="oversized-fill"))
    manager.apply_event(event(oversized, OrderStatus.CANCELED, canceled=99, suffix="oversized-cancel"))

    cancel_pending = create_order()
    manager.apply_event(event(cancel_pending, OrderStatus.QUEUEING, suffix="queue"))
    created_instructions.append(
        insert_instruction(
            conn,
            type_="cancel",
            status=InstructionStatus.PENDING.value,
            order_id=cancel_pending,
            review_reason="demo: cancel instruction waiting for another trader",
        )
    )

    cancel_sent = create_order()
    manager.apply_event(event(cancel_sent, OrderStatus.QUEUEING, suffix="queue"))
    created_instructions.append(
        insert_instruction(
            conn,
            type_="cancel",
            status=InstructionStatus.SENT.value,
            order_id=cancel_sent,
            reviewer_id=2,
            review_time=now_ms(),
            review_reason="demo: cancel request sent to exchange",
        )
    )

    risk_cases = [
        ("order", "PRICE_INVALID", "下单价格必须大于 0", None, 0, 1),
        ("order", "PRICE_OUT_OF_RANGE", f"{instrument_id} 下单价格必须在 3000-7000 之间", None, 8000, 1),
        ("order", "PRICE_TOO_HIGH", "下单价格不能超过 1000000", None, 1_000_001, 1),
        ("order", "ORDER_RATE_SECOND", "每秒委托笔数超过 3", None, 3500, 1),
        ("order", "ORDER_RATE_DAILY", "每日委托笔数超过 100", None, 3500, 1),
        ("order", "OPEN_ORDER_LIMIT", "未完结订单数超过 2", None, 3500, 1),
        ("order", "TURNOVER_LIMIT", "每分钟成交额超过 100000", None, 3500, 1),
        ("cancel", "ORDER_NOT_FOUND", "订单不存在", "missing-order", None, None),
        ("cancel", "ORDER_NOT_CONFIRMED", "订单尚未被交易所确认", "ord-unconfirmed-sample", None, None),
        ("cancel", "ORDER_TERMINAL", "终态订单不允许撤单", rejected, None, None),
        ("cancel", "CANCEL_DEBOUNCE", "同一订单 1 秒内不允许重复撤单", cancel_sent, None, None),
    ]
    for type_, code, message, order_id, price, volume in risk_cases:
        created_instructions.append(
            insert_instruction(
                conn,
                type_=type_,
                status=InstructionStatus.RISK_REJECTED.value,
                order_id=order_id,
                price=price,
                volume=volume,
                risk_error_code=code,
                risk_error_message=message,
                reviewer_id=2,
                review_time=now_ms(),
                review_reason=f"demo: {code}",
            )
        )

    return {
        "orders": len(created_orders),
        "instructions": len(created_instructions),
        "events": conn.execute("SELECT COUNT(*) FROM order_events").fetchone()[0],
        "trades": conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
    }


def insert_instruction(
    conn: sqlite3.Connection,
    *,
    type_: str,
    status: str,
    order_id: str | None = None,
    price: float | None = None,
    volume: int | None = None,
    risk_error_code: str | None = None,
    risk_error_message: str | None = None,
    reviewer_id: int | None = None,
    review_time: int | None = None,
    review_reason: str | None = None,
) -> int:
    ts = now_ms()
    cur = conn.execute(
        """
        INSERT INTO instructions(
            type, status, submitter_id, reviewer_id, review_time, review_reason,
            instrument_id, side, price, volume, order_id,
            risk_error_code, risk_error_message, created_at, updated_at
        )
        VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            type_,
            status,
            reviewer_id,
            review_time,
            review_reason,
            default_instrument() if type_ == "order" else None,
            "kBuy" if type_ == "order" else None,
            price,
            volume,
            order_id,
            risk_error_code,
            risk_error_message,
            ts,
            ts,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)
