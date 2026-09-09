"""Google Flights price verification.

Primary: SerpApi google_flights engine. Keys come from env SERPAPI_KEYS
(comma-separated). Each key's remaining monthly quota is checked once per run
and keys are used round-robin; a key is retired when it drops below the reserve.

Fallback: `fast_flights` scraper (free, no quota, breaks occasionally).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

import requests

from ..models import env_list, google_flights_link

log = logging.getLogger(__name__)


class SerpApiPool:
    def __init__(self, reserve: int = 20):
        self.reserve = reserve
        self.keys: list[dict] = []
        for k in env_list("SERPAPI_KEYS"):
            left = self._searches_left(k)
            self.keys.append({"key": k, "left": left, "used": 0})
            log.info("SerpApi key …%s: %s searches left", k[-4:], left)
        self._i = 0

    @staticmethod
    def _searches_left(key: str) -> int:
        try:
            r = requests.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=20)
            r.raise_for_status()
            return int(r.json().get("plan_searches_left", 0))
        except Exception as e:
            log.warning("SerpApi account check failed: %s", e)
            return 0

    def next_key(self) -> Optional[str]:
        usable = [k for k in self.keys if k["left"] > self.reserve]
        if not usable:
            return None
        k = usable[self._i % len(usable)]
        self._i += 1
        k["left"] -= 1
        k["used"] += 1
        return k["key"]

    @property
    def summary(self) -> str:
        return ", ".join(f"…{k['key'][-4:]}: {k['left']} left (used {k['used']})" for k in self.keys) or "no keys"


def _serpapi_price(key: str, origin: str, dest: str, out: date, ret: date) -> Optional[float]:
    try:
        r = requests.get("https://serpapi.com/search.json", params={
            "engine": "google_flights", "departure_id": origin, "arrival_id": dest,
            "outbound_date": out.isoformat(), "return_date": ret.isoformat(),
            "currency": "EUR", "hl": "en", "type": "1", "adults": "1", "api_key": key,
        }, timeout=45)
        r.raise_for_status()
        j = r.json()
        flights = (j.get("best_flights") or []) + (j.get("other_flights") or [])
        prices = [f["price"] for f in flights if isinstance(f.get("price"), (int, float))]
        return float(min(prices)) if prices else None
    except Exception as e:
        log.warning("serpapi %s-%s: %s", origin, dest, e)
        return None


def _fast_flights_price(origin: str, dest: str, out: date, ret: date) -> Optional[float]:
    try:
        from fast_flights import FlightData, Passengers, get_flights  # type: ignore
        res = get_flights(
            flight_data=[FlightData(date=out.isoformat(), from_airport=origin, to_airport=dest),
                         FlightData(date=ret.isoformat(), from_airport=dest, to_airport=origin)],
            trip="round-trip", seat="economy", passengers=Passengers(adults=1), fetch_mode="fallback",
        )
        prices = []
        for f in res.flights:
            m = re.search(r"[\d.,]+", str(f.price))
            if m:
                prices.append(float(m.group().replace(",", "")))
        return min(prices) if prices else None
    except Exception as e:
        log.info("fast_flights %s-%s: %s", origin, dest, e)
        return None


def verify(pool: SerpApiPool, origin: str, dest: str, out: date, ret: date, allow_fallback: bool) -> tuple[Optional[float], str]:
    """Returns (price, source). source in {google, fast_flights, none}."""
    key = pool.next_key()
    if key:
        p = _serpapi_price(key, origin, dest, out, ret)
        if p is not None:
            return p, "google"
    if allow_fallback:
        p = _fast_flights_price(origin, dest, out, ret)
        if p is not None:
            return p, "fast_flights"
    return None, "none"
