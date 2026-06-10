from __future__ import annotations

import asyncio
import random
import uuid

from .db import now_ms
from .models import ExchangeEvent, OrderStatus

FIRST_TRADE_DELAY_RANGE = (3.0, 5.0)
NEXT_TRADE_DELAY_RANGE = (2.0, 5.0)
TRADE_PROBABILITY = 0.35
FULL_FILL_PROBABILITY = 0.15


class SimulatedExchange:
    def __init__(self, event_queue: asyncio.Queue[ExchangeEvent]):
        self.event_queue = event_queue
        self._active: dict[str, dict] = {}

    async def place_order(self, order: dict) -> None:
        self._active[order["order_id"]] = order
        asyncio.create_task(self._simulate_order(order))

    async def cancel_order(self, order: dict) -> None:
        await asyncio.sleep(random.uniform(0.05, 0.2))
        await self.event_queue.put(
            ExchangeEvent(
                event_id=self._event_id(),
                order_id=order["order_id"],
                instrument_id=order["instrument_id"],
                gen_time=now_ms(),
                status=OrderStatus.CANCELED,
                canceled_volume=max(0, int(order["original_volume"]) - int(order["traded_volume"])),
                message="cancel accepted",
            )
        )
        self._active.pop(order["order_id"], None)

    async def _simulate_order(self, order: dict) -> None:
        await asyncio.sleep(random.uniform(0.05, 0.25))
        if random.random() < 0.15:
            await self.event_queue.put(
                ExchangeEvent(
                    event_id=self._event_id(),
                    order_id=order["order_id"],
                    instrument_id=order["instrument_id"],
                    gen_time=now_ms(),
                    status=OrderStatus.REJECTED,
                    message="random reject",
                )
            )
            self._active.pop(order["order_id"], None)
            return

        await self.event_queue.put(
            ExchangeEvent(
                event_id=self._event_id(),
                order_id=order["order_id"],
                instrument_id=order["instrument_id"],
                gen_time=now_ms(),
                status=OrderStatus.QUEUEING,
                message="accepted",
            )
        )

        traded = 0
        original = int(order["original_volume"])
        first_trade = True
        while order["order_id"] in self._active and traded < original:
            delay_range = FIRST_TRADE_DELAY_RANGE if first_trade else NEXT_TRADE_DELAY_RANGE
            await asyncio.sleep(random.uniform(*delay_range))
            first_trade = False
            if order["order_id"] not in self._active:
                return
            remaining = original - traded
            if random.random() < TRADE_PROBABILITY:
                if random.random() < FULL_FILL_PROBABILITY:
                    qty = remaining
                else:
                    max_partial = max(1, remaining // 2)
                    qty = random.randint(1, max_partial)
                traded += qty
                await self.event_queue.put(
                    ExchangeEvent(
                        event_id=self._event_id(),
                        order_id=order["order_id"],
                        instrument_id=order["instrument_id"],
                        gen_time=now_ms(),
                        status=OrderStatus.ALL_TRADED if traded == original else OrderStatus.QUEUEING,
                        traded_volume=traded,
                        trade_price=float(order["price"]),
                        message="trade",
                    )
                )
            if traded == original:
                self._active.pop(order["order_id"], None)

    @staticmethod
    def _event_id() -> str:
        return f"evt-{uuid.uuid4().hex}"
