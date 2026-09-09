from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


def load_yaml(name: str):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_settings() -> dict:
    return load_yaml("settings.yaml")


def load_destinations() -> list[dict]:
    return load_yaml("destinations.yaml")


@dataclass
class FlightQuote:
    origin: str
    dest_airport: str
    out_date: date
    ret_date: date
    price: float                 # EUR, round trip, no bags
    airline: str
    source: str                  # ryanair | travelpayouts | google | fast_flights
    link: str = ""
    verified: bool = False       # confirmed by Google Flights
    verified_price: Optional[float] = None

    @property
    def nights(self) -> int:
        return (self.ret_date - self.out_date).days

    def to_dict(self):
        d = asdict(self)
        d["out_date"] = self.out_date.isoformat()
        d["ret_date"] = self.ret_date.isoformat()
        return d


@dataclass
class TripEstimate:
    dest_id: str
    name: str
    country: str
    flight: FlightQuote
    bag: float
    transit: float
    hostel_per_night: float
    hostel_total: float
    extras: float
    in_season: bool
    closure_note: str
    links: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    @property
    def flight_price(self) -> float:
        return self.flight.verified_price if self.flight.verified_price is not None else self.flight.price

    @property
    def total(self) -> float:
        return round(self.flight_price + self.bag + self.transit + self.hostel_total + self.extras, 2)

    def to_dict(self):
        return {
            "dest_id": self.dest_id,
            "name": self.name,
            "country": self.country,
            "flight": self.flight.to_dict(),
            "bag": self.bag,
            "transit": self.transit,
            "hostel_per_night": self.hostel_per_night,
            "hostel_total": self.hostel_total,
            "extras": self.extras,
            "in_season": self.in_season,
            "closure_note": self.closure_note,
            "total": self.total,
            "links": self.links,
            "flags": self.flags,
        }


# ---------------------------------------------------------------- links ----
def google_flights_link(origin: str, dest: str, out: date, ret: date) -> str:
    q = f"Flights from {origin} to {dest} on {out.isoformat()} through {ret.isoformat()}"
    return "https://www.google.com/travel/flights?q=" + quote_plus(q)


def ryanair_link(origin: str, dest: str, out: date, ret: date) -> str:
    return (
        "https://www.ryanair.com/es/en/trip/flights/select?adults=1&isReturn=true"
        f"&dateOut={out.isoformat()}&dateIn={ret.isoformat()}"
        f"&originIata={origin}&destinationIata={dest}"
    )


def hostelworld_link(city: str, out: date, ret: date) -> str:
    return (
        "https://www.hostelworld.com/search?search_keywords=" + quote_plus(city)
        + f"&date_from={out.isoformat()}&date_to={ret.isoformat()}&number_of_guests=1"
    )


def hostelz_link(city: str) -> str:
    return "https://www.hostelz.com/search?q=" + quote_plus(city)


def booking_hostels_link(city: str, out: date, ret: date) -> str:
    # nflt=ht_id%3D203 filters to hostels
    return (
        "https://www.booking.com/searchresults.html?ss=" + quote_plus(city)
        + f"&checkin={out.isoformat()}&checkout={ret.isoformat()}"
        + "&group_adults=1&no_rooms=1&nflt=ht_id%3D203&order=price"
    )


def rome2rio_link(dest_airport: str, city: str) -> str:
    return f"https://www.rome2rio.com/s/{dest_airport}/{quote_plus(city)}"


def env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


# ------------------------------------------------------------ fixed dates ----
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _next_occurrence(month: int, day: int, today: date) -> date:
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def parse_date_ranges(spec, today: Optional[date] = None) -> list[tuple[date, date]]:
    """Parse exact trip dates (out, back). Accepts a string or a list of strings:

        "Oct 3-10"                    month name + days, year = next occurrence
        "October 3 - 10"
        "2026-10-03:2026-10-10"       ISO out:back
        "2026-10-03:10"               ISO out, back = same month day 10
        "Oct 3-10, Nov 5-9"           several ranges, comma-separated
    """
    today = today or date.today()
    if isinstance(spec, (list, tuple)):
        spec = ",".join(str(s) for s in spec)
    out: list[tuple[date, date]] = []
    for part in (p.strip() for p in str(spec or "").split(",")):
        if not part:
            continue
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?::|\.\.)\s*(\d{4}-\d{2}-\d{2}|\d{1,2})", part)
        if m:
            a = date.fromisoformat(m.group(1))
            b = date.fromisoformat(m.group(2)) if len(m.group(2)) > 2 else a.replace(day=int(m.group(2)))
        else:
            m = re.fullmatch(r"([A-Za-z]+)\.?\s*(\d{1,2})\s*(?:-|–|to)\s*(?:([A-Za-z]+)\.?\s*)?(\d{1,2})", part)
            if not m or m.group(1)[:3].lower() not in _MONTHS:
                raise ValueError(f"can't parse dates {part!r}; use 'Oct 3-10' or '2026-10-03:2026-10-10'")
            m1 = _MONTHS[m.group(1)[:3].lower()]
            m2 = _MONTHS[m.group(3)[:3].lower()] if m.group(3) else m1
            a = _next_occurrence(m1, int(m.group(2)), today)
            b = date(a.year, m2, int(m.group(4)))
            if b < a and m2 != m1:          # e.g. "Dec 28 - Jan 3" wraps into next year
                b = date(a.year + 1, m2, int(m.group(4)))
        if b <= a:
            raise ValueError(f"return date must be after departure in {part!r}")
        out.append((a, b))
    return out


def format_date_ranges(ranges: list[tuple[date, date]]) -> str:
    return ", ".join(f"{a:%d %b} → {b:%d %b} ({(b - a).days}n)" for a, b in ranges)
