from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    UNCONFIRMED = "kUnconfirmed"
    REJECTED = "kRejected"
    QUEUEING = "kQueueing"
    ALL_TRADED = "kAllTraded"
    CANCELED = "kCanceled"


class Side(str, Enum):
    BUY = "kBuy"
    SELL = "kSell"


class InstructionStatus(str, Enum):
    PENDING = "PendingReview"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    RISK_REJECTED = "RiskRejected"
    SENT = "Sent"


class PlaceOrderRequest(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=32)
    side: Side
    price: float = Field(gt=0)
    volume: int = Field(gt=0, le=1_000_000)


class CancelOrderRequest(BaseModel):
    order_id: str


class ExchangeOrderRequest(PlaceOrderRequest):
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    account_id: str = "direct"


class LoginRequest(BaseModel):
    username: str
    password: str


class ReviewRequest(BaseModel):
    decision: Literal["Approved", "Rejected"]
    reason: Optional[str] = None


class ExchangeEvent(BaseModel):
    event_id: str
    order_id: str
    instrument_id: str
    gen_time: int
    status: Optional[OrderStatus] = None
    traded_volume: int = 0
    canceled_volume: int = 0
    trade_price: Optional[float] = None
    message: Optional[str] = None


TERMINAL_STATUSES = {
    OrderStatus.REJECTED.value,
    OrderStatus.ALL_TRADED.value,
    OrderStatus.CANCELED.value,
}
