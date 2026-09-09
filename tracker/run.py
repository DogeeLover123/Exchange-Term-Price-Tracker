"""python -m tracker.run [--dry-run] [--only id1,id2] [--dates "Oct 3-10"] [--no-email] [--force-verify]"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from . import chain as chain_mod, dashboard
from .models import (FlightQuote, format_date_ranges, google_flights_link, load_destinations, load_settings,
                     parse_date_ranges)
from .notify import build_email, send_email
from .scoring import (build_estimate, deal_reasons, load_history, pick_best_per_dates, pick_best_per_month,
                      save_history)
from .sources import google_flights, ryanair, travelpayouts

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run")


def scan_flights(dest: dict, settings: dict, stats: dict, ranges=None):
    """All quotes for one destination. With `ranges` (fixed-dates mode) each source is asked
    for exactly those out/back days instead of the cheapest trips in the window."""
    mn, mx = settings["trip_nights"]["min"], settings["trip_nights"]["max"]
    quotes = []
    for origin in settings["origin_airports"]:
        for ap in dest["airports"]:
            if ranges:
                for a, b in ranges:
                    if settings["sources"].get("ryanair"):
                        q = ryanair.search_exact(origin, ap, a, b)
                        stats["ryanair"] += len(q)
                        quotes += q
                    if settings["sources"].get("travelpayouts"):
                        q = travelpayouts.search_exact(origin, ap, a, b)
                        stats["travelpayouts"] += len(q)
                        quotes += q
                continue
            start = max(date.fromisoformat(settings["search_window"]["start"]), date.today())
            end = date.fromisoformat(settings["search_window"]["end"])
            if settings["sources"].get("ryanair"):
                q = ryanair.search(origin, ap, start, end, mn, mx)
                stats["ryanair"] += len(q)
                quotes += q
            if settings["sources"].get("travelpayouts"):
                q = travelpayouts.search(origin, ap, start, end, mn, mx)
                stats["travelpayouts"] += len(q)
                quotes += q
    return quotes


def google_fill(dests: list[dict], estimates: list, ranges, settings: dict, pool, stats: dict) -> list:
    """Fixed-dates mode: destinations with no Ryanair/Travelpayouts price for a date range get one
    Google Flights lookup each (in-season first, first airport only), capped by
    alert.google_fill_fixed_dates so a full 40-destination run can't drain the SerpApi quota."""
    budget = int(settings["alert"].get("google_fill_fixed_dates", 0))
    if not ranges or budget <= 0 or pool is None:
        return []
    have = {(e.dest_id, e.flight.out_date, e.flight.ret_date) for e in estimates}
    todo = [(d, a, b) for d in dests for a, b in ranges if (d["id"], a, b) not in have]
    todo.sort(key=lambda x: a_in_season(x[0], x[1]) is False)   # in-season first
    filled = []
    origin = settings["origin_airports"][0]
    allow_fb = settings["sources"].get("fast_flights_fallback", False)
    for d, a, b in todo[:budget]:
        ap = d["airports"][0]
        price, src = google_flights.verify(pool, origin, ap, a, b, allow_fb)
        if price is None:
            log.info("%s %s→%s: no Google price either", d["id"], a, b)
            continue
        stats[src] += 1
        q = FlightQuote(origin=origin, dest_airport=ap, out_date=a, ret_date=b, price=price, airline="?",
                        source=src, link=google_flights_link(origin, ap, a, b),
                        verified=(src == "google"), verified_price=price if src == "google" else None)
        est = build_estimate(d, q, settings)
        log.info("%s %s: €%.0f flight %s→%s %s (%s, filled) total €%.0f", d["id"], a.strftime("%b"),
                 price, origin, ap, a, src, est.total)
        filled.append(est)
    skipped = len(todo) - min(len(todo), budget)
    if skipped:
        log.info("fixed dates: %d destination/date pairs left unpriced (google_fill_fixed_dates=%d)", skipped, budget)
    return filled


def a_in_season(dest: dict, out: date) -> bool:
    return out.month in dest.get("open_months", list(range(1, 13)))


def _csv(v):
    return {x.strip().lower() for x in v.split(",") if x.strip()} if v else set()


def apply_fixed_dates(settings: dict, cli_value: str | None) -> list[tuple[date, date]]:
    """Fixed-dates mode: CLI --dates wins over settings `fixed_dates`; empty = normal window mode.
    Narrows the search window and trip-length filter so the sources only fetch what's needed."""
    ranges = parse_date_ranges(cli_value if cli_value else settings.get("fixed_dates") or [])
    today = date.today()
    past = [r for r in ranges if r[0] < today]
    for a, b in past:
        log.warning("skipping %s → %s: departure is in the past", a, b)
    ranges = [r for r in ranges if r[0] >= today]
    if ranges:
        settings["search_window"] = {"start": min(a for a, _ in ranges).isoformat(),
                                     "end": max(b for _, b in ranges).isoformat()}
        nights = [(b - a).days for a, b in ranges]
        settings["trip_nights"] = {"min": min(nights), "max": max(nights)}
    return ranges


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
    ap.add_argument("--dates", help="exact trip dates instead of a window, e.g. 'Oct 3-10' or "
                                    "'2026-10-03:2026-10-10'; several comma-separated (overrides settings)")
    ap.add_argument("--force-verify", action="store_true", help="verify every in-season destination (uses quota!)")
    ap.add_argument("--chain", action="store_true", help="also build multi-city itineraries from one-way fares "
                                                        "(window must be ≤ chain.max_window_days)")
    args = ap.parse_args(argv)

    settings = load_settings()
    if args.date_from:
        settings["search_window"]["start"] = args.date_from
    if args.date_to:
        settings["search_window"]["end"] = args.date_to
    ranges = apply_fixed_dates(settings, args.dates)
    dests = select_destinations(load_destinations(), settings, args)
    if ranges:
        log.info("tracking %d destinations in %s, exact dates: %s", len(dests),
                 sorted({d["country"] for d in dests}), format_date_ranges(ranges))
    else:
        log.info("tracking %d destinations in %s, window %s → %s", len(dests),
                 sorted({d["country"] for d in dests}), settings["search_window"]["start"], settings["search_window"]["end"])
    history = load_history()
    alert = settings["alert"]
    stats = {"ryanair": 0, "travelpayouts": 0, "google": 0, "fast_flights": 0}
    pool = google_flights.SerpApiPool(reserve=alert["min_serpapi_searches_left"]) if settings["sources"].get("serpapi") else None

    # 1. scan + cost
    estimates = []
    for d in dests:
        quotes = scan_flights(d, settings, stats, ranges)
        # one candidate per exact date range (fixed dates) or per departure month (window)
        bests = pick_best_per_dates(quotes, ranges) if ranges else pick_best_per_month(quotes)
        if not bests:
            log.info("%s: no flights found", d["id"])
            continue
        for best in bests:
            est = build_estimate(d, best, settings)
            estimates.append(est)
            log.info("%s %s: €%.0f flight %s→%s %s (%s) total €%.0f", d["id"], best.out_date.strftime("%b"),
                     best.price, best.origin, best.dest_airport, best.out_date, best.source, est.total)
    estimates += google_fill(dests, estimates, ranges, settings, pool, stats)

    # 2. pick candidates to verify on Google
    by_id = {d["id"]: d for d in dests}
    candidates = []
    for est in estimates:
        reasons = deal_reasons(est, history, by_id[est.dest_id], alert, bool(ranges))
        if reasons or (args.force_verify and est.in_season):
            candidates.append((est, reasons))
    candidates.sort(key=lambda x: x[0].total)
    candidates = candidates[: alert["max_google_verifications"]]

    if pool is not None and candidates:
        for est, _ in candidates:
            f = est.flight
            if f.verified:
                continue
            price, src = google_flights.verify(pool, f.origin, f.dest_airport, f.out_date, f.ret_date,
                                               settings["sources"].get("fast_flights_fallback", False))
            if price is not None:
                f.verified, f.verified_price = True, price
                stats[src] += 1
                log.info("verified %s: €%.0f (%s) vs %s €%.0f", est.dest_id, price, src, f.source, f.price)
    quota = pool.summary if pool is not None else "not used"

    # 3. recompute deals after verification, rank everything
    deals = []
    for est in estimates:
        reasons = deal_reasons(est, history, by_id[est.dest_id], alert, bool(ranges))
        if reasons:
            deals.append((est, reasons))
    deals.sort(key=lambda x: x[0].total)
    cheapest: dict[str, object] = {}
    for e in sorted(estimates, key=lambda e: (not e.in_season, e.total)):
        cheapest.setdefault(e.dest_id, e)          # first = best in-season, else best overall
    ranked = list(cheapest.values())

    # 3b. multi-city chains from one-way fares (short windows only)
    chains = []
    if args.chain or (settings.get("chain") or {}).get("enabled"):
        chains = chain_mod.run_chains(dests, settings, stats)
        for it in chains:
            log.info("chain €%.0f %s %s→%s: %s", it.total, it.route, it.legs[0].day, it.legs[-1].day,
                     " | ".join(f"{s.name.split(' –')[0]} {s.nights}n" for s in it.stops))

    # 4. persist
    if not args.dry_run:
        history["runs"].append({"at": datetime.utcnow().isoformat(), "mode": "fixed" if ranges else "window",
                                "dates": format_date_ranges(ranges) if ranges else None,
                                "estimates": [e.to_dict() for e in estimates],
                                "chains": [c.to_dict() for c in chains]})
        history["runs"] = history["runs"][-90:]
        save_history(history)
        dashboard.write(estimates, deals, chains, settings["search_window"])

    # 5. notify
    subject, body = build_email(deals, ranked, quota, stats, chains)
    log.info("subject: %s", subject)
    if args.dry_run or args.no_email:
        print("\n".join(f"{e.total:7.0f}  {e.name}  [{e.flight.out_date}→{e.flight.ret_date} {e.flight.source}]"
                        + ("  " + "; ".join(r) if r else "") for e, r in ([(e, []) for e in ranked])))
        for it in chains:
            print(f"{it.total:7.0f}  🔗 {it.route}  [{it.legs[0].day}→{it.legs[-1].day}]  "
                  + " | ".join(f"{s.name.split(' –')[0]} {s.nights}n" for s in it.stops))
        return 0
    if deals or alert.get("digest_always"):
        send_email(subject, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
