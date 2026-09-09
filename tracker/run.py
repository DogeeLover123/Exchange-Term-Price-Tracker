"""python -m tracker.run [--dry-run] [--only id1,id2] [--no-email] [--force-verify]"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from . import dashboard
from .models import load_destinations, load_settings
from .notify import build_email, send_email
from .scoring import (build_estimate, deal_reasons, load_history, pick_best_per_month, save_history)
from .sources import google_flights, ryanair, travelpayouts

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run")


def scan_flights(dest: dict, settings: dict, stats: dict):
    start = date.fromisoformat(settings["search_window"]["start"])
    end = date.fromisoformat(settings["search_window"]["end"])
    today = date.today()
    start = max(start, today)
    mn, mx = settings["trip_nights"]["min"], settings["trip_nights"]["max"]
    quotes = []
    for origin in settings["origin_airports"]:
        for ap in dest["airports"]:
            if settings["sources"].get("ryanair"):
                q = ryanair.search(origin, ap, start, end, mn, mx)
                stats["ryanair"] += len(q)
                quotes += q
            if settings["sources"].get("travelpayouts"):
                q = travelpayouts.search(origin, ap, start, end, mn, mx)
                stats["travelpayouts"] += len(q)
                quotes += q
    return quotes


def _csv(v):
    return {x.strip().lower() for x in v.split(",") if x.strip()} if v else set()


def select_destinations(dests: list[dict], settings: dict, args) -> list[dict]:
    """Country / destination toggles: CLI flags win over settings.yaml; empty = all."""
    countries = _csv(args.countries) or {c.lower() for c in settings.get("countries") or []}
    skip_c = _csv(args.skip_countries) or {c.lower() for c in settings.get("skip_countries") or []}
    only = _csv(args.only) or {x.lower() for x in settings.get("only_destinations") or []}
    skip_d = {x.lower() for x in settings.get("skip_destinations") or []}
    out = []
    for d in dests:
        c, i = d["country"].lower(), d["id"].lower()
        if countries and c not in countries: continue
        if c in skip_c: continue
        if only and i not in only: continue
        if i in skip_d: continue
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't save history or email")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--only", help="comma-separated destination ids (overrides settings)")
    ap.add_argument("--countries", help="comma-separated countries to track this run (overrides settings)")
    ap.add_argument("--skip-countries", help="comma-separated countries to skip this run")
    ap.add_argument("--from", dest="date_from", help="override search window start YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="override search window end YYYY-MM-DD")
    ap.add_argument("--force-verify", action="store_true", help="verify every in-season destination (uses quota!)")
    args = ap.parse_args(argv)

    settings = load_settings()
    if args.date_from:
        settings["search_window"]["start"] = args.date_from
    if args.date_to:
        settings["search_window"]["end"] = args.date_to
    dests = select_destinations(load_destinations(), settings, args)
    log.info("tracking %d destinations in %s, window %s → %s", len(dests),
             sorted({d["country"] for d in dests}), settings["search_window"]["start"], settings["search_window"]["end"])
    history = load_history()
    alert = settings["alert"]
    stats = {"ryanair": 0, "travelpayouts": 0, "google": 0, "fast_flights": 0}

    # 1. scan + cost
    estimates = []
    for d in dests:
        quotes = scan_flights(d, settings, stats)
        bests = pick_best_per_month(quotes)   # one candidate per departure month
        if not bests:
            log.info("%s: no flights found", d["id"])
            continue
        for best in bests:
            est = build_estimate(d, best, settings)
            estimates.append(est)
            log.info("%s %s: €%.0f flight %s→%s %s (%s) total €%.0f", d["id"], best.out_date.strftime("%b"),
                     best.price, best.origin, best.dest_airport, best.out_date, best.source, est.total)

    # 2. pick candidates to verify on Google
    by_id = {d["id"]: d for d in dests}
    candidates = []
    for est in estimates:
        reasons = deal_reasons(est, history, by_id[est.dest_id], alert)
        if reasons or (args.force_verify and est.in_season):
            candidates.append((est, reasons))
    candidates.sort(key=lambda x: x[0].total)
    candidates = candidates[: alert["max_google_verifications"]]

    if settings["sources"].get("serpapi") and candidates:
        pool = google_flights.SerpApiPool(reserve=alert["min_serpapi_searches_left"])
        for est, _ in candidates:
            f = est.flight
            price, src = google_flights.verify(pool, f.origin, f.dest_airport, f.out_date, f.ret_date,
                                               settings["sources"].get("fast_flights_fallback", False))
            if price is not None:
                f.verified, f.verified_price = True, price
                stats[src] += 1
                log.info("verified %s: €%.0f (%s) vs %s €%.0f", est.dest_id, price, src, f.source, f.price)
        quota = pool.summary
    else:
        quota = "not used"

    # 3. recompute deals after verification, rank everything
    deals = []
    for est in estimates:
        reasons = deal_reasons(est, history, by_id[est.dest_id], alert)
        if reasons:
            deals.append((est, reasons))
    deals.sort(key=lambda x: x[0].total)
    cheapest: dict[str, object] = {}
    for e in sorted(estimates, key=lambda e: (not e.in_season, e.total)):
        cheapest.setdefault(e.dest_id, e)          # first = best in-season, else best overall
    ranked = list(cheapest.values())

    # 4. persist
    if not args.dry_run:
        history["runs"].append({"at": datetime.utcnow().isoformat(), "estimates": [e.to_dict() for e in estimates]})
        history["runs"] = history["runs"][-90:]
        save_history(history)
        dashboard.write(estimates, deals)

    # 5. notify
    subject, body = build_email(deals, ranked, quota, stats)
    log.info("subject: %s", subject)
    if args.dry_run or args.no_email:
        print("\n".join(f"{e.total:7.0f}  {e.name}  [{e.flight.out_date}→{e.flight.ret_date} {e.flight.source}]"
                        + ("  " + "; ".join(r) if r else "") for e, r in ([(e, []) for e in ranked])))
        return 0
    if deals or alert.get("digest_always"):
        send_email(subject, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
