"""Multi-city itineraries from one-way fares: MAD → A → B (→ C) → MAD.

Why: a round trip is just two one-ways, and Ryanair prices every hop the same way, so chaining
cheap one-ways often beats flying home between trips. This module

  1. collects one-way legs for every day in the window from every airport in destinations.yaml
     (Ryanair fare-finder: one call per airport per day returns the cheapest fare to *every*
     destination that day; Travelpayouts one-way cache for the Madrid legs only),
  2. depth-first searches itineraries that leave Madrid, sleep `nights_per_stop` at each stop,
     never revisit a destination, and land back in Madrid before the window ends,
  3. costs each one exactly like a single trip (flights + bag per leg + hostel × nights + transit
     + extras per stop) and returns the cheapest unique routes.

The one-way scan is per airport × per day, so it is only allowed on short windows
(`chain.max_window_days`) – use --from/--to or --dates, e.g. "I'm free Sep 15-22".
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests

from .models import google_oneway_link, ryanair_oneway_link

log = logging.getLogger(__name__)
HOME_DEFAULT = "MAD"


@dataclass
class Leg:
    day: date
    origin: str
    dest: str
    price: float
    airline: str
    source: str            # ryanair | travelpayouts
    link: str

    def to_dict(self):
        d = self.__dict__.copy()
        d["day"] = self.day.isoformat()
        return d


@dataclass
class Stop:
    dest_id: str
    name: str
    country: str
    airport: str
    arrive: date
    leave: date
    hostel_per_night: float
    transit: float
    extras: float
    in_season: bool
    alternatives: list[str] = field(default_factory=list)   # other trips served by this airport

    @property
    def nights(self) -> int:
        return (self.leave - self.arrive).days

    @property
    def ground(self) -> float:
        return self.hostel_per_night * self.nights + self.transit + self.extras

    def to_dict(self):
        return {"dest_id": self.dest_id, "name": self.name, "country": self.country, "airport": self.airport,
                "arrive": self.arrive.isoformat(), "leave": self.leave.isoformat(), "nights": self.nights,
                "hostel_per_night": self.hostel_per_night, "transit": self.transit, "extras": self.extras,
                "ground": round(self.ground, 2), "in_season": self.in_season, "alternatives": self.alternatives}


@dataclass
class Itinerary:
    legs: list[Leg]
    stops: list[Stop]
    bag_per_leg: float

    @property
    def flights(self) -> float:
        return sum(l.price for l in self.legs)

    @property
    def bags(self) -> float:
        return self.bag_per_leg * len(self.legs)

    @property
    def ground(self) -> float:
        return sum(s.ground for s in self.stops)

    @property
    def total(self) -> float:
        return round(self.flights + self.bags + self.ground, 2)

    @property
    def route(self) -> str:
        return " → ".join([self.legs[0].origin] + [l.dest for l in self.legs])

    @property
    def nights(self) -> int:
        return (self.legs[-1].day - self.legs[0].day).days

    @property
    def all_in_season(self) -> bool:
        return all(s.in_season for s in self.stops)

    def to_dict(self):
        return {"route": self.route, "out": self.legs[0].day.isoformat(), "back": self.legs[-1].day.isoformat(),
                "nights": self.nights, "flights": round(self.flights, 2), "bags": round(self.bags, 2),
                "ground": round(self.ground, 2), "total": self.total, "in_season": self.all_in_season,
                "legs": [l.to_dict() for l in self.legs], "stops": [s.to_dict() for s in self.stops]}


# ---------------------------------------------------------------- legs ----
_tls = threading.local()


def _ryanair_api():
    if not hasattr(_tls, "api"):
        try:
            from ryanair import Ryanair  # type: ignore
            _tls.api = Ryanair(currency="EUR")
        except Exception as e:  # pragma: no cover
            log.warning("ryanair-py unavailable: %s", e)
            _tls.api = None
    return _tls.api


def _ryanair_day(origin: str, day: date, wanted: set[str], max_price: float) -> list[Leg]:
    api = _ryanair_api()
    if api is None:
        return []
    try:
        flights = api.get_cheapest_flights(origin, day, day) or []
    except Exception as e:
        log.debug("ryanair one-way %s %s: %s", origin, day, e)
        return []
    out = []
    for f in flights:
        try:
            if f.destination not in wanted or float(f.price) > max_price:
                continue
            d = f.departureTime.date() if hasattr(f.departureTime, "date") else datetime.fromisoformat(str(f.departureTime)).date()
            out.append(Leg(d, origin, f.destination, float(f.price), "Ryanair", "ryanair",
                           ryanair_oneway_link(origin, f.destination, d)))
        except Exception:
            continue
    return out


def _travelpayouts_oneway(origin: str, dest: str, start: date, end: date, max_price: float) -> list[Leg]:
    token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        return []
    out = []
    months, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    for month in months:
        try:
            r = requests.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates", params={
                "origin": origin, "destination": dest, "departure_at": month, "one_way": "true",
                "currency": "eur", "unique": "false", "sorting": "price", "direct": "false", "limit": 100,
                "token": token}, timeout=30)
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception as e:
            log.debug("travelpayouts one-way %s-%s %s: %s", origin, dest, month, e)
            continue
        for it in data:
            try:
                d = datetime.fromisoformat(it["departure_at"][:10]).date()
                p = float(it["price"])
            except Exception:
                continue
            if start <= d <= end and p <= max_price:
                out.append(Leg(d, origin, dest, p, it.get("airline", "?"), "travelpayouts",
                               "https://www.aviasales.com" + it.get("link", "")))
    return out


_RANK = {"ryanair": 0, "travelpayouts": 1}


def collect_legs(airports: set[str], home: str, start: date, end: date, cfg: dict, sources: dict,
                 stats: dict) -> dict[tuple[str, date], list[Leg]]:
    """{(origin, day): [cheapest Leg per destination]} for every airport and day in the window."""
    wanted = set(airports) | {home}
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    max_price = float(cfg.get("max_leg_price", 300))
    legs: list[Leg] = []
    if sources.get("ryanair", True):
        jobs = [(a, d) for a in sorted(wanted) for d in days]
        with ThreadPoolExecutor(max_workers=int(cfg.get("threads", 4))) as ex:
            for res in ex.map(lambda j: _ryanair_day(j[0], j[1], wanted, max_price), jobs):
                legs += res
        stats["oneway_ryanair"] = len(legs)
    if sources.get("travelpayouts", True):
        n0 = len(legs)
        for a in sorted(airports):
            legs += _travelpayouts_oneway(home, a, start, end, max_price)
            legs += _travelpayouts_oneway(a, home, start, end, max_price)
        stats["oneway_travelpayouts"] = len(legs) - n0
    best: dict[tuple[str, date, str], Leg] = {}
    for l in legs:
        k = (l.origin, l.day, l.dest)
        if k not in best or (l.price, _RANK.get(l.source, 9)) < (best[k].price, _RANK.get(best[k].source, 9)):
            best[k] = l
    edges: dict[tuple[str, date], list[Leg]] = {}
    for l in best.values():
        edges.setdefault((l.origin, l.day), []).append(l)
    for v in edges.values():
        v.sort(key=lambda l: l.price)
    log.info("one-way legs: %d unique (origin, day, dest) from %d airports × %d days", len(best), len(wanted), len(days))
    return edges


# -------------------------------------------------------------- search ----
def _stop_for(airport: str, arrive: date, leave: date, dests: list[dict], settings: dict, used: set[str]):
    """Best trip idea served by this airport for these dates (in-season first, then cheapest ground)."""
    free = set(settings.get("free_stay") or [])
    default_pn = float(settings.get("default_hostel_per_night", 30))
    options = []
    for d in dests:
        if airport not in d["airports"] or d["id"] in used:
            continue
        hostel = d.get("hostel") or {}
        pn = 0.0 if d["id"] in free else float(hostel.get("per_night", default_pn))
        transit = sum(float(l.get("cost", 0)) for l in d.get("transit", []))
        extras = float(d["extras"]["cost"]) if d.get("extras") else 0.0
        in_season = arrive.month in d.get("open_months", list(range(1, 13)))
        options.append(Stop(d["id"], d["name"], d["country"], airport, arrive, leave, pn, transit, extras, in_season))
    if not options:
        return None
    options.sort(key=lambda s: (not s.in_season, s.ground))
    best = options[0]
    best.alternatives = [o.name.split(" –")[0] for o in options[1:4]]
    return best


def find_chains(edges: dict, dests: list[dict], home: str, start: date, end: date, cfg: dict,
                settings: dict) -> list[Itinerary]:
    min_stops, max_stops = int(cfg.get("min_stops", 2)), int(cfg.get("max_stops", 3))
    n_min, n_max = int(cfg["nights_per_stop"]["min"]), int(cfg["nights_per_stop"]["max"])
    bag = float(settings.get("cabin_bag_estimate", 0)) / 2          # settings value is for a round trip
    top = int(cfg.get("top", 10))
    found: list[Itinerary] = []
    best_by_route: dict[str, float] = {}

    def worst_kept() -> float:
        return sorted(best_by_route.values())[top - 1] if len(best_by_route) >= top else float("inf")

    def rec(cur: str, arrive: date, legs: list[Leg], stops: list[Stop], used: set[str], partial: float):
        if partial >= worst_kept():
            return
        for n in range(n_min, n_max + 1):
            leave = arrive + timedelta(days=n)
            if leave > end:
                break
            stop = _stop_for(cur, arrive, leave, dests, settings, used)
            if stop is None:
                return
            here = partial + stop.ground
            if here >= worst_kept():
                continue
            for leg in edges.get((cur, leave), []):
                cost = here + leg.price + bag
                if cost >= worst_kept():
                    break                       # edges are price-sorted
                if leg.dest == home:
                    if len(stops) + 1 >= min_stops:
                        it = Itinerary(legs + [leg], stops + [stop], bag)
                        if it.total < best_by_route.get(it.route, float("inf")):
                            best_by_route[it.route] = it.total
                            found.append(it)
                elif len(stops) + 1 < max_stops and leg.dest not in {s.airport for s in stops} | {cur}:
                    rec(leg.dest, leave, legs + [leg], stops + [stop], used | {stop.dest_id}, cost)

    for d0 in (start + timedelta(days=i) for i in range((end - start).days + 1)):
        for leg in edges.get((home, d0), []):
            if leg.dest != home:
                rec(leg.dest, d0, [leg], [], set(), leg.price + bag)

    # cheapest per route, then top N routes
    uniq: dict[str, Itinerary] = {}
    for it in sorted(found, key=lambda i: i.total):
        uniq.setdefault(it.route, it)
    out = sorted(uniq.values(), key=lambda i: (not i.all_in_season, i.total))[:top]
    log.info("chains: %d itineraries, %d unique routes, showing %d", len(found), len(uniq), len(out))
    return out


def run_chains(dests: list[dict], settings: dict, stats: dict) -> list[Itinerary]:
    cfg = settings.get("chain") or {}
    start = max(date.fromisoformat(settings["search_window"]["start"]), date.today())
    end = date.fromisoformat(settings["search_window"]["end"])
    span = (end - start).days + 1
    limit = int(cfg.get("max_window_days", 21))
    if span > limit:
        log.warning("chain mode skipped: window is %d days, max is %d (chain.max_window_days). "
                    "Narrow it with --from/--to.", span, limit)
        return []
    home = settings["origin_airports"][0]
    airports = {a for d in dests for a in d["airports"]}
    edges = collect_legs(airports, home, start, end, cfg, settings.get("sources", {}), stats)
    return find_chains(edges, dests, home, start, end, cfg, settings)
