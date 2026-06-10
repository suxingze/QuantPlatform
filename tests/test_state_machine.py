import sqlite3

from app.db import init_db, now_ms
from app.instruments import default_instrument
from app.models import ExchangeEvent, OrderStatus
from app.risk import RiskEngine
from app.state_machine import OrderStateManager

INSTRUMENT_ID = default_instrument()


def make_manager():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    manager = OrderStateManager(conn)
    manager.create_order(
        order_id="ord-1",
        client_order_id="cli-1",
        instruction_id=1,
        instrument_id=INSTRUMENT_ID,
        side="kBuy",
        price=3500,
        volume=10,
    )
    return conn, manager


def event(event_id, status=None, traded=0, canceled=0):
    return ExchangeEvent(
        event_id=event_id,
        order_id="ord-1",
        instrument_id=INSTRUMENT_ID,
        gen_time=now_ms(),
        status=status,
        traded_volume=traded,
        canceled_volume=canceled,
        trade_price=3500 if traded else None,
    )


def test_duplicate_queueing_is_idempotent():
    _, manager = make_manager()
    order, inserted = manager.apply_event(event("e1", OrderStatus.QUEUEING))
    assert inserted is True
    assert order["status"] == "kQueueing"

    order, inserted = manager.apply_event(event("e1", OrderStatus.QUEUEING))
    assert inserted is False
    assert order["status"] == "kQueueing"
    assert order["version"] == 1


def test_trade_before_queueing_does_not_regress():
    _, manager = make_manager()
    order, _ = manager.apply_event(event("e1", OrderStatus.QUEUEING, traded=4))
    assert order["status"] == "kQueueing"
    assert order["traded_volume"] == 4

    order, _ = manager.apply_event(event("e2", OrderStatus.QUEUEING))
    assert order["status"] == "kQueueing"
    assert order["traded_volume"] == 4


def test_cancel_before_partial_trade_converges():
    _, manager = make_manager()
    order, _ = manager.apply_event(event("e1", OrderStatus.CANCELED, canceled=10))
    assert order["status"] == "kCanceled"
    assert order["canceled_volume"] == 10

    order, _ = manager.apply_event(event("e2", OrderStatus.QUEUEING, traded=3))
    assert order["status"] == "kCanceled"
    assert order["traded_volume"] == 3
    assert order["canceled_volume"] == 7


def test_all_traded_wins_over_cancel():
    _, manager = make_manager()
    order, _ = manager.apply_event(event("e1", OrderStatus.ALL_TRADED, traded=10))
    assert order["status"] == "kAllTraded"

    order, _ = manager.apply_event(event("e2", OrderStatus.CANCELED, canceled=10))
    assert order["status"] == "kAllTraded"
    assert order["traded_volume"] == 10
    assert order["canceled_volume"] == 0


def test_reject_after_trade_does_not_override():
    _, manager = make_manager()
    order, _ = manager.apply_event(event("e1", OrderStatus.QUEUEING, traded=2))
    assert order["traded_volume"] == 2

    order, _ = manager.apply_event(event("e2", OrderStatus.REJECTED))
    assert order["status"] == "kQueueing"
    assert order["traded_volume"] == 2


def test_turnover_limit_rejects_at_limit():
    conn, _ = make_manager()
    conn.execute(
        """
        INSERT INTO trades(event_id, order_id, instrument_id, side, price, volume, amount, created_at)
        VALUES ('trade-limit', 'ord-1', ?, 'kBuy', 1000, 100, 100000, ?)
        """,
        (INSTRUMENT_ID, now_ms()),
    )
    conn.commit()

    result = RiskEngine(conn).check_place_order()
    assert result.ok is False
    assert result.code == "TURNOVER_LIMIT"


def test_price_band_rejects_out_of_range():
    conn, _ = make_manager()

    result = RiskEngine(conn).check_place_order(instrument_id=INSTRUMENT_ID, price=8000)

    assert result.ok is False
    assert result.code == "PRICE_OUT_OF_RANGE"


def test_global_price_limit_rejects_extreme_price():
    conn, _ = make_manager()

    result = RiskEngine(conn).check_place_order(instrument_id="CUSTOM", price=1_000_001)

    assert result.ok is False
    assert result.code == "PRICE_TOO_HIGH"


def test_rejects_non_tradable_instrument():
    conn, _ = make_manager()

    result = RiskEngine(conn).check_place_order(instrument_id="IF9999", price=3500)

    assert result.ok is False
    assert result.code == "INSTRUMENT_NOT_TRADABLE"
