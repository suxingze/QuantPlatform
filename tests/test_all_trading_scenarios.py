import sqlite3
import uuid
from datetime import date

from app.db import init_db, now_ms
from app.instruments import available_instruments, contract_months, default_instrument
from app.models import ExchangeEvent, OrderStatus
from app.risk import RiskEngine
from app.state_machine import OrderStateManager

INSTRUMENT_ID = default_instrument()


def test_current_tradable_if_ic_im_contracts_are_generated():
    months = contract_months(date(2026, 6, 10))
    instruments = available_instruments(date(2026, 6, 10))

    assert months == [(2026, 6), (2026, 7), (2026, 9), (2026, 12)]
    assert instruments == [
        "IF2606", "IF2607", "IF2609", "IF2612",
        "IC2606", "IC2607", "IC2609", "IC2612",
        "IM2606", "IM2607", "IM2609", "IM2612",
    ]


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def make_order(conn, order_id=None, volume=10, price=3500):
    manager = OrderStateManager(conn)
    order_id = order_id or f"ord-{uuid.uuid4().hex[:12]}"
    order = manager.create_order(
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        instruction_id=None,
        instrument_id=INSTRUMENT_ID,
        side="kBuy",
        price=price,
        volume=volume,
    )
    return manager, order


def report(order_id, status=None, traded=0, canceled=0, price=3500, event_id=None):
    return ExchangeEvent(
        event_id=event_id or f"evt-{uuid.uuid4().hex}",
        order_id=order_id,
        instrument_id=INSTRUMENT_ID,
        gen_time=now_ms(),
        status=status,
        traded_volume=traded,
        canceled_volume=canceled,
        trade_price=price if traded else None,
    )


def insert_order_instruction(conn, *, status="Sent", review_time=None):
    ts = review_time or now_ms()
    conn.execute(
        """
        INSERT INTO instructions(
            type, status, submitter_id, reviewer_id, review_time,
            instrument_id, side, price, volume, created_at, updated_at
        )
        VALUES ('order', ?, 1, 2, ?, ?, 'kBuy', 3500, 1, ?, ?)
        """,
        (status, ts, INSTRUMENT_ID, ts, ts),
    )
    conn.commit()


def test_new_order_starts_unconfirmed():
    conn = make_conn()
    _, order = make_order(conn)

    assert order["status"] == "kUnconfirmed"
    assert order["traded_volume"] == 0
    assert order["canceled_volume"] == 0
    assert order["exchange_accepted"] == 0


def test_rejected_report_before_acceptance_makes_order_rejected():
    conn = make_conn()
    manager, order = make_order(conn)

    updated, inserted = manager.apply_event(report(order["order_id"], OrderStatus.REJECTED))

    assert inserted is True
    assert updated["status"] == "kRejected"
    assert updated["traded_volume"] == 0
    assert updated["canceled_volume"] == 0


def test_queueing_partial_trade_and_all_traded_flow():
    conn = make_conn()
    manager, order = make_order(conn)
    order_id = order["order_id"]

    queued, _ = manager.apply_event(report(order_id, OrderStatus.QUEUEING))
    partial, _ = manager.apply_event(report(order_id, OrderStatus.QUEUEING, traded=4))
    final, _ = manager.apply_event(report(order_id, OrderStatus.ALL_TRADED, traded=10))

    assert queued["status"] == "kQueueing"
    assert partial["status"] == "kQueueing"
    assert partial["traded_volume"] == 4
    assert final["status"] == "kAllTraded"
    assert final["traded_volume"] == 10
    assert final["canceled_volume"] == 0

    trade = conn.execute("SELECT SUM(volume) AS volume, SUM(amount) AS amount FROM trades").fetchone()
    assert trade["volume"] == 10
    assert trade["amount"] == 35000


def test_duplicate_report_is_idempotent():
    conn = make_conn()
    manager, order = make_order(conn)
    event_id = "same-event-id"

    first, inserted = manager.apply_event(report(order["order_id"], OrderStatus.QUEUEING, traded=3, event_id=event_id))
    second, duplicated = manager.apply_event(report(order["order_id"], OrderStatus.QUEUEING, traded=3, event_id=event_id))

    assert inserted is True
    assert duplicated is False
    assert second["traded_volume"] == 3
    assert second["version"] == first["version"]
    assert conn.execute("SELECT COUNT(*) FROM order_events WHERE event_id = ?", (event_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT COALESCE(SUM(volume), 0) FROM trades").fetchone()[0] == 3


def test_out_of_order_trade_before_queueing_does_not_regress():
    conn = make_conn()
    manager, order = make_order(conn)
    order_id = order["order_id"]

    traded, _ = manager.apply_event(report(order_id, OrderStatus.QUEUEING, traded=4))
    stale_queueing, _ = manager.apply_event(report(order_id, OrderStatus.QUEUEING))

    assert traded["status"] == "kQueueing"
    assert stale_queueing["status"] == "kQueueing"
    assert stale_queueing["traded_volume"] == 4
    assert stale_queueing["exchange_accepted"] == 1


def test_out_of_order_cancel_before_trade_converges_quantities():
    conn = make_conn()
    manager, order = make_order(conn)
    order_id = order["order_id"]

    canceled, _ = manager.apply_event(report(order_id, OrderStatus.CANCELED, canceled=10))
    late_trade, _ = manager.apply_event(report(order_id, OrderStatus.QUEUEING, traded=3))

    assert canceled["status"] == "kCanceled"
    assert late_trade["status"] == "kCanceled"
    assert late_trade["traded_volume"] == 3
    assert late_trade["canceled_volume"] == 7


def test_all_traded_wins_over_late_cancel_report():
    conn = make_conn()
    manager, order = make_order(conn)
    order_id = order["order_id"]

    filled, _ = manager.apply_event(report(order_id, OrderStatus.ALL_TRADED, traded=10))
    late_cancel, _ = manager.apply_event(report(order_id, OrderStatus.CANCELED, canceled=10))

    assert filled["status"] == "kAllTraded"
    assert late_cancel["status"] == "kAllTraded"
    assert late_cancel["traded_volume"] == 10
    assert late_cancel["canceled_volume"] == 0


def test_consistency_constraints_clip_over_sized_reports():
    conn = make_conn()
    manager, order = make_order(conn, volume=10)
    order_id = order["order_id"]

    filled, _ = manager.apply_event(report(order_id, OrderStatus.ALL_TRADED, traded=99))
    late_cancel, _ = manager.apply_event(report(order_id, OrderStatus.CANCELED, canceled=99))

    assert filled["status"] == "kAllTraded"
    assert filled["traded_volume"] == 10
    assert filled["canceled_volume"] == 0
    assert late_cancel["traded_volume"] == 10
    assert late_cancel["canceled_volume"] == 0


def test_place_order_risk_rejects_invalid_prices():
    conn = make_conn()
    risk = RiskEngine(conn)

    assert risk.check_place_order(instrument_id=INSTRUMENT_ID, price=0).code == "PRICE_INVALID"
    assert risk.check_place_order(instrument_id=INSTRUMENT_ID, price=8000).code == "PRICE_OUT_OF_RANGE"
    assert risk.check_place_order(instrument_id="CUSTOM", price=1_000_001).code == "PRICE_TOO_HIGH"


def test_place_order_risk_rejects_rate_limits():
    conn = make_conn()
    risk = RiskEngine(conn)
    for _ in range(3):
        insert_order_instruction(conn)

    result = risk.check_place_order(instrument_id=INSTRUMENT_ID, price=3500)

    assert result.ok is False
    assert result.code == "ORDER_RATE_SECOND"


def test_place_order_risk_rejects_daily_limit():
    conn = make_conn()
    risk = RiskEngine(conn)
    old_enough_to_avoid_second_limit = now_ms() - 2000
    for _ in range(100):
        insert_order_instruction(conn, review_time=old_enough_to_avoid_second_limit)

    result = risk.check_place_order(instrument_id=INSTRUMENT_ID, price=3500)

    assert result.ok is False
    assert result.code == "ORDER_RATE_DAILY"


def test_place_order_risk_rejects_open_order_limit():
    conn = make_conn()
    make_order(conn, order_id="ord-open-1")
    make_order(conn, order_id="ord-open-2")

    result = RiskEngine(conn).check_place_order(instrument_id=INSTRUMENT_ID, price=3500)

    assert result.ok is False
    assert result.code == "OPEN_ORDER_LIMIT"


def test_place_order_risk_rejects_turnover_limit():
    conn = make_conn()
    conn.execute(
        """
        INSERT INTO trades(event_id, order_id, instrument_id, side, price, volume, amount, created_at)
        VALUES ('trade-limit', 'ord-1', ?, 'kBuy', 1000, 100, 100000, ?)
        """,
        (INSTRUMENT_ID, now_ms()),
    )
    conn.commit()

    result = RiskEngine(conn).check_place_order(instrument_id=INSTRUMENT_ID, price=3500)

    assert result.ok is False
    assert result.code == "TURNOVER_LIMIT"


def test_cancel_risk_rejects_not_found_unconfirmed_and_terminal_orders():
    conn = make_conn()
    risk = RiskEngine(conn)

    assert risk.check_cancel("missing-order").code == "ORDER_NOT_FOUND"

    manager, unconfirmed = make_order(conn, order_id="ord-unconfirmed")
    assert risk.check_cancel(unconfirmed["order_id"]).code == "ORDER_NOT_CONFIRMED"

    manager.apply_event(report(unconfirmed["order_id"], OrderStatus.REJECTED))
    assert risk.check_cancel(unconfirmed["order_id"]).code == "ORDER_TERMINAL"


def test_cancel_risk_accepts_confirmed_order_and_debounces_repeat_cancel():
    conn = make_conn()
    manager, order = make_order(conn)
    manager.apply_event(report(order["order_id"], OrderStatus.QUEUEING))
    risk = RiskEngine(conn)

    first = risk.check_cancel(order["order_id"])
    second = risk.check_cancel(order["order_id"])

    assert first.ok is True
    assert second.ok is False
    assert second.code == "CANCEL_DEBOUNCE"
