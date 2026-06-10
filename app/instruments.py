from __future__ import annotations

import calendar
from datetime import date

INDEX_FUTURE_PREFIXES = ("IF", "IC", "IM")
QUARTER_MONTHS = (3, 6, 9, 12)


def third_friday(year: int, month: int) -> date:
    fridays = [
        day
        for day in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, day).weekday() == 4
    ]
    return date(year, month, fridays[2])


def add_month(year: int, month: int, offset: int = 1) -> tuple[int, int]:
    zero_based = year * 12 + month - 1 + offset
    return zero_based // 12, zero_based % 12 + 1


def contract_months(today: date | None = None) -> list[tuple[int, int]]:
    today = today or date.today()
    year, month = today.year, today.month
    if today > third_friday(year, month):
        year, month = add_month(year, month)

    current = (year, month)
    next_month = add_month(year, month)
    months = [current, next_month]

    cursor_year, cursor_month = next_month
    while len(months) < 4:
        cursor_year, cursor_month = add_month(cursor_year, cursor_month)
        if cursor_month in QUARTER_MONTHS:
            months.append((cursor_year, cursor_month))
    return months


def available_instruments(today: date | None = None) -> list[str]:
    months = contract_months(today)
    return [
        f"{prefix}{year % 100:02d}{month:02d}"
        for prefix in INDEX_FUTURE_PREFIXES
        for year, month in months
    ]


def default_instrument(today: date | None = None) -> str:
    return available_instruments(today)[0]
