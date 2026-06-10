from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import now_ms
from .instruments import available_instruments
from .models import TERMINAL_STATUSES

MAX_ORDER_PRICE = 1_000_000.0
PRICE_LIMITS = {
    "IF": (3000.0, 7000.0),
    "IC": (3000.0, 9000.0),
    "IM": (3000.0, 10000.0),
}


@dataclass
class RiskResult:
    ok: bool
    code: str = ""
    message: str = ""


class RiskEngine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def check_place_order(self, instrument_id: str | None = None, price: float | None = None) -> RiskResult:
        price_result = self._check_price(instrument_id, price)
        if not price_result.ok:
            return price_result

        now = now_ms()
        one_sec = now - 1000
        today = now - 24 * 3600 * 1000
        one_min = now - 60 * 1000

        per_sec = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM instructions
            WHERE type = 'order' AND status IN ('Sent', 'Approved') AND review_time >= ?
            """,
            (one_sec,),
        ).fetchone()["c"]
        if per_sec >= 3:
            return RiskResult(False, "ORDER_RATE_SECOND", "每秒委托笔数超过 3")

        daily = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM instructions
            WHERE type = 'order' AND status IN ('Sent', 'Approved') AND review_time >= ?
            """,
            (today,),
        ).fetchone()["c"]
        if daily >= 100:
            return RiskResult(False, "ORDER_RATE_DAILY", "每日委托笔数超过 100")

        open_orders = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM orders WHERE status NOT IN ({','.join('?' for _ in TERMINAL_STATUSES)})",
            tuple(TERMINAL_STATUSES),
        ).fetchone()["c"]
        if open_orders >= 2:
            return RiskResult(False, "OPEN_ORDER_LIMIT", "未完结订单数超过 2")

        amount = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS a FROM trades WHERE created_at >= ?",
            (one_min,),
        ).fetchone()["a"]
        if float(amount) >= 100000:
            return RiskResult(False, "TURNOVER_LIMIT", "每分钟成交额超过 100000")

        return RiskResult(True)

    @staticmethod
    def _check_price(instrument_id: str | None, price: float | None) -> RiskResult:
        if price is None:
            return RiskResult(True)
        if price <= 0:
            return RiskResult(False, "PRICE_INVALID", "下单价格必须大于 0")
        if price > MAX_ORDER_PRICE:
            return RiskResult(False, "PRICE_TOO_HIGH", f"下单价格不能超过 {MAX_ORDER_PRICE:g}")

        if instrument_id not in available_instruments():
            return RiskResult(False, "INSTRUMENT_NOT_TRADABLE", "合约不在当前可交易合约列表中")

        prefix = instrument_id[:2]
        lower, upper = PRICE_LIMITS[prefix]
        if price < lower or price > upper:
            return RiskResult(False, "PRICE_OUT_OF_RANGE", f"{instrument_id} 下单价格必须在 {lower:g}-{upper:g} 之间")
        return RiskResult(True)

    def check_cancel(self, order_id: str) -> RiskResult:
        order = self.conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not order:
            return RiskResult(False, "ORDER_NOT_FOUND", "订单不存在")
        if order["status"] in TERMINAL_STATUSES:
            return RiskResult(False, "ORDER_TERMINAL", "终态订单不允许撤单")
        if not int(order["exchange_accepted"]) and int(order["traded_volume"]) == 0:
            return RiskResult(False, "ORDER_NOT_CONFIRMED", "订单尚未被交易所确认")

        now = now_ms()
        last = self.conn.execute(
            "SELECT last_request_at FROM cancel_requests WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if last and now - int(last["last_request_at"]) < 1000:
            return RiskResult(False, "CANCEL_DEBOUNCE", "同一订单 1 秒内不允许重复撤单")
        self.conn.execute(
            """
            INSERT INTO cancel_requests(order_id, last_request_at)
            VALUES (?, ?)
            ON CONFLICT(order_id) DO UPDATE SET last_request_at = excluded.last_request_at
            """,
            (order_id, now),
        )
        self.conn.commit()
        return RiskResult(True)
