# Trip Tracker – all-in hiking trip price watcher (Madrid, Sept–Dec)

Every 2 days: scans flights from Madrid to ~40 hiking destinations, adds cabin bag,
ground transit recipe, hostel baseline and lifts, checks the season window, verifies
the best deals on Google Flights, and emails you a ranked digest with booking links.

## How it works

```
Ryanair fare-finder  ─┐  (free, exact base fare, unofficial)
Travelpayouts API    ─┤─▶ cheapest round trip per destination ─▶ + bag + transit + hostel + extras
                      │                                              │
                      │           deals (under threshold / dropped / new low)
                      │                                              ▼
SerpApi Google Flights (keys rotated, quota tracked) ◀── verify top 8 ──▶ email digest + history.json
   └ fast-flights scraper as no-quota fallback
```

**What's live vs estimated**

| Item | Source | Accuracy |
|---|---|---|
| Flight | Ryanair (exact) / Travelpayouts (cached, ±10–20%) / Google (verified) | good |
| Cabin bag | fixed estimate (`cabin_bag_estimate`) | ±€10 |
| Transit | hand-written recipe per destination | ±€5 |
| Hostel | baseline €/night per destination | ±25% – click the link for live prices |
| Season / closures | `open_months` + `closure_note` per destination | rule-based, not live |

Each destination is tracked **per departure month** (Sept, Oct, Nov, Dec) independently, so a
December bargain is flagged today even when October is cheaper.

No public hostel or bus price APIs exist for individuals, so those are estimates with
deep links (Hostelworld, Hostelz, Booking hostel filter, Rome2Rio, operator sites).

## Put it on GitHub (2 minutes)

```bash
# 1. create an empty repo on github.com (name it trip-tracker, public so Pages works on the free plan)
# 2. in the unzipped folder:
git init
git add .
git commit -m "trip tracker"
git branch -M main
git remote add origin https://github.com/<you>/trip-tracker.git
git push -u origin main
```

Your keys never go in the repo — they go in Settings → Secrets (next section). `.gitignore` already
excludes `.env`.

## Setup (15 minutes)

1. **Push this folder to a GitHub repo** (see above).
2. **Get free tokens**
   - Travelpayouts: sign up at travelpayouts.com → Tools → API → copy token.
   - SerpApi: serpapi.com → free plan (250 searches/mo) → API key. Several keys can be
     comma-separated in `SERPAPI_KEYS`; they're rotated and each one's remaining quota is checked.
3. **Email**: Gmail → Google Account → Security → 2-Step Verification → App passwords → create one.
4. **Repo → Settings → Secrets and variables → Actions → New repository secret** for each of:
   `TRAVELPAYOUTS_TOKEN`, `SERPAPI_KEYS`, `SMTP_HOST` (smtp.gmail.com), `SMTP_PORT` (587),
   `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
5. **Dashboard (Trail board)**: Settings → Pages → Source "Deploy from a branch" → branch `main`, folder `/docs`.
   Then Settings → Secrets and variables → Actions → **Variables** → `DASHBOARD_URL` =
   `https://<you>.github.io/<repo>` (this turns on the ★ Save links in the email).
   Pages on a private repo needs GitHub Pro; on the free plan make the repo public — it only contains
   the destination list and price history, never your keys.
6. Actions tab → **trip-tracker → Run workflow** for a force run. It then runs itself every 2 days
   and commits `data/history.json` back (which also keeps GitHub from disabling the schedule).

### Run locally instead / as well

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in
export $(grep -v '^#' .env | xargs)
python -m tracker.run --dry-run        # prints ranking, no email, no history
python -m tracker.run                  # real run
python -m tracker.run --only madeira,toubkal --no-email
python -m tracker.run --dates "Oct 3-10"        # price exactly depart 3 Oct / back 10 Oct
python -m tracker.run --force-verify   # Google-check every in-season trip (burns quota)
```

## Using it day to day

- **Every 2 days at 08:00 Madrid** an email arrives: a 🔥 deals table (if any) and the full ranking,
  each row with Book flight / Google Flights / Hostelworld / Hostelz / Booking / Rome2Rio / ★ Save links.
- **Want a run now?** GitHub → Actions → trip-tracker → Run workflow. Optional boxes: limit to some
  countries, cap the date, price **exact dates** (e.g. `Oct 3-10`), or "force verify" everything on
  Google (uses quota).
- **Found something good?** Click ★ Save in the email (or Save on the board). It's in the Saved tab
  with the price you saw; the next scans tell you if it's moved.
- **Sharing with friends:** Saved tab → Copy share link → send it.
- **Changing what you track:** edit `config/settings.yaml` (countries, dates, thresholds) or
  `config/destinations.yaml` (add a trip, fix a hostel price) and push — the next run picks it up.
- **Something looks off?** Open the workflow run's log, or run `python -m tracker.run --dry-run` locally.
  The "Sources" line at the bottom of every email shows how many prices each source returned.

## Trail board (dashboard) – Latest / Saved / share

- **Latest**: every destination-month from the last scan, ranked by all-in cost, filter box, deals-only toggle.
- **Save**: press Save on any row, or click ★ Save in the email — the trip is stored in that browser
  with the price it had when you saved it, so later you see "saved at €149 · now down €12".
- **Saved tab**: your list, re-priced against each new scan; trips that fall out of the window stay
  listed as "no longer in the latest scan".
- **Share**: "Copy share link" packs your saved trips into the URL. Friends open it, see the same
  trips with current prices, and they're added to their own Saved tab. No accounts, no server.
- **Download saved.json** to back up or move your list to another device.

Limitation: saved trips live in the browser (localStorage), not in the repo, so use the same device
or the share link to move them.

## Choosing what to track (e.g. only Sept–Oct, only the Alps + Bulgaria)

In `config/settings.yaml`:

```yaml
countries: [Bulgaria, Switzerland, Austria, France, Italy]   # empty = all
skip_countries: [Morocco, Portugal]
only_destinations: []            # or pin specific ids
skip_destinations: [vitosha]
search_window: {start: "2026-09-12", end: "2026-10-31"}
```

One-off overrides without editing the file — locally:

```bash
python -m tracker.run --countries Bulgaria,Switzerland --to 2026-10-31
```

or on GitHub: Actions → trip-tracker → Run workflow → fill "countries" / "date_to".
The scheduled run always uses `settings.yaml`. Fewer destinations = faster runs and more Google
quota per destination, so narrowing is worth doing whenever you know your booking horizon.

## Exact dates (e.g. "I can only go October 3–10")

By default the tracker hunts for the cheapest 2–5 night trip anywhere in the window. If your dates are
fixed, tell it so and it prices **only** flights that leave and return on those exact days, one row per
date range per destination:

```bash
python -m tracker.run --dates "Oct 3-10"                       # month name, year = next occurrence
python -m tracker.run --dates "2026-10-03:2026-10-10"           # ISO out:back
python -m tracker.run --dates "Oct 3-10, Nov 5-9, Dec 28 - Jan 3" # several ranges
```

On GitHub: Actions → trip-tracker → Run workflow → fill the **dates** box. To make it permanent (every
scheduled run), set it in `config/settings.yaml`:

```yaml
fixed_dates: ["Oct 3-10"]     # [] = back to window mode
```

`--dates` overrides `fixed_dates`, which overrides `search_window` / `trip_nights`. Ranges whose
departure is already past are skipped with a warning. Price history is still kept per destination-month,
so drop/new-low alerts compare against earlier scans of that month.

How exact dates are priced: Ryanair is asked for that exact day pair (reliable). Travelpayouts is a
cache of other people's searches, so it is often empty for a specific pair. Destinations that neither
source can price are looked up on Google Flights, up to `alert.google_fill_fixed_dates` per run
(default 15, in-season first) so a full 40-destination run doesn't drain the free SerpApi quota. Narrow
with `--countries` or `--only` when you want every destination priced on your dates.

## Tuning

- `config/settings.yaml`: date window, trip length, bag cost, alert thresholds, which sources are on,
  `free_stay` (destinations where you sleep at a friend's → hostel = €0).
- `config/destinations.yaml`: add/remove trips, adjust hostel baselines, transit recipes, `open_months`,
  `alert_below` per destination. Airports list is searched in full, so add alternatives freely.
- Quota maths: 15 runs/month × up to 8 verifications = 120 Google searches/month per run-set, so one
  free SerpApi key is usually enough; `min_serpapi_searches_left` keeps a reserve.

## Known limitations

- Ryanair endpoint is unofficial; if it changes, the run continues on Travelpayouts only (check the
  "Sources" line at the bottom of the email — `ryanair: 0` means it's broken; `pip install -U ryanair-py`).
- GitHub runners use US IPs; if Ryanair starts blocking them, run locally (same command).
- Hostel search links are best-effort URL templates; if a site changes its URL format the link still
  lands on the site, just without dates prefilled.
- Travelpayouts has thin data on rarely-searched routes (e.g. MAD→Sofia mid-week); Ryanair covers those.
