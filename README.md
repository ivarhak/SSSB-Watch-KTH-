# SSSB → KTH housing finder

A local tool that logs into SSSB, checks what student housing is currently
available, works out the commute to KTH for each area, and shows it all on
a map — sorted by ascending queue days, with a refresh button and a desktop
notification when something new gets published. It also pulls in Stockholm's
**Bostadsförmedlingen** listings alongside SSSB's — that's where Svenska
Bostäder advertises its student apartments, and it needs no login at all
(it's a public JSON feed).

Two pieces:
- `sssb_kth_monitor.py` — runs on your machine: Selenium scraping for SSSB, a plain HTTP fetch for Bostadsförmedlingen, commute math, and a small local API.
- `sssb_kth_dashboard.html` — the UI. Served by the script itself, so it's all same-origin (no CORS headaches).

## 1. Why this needs to run on your machine

SSSB's listings only render after you're logged in, and the content is
loaded by their JavaScript app rather than being present in the raw page —
so this needs a real (automated) browser, not a simple web request. There's
also no public API for it, so this can't run as a hosted web app — it has
to run locally.

## 2. Setup

I would highly recommend running this on Pycharm or similar

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

There might be some other modules you need, simply pip install when your computer says you dont have them

Chrome (or Chromium) needs to be installed — `webdriver-manager` handles the
matching driver automatically.

### Logging in

Your SSSB username/password are **never written to a file in this project
folder**. Instead:

```bash
python sssb_kth_monitor.py --login
```

This prompts for your username and password (password input is hidden) and
asks if you want to save them to your computer's own secure keychain —
macOS Keychain, Windows Credential Locker, or Linux Secret Service,
depending on your OS, via the `keyring` package. If you say no, or skip
`--login` entirely, you'll just get the same prompt every time `--debug` or
`--serve` needs to log in — nothing is stored anywhere.

To remove saved credentials later:
```bash
python sssb_kth_monitor.py --forget-login
```

> **Unattended `--once` runs (cron, Task Scheduler):** there's no terminal to
> prompt on, so run `--login` once by hand first — cron will then silently
> read from your OS keychain. On a headless Linux box without a keyring
> daemon running (gnome-keyring/kwallet), that may not work — in that case,
> set `SSSB_USERNAME`/`SSSB_PASSWORD` directly as environment variables in
> the crontab entry itself (the script checks for these as a fallback). That's
> fine security-wise since your crontab isn't part of this project folder —
> just don't put those `export` lines in a script that lives in here.

Optional, for **real transit times** (otherwise you'll just get the
straight-line/bike estimate): get a free key for the **Resrobot** API at
[trafiklab.se](https://www.trafiklab.se/) (sign up → create a project →
add the "Resrobot v2.1" API), then:

```bash
export RESROBOT_API_KEY="your key"
```

## 3. Fixing the selectors (important — do this first)

I wrote `login()` and `scrape_listings()` in `sssb_kth_monitor.py` from
general knowledge of how these portals are usually built, since I can't
load minasidor.sssb.se myself (it's behind login and outside what I can
reach from here). They're probably *close* but not exact. To fix them:

```bash
python sssb_kth_monitor.py --debug
```

This runs a **visible** browser window (so you can watch what happens) and
saves the fully-rendered page to `debug_page.html`. If login fails, open
`debug_page.html` (or just watch the browser window), right-click the
username/password fields on minasidor.sssb.se → **Inspect**, and update the
`By.CSS_SELECTOR` values in `login()` to match.

`scrape_listings()` finds real listings by looking for links containing
`refid=` in the URL (confirmed against a real SSSB booking link), then reads
the rent/size/queue-days text from a few levels up the DOM from each one —
so it shouldn't need hand-editing the way `login()` might. If it ever comes
back empty while you can see real listings on the site yourself, run
`--debug` and check whether `debug_page.html` actually contains `refid=`
anywhere (`grep -c "refid=" debug_page.html`) — if SSSB changes their link
format, that's the thing to update.

This is a five-minute fix once you can see the real markup — I just
couldn't do that part myself.

This section is SSSB-specific — Bostadsförmedlingen needs no selector
fixing since `fetch_bostadsformedlingen()` reads a plain JSON feed rather
than scraped markup. If its field names ever drift, the terminal prints
the first ad's actual keys on every run, so you can fix the candidate
names in `_bf_field()` from that alone.

## 4. Running it

**First time only** — store your login (see "Logging in" above):
```bash
python sssb_kth_monitor.py --login
```

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

**Don't want to log in?** Ivar confirmed the vacancy list renders fine in a
logged-out private tab, so:
```bash
python sssb_kth_monitor.py --serve --no-login
```
skips the login step entirely — no credentials, no keyring, and none of the
login-selector fragility described in section 3. It still needs Chrome. The
one open question is whether SSSB shows the **Ködagar** (queue days) column to
logged-out visitors; if it doesn't, the run prints a loud warning and you
should drop the flag, since the dashboard sorts SSSB listings by queue days.

**No Chrome available?** (phone/tablet Python interpreters, minimal servers):
```bash
python sssb_kth_monitor.py --serve --bf-only
```
`--bf-only` skips the SSSB browser scrape entirely — no Selenium, no Chrome,
no login, no keyring — so the only packages it needs are `requests`, `flask`
and `flask-cors`. It fetches Bostadsförmedlingen live, reuses the last saved
SSSB listings from `data/current_listings.json` (run a full scrape on a real
computer now and then to refresh those), and serves the same dashboard at
http://localhost:5055. Desktop notifications via `plyer` generally don't work
on mobile — new-listing detection still shows up as NEW tags in the dashboard.

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
  (floor 0 = *bottenvåning*), minimum contract length, and an **Elström**
  requirement. The sliders behave the same way as the main ones — "Any" until
  you pull one in — and while any cog filter is engaged the cog shows an amber
  count, so you can collapse the panel without forgetting the map is still
  narrowed. "Reset these" clears just that row.
  - *Min contract* uses SSSB's "Max N år" badge, which caps how long you may
    hold the contract. Only a stated cap **below** your minimum is excluded;
    listings with no stated cap always pass, since no cap is the better case.
    In practice SSSB only ever seems to print "Max 4 år", so this slider
    mostly acts as a switch between "include those" and "exclude those".
  - *Elström* is the one filter that deliberately hides unknowns: it keeps
    only listings whose card explicitly says "Elström ingår" (48 of 76 in a
    real scrape). Cards that say nothing about electricity — and every
    Bostadsförmedlingen ad, which has no such field — drop out while it's on.
    That's the point of it being an explicit opt-in rather than a slider.
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
