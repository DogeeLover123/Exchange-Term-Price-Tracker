"""Ryanair fare-finder via the `ryanair` (ryanair-py) package.

One call per (origin, dest, month) returns the cheapest return trips in that
window. Unofficial endpoint: if it breaks, this source silently yields nothing
and the other sources carry the run.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from ..models import FlightQuote, ryanair_link

log = logging.getLogger(__name__)

try:
    from ryanair import Ryanair  # type: ignore
    _api = Ryanair(currency="EUR")
except Exception as e:  # pragma: no cover
    _api = None
    log.warning("ryanair-py unavailable: %s", e)


def _month_windows(start: date, end: date):
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(end, nxt - timedelta(days=1))
        cur = nxt


def search(origin: str, dest: str, start: date, end: date, min_n: int, max_n: int) -> list[FlightQuote]:
    if _api is None:
        return []
    quotes: list[FlightQuote] = []
    for w_start, w_end in _month_windows(start, end):
        try:
            trips = _api.get_cheapest_return_flights(
                origin, w_start, w_end,
                w_start + timedelta(days=min_n), min(end, w_end + timedelta(days=max_n)),
                destination_airport=dest,
            )
        except Exception as e:
            log.info("ryanair %s-%s %s: %s", origin, dest, w_start, e)
            continue
        for t in trips or []:
            try:
                out = t.outbound.departureTime.date()
                ret = t.inbound.departureTime.date()
            except Exception:
                continue
            n = (ret - out).days
            if n < min_n or n > max_n:
                continue
            quotes.append(FlightQuote(
                origin=origin, dest_airport=dest, out_date=out, ret_date=ret,
                price=float(t.totalPrice), airline="Ryanair", source="ryanair",
                link=ryanair_link(origin, dest, out, ret),
            ))
    return quotes
