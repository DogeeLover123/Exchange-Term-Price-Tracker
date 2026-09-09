from __future__ import annotations

import html
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .dashboard import save_link
from .models import TripEstimate

log = logging.getLogger(__name__)


def _links_html(links: dict) -> str:
    order = ["flight", "google_flights", "hostelworld", "hostelz", "booking_hostels", "transit"]
    labels = {"flight": "Book flight", "google_flights": "Google Flights", "hostelworld": "Hostelworld",
              "hostelz": "Hostelz", "booking_hostels": "Booking (hostels)", "transit": "Rome2Rio"}
    parts = [f'<a href="{html.escape(links[k])}">{labels[k]}</a>' for k in order if links.get(k)]
    if links.get("save"):
        parts.insert(0, f'<a href="{html.escape(links["save"])}" style="font-weight:bold">★ Save</a>')
    parts += [f'<a href="{html.escape(v)}">{html.escape(k.replace("transit: ", ""))}</a>'
              for k, v in links.items() if k.startswith("transit: ")]
    return " · ".join(parts)


def _row(est: TripEstimate, reasons: list[str] | None = None) -> str:
    f = est.flight
    if save_link(est):
        est.links["save"] = save_link(est)
    src = f"{f.source}" + (" ✔ google" if f.verified else "")
    style = ' style="background:#e8f7e8"' if reasons else (' style="color:#999"' if not est.in_season else "")
    note = " · ".join(est.flags + ([est.closure_note] if est.closure_note else []))
    why = f'<div style="color:#2a7d2a;font-size:12px">{html.escape("; ".join(reasons))}</div>' if reasons else ""
    return f"""<tr{style}>
<td><b>{html.escape(est.name)}</b><br><span style="font-size:12px">{html.escape(est.country)}</span>{why}</td>
<td>{f.out_date:%d %b} → {f.ret_date:%d %b} ({f.nights}n)<br><span style="font-size:12px">{f.origin}→{f.dest_airport} {html.escape(f.airline)}</span></td>
<td align="right">€{est.flight_price:.0f}<br><span style="font-size:11px">{src}</span></td>
<td align="right">€{est.bag:.0f}</td>
<td align="right">€{est.transit:.0f}</td>
<td align="right">€{est.hostel_total:.0f}<br><span style="font-size:11px">€{est.hostel_per_night:.0f}/n</span></td>
<td align="right">€{est.extras:.0f}</td>
<td align="right"><b>€{est.total:.0f}</b></td>
<td style="font-size:12px">{html.escape(note)}<br>{_links_html(est.links)}</td>
</tr>"""


def _chain_row(it) -> str:
    stops = "<br>".join(
        f"{html.escape(s.name.split(' –')[0])} <span style=\"font-size:11px\">({s.airport}, {s.arrive:%d %b}–{s.leave:%d %b}, "
        f"{s.nights}n, €{s.ground:.0f} ground{'' if s.in_season else ', OUT OF SEASON'})</span>" for s in it.stops)
    legs = "<br>".join(
        f'<a href="{html.escape(l.link)}">{l.day:%d %b} {l.origin}→{l.dest}</a> €{l.price:.0f} '
        f'<span style="font-size:11px">{html.escape(l.airline)}{"" if l.source == "ryanair" else " (cached – verify)"}</span>'
        for l in it.legs)
    style = "" if it.all_in_season else ' style="color:#999"'
    return (f"<tr{style}><td><b>{html.escape(it.route)}</b><br><span style=\"font-size:12px\">{it.legs[0].day:%d %b} → "
            f"{it.legs[-1].day:%d %b} · {it.nights} nights · €{it.total / max(it.nights, 1):.0f}/night</span></td>"
            f"<td style=\"font-size:12px\">{stops}</td><td style=\"font-size:12px\">{legs}</td>"
            f"<td align=\"right\">€{it.flights:.0f}</td><td align=\"right\">€{it.bags:.0f}</td>"
            f"<td align=\"right\">€{it.ground:.0f}</td><td align=\"right\"><b>€{it.total:.0f}</b></td></tr>")


def build_email(deals: list[tuple[TripEstimate, list[str]]], ranked: list[TripEstimate],
                quota_summary: str, source_stats: dict, chains=None) -> tuple[str, str]:
    head = "<tr><th>Trip</th><th>Dates</th><th>Flight</th><th>Bag</th><th>Transit</th><th>Hostel</th><th>Extras</th><th>Total</th><th>Notes & links</th></tr>"
    css = "<style>table{border-collapse:collapse;font-family:sans-serif;font-size:13px}td,th{border:1px solid #ddd;padding:6px;vertical-align:top}th{background:#f3f3f3}</style>"
    parts = [css]
    if deals:
        parts.append(f"<h2>🔥 {len(deals)} deal(s)</h2><table>{head}" + "".join(_row(e, r) for e, r in deals) + "</table>")
    else:
        parts.append("<h2>No new deals this run</h2>")
    parts.append(f"<h2>All destinations, cheapest all-in (any month in the window)</h2><table>{head}" + "".join(_row(e) for e in ranked) + "</table>")
    if chains:
        chead = "<tr><th>Route</th><th>Stops</th><th>One-way legs</th><th>Flights</th><th>Bags</th><th>Ground</th><th>Total</th></tr>"
        parts.append(f"<h2>🔗 Multi-city chains – {len(chains)} route(s) from one-way fares</h2>"
                     "<p style='font-size:12px;color:#555'>Several places in one trip: each leg is a separate one-way ticket "
                     "(book each link). Ground = hostel × nights + transit + extras per stop. Grey = a stop is out of season.</p>"
                     f"<table>{chead}" + "".join(_chain_row(c) for c in chains) + "</table>")
    dash = os.environ.get("DASHBOARD_URL")
    if dash:
        parts.append(f"<p><a href='{html.escape(dash)}'>Open the trail board</a> – Latest and Saved tabs, share links for friends.</p>")
    parts.append(f"<p style='font-size:11px;color:#777'>Sources: {html.escape(str(source_stats))}<br>SerpApi quota: {html.escape(quota_summary)}<br>"
                 "Hostel = baseline estimate; transit = fixed recipe; flight = live. Grey rows are out of season.</p>")
    subject = (f"✈ {len(deals)} trip deal(s): " + ", ".join(f"{e.name.split(' –')[0]} ({e.flight.out_date:%b})" for e, _ in deals[:3])) if deals else "✈ Trip tracker digest – no new deals"
    if chains:
        subject += f" · 🔗 {chains[0].route} €{chains[0].total:.0f}"
    return subject, "".join(parts)


def send_email(subject: str, body_html: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO", user)
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    log.info("email sent to %s", to)
