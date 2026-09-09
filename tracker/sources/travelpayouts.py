"""Travelpayouts Data API – cached Aviasales prices, all airlines.

Docs: https://travelpayouts.github.io/slate/  (prices_for_dates, v3)
Token: env TRAVELPAYOUTS_TOKEN. Prices may be 1–2 days stale.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime

import requests

from ..models import FlightQuote

log = logging.getLogger(__name__)
API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{y}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def search(origin: str, dest: str, start: date, end: date, min_n: int, max_n: int) -> list[FlightQuote]:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return []
    quotes: list[FlightQuote] = []
    for month in _months(start, end):
        try:
            r = requests.get(API, params={
                "origin": origin, "destination": dest, "departure_at": month,
                "return_at": month, "currency": "eur", "unique": "false",
                "sorting": "price", "direct": "false", "limit": 50, "token": token,
            }, timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception as e:
            log.info("travelpayouts %s-%s %s: %s", origin, dest, month, e)
            continue
        for it in data:
            try:
                out = datetime.fromisoformat(it["departure_at"][:10]).date()
                ret = datetime.fromisoformat(it["return_at"][:10]).date()
            except Exception:
                continue
            n = (ret - out).days
            if out < start or ret > end or n < min_n or n > max_n:
                continue
            quotes.append(FlightQuote(
                origin=origin, dest_airport=dest, out_date=out, ret_date=ret,
                price=float(it["price"]), airline=it.get("airline", "?"),
                source="travelpayouts",
                link="https://www.aviasales.com" + it.get("link", ""),
            ))
    return quotes
