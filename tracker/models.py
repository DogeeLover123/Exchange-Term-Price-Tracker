from __future__ import annotations

import os
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
