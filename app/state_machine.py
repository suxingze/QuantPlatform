from __future__ import annotations

import sqlite3
from typing import Any

from .db import dict_row, now_ms
from .models import ExchangeEvent, OrderStatus, TERMINAL_STATUSES


class OrderStateManager:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_order(
        self,
        *,
        order_id: str,
        client_order_id: str,
        instruction_id: int | None,
        instrument_id: str,
        side: str,
        price: float,
        volume: int,
        account_id: str = "default",
    ) -> dict[str, Any]:
        ts = now_ms()
        self.conn.execute(
            """
            INSERT INTO orders(
                order_id, account_id, client_order_id, instruction_id, instrument_id,
                gen_time, side, price, original_volume, traded_volume, canceled_volume,
                status, exchange_accepted, created_at, updated_at, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, ?, 0)
            """,
            (
                order_id,
                account_id,
                client_order_id,
                instruction_id,
                instrument_id,
                ts,
                side,
                price,
                volume,
                OrderStatus.UNCONFIRMED.value,
                ts,
                ts,
            ),
        )
        self.conn.commit()
        return self.get_order(order_id)

    def apply_event(self, event: ExchangeEvent) -> tuple[dict[str, Any], bool]:
        """Persist raw event and fold it into the current order state.

        Returns (order, inserted). inserted is false for duplicate event_id.
        """
        ts = now_ms()
        with self.conn:
            try:
                self.conn.execute(
                    """
                    INSERT INTO order_events(
                        event_id, order_id, instrument_id, gen_time, status,
                        traded_volume, canceled_volume, trade_price, message, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.order_id,
                        event.instrument_id,
                        event.gen_time,
                        event.status.value if event.status else None,
                        event.traded_volume,
                        event.canceled_volume,
                        event.trade_price,
                        event.message,
                        ts,
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError:
                inserted = False

            if inserted:
                order = self._fold_event(event)
            else:
                order = self.get_order(event.order_id)
        return order, inserted

    def _fold_event(self, event: ExchangeEvent) -> dict[str, Any]:
        order = self.get_order(event.order_id)
        if order is None:
            raise ValueError(f"unknown order_id {event.order_id}")

        original = int(order["original_volume"])
        traded = max(int(order["traded_volume"]), int(event.traded_volume or 0))
        traded = min(traded, original)

        requested_cancel = max(int(order["canceled_volume"]), int(event.canceled_volume or 0))
        canceled = min(requested_cancel, original - traded)
        status = order["status"]
        accepted = int(order["exchange_accepted"])

        if event.status == OrderStatus.QUEUEING:
            accepted = 1
            if status not in TERMINAL_STATUSES:
                status = OrderStatus.QUEUEING.value

        if traded > 0 and status not in TERMINAL_STATUSES:
            status = OrderStatus.QUEUEING.value
            accepted = 1

        if event.status == OrderStatus.REJECTED:
            if not accepted and traded == 0 and status == OrderStatus.UNCONFIRMED.value:
                status = OrderStatus.REJECTED.value
                canceled = 0

        if event.status == OrderStatus.CANCELED and status != OrderStatus.ALL_TRADED.value:
            accepted = 1
            status = OrderStatus.CANCELED.value
            canceled = original - traded

        if traded == original:
            status = OrderStatus.ALL_TRADED.value
            canceled = 0

        self._validate(original, traded, canceled, status)
        ts = now_ms()
        self.conn.execute(
            """
            UPDATE orders
            SET traded_volume = ?, canceled_volume = ?, status = ?,
                exchange_accepted = ?, updated_at = ?, version = version + 1
            WHERE order_id = ?
            """,
            (traded, canceled, status, accepted, ts, event.order_id),
        )

        if event.trade_price is not None:
            previous_trade = self.conn.execute(
                "SELECT MAX(traded_volume) AS max_traded FROM order_events WHERE order_id = ? AND event_id <> ?",
                (event.order_id, event.event_id),
            ).fetchone()
            prev = int(previous_trade["max_traded"] or 0)
            delta = max(0, traded - prev)
            if delta > 0:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO trades(event_id, order_id, instrument_id, side, price, volume, amount, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.order_id,
                        event.instrument_id,
                        order["side"],
                        float(event.trade_price),
                        delta,
                        float(event.trade_price) * delta,
                        ts,
                    ),
                )
        return self.get_order(event.order_id)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        return dict_row(self.conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone())

    @staticmethod
    def _validate(original: int, traded: int, canceled: int, status: str) -> None:
        if not (0 <= traded <= original):
            raise ValueError("invalid traded_volume")
        if not (0 <= canceled <= original - traded):
            raise ValueError("invalid canceled_volume")
        if status in TERMINAL_STATUSES and status not in {
            OrderStatus.REJECTED.value,
            OrderStatus.ALL_TRADED.value,
            OrderStatus.CANCELED.value,
        }:
            raise ValueError("invalid terminal status")
