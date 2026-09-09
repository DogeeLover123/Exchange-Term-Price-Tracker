from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from typing import Optional

from .models import (DATA_DIR, FlightQuote, TripEstimate, booking_hostels_link,
                     google_flights_link, hostelworld_link, hostelz_link, rome2rio_link)

HISTORY = DATA_DIR / "history.json"


# ------------------------------------------------------------- costing ----
def build_estimate(dest: dict, flight: FlightQuote, settings: dict) -> TripEstimate:
    nights = flight.nights
    hostel = dest.get("hostel") or {}
    per_night = 0.0 if dest["id"] in (settings.get("free_stay") or []) else float(
        hostel.get("per_night", settings.get("default_hostel_per_night", 30)))
    transit = sum(float(leg.get("cost", 0)) for leg in dest.get("transit", []))
    extras_cfg = dest.get("extras")
    extras = float(extras_cfg["cost"]) if extras_cfg else 0.0
    in_season = flight.out_date.month in dest.get("open_months", list(range(1, 13)))

    city = hostel.get("city", dest["name"])
    links = {
        "flight": flight.link or google_flights_link(flight.origin, flight.dest_airport, flight.out_date, flight.ret_date),
        "google_flights": google_flights_link(flight.origin, flight.dest_airport, flight.out_date, flight.ret_date),
        "hostelworld": hostelworld_link(city, flight.out_date, flight.ret_date),
        "hostelz": hostelz_link(city),
        "booking_hostels": booking_hostels_link(city, flight.out_date, flight.ret_date),
        "transit": rome2rio_link(flight.dest_airport, city),
    }
    for leg in dest.get("transit", []):
        if leg.get("link"):
            links[f"transit: {leg['name']}"] = leg["link"]

    flags = []
    if not in_season:
        flags.append("OUT OF SEASON")
    if per_night == 0 and hostel:
        flags.append("free stay (friend)")
    if flight.source == "travelpayouts" and not flight.verified:
        flags.append("cached price – verify")

    return TripEstimate(
        dest_id=dest["id"], name=dest["name"], country=dest["country"], flight=flight,
        bag=float(settings.get("cabin_bag_estimate", 0)), transit=transit,
        hostel_per_night=per_night, hostel_total=per_night * nights, extras=extras,
        in_season=in_season, closure_note=dest.get("closure_note", ""), links=links, flags=flags,
    )


_RANK = {"google": 0, "ryanair": 1, "fast_flights": 2, "travelpayouts": 3}


def pick_best(quotes: list[FlightQuote]) -> Optional[FlightQuote]:
    """Cheapest quote; prefer Ryanair on ties (exact) over cached sources."""
    if not quotes:
        return None
    return min(quotes, key=lambda q: (q.price, _RANK.get(q.source, 9)))


def pick_best_per_month(quotes: list[FlightQuote]) -> list[FlightQuote]:
    """Cheapest quote for EACH departure month, so a cheap October trip never hides
    a cheap December one and alerts fire independently for every future month."""
    by_month: dict[str, list[FlightQuote]] = {}
    for q in quotes:
        by_month.setdefault(q.out_date.strftime("%Y-%m"), []).append(q)
    return [pick_best(v) for _, v in sorted(by_month.items())]


# ------------------------------------------------------------- history ----
def load_history() -> dict:
    if HISTORY.exists():
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)
    return {"runs": []}


def save_history(h: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=1, ensure_ascii=False)


def _month(e: dict) -> str:
    return e["flight"]["out_date"][:7]


def _matches(e: dict, run: dict, est: TripEstimate, fixed: bool) -> bool:
    """Window mode compares against window-mode scans of the same destination-month; fixed-dates
    mode only against fixed-dates scans of the same destination on the same exact days."""
    if e["dest_id"] != est.dest_id or (run.get("mode", "window") == "fixed") != fixed:
        return False
    if fixed:
        return (e["flight"]["out_date"], e["flight"]["ret_date"]) == (est.flight.out_date.isoformat(),
                                                                       est.flight.ret_date.isoformat())
    return _month(e) == est.flight.out_date.strftime("%Y-%m")


def past_prices(history: dict, est: TripEstimate, days: int = 30, fixed: bool = False) -> list[float]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    out = []
    for run in history["runs"]:
        if datetime.fromisoformat(run["at"]) < cutoff:
            continue
        for e in run["estimates"]:
            if _matches(e, run, est, fixed):
                out.append(e["flight"]["verified_price"] or e["flight"]["price"])
    return out


def all_time_low(history: dict, est: TripEstimate, fixed: bool = False) -> Optional[float]:
    vals = [e["flight"]["verified_price"] or e["flight"]["price"]
            for run in history["runs"] for e in run["estimates"] if _matches(e, run, est, fixed)]
    return min(vals) if vals else None


def deal_reasons(est: TripEstimate, history: dict, dest: dict, alert: dict, fixed: bool = False) -> list[str]:
    if not est.in_season:
        return []
    reasons = []
    threshold = float(dest.get("alert_below", alert["total_below"]))
    if est.total <= threshold:
        reasons.append(f"all-in €{est.total:.0f} ≤ €{threshold:.0f}")
    min_eur = float(alert.get("drop_min_eur", 0))
    recent = past_prices(history, est, int(alert.get("norm_days", 30)), fixed)
    if len(recent) >= 2:                       # need some history before "the norm" means anything
        norm = statistics.median(recent)
        drop_eur = norm - est.flight_price
        drop_pct = drop_eur / norm * 100 if norm else 0
        if drop_pct >= alert["drop_pct"] and drop_eur >= min_eur:
            reasons.append(f"flight €{drop_eur:.0f} ({drop_pct:.0f}%) under its {len(recent)}-scan norm of €{norm:.0f}")
    low = all_time_low(history, est, fixed)
    if alert.get("new_low") and low is not None and low - est.flight_price >= max(min_eur, 1):
        reasons.append(f"new low: €{low - est.flight_price:.0f} under previous best €{low:.0f}")
    return reasons


def pick_best_per_dates(quotes: list[FlightQuote], ranges: list[tuple[date, date]]) -> list[FlightQuote]:
    """Fixed-dates mode: cheapest quote for EACH exact (out, back) pair the user asked for."""
    out = []
    for a, b in ranges:
        best = pick_best([q for q in quotes if q.out_date == a and q.ret_date == b])
        if best:
            out.append(best)
    return out
