<img width="1440" height="811" alt="Screenshot 2026-07-09 at 4 41 35 PM" src="https://github.com/user-attachments/assets/3d6bf8e8-27f9-46aa-ad76-657f7d64da02" />

# SSSB → KTH Commute Board

A local tool that logs into SSSB, checks what student housing is currently
available, displays information on housing, works out the commute to KTH for each area, and shows it all on
a map — sorted by ascending queue days, with a refresh button and a desktop
notification when something new gets published.

Two pieces:
- `sssb_kth_monitor.py` — runs on your machine, does the actual scraping (via Selenium), commute math, and serves a small local API.
- `sssb_kth_dashboard.html` — the UI. Served by the script itself, so it's all same-origin (no CORS headaches).

## 1. Why this needs to run on your machine

SSSB's listings only render after you're logged in, and the content is
loaded by their JavaScript app rather than being present in the raw page —
so this needs a real (automated) browser, not a simple web request. There's
also no public API for it, so this can't run as a hosted web app — it has
to run locally.

## 2. Setup

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
- **If SSSB changes their site**, the selectors in step 3 are the only
  place you should need to touch.
- **Rate limiting**: don't drop the cron interval much below ~15 minutes —
  there's no need to hammer their login endpoint, and it's not clear how
  they'd react to it.


Made by IvarHak on GitHub with Claude code
Feel free to use and modify however you want, Im sure its somewhat easy to convert this tool to add other schools in the Stockholm area or use it as a framework for a similar tool for another school.
