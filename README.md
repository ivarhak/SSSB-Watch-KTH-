# SSSB → KTH housing finder

A local tool that checks what SSSB student housing is currently available,
works out the commute to your campus for each area, and shows it all on a map —
sorted by ascending queue days, with a refresh button and a desktop
notification when something new gets published. It also pulls in Stockholm's
**Bostadsförmedlingen** listings alongside SSSB's — that's where Svenska
Bostäder advertises its student apartments, and it needs no login at all
(it's a public JSON feed).

**No SSSB login required either.** The vacancy list is public — queue days
included — so out of the box this asks for no credentials at all. There's a
`--with-login` escape hatch if SSSB ever changes that; see section 3.

Two pieces:
- `sssb_kth_monitor.py` — runs on your machine: Selenium scraping for SSSB, a plain HTTP fetch for Bostadsförmedlingen, commute math, and a small local API.
- `sssb_kth_dashboard.html` — the UI. Served by the script itself, so it's all same-origin (no CORS headaches).

## 1. Why this runs on your laptop

It's a personal tool that watches your queues and pops desktop notifications,
so it lives on your machine — no server to pay for, nothing to deploy. It also
keeps Chrome available as a fallback for reading SSSB's listing page, which is
rendered by their JavaScript app rather than served as plain HTML.

This is deliberately a laptop tool. Running it on a phone was tried and
abandoned: iOS suspends background processes, so the dashboard's local server
dies the moment you switch to Safari, and working around that added more
complexity than the convenience was worth.

## 2. Setup

I would highly recommend running this on Pycharm or similar

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

There might be some other modules you need, simply pip install when your computer says you dont have them

Chrome (or Chromium) needs to be installed — `webdriver-manager` handles the
matching driver automatically. It's only used as a fallback (see "How it reads
SSSB" in section 4), but keep it around so that fallback is there.

That's the whole setup — there's no login step. Cron and Task Scheduler runs
need nothing extra either, since nothing prompts for input.

<details>
<summary>If SSSB ever starts requiring a login again</summary>

Pass `--with-login`, and store credentials first with:

```bash
python sssb_kth_monitor.py --login
```

This prompts for your username and password (password input is hidden) and
asks if you want to save them to your computer's own secure keychain —
macOS Keychain, Windows Credential Locker, or Linux Secret Service,
depending on your OS, via the `keyring` package. They are **never written to
a file in this project folder**. `--forget-login` removes them again.

For unattended runs where there's no terminal to prompt on and no keyring
daemon (a headless Linux box, say), set `SSSB_USERNAME`/`SSSB_PASSWORD` as
environment variables in the crontab entry itself — the script reads those as
a fallback. That's fine security-wise since your crontab isn't part of this
project folder; just don't put those `export` lines in a script that lives in
here.

Note that `login()`'s field selectors have never been verified against SSSB's
real markup, because nobody has needed this path. If it fails, run
`--debug --with-login` and fix the selectors from `debug_page.html`.
</details>

Optional, for **real transit times** (otherwise you'll just get the
straight-line/bike estimate): get a free key for the **Resrobot** API at
[trafiklab.se](https://www.trafiklab.se/) (sign up → create a project →
add the "Resrobot v2.1" API), then:

```bash
export RESROBOT_API_KEY="your key"
```

## 3. If the scrape ever comes back empty

`scrape_listings()` finds real listings by looking for links containing
`refid=` in the URL (confirmed against a real SSSB booking link), then reads
the area/rent/size/queue-days/floor values out of the surrounding card text.
If it ever comes back empty while you can see real listings on the site
yourself:

```bash
python sssb_kth_monitor.py --debug
```

That runs a **visible** browser window (so you can watch what happens) and
saves the fully-rendered page to `debug_page.html`. Check whether that file
actually contains `refid=` anywhere (`grep -c "refid=" debug_page.html`) — if
SSSB changed their link format, that's the thing to update.

If instead the run reports listings but with empty queue days, SSSB may have
moved the "Ködagar" column behind a login again; try `--with-login` (see the
collapsed section above). The scrape prints a warning telling you so.

Bostadsförmedlingen needs no selector fixing at all, since
`fetch_bostadsformedlingen()` reads a plain JSON feed rather than scraped
markup. If its field names ever drift, the terminal prints the first ad's
actual keys on every run, so you can fix the candidate names in `_bf_field()`
from that alone.

## 4. Running it

**One-off check** (scrapes once, saves, notifies if there's something new, exits):
```bash
python sssb_kth_monitor.py --once
```

**Dashboard** (scrapes once if needed, then serves the UI + API):
```bash
python sssb_kth_monitor.py --serve
```
Open **http://localhost:5055** in your browser. It auto-checks SSSB in the
background every 15 minutes by default (change with `--interval 30`, don't
go below 5 — see rate-limiting note below), and the dashboard itself polls
for fresh results every 60 seconds, so you don't need to click anything for
it to notice new listings — you'll just see them appear, plus the desktop
notification. "Refresh listings" still triggers an immediate check on demand
instead of waiting for the next scheduled one.

> Opening `sssb_kth_dashboard.html` directly as a file (or previewing it in
> Claude) shows example data with a banner saying so — the live version only
> works served from `http://localhost:5055` since that's what makes the
> `/api/...` calls same-origin.

### How it reads SSSB

A normal run first tries reading the vacancy list with a plain HTTP request,
and only launches Chrome if that comes back empty — which makes the usual case
noticeably faster, since no browser has to start at all. You don't need to
think about this; it's automatic.

`--http-only` forces the fast path and fails loudly instead of falling back,
which is handy for checking whether it still works. If the page ever does need
a real browser, that run tells you so and prints any API-looking URLs it found
in the page, since one of those is probably the endpoint the page fetches its
listings from.

## 5. Getting notified automatically

If you leave `python sssb_kth_monitor.py --serve` running, you're already
covered — its background auto-check (every 15 min by default) fires the
same desktop notification on new listings as `--once` does. The cron/Task
Scheduler route below is only needed if you'd rather *not* keep the
dashboard process running all the time and just want periodic checks:

**macOS/Linux (cron)** — checks every 30 min:
```bash
crontab -e
# add:
*/30 * * * * cd /path/to/sssb-kth-tool && /path/to/venv/bin/python sssb_kth_monitor.py --once >> cron.log 2>&1
```

**Windows (Task Scheduler)**: create a basic task that runs
`venv\Scripts\python.exe sssb_kth_monitor.py --once` every 30 minutes,
with "Start in" set to this folder.

## 6. Notes / known limitations

- **Coordinates**: area coordinates are looked up automatically via
  OpenStreetMap (free, no key) and cached in `data/geocode_cache.json`. If a
  pin looks wrong on the map, open that file and hand-correct the
  `[lat, lon]` for that area.
- **Campus dropdown**: the "Campus" picker in the top bar re-centres the map
  and recomputes every commute from that school instead of KTH, so the max-
  commute slider, the sorting and the map counts all follow. KTH is the
  default. You can also click any of the other campus pins on the map to
  switch to it. Seven Stockholm schools are built in (KTH, SU, KI, SSE,
  Konstfack, KMH, KKH) — add more in `SCHOOLS` in `sssb_kth_monitor.py` and
  the dropdown picks them up automatically.
- **Bike times are real routes, not straight lines.** They come from
  [FOSSGIS's public Valhalla](https://valhalla1.openstreetmap.de/) routing
  service over OSM's cycling network — free, no key. Results are cached in
  `data/bike_route_cache.json`, so the first run is slow (it routes each area
  to each campus) and later runs are instant. A listing's row shows
  `12 min bike` for a routed time and `~12 min bike` for a fallback estimate,
  so you can always tell which you're looking at. If the service is
  unreachable the run says so and falls back to the old straight-line guess
  rather than failing; `--no-bike-routes` skips routing entirely.
- **Filtering far-away areas**: the "Max commute" slider in the dashboard
  hides areas beyond that many minutes (transit time if you set up
  Resrobot, otherwise the bike estimate). Flemingsberg in particular is
  quite far from central KTH — it'll likely get filtered out by default,
  which is probably what you want.
- **Queue-days and rent sliders**: next to "Max commute". Both start at
  "Any" (hiding nothing) and filter individual listings rather than whole
  areas, so the counts on the map roundels drop as you pull them down and an
  area shows the gray × once nothing in it still qualifies. Bostadsförmedlingen
  ads are never hidden by the queue-days slider — they publish an application
  deadline instead of a queue-days figure, so there's nothing to compare
  against. The "All listings" search tab ignores every slider, so it's
  the way to look at everything regardless of what's filtered.
- **The cog** (next to those sliders) opens a second row with the
  finer-grained filters: minimum size, a lowest/highest floor range
  (floor 0 = *bottenvåning*), and minimum contract length. They behave the
  same way as the main sliders — "Any" until you pull one in — and while any
  cog filter is engaged the cog shows an amber count, so you can collapse the
  panel without forgetting the map is still narrowed. "Reset these" clears
  just that row.
  - *Min contract* uses SSSB's "Max N år" badge, which caps how long you may
    hold the contract. Only a stated cap **below** your minimum is excluded;
    listings with no stated cap always pass, since no cap is the better case.
    In practice SSSB only ever seems to print "Max 4 år", so this slider
    mostly acts as a switch between "include those" and "exclude those".
- **Electricity** is shown, not filtered on. Cards that say "Elström ingår"
  display *el ingår* in the listing row (and it's searchable in the "All
  listings" tab), but there's no toggle for it: a card saying nothing about
  electricity isn't the same as one that excludes it, and Bostadsförmedlingen
  ads have no such field at all, so a filter would have quietly dropped that
  entire provider.
- **No elevator filter**: SSSB's vacancy list doesn't publish whether a
  building has a lift — the card only carries type, address, area, size,
  rent, move-in date, queue days, floor, and the two badges above. Adding one
  would mean opening each listing's own page during every scrape (76 extra
  page loads), and it's not confirmed that page states it either. The floor
  sliders are the practical stand-in for "no walk-up".
- **If SSSB changes their site**, the selectors in step 3 are the only
  place you should need to touch.
- **Rate limiting**: don't drop the cron interval much below ~15 minutes —
  there's no need to hammer their login endpoint, and it's not clear how
  they'd react to it.
- **Bostadsförmedlingen is temporarily unavailable.** Their old public feed
  (`/Lista/AllaAnnonser`) started returning 404, so runs are SSSB-only for now
  and the terminal says so. Once the current endpoint is known, set
  `BF_ADS_URL=<url>` in your environment (or add it to `BF_ALL_ADS_URLS` in the
  script) and BF listings come back with no other changes. To find it: open the
  search page on bostad.stockholm.se, DevTools → Network → Fetch/XHR, and copy
  the request that returns the ad list.
- **Desktop notifications** need nothing extra on macOS — the script uses the
  built-in `osascript`. (`plyer`, listed in requirements, needs a compiled
  `pyobjus` extension that often won't install; it's only a fallback for
  Windows now.)
- **Bostadsförmedlingen**: `fetch_bostadsformedlingen()` pulls Stockholm's
  city housing agency's public ad feed
  (`bostad.stockholm.se/Lista/AllaAnnonser`) and keeps only the student ads
  — no login, no API key. Svenska Bostäder doesn't run a separate student
  queue of its own; their listings show up through this same feed (tagged
  with a `landlord`). Each ad carries its own coordinates, so these get
  individual purple pins on the map instead of SSSB's per-area dots, and
  sort by application deadline instead of queue days, since BF doesn't
  publish a queue-days figure up front.
- **Provider filter**: the dashboard's "Provider" chips (top bar) toggle
  SSSB and Bostadsförmedlingen independently of the SSSB-only "SSSB lines"
  filter. The "Queues to join" sidebar tab explains how to actually
  register for each queue.


Made by IvarHak on GitHub with the help of Claude Code
Feel free to use or modify however
