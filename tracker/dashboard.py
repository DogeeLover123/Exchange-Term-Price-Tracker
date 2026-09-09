"""Writes docs/data.json for the GitHub Pages dashboard (docs/index.html)."""
from __future__ import annotations

import json
import os
from datetime import datetime

from .models import ROOT, TripEstimate

DOCS = ROOT / "docs"


def trip_key(e: TripEstimate) -> str:
    f = e.flight
    return "|".join([e.dest_id, f.out_date.isoformat(), f.ret_date.isoformat(), f.dest_airport])


def save_link(e: TripEstimate) -> str | None:
    base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    return f"{base}/#save={trip_key(e)}" if base else None


def write(estimates: list[TripEstimate], deals: list[tuple[TripEstimate, list[str]]]) -> None:
    DOCS.mkdir(exist_ok=True)
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "estimates": [e.to_dict() for e in estimates],
        "deals": {trip_key(e): reasons for e, reasons in deals},
    }
    with open(DOCS / "data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
