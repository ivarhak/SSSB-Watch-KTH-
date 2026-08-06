#!/usr/bin/env python3
"""
SSSB → KTH Commute Monitor
===========================

Scrapes SSSB's currently available student housing (minasidor.sssb.se) plus
Bostadsförmedlingen's public student ads, works out how far each place is from
KTH (both a rough straight-line estimate and a real public-transit time via
Trafiklab's Resrobot API), diffs against the last run to spot newly-published
listings, fires a desktop notification when something new shows up, and serves
it all to the dashboard (sssb_kth_dashboard.html) over a tiny local API.

NO LOGIN NEEDED: SSSB's vacancy list is public — confirmed 2026-08-06,
queue days ("Ködagar") included. Nothing here asks for credentials by
default. `--with-login` still exists as an escape hatch if SSSB ever puts the
list back behind a login, in which case `--login` stores credentials in your
OS keychain (macOS Keychain / Windows Credential Locker / Linux Secret
Service via `keyring`) rather than any file that could end up in a commit.

WHY SELENIUM: the listings page renders its content client-side (the raw HTML
is just template placeholders until their JS app runs), so a real browser is
used to render the page before parsing it. Whether the underlying data is
reachable as plain JSON — which would drop the browser requirement entirely —
is still an open question; see CLAUDE.md.

Usage:
    python sssb_kth_monitor.py --once           # one scrape, save + notify, exit
    python sssb_kth_monitor.py --serve          # run scrape + start local API/dashboard server
    python sssb_kth_monitor.py --bf-only        # no browser at all: Bostadsförmedlingen only
    python sssb_kth_monitor.py --debug          # also dump rendered HTML to debug_page.html
    python sssb_kth_monitor.py --with-login     # only if SSSB starts requiring a login again
"""

import argparse
import getpass
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────

KEYRING_SERVICE = "sssb-kth-tool"
RESROBOT_API_KEY = os.environ.get("RESROBOT_API_KEY") or (
    keyring.get_password(KEYRING_SERVICE, "resrobot_api_key") if KEYRING_AVAILABLE else None
)

LOGIN_URL = "https://minasidor.sssb.se/en/login/"
# Ivar found that SSSB's listings page takes `pagination`/`paginationantal`
# query params directly — requesting a page size of 200 (there are ~76
# listings total) returns everything in one render, so we don't need to
# click through a numbered pager at all.
LISTINGS_URL = "https://minasidor.sssb.se/lediga-bostader/?pagination=1&paginationantal=200"

# Bostadsförmedlingen (Stockholm's city housing agency) exposes all current
# ads as plain JSON here — no login, no browser needed. Svenska Bostäder's
# student apartments are advertised THROUGH this same system (their own site
# links every listing to bostad.stockholm.se/bostad/<id>), so this one feed
# covers both of Ivar's requested extra sources.
BF_ALL_ADS_URL = "https://bostad.stockholm.se/Lista/AllaAnnonser"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CURRENT_FILE = DATA_DIR / "current_listings.json"
GEOCODE_CACHE_FILE = DATA_DIR / "geocode_cache.json"
DEBUG_HTML_FILE = Path(__file__).parent / "debug_page.html"

PORT = int(os.environ.get("PORT", 5055))

# KTH main campus, Valhallavägen 79, Stockholm — well-established coordinates.
KTH_COORDS = (59.3467, 18.0716)

# The 26 SSSB housing areas, grouped exactly the way SSSB groups them on
# sssb.se/en/our-homes/ (North / South / City).
AREAS = {
    "North": ["Freja", "Frösunda", "Kungshamra", "Lappkärrsberget", "Pax", "Strix"],
    "South": ["Balder", "Birka", "Embla", "Flemingsberg", "Skärmarbrink"],
    "City": [
        "Apeln", "Domus", "Forum", "Fyrtalet", "Hugin & Munin", "Idun",
        "Jerum", "Kurland", "Lucidor", "Marieberg", "Mjölner", "Nyponet",
        "Roslagstull", "Tanto", "Vätan",
    ],
}
ALL_AREAS = [a for group in AREAS.values() for a in group]

# Real street addresses (pulled from each area's page on sssb.se/en/) used to
# geocode precisely — geocoding on the bare area name alone (e.g. "Balder,
# Stockholm, Sweden") is unreliable since several of these are common Norse
# names/words that Nominatim can match to an unrelated place; this caused
# real, confirmed bad pins (Balder resolving ~30km south near Nynäshamn,
# Birka resolving near Mariefred, Strix resolving to the wrong Stockholm
# location) and some empty results (Jerum, Domus, Lucidor, Nyponet). If a pin
# still looks wrong, hand-correct the `[lat, lon]` in data/geocode_cache.json
# directly rather than editing the address here.
AREA_ADDRESSES = {
    "Freja": "Gärdesvägen 2, 183 30 Täby, Sweden",
    "Frösunda": "Gustav III:s Boulevard 2, 169 72 Solna, Sweden",
    "Kungshamra": "Kungshamra 1, 170 70 Solna, Sweden",
    "Lappkärrsberget": "Professorsslingan 9, 114 17 Stockholm, Sweden",
    "Pax": "Emmylundsvägen 1, 171 72 Solna, Sweden",
    "Strix": "Armégatan 32, 171 59 Solna, Sweden",
    "Balder": "Edinsvägen 22, 131 47 Nacka, Sweden",
    "Birka": "Simrishamnsvägen 15, 121 53 Johanneshov, Sweden",
    "Embla": "Maltgatan 4, 120 79 Stockholm, Sweden",
    "Flemingsberg": "Röntgenvägen 1, 141 52 Huddinge, Sweden",
    "Skärmarbrink": "Nathorstvägen 46, 121 37 Johanneshov, Sweden",
    "Apeln": "Drottninggatan 67, 111 36 Stockholm, Sweden",
    "Domus": "Körsbärsvägen 3, 114 23 Stockholm, Sweden",
    "Forum": "Körsbärsvägen 2, 114 23 Stockholm, Sweden",
    "Fyrtalet": "Värtavägen 66, 115 38 Stockholm, Sweden",
    "Hugin & Munin": "Öregrundsgatan 9, 115 59 Stockholm, Sweden",
    "Idun": "Norra Stationsgatan 99, 113 64 Stockholm, Sweden",
    "Jerum": "Studentbacken 21, 115 57 Stockholm, Sweden",
    "Kurland": "Holländargatan 21, 111 60 Stockholm, Sweden",
    "Lucidor": "Skomakargatan 24, 111 29 Stockholm, Sweden",
    "Marieberg": "Fyrverkarbacken 23, 112 60 Stockholm, Sweden",
    "Mjölner": "Löjtnantsgatan 11, 115 50 Stockholm, Sweden",
    "Nyponet": "Körsbärsvägen 9, 114 23 Stockholm, Sweden",
    "Roslagstull": "Roslagstullsbacken 5, 114 22 Stockholm, Sweden",
    "Tanto": "Tantogatan 59, 118 42 Stockholm, Sweden",
    "Vätan": "David Bagares gata 6, 111 38 Stockholm, Sweden",
}


# ── Credentials ───────────────────────────────────────────────────────────
# Nothing here ever gets written to a file inside this project folder — so
# there's nothing here for a `git add .` / accidental push to leak.

_cred_cache = {}


def _prompt_and_store():
    print("\nSSSB login (this is not written to any file in this folder):")
    username = input("  Username (personnummer or p-number): ").strip()
    password = getpass.getpass("  Password: ")

    if KEYRING_AVAILABLE:
        save = input(
            "  Save to this computer's secure keychain so you're not asked again? [y/N]: "
        ).strip().lower()
        if save == "y":
            keyring.set_password(KEYRING_SERVICE, "username", username)
            keyring.set_password(KEYRING_SERVICE, "password", password)
            print("  Saved to your OS keychain. Run --forget-login later to remove it.")
    else:
        print("  (install the 'keyring' package to save this for next time)")

    return username, password


def get_credentials() -> tuple[str, str]:
    """Resolve SSSB credentials, in order: already-prompted this run →
    OS keychain (if saved via --login) → SSSB_USERNAME/SSSB_PASSWORD env vars
    (for cron/unattended setups where the keychain isn't reachable — set
    these directly in your crontab/task, not in a file in this folder) →
    interactive prompt.
    """
    if _cred_cache:
        return _cred_cache["username"], _cred_cache["password"]

    if KEYRING_AVAILABLE:
        kr_user = keyring.get_password(KEYRING_SERVICE, "username")
        kr_pass = keyring.get_password(KEYRING_SERVICE, "password") if kr_user else None
        if kr_user and kr_pass:
            _cred_cache.update(username=kr_user, password=kr_pass)
            return kr_user, kr_pass

    env_user, env_pass = os.environ.get("SSSB_USERNAME"), os.environ.get("SSSB_PASSWORD")
    if env_user and env_pass:
        _cred_cache.update(username=env_user, password=env_pass)
        return env_user, env_pass

    if not sys.stdin.isatty():
        raise SystemExit(
            "No saved credentials, and this doesn't look like an interactive terminal "
            "(likely a cron/scheduled run). Run `python sssb_kth_monitor.py --login` "
            "once by hand first to store credentials in your OS keychain, then "
            "unattended runs will pick them up automatically."
        )

    username, password = _prompt_and_store()
    _cred_cache.update(username=username, password=password)
    return username, password


def forget_credentials():
    if not KEYRING_AVAILABLE:
        print("keyring isn't installed — there's nothing stored to remove.")
        return
    for key in ("username", "password", "resrobot_api_key"):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass
    print("Removed any saved credentials from your OS keychain.")


# ── Geocoding (OpenStreetMap Nominatim — free, no key) ──────────────────────

def _load_geocode_cache():
    if GEOCODE_CACHE_FILE.exists():
        return json.loads(GEOCODE_CACHE_FILE.read_text())
    return {}


def _save_geocode_cache(cache):
    GEOCODE_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def geocode_area(name: str, cache: dict) -> tuple | None:
    """Look up (lat, lon) for an SSSB area name, cached to disk.

    You can hand-correct any entry by editing data/geocode_cache.json directly
    — e.g. if Nominatim resolves "Pax" to the wrong Pax somewhere in Sweden.
    """
    if name in cache and cache[name]:
        return tuple(cache[name])

    query = AREA_ADDRESSES.get(name, f"{name}, Stockholm, Sweden")
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "sssb-kth-commute-tool/1.0 (personal use)"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = (float(results[0]["lat"]), float(results[0]["lon"]))
            cache[name] = list(coords)
            _save_geocode_cache(cache)
            time.sleep(1)  # respect Nominatim's 1 req/sec usage policy
            return coords
    except requests.RequestException as e:
        print(f"  ! geocoding failed for {name}: {e}")

    cache[name] = None
    _save_geocode_cache(cache)
    return None


# ── Commute calculations ─────────────────────────────────────────────────

def haversine_km(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [*a, *b])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def straight_line_estimate(coords: tuple) -> dict:
    """Rough, no-API-needed estimate. Not a real route — just a sanity check."""
    km = haversine_km(coords, KTH_COORDS)
    return {
        "distance_km": round(km, 2),
        # crude rule of thumb for Stockholm: biking ~15km/h + 3 min overhead,
        # walking ~5km/h. Treat as ballpark only.
        "bike_min": round(km / 15 * 60 + 3),
        "walk_min": round(km / 5 * 60),
    }


_resrobot_stop_cache = {}


def _nearest_stop_id(coords: tuple) -> str | None:
    if coords in _resrobot_stop_cache:
        return _resrobot_stop_cache[coords]
    try:
        resp = requests.get(
            "https://api.resrobot.se/v2.1/location.nearbystops",
            params={
                "accessId": RESROBOT_API_KEY,
                "originCoordLat": coords[0],
                "originCoordLong": coords[1],
                "format": "json",
                "maxNo": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        stops = resp.json().get("stopLocationOrCoordLocation", [])
        stop_id = stops[0]["StopLocation"]["extId"] if stops else None
        _resrobot_stop_cache[coords] = stop_id
        return stop_id
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"  ! resrobot nearbystops failed: {e}")
        return None


def real_transit_time(coords: tuple) -> int | None:
    """Real public-transit journey time (minutes) to KTH via Resrobot.
    Returns None if RESROBOT_API_KEY isn't set or the lookup fails —
    the dashboard just shows the straight-line estimate in that case.
    """
    if not RESROBOT_API_KEY:
        return None
    origin_id = _nearest_stop_id(coords)
    dest_id = _nearest_stop_id(KTH_COORDS)
    if not origin_id or not dest_id:
        return None
    try:
        resp = requests.get(
            "https://api.resrobot.se/v2.1/trip",
            params={
                "accessId": RESROBOT_API_KEY,
                "originId": origin_id,
                "destId": dest_id,
                "format": "json",
                "numF": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        trip = resp.json()["Trip"][0]
        origin_time = trip["Origin"]["time"]
        origin_date = trip["Origin"]["date"]
        dest_time = trip["Destination"]["time"]
        dest_date = trip["Destination"]["date"]
        fmt = "%Y-%m-%d %H:%M:%S"
        t0 = datetime.strptime(f"{origin_date} {origin_time}", fmt)
        t1 = datetime.strptime(f"{dest_date} {dest_time}", fmt)
        return round((t1 - t0).total_seconds() / 60)
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"  ! resrobot trip failed: {e}")
        return None


# ── Selenium scraping ────────────────────────────────────────────────────

def init_driver(headless: bool = True):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _dismiss_cookie_banner(driver):
    """Best-effort dismissal of a cookie-consent overlay, which is the most
    common cause of 'element not interactable' on Swedish sites — it sits on
    top of the form and blocks clicks even though the form itself is fine.
    Safe no-op if nothing matches.
    """
    from selenium.webdriver.common.by import By

    candidates = [
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ', 'abcdefghijklmnopqrstuvwxyzåäö'), 'godkänn')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ', 'abcdefghijklmnopqrstuvwxyzåäö'), 'acceptera')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]",
        "//button[contains(., 'OK')]",
        "#onetrust-accept-btn-handler",
        ".cookie-consent button",
        "[id*='cookie'] button",
    ]
    for sel in candidates:
        try:
            by = By.XPATH if sel.startswith("//") else By.CSS_SELECTOR
            el = driver.find_element(by, sel)
            if el.is_displayed():
                el.click()
                time.sleep(0.5)
                return True
        except Exception:
            continue
    return False


def _click(driver, element):
    """Click, scrolling into view first and falling back to a JS click if
    Selenium's own interactability check fails (covered element, mid-animation,
    just-off-viewport, etc.) — all common and all harmless to work around.
    """
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.3)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def login(driver):
    """Log into minasidor.sssb.se. NOT called unless you pass --with-login.

    Ivar confirmed (2026-08-06) that the vacancy list renders fine in a
    logged-out private tab, queue days ("Ködagar") included, so scraping needs
    no credentials at all and this is skipped by default. It's kept only as an
    escape hatch in case SSSB puts the list back behind a login.

    CONFIGURABLE, AND STILL UNVERIFIED: the field selectors below are a best
    guess (SSSB commonly uses a personnummer + password form) and have never
    been checked against real markup — nobody has needed to run this path. If
    it fails, run with --debug --with-login, open debug_page.html, right-click
    the username/password fields → Inspect, and update the `By.CSS_SELECTOR`
    values below to match.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    username, password = get_credentials()

    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)
    _dismiss_cookie_banner(driver)

    # Best-guess selectors — adjust if SSSB's form differs:
    username_field = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username'], input#username, input[type='text']"))
    )
    _click(driver, username_field)
    username_field.send_keys(username)

    password_field = driver.find_element(By.CSS_SELECTOR, "input[name='password'], input#password, input[type='password']")
    password_field.send_keys(password)

    submit_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
    _click(driver, submit_button)

    # Wait for the login form to disappear (i.e. we've navigated away from /login/)
    try:
        wait.until(lambda d: "/login" not in d.current_url)
    except Exception:
        raise SystemExit(
            "Still on the login page after submitting — either the credentials "
            "were rejected, or the submit button selector is wrong. Run with "
            "--debug (visible browser) to see which."
        )



def _click_next_or_load_more(driver) -> bool:
    """Best-effort click on whatever 'next page' / 'load more' control exists.
    Tries English and Swedish button text, plus common aria-labels. Returns
    True if something was clicked, False if no such control was found —
    treat False as "reached the end".
    """
    from selenium.webdriver.common.by import By

    UPPER_EN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    LOWER_EN = "abcdefghijklmnopqrstuvwxyz"
    UPPER_SV = "ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ"
    LOWER_SV = "abcdefghijklmnopqrstuvwxyzåäö"

    phrases_sv = ["nästa", "visa fler", "fler bostäder", "ladda fler"]
    phrases_en = ["next", "load more", "show more", "more results"]

    xpaths = []
    for p in phrases_sv:
        xpaths.append(f"//button[contains(translate(normalize-space(.), '{UPPER_SV}', '{LOWER_SV}'), '{p}')]")
        xpaths.append(f"//a[contains(translate(normalize-space(.), '{UPPER_SV}', '{LOWER_SV}'), '{p}')]")
    for p in phrases_en:
        xpaths.append(f"//button[contains(translate(normalize-space(.), '{UPPER_EN}', '{LOWER_EN}'), '{p}')]")
        xpaths.append(f"//a[contains(translate(normalize-space(.), '{UPPER_EN}', '{LOWER_EN}'), '{p}')]")
    xpaths.append("//*[self::button or self::a][contains(@aria-label,'ext') or contains(@aria-label,'ästa')]")

    for xp in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed() and el.is_enabled():
                    _click(driver, el)
                    return True
        except Exception:
            continue
    return False


def _parse_listing_from_link(link, url: str) -> dict:
    """Given a `refid=` <a> tag and its resolved absolute URL, walk up the
    DOM to find the surrounding card text and pull out area/rent/size/queue-days.
    """
    import re

    card_text = ""
    node = link
    for _ in range(6):
        if node.parent is None:
            break
        node = node.parent
        candidate = node.get_text(" ", strip=True)
        if 15 <= len(candidate) <= 500 and (
            "kr" in candidate or re.search(r"\d\s*(day|days|dag|dagar)", candidate, re.IGNORECASE)
        ):
            card_text = candidate
            break
    if not card_text:
        card_text = node.get_text(" ", strip=True)[:500]

    area = None
    for area_name in ALL_AREAS:
        if area_name.lower() in card_text.lower():
            area = area_name
            break
    if area is None:
        area = "Unknown"

    (housing_type, queue_days, rent_sek, size_sqm, floor,
     max_years, el_included) = _parse_card_fields(card_text)

    return {
        "id": url,
        "area": area,
        "raw_text": card_text[:300],
        "type": housing_type,
        "queue_days": queue_days,
        "rent_sek": rent_sek,
        "size_sqm": size_sqm,
        "floor": floor,
        "max_years": max_years,      # contract cap in years; None = none stated
        "el_included": el_included,  # True = "Elström ingår"; None = not stated
        "url": url,
    }


# Confirmed live (2026-07-09) that a real card's text is a labeled table, not
# free-flowing prose — e.g.:
#   "Previous Next Rum i korridor Studentbacken 23 / 1313 10 mån hyra Elström
#    ingår Område: Boyta: Hyra: Inflyttning: Ködagar: Våning: Jerum 17 m²
#    4 968 kr 2026-08-01 91 (3st) 3 Previous Next"
# i.e. the labels (Område/Boyta/Hyra/Inflyttning/Ködagar/Våning) are listed
# first, then the values follow in the same order. The old approach (search
# for any "<number> kr" / "<number> dagar" anywhere in the text) silently
# returned None for queue_days here, because the value never actually sits
# next to the word "dagar" in this format — hence dashboard showing "--" for
# every listing. Parsing the label block's value run directly fixes that and
# is far less guessable-content-dependent than the old free text regexes.
_CARD_VALUES_RE = re.compile(
    r"(?P<size>\d{1,3})\s*m²\s*"
    r"(?P<rent>[\d\s]{3,7})\s*kr\s*"
    r"\d{4}-\d{2}-\d{2}\s*"  # move-in date — not currently surfaced
    r"(?P<queue>[\d\s]{1,6}?)\s*\(\d+\s*st\)"
)

# "Våning" (floor) is the last value in that same run, right after the queue
# figure's "(Nst)" token — either a number or "Bottenvåning" (ground floor).
# Confirmed against all 76 listings in a real scrape: values 1–11 plus
# "Bottenvåning". Anything else (e.g. the card's trailing "Previous") parses
# to None rather than a wrong number.
_CARD_FLOOR_RE = re.compile(r"\(\d+\s*st\)\s*(\S+)")

# Two optional badges SSSB puts in the card body, before the labeled table:
#   "Max 4 år"      — a cap on how long you may hold the contract. Confirmed
#                     on 18 of 76 listings (all "4"); absent on the rest,
#                     which means no cap is stated (i.e. the better case).
#   "Elström ingår" — electricity included in the rent. Confirmed on 48 of 76.
# Absence of either is "not stated", NOT a known negative — hence None rather
# than 0/False, so the dashboard can tell the difference.
_CARD_MAX_YEARS_RE = re.compile(r"Max\s+(\d+)\s*år", re.IGNORECASE)
_CARD_EL_RE = re.compile(r"Elström\s+ingår", re.IGNORECASE)


def _parse_floor(card_text: str) -> int | None:
    """Floor as an int, with ground floor = 0. None if it isn't stated."""
    m = _CARD_FLOOR_RE.search(card_text)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.isdigit():
        return int(raw)
    if raw.lower().startswith("botten"):  # "Bottenvåning" = ground floor
        return 0
    return None


def _parse_max_years(card_text: str) -> int | None:
    m = _CARD_MAX_YEARS_RE.search(card_text)
    return int(m.group(1)) if m else None


def _parse_el_included(card_text: str) -> bool | None:
    """True if the card says electricity is included; None if it says nothing
    (deliberately not False — we don't actually know it's excluded)."""
    return True if _CARD_EL_RE.search(card_text) else None

# The housing type ("Rum i korridor" = corridor/dorm room, "2 rum och kök" =
# 2-room + kitchen, etc.) is whatever text sits between "Previous Next" and
# the start of the street address (a word immediately followed by "<number>
# / <number>", e.g. "Studentbacken 23 / 1313").
_CARD_TYPE_RE = re.compile(r"^(?:Previous\s+Next\s+)?(.*?)\s+\S+\s+\d+\s*/\s*\d+\s")

_TYPE_TRANSLATIONS = {
    "rum i korridor": "Corridor room (dorm)",
    "korridorrum": "Corridor room (dorm)",
    "studentlägenhet": "Studio",
}


def _translate_housing_type(raw: str) -> str:
    key = raw.strip().lower()
    if key in _TYPE_TRANSLATIONS:
        return _TYPE_TRANSLATIONS[key]
    m = re.match(r"(\d+)\s*rum\s*och\s*(kök|kokvrå)", key)
    if m:
        n, kitchen_word = m.groups()
        return f"{n} room + {'kitchen' if kitchen_word == 'kök' else 'kitchenette'}"
    return raw.strip()


def _parse_card_fields(card_text: str):
    """Returns (housing_type, queue_days, rent_sek, size_sqm, floor,
    max_years, el_included), any of which may be None if the card text doesn't
    match the expected shape (falls back to the older, looser regexes so a
    format change degrades rather than silently returning nothing).
    """
    housing_type = None
    type_match = _CARD_TYPE_RE.match(card_text)
    if type_match and type_match.group(1).strip():
        housing_type = _translate_housing_type(type_match.group(1))

    floor = _parse_floor(card_text)
    extras = (_parse_max_years(card_text), _parse_el_included(card_text))

    values_match = _CARD_VALUES_RE.search(card_text)
    if values_match:
        size_sqm = int(values_match.group("size"))
        rent_sek = int(re.sub(r"\s", "", values_match.group("rent")))
        queue_days = int(re.sub(r"\s", "", values_match.group("queue")))
        return (housing_type, queue_days, rent_sek, size_sqm, floor, *extras)

    # Fallback: older free-text heuristics, in case SSSB's card layout has
    # drifted from the labeled-table format confirmed above.
    queue_match = re.search(r"(\d[\d\s]{0,6})\s*(day|days|dag|dagar)", card_text, re.IGNORECASE)
    queue_days = int(re.sub(r"\s", "", queue_match.group(1))) if queue_match else None

    rent_match = re.search(r"(\d[\d\s]{2,6})\s*kr", card_text)
    rent_sek = int(re.sub(r"\s", "", rent_match.group(1))) if rent_match else None

    size_match = re.search(r"(\d{1,3})\s*m²", card_text)
    size_sqm = int(size_match.group(1)) if size_match else None

    return (housing_type, queue_days, rent_sek, size_sqm, floor, *extras)



def _decode_response(resp) -> str:
    """Decode an HTTP response to text, preferring UTF-8 over requests' default.

    Necessary, not cosmetic: when a server sends `Content-Type: text/html` with
    no charset, requests falls back to ISO-8859-1 per the HTTP spec, which turns
    this page's Swedish into mojibake — "kök" → "kÃ¶k", "m²" → "mÂ²", and the
    non-breaking space inside "7 218 kr" into "Â ". That silently broke the
    card regexes and produced a rent of 218 kr instead of 7218. Confirmed
    against a fixture built from real scraped cards.
    """
    declared = "charset" in (resp.headers.get("content-type") or "").lower()
    if declared and resp.encoding:
        return resp.text
    for enc in ("utf-8", resp.apparent_encoding, "iso-8859-1"):
        if not enc:
            continue
        try:
            return resp.content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return resp.content.decode("utf-8", "replace")


def _refid_links_from_html(html: str) -> dict:
    """Every real-listing link in a page of HTML, keyed by absolute URL.

    Shared by the Selenium path (which passes `driver.page_source`) and the
    browserless path (which passes the raw response body) so both get
    identical parsing.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.select("a[href]"):
        href = a["href"]
        if "refid=" not in href:
            continue
        url = href if href.startswith("http") else "https://minasidor.sssb.se" + href
        out.setdefault(url, a)
    return out


def _expected_total_from_html(html: str) -> int | None:
    """SSSB shows "Shown X - Y of Z vacant homes" (Swedish: "Visas X - Y av Z
    lediga bostäder") — grab Z if we can, purely to report whether we got
    everything."""
    try:
        m = re.search(r"(?:of|av)\s+(\d+)\s+(?:vacant|lediga)", html, re.IGNORECASE)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _listings_from_links(links_by_url: dict, expected_total: int | None) -> list[dict]:
    """Parse + report on a set of `refid=` links. Kept verbose on purpose —
    this project gets debugged from terminal output alone."""
    listing_links = list(links_by_url.items())
    print(f"  found {len(listing_links)} unique link(s) containing 'refid='")

    listings = [_parse_listing_from_link(link, url) for url, link in listing_links]
    unknown_area_count = sum(1 for l in listings if l["area"] == "Unknown")

    print(f"  parsed {len(listings)} listing(s):")
    for l in listings:
        print(f"    [{l['area']}] queue_days={l['queue_days']} rent={l['rent_sek']} "
              f"size={l['size_sqm']} floor={l['floor']} max_years={l['max_years']} "
              f"el={l['el_included']} :: {l['raw_text'][:90]}")

    if len(listing_links) == 0:
        print("  ! No 'refid=' links found at all — either 0 listings are published right "
              "now, or SSSB's link format changed. Run --debug and grep debug_page.html "
              "for 'refid=' to confirm which.")
    if expected_total and len(listing_links) < expected_total:
        print(f"  ! Only found {len(listing_links)} of an expected ~{expected_total}.")
    if unknown_area_count:
        print(f"  ! {unknown_area_count} listing(s) didn't match a known area name — the "
              "surrounding-text heuristic may be grabbing the wrong ancestor for those. Check "
              "the raw_text above.")

    missing_queue = sum(1 for l in listings if l["queue_days"] is None)
    if listings and missing_queue > len(listings) // 2:
        print(f"  ! {missing_queue} of {len(listings)} listing(s) have no queue-days figure. The "
              "'Ködagar' column was confirmed public in Aug 2026, so if this run wasn't already "
              "using --with-login, try that — SSSB may have moved it back behind a login (the "
              "dashboard sorts SSSB rows by queue days, so without it that ordering is meaningless).")

    return listings


# URL fragments worth reporting if the raw HTML turns out to be a JS shell —
# whatever endpoint the page fetches its data from is the thing to scrape next.
_ENDPOINT_HINT_RE = re.compile(
    r"[\"\x27(]([^\"\x27()\s]*(?:api|json|ajax|handler|\.asmx|/Lista/|sok|search)"
    r"[^\"\x27()\s]*)", re.IGNORECASE)


def fetch_sssb_http(debug: bool = False) -> list[dict] | None:
    """Read the SSSB vacancy list with a plain HTTP GET — no browser at all.

    This is what makes the tool runnable somewhere Chrome doesn't exist (a
    phone, a small server). It only works if the listings are present in the
    raw HTML rather than being drawn in later by SSSB's JS; returns None if
    they aren't, so the caller can fall back to Selenium. On that failure it
    prints any API-ish URLs found in the page, since one of them is likely the
    endpoint the JS calls — which would be the better thing to scrape.
    """
    print("fetching SSSB vacancy list over plain HTTP (no browser)...")
    try:
        resp = requests.get(
            LISTINGS_URL,
            headers={
                # SSSB serves the list to logged-out visitors; a browser-ish UA
                # just avoids being treated as a bot.
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
                "Accept-Language": "sv,en;q=0.8",
            },
            timeout=30,
        )
        resp.raise_for_status()
        html = _decode_response(resp)
    except requests.RequestException as e:
        print(f"  ! HTTP fetch failed ({e})")
        return None

    print(f"  got {len(html):,} chars (HTTP {resp.status_code}, final URL {resp.url})")
    if debug:
        DEBUG_HTML_FILE.write_text(html, encoding="utf-8")
        print(f"  wrote raw HTML to {DEBUG_HTML_FILE} for inspection")

    if "/login" in resp.url:
        print("  ! redirected to a login page — the list isn't public after all; "
              "use --with-login (which needs Chrome).")
        return None

    links = _refid_links_from_html(html)
    if not links:
        placeholders = html.count("{{")
        print(f"  ! no 'refid=' links in the raw HTML ({placeholders} '{{{{' template "
              "placeholder(s) found) — the page is drawn by JavaScript, so this "
              "browserless path can't read it.")
        candidates = sorted(set(_ENDPOINT_HINT_RE.findall(html)))[:25]
        if candidates:
            print("  Candidate data endpoints spotted in the page — one of these is probably "
                  "what its JS calls for the listings:")
            for c in candidates:
                print(f"    {c}")
            print("  Paste that list into the chat and the scraper can target it directly, "
                  "which would drop the browser requirement for good.")
        return None

    expected_total = _expected_total_from_html(html)
    if expected_total:
        print(f"  page reports ~{expected_total} vacant home(s) in total")
    return _listings_from_links(links, expected_total)


def scrape_listings(driver, debug: bool = False) -> list[dict]:
    """Scrape currently published listings.

    LISTINGS_URL already requests a 200-per-page size via
    `?pagination=1&paginationantal=200`, so normally everything renders in
    one go and the page-click loop below never has anything to click (it
    breaks immediately once `expected_total` is reached). It's kept as a
    fallback in case SSSB caps `paginationantal` below the real listing
    count some day.

    Real SSSB listings all link to a URL containing `refid=` in the query
    string (confirmed against an actual booking link), so rather than
    guessing CSS class names for a "card" wrapper — which kept matching
    unrelated page chrome — this anchors on that instead: find every
    `refid=` link, then walk a few levels up the DOM from each one to find
    the surrounding text (rent, size, queue days, area).
    """
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(LISTINGS_URL)

    # Wait until the Angular/Vue template placeholders have been replaced
    # with real numbers (the un-rendered page literally contains "{{alla}}").
    wait = WebDriverWait(driver, 25)
    try:
        wait.until(lambda d: "{{" not in d.page_source)
    except Exception:
        pass  # proceed anyway; page may just have 0 listings right now

    time.sleep(2)  # small buffer for any trailing async rendering

    expected_total = _expected_total_from_html(driver.page_source)

    all_links_by_url = {}
    for page_num in range(1, 26):  # hard cap so a broken "next" click can't loop forever
        page_links = _refid_links_from_html(driver.page_source)

        new_count = 0
        for url, a in page_links.items():
            if url not in all_links_by_url:
                all_links_by_url[url] = a
                new_count += 1

        print(f"  page {page_num}: {len(page_links)} link(s) visible, {new_count} new "
              f"(total so far: {len(all_links_by_url)}"
              + (f" of ~{expected_total}" if expected_total else "") + ")")

        if debug and page_num == 1:
            DEBUG_HTML_FILE.write_text(driver.page_source, encoding="utf-8")
            print(f"  wrote rendered HTML (page 1) to {DEBUG_HTML_FILE} for inspection")

        if expected_total and len(all_links_by_url) >= expected_total:
            break
        if new_count == 0 and page_num > 1:
            break  # clicking next/load-more stopped producing anything new

        if not _click_next_or_load_more(driver):
            break
        time.sleep(2)  # let new content render before the next pass

    return _listings_from_links(all_links_by_url, expected_total)



# ── Diff + notifications ─────────────────────────────────────────────────

def load_previous() -> dict:
    if CURRENT_FILE.exists():
        return json.loads(CURRENT_FILE.read_text())
    return {"listings": [], "generated_at": None}


def notify_new(new_listings: list[dict]):
    if not new_listings:
        return
    try:
        from plyer import notification
        areas = ", ".join(sorted({l["area"] for l in new_listings}))
        notification.notify(
            title=f"SSSB: {len(new_listings)} new listing(s)",
            message=f"In: {areas}",
            timeout=15,
        )
    except Exception as e:
        print(f"  ! desktop notification failed ({e}) — new listings: "
              f"{[l['area'] for l in new_listings]}")


# ── Main pipeline ────────────────────────────────────────────────────────

_scrape_lock = threading.Lock()


def _bf_field(ad: dict, *names, default=None):
    """Tolerant field getter — the AllaAnnonser JSON's exact key names have
    shifted over the years (community scrapers show several variants), so try
    each candidate name case-insensitively rather than hard-failing.
    """
    lower_map = {k.lower(): v for k, v in ad.items()}
    for n in names:
        if n.lower() in lower_map and lower_map[n.lower()] is not None:
            return lower_map[n.lower()]
    return default


def fetch_bostadsformedlingen() -> list[dict]:
    """Fetch current STUDENT ads from Bostadsförmedlingen's public JSON feed.

    Every ad carries its own coordinates and landlord (hyresvärd) — e.g.
    Svenska Bostäder — so these get precise per-listing pins on the map
    rather than SSSB-style area dots, plus a provider tag.

    No credentials involved; plain GET. If the feed's shape changes, this
    prints the first ad's keys so the field mapping is fixable from terminal
    output alone.
    """
    print("fetching Bostadsförmedlingen ads (bostad.stockholm.se)...")
    try:
        resp = requests.get(
            BF_ALL_ADS_URL,
            headers={"User-Agent": "Mozilla/5.0 (personal student-housing monitor)"},
            timeout=25,
        )
        resp.raise_for_status()
        ads = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  ! Bostadsförmedlingen fetch failed ({e}) — continuing with SSSB only")
        return []

    if not isinstance(ads, list):
        print(f"  ! unexpected response shape ({type(ads).__name__}) — continuing with SSSB only")
        return []
    print(f"  feed contains {len(ads)} total ads")
    if ads:
        print(f"  (first ad's fields: {sorted(ads[0].keys())[:20]}...)")

    listings = []
    for ad in ads:
        if not _bf_field(ad, "Student", "student"):
            continue  # only student housing

        ad_id = _bf_field(ad, "AnnonsId", "annonsid", "Id")
        lat = _bf_field(ad, "KoordinatLatitud", "Latitud", "lat")
        lon = _bf_field(ad, "KoordinatLongitud", "Longitud", "lng", "lon")
        landlord = _bf_field(ad, "Hyresvard", "Hyresvärd", "Uthyrare", default="")
        district = _bf_field(ad, "Stadsdel", "Omrade", "Område", default="") or ""
        kommun = _bf_field(ad, "Kommun", default="") or ""

        try:
            coords = [float(lat), float(lon)] if lat and lon else None
        except (TypeError, ValueError):
            coords = None

        listings.append({
            "id": f"bf-{ad_id}",
            "provider": "Bostadsförmedlingen",
            "landlord": landlord or None,
            "area": district or kommun or "Stockholm",
            "address": _bf_field(ad, "Gatuadress", "Adress", default=""),
            "raw_text": "",
            "queue_days": None,  # BF doesn't publish required queue time up front
            "rent_sek": _bf_field(ad, "Hyra", "Manadshyra"),
            "size_sqm": _bf_field(ad, "Yta", "Kvm"),
            "rooms": _bf_field(ad, "AntalRum", "Rum"),
            # Not known to be in the feed — tried tolerantly so the dashboard's
            # floor filter can use it if it is. None just means "not stated",
            # which never hides a listing.
            "floor": _bf_field(ad, "Vaning", "Våning", "Floor", "Etage"),
            "deadline": _bf_field(ad, "AnnonseradTill", "SistaAnsokan", "AnmalanSenast"),
            "coords": coords,
            "url": f"https://bostad.stockholm.se/bostad/{ad_id}/" if ad_id else None,
        })

    with_coords = sum(1 for l in listings if l["coords"])
    print(f"  kept {len(listings)} student ad(s) ({with_coords} with coordinates)")
    if listings and with_coords == 0:
        print("  ! none had parseable coordinates — the lat/lon field names likely "
              "changed; check the printed field list above and update _bf_field calls.")
    for l in listings[:5]:
        print(f"    [{l['area']}] {l['address']} rent={l['rent_sek']} size={l['size_sqm']} "
              f"landlord={l['landlord']}")
    if len(listings) > 5:
        print(f"    ... and {len(listings) - 5} more")
    return listings


def run_scrape(debug: bool = False, bf_only: bool = False, use_login: bool = False,
               http_only: bool = False) -> dict:
    with _scrape_lock:
        return _run_scrape_impl(debug=debug, bf_only=bf_only, use_login=use_login,
                                http_only=http_only)


def _run_scrape_impl(debug: bool = False, bf_only: bool = False, use_login: bool = False,
                     http_only: bool = False) -> dict:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] starting scrape...")
    previous = load_previous()
    previous_ids = {l["id"] for l in previous["listings"]}

    geocode_cache = _load_geocode_cache()
    print("geocoding areas (cached after first run)...")
    area_info = {}
    for group, names in AREAS.items():
        for name in names:
            coords = geocode_area(name, geocode_cache)
            area_info[name] = {
                "group": group,
                "coords": coords,
                "straight_line": straight_line_estimate(coords) if coords else None,
                "transit_min": real_transit_time(coords) if coords else None,
            }

    if bf_only:
        # No browser available (e.g. running on a phone/tablet interpreter):
        # skip Selenium entirely and carry the last saved SSSB listings over
        # unchanged, so the dashboard still shows both providers. Older saved
        # files predate the provider field — tag those on the way through.
        print("(--bf-only: skipping the SSSB browser scrape — reusing last saved SSSB listings)")
        sssb_listings = [l for l in previous["listings"] if l.get("provider", "SSSB") == "SSSB"]
        for l in sssb_listings:
            l.setdefault("provider", "SSSB")
            l.setdefault("landlord", "SSSB")
    else:
        sssb_listings = None
        # Try the browserless path first: it's much faster than launching Chrome
        # and it's the only one that works where Chrome doesn't exist. Falls
        # through to Selenium if the raw HTML turns out to be a JS shell.
        if not use_login:
            sssb_listings = fetch_sssb_http(debug=debug)
            if sssb_listings is None and http_only:
                raise SystemExit(
                    "--http-only was requested but the vacancy list couldn't be read without a "
                    "browser (see the diagnostics above). Drop --http-only to fall back to "
                    "Selenium, or use --bf-only to skip SSSB entirely."
                )
        elif http_only:
            raise SystemExit("--http-only and --with-login are contradictory: logging in needs a browser.")

        if sssb_listings is None:
            print("falling back to the browser..." if not use_login
                  else "launching browser + logging in...")
            driver = init_driver(headless=not debug)
            try:
                if use_login:
                    login(driver)
                print("scraping listings...")
                sssb_listings = scrape_listings(driver, debug=debug)
            finally:
                driver.quit()

        for l in sssb_listings:
            l["provider"] = "SSSB"
            l["landlord"] = "SSSB"

    bf_listings = fetch_bostadsformedlingen()
    for l in bf_listings:
        if l["coords"]:
            l["straight_line"] = straight_line_estimate(tuple(l["coords"]))
            l["transit_min"] = real_transit_time(tuple(l["coords"]))

    listings = sssb_listings + bf_listings

    new_listings = [l for l in listings if l["id"] not in previous_ids]
    print(f"found {len(listings)} listings total — {len(sssb_listings)} SSSB, "
          f"{len(bf_listings)} Bostadsförmedlingen ({len(new_listings)} new)")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kth_coords": KTH_COORDS,
        "areas": area_info,
        "listings": listings,
        "new_listing_ids": [l["id"] for l in new_listings],
    }
    CURRENT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    notify_new(new_listings)
    return result


# ── Local API + dashboard server ─────────────────────────────────────────

def _background_poll_loop(interval_minutes: float, bf_only: bool = False, use_login: bool = False,
                          http_only: bool = False):
    """Runs for the lifetime of `--serve`, re-scraping on its own so you
    don't have to sit there clicking Refresh. Any failure (SSSB hiccup,
    network blip) is logged and skipped rather than killing the loop.
    """
    while True:
        time.sleep(interval_minutes * 60)
        try:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] auto-check...")
            run_scrape(bf_only=bf_only, use_login=use_login, http_only=http_only)
        except SystemExit as e:
            print(f"  ! auto-check stopped early: {e}")
        except Exception as e:
            print(f"  ! auto-check failed, will retry next interval: {e}")


def serve(interval_minutes: float, bf_only: bool = False, use_login: bool = False,
          http_only: bool = False):
    from flask import Flask, jsonify, send_from_directory
    from flask_cors import CORS

    app = Flask(__name__)
    CORS(app)  # local dev tool — fine to allow any origin

    static_dir = Path(__file__).parent

    @app.route("/")
    def index():
        return send_from_directory(static_dir, "sssb_kth_dashboard.html")

    @app.route("/api/listings")
    def api_listings():
        if CURRENT_FILE.exists():
            data = json.loads(CURRENT_FILE.read_text())
            data["poll_interval_min"] = interval_minutes
            return jsonify(data)
        return jsonify(run_scrape(bf_only=bf_only, use_login=use_login, http_only=http_only))

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        return jsonify(run_scrape(bf_only=bf_only, use_login=use_login, http_only=http_only))

    threading.Thread(target=_background_poll_loop,
                     args=(interval_minutes, bf_only, use_login, http_only), daemon=True).start()

    print(f"\nDashboard running → http://localhost:{PORT}")
    print(f"Auto-checking SSSB every {interval_minutes:g} min in the background (Ctrl+C to stop)\n")
    app.run(port=PORT, debug=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true", help="prompt for SSSB credentials and store them in your OS keychain")
    parser.add_argument("--forget-login", action="store_true", help="remove saved credentials from your OS keychain")
    parser.add_argument("--once", action="store_true", help="scrape once, save, notify, exit")
    parser.add_argument("--serve", action="store_true", help="start local dashboard + API server")
    parser.add_argument("--interval", type=float, default=15, help="minutes between auto-checks in --serve mode (default: 15)")
    parser.add_argument("--bf-only", action="store_true",
                        help="skip the SSSB browser scrape entirely (no Chrome/Selenium needed — e.g. on a "
                             "phone/tablet Python interpreter); refreshes Bostadsförmedlingen live and reuses "
                             "the last saved SSSB listings")
    parser.add_argument("--http-only", action="store_true",
                        help="never launch a browser: read SSSB over plain HTTP and fail loudly if that "
                             "isn't possible. This is the phone/tablet mode — combined with the fact that "
                             "Bostadsförmedlingen is already a plain JSON feed, it needs no Chrome at all")
    parser.add_argument("--with-login", action="store_true",
                        help="log in before scraping. Not needed — the vacancy list, queue days included, is "
                             "public (confirmed 2026-08-06). Use this only if SSSB starts hiding listings or "
                             "the Ködagar column behind a login again")
    parser.add_argument("--no-login", action="store_true",
                        help="(now the default; accepted for compatibility and does nothing)")
    parser.add_argument("--debug", action="store_true", help="run visible browser + dump debug_page.html")
    args = parser.parse_args()

    if args.forget_login:
        forget_credentials()
    elif args.login:
        _prompt_and_store()
        print("Done — future runs will use this automatically.")
    elif args.once:
        run_scrape(debug=args.debug, bf_only=args.bf_only, use_login=args.with_login,
                   http_only=args.http_only)
    elif args.serve:
        if args.interval < 5:
            parser.error("--interval below 5 minutes isn't a great idea — see README on rate limiting.")
        if not CURRENT_FILE.exists():
            run_scrape(debug=args.debug, bf_only=args.bf_only, use_login=args.with_login,
                       http_only=args.http_only)
        serve(interval_minutes=args.interval, bf_only=args.bf_only, use_login=args.with_login,
              http_only=args.http_only)
    else:
        parser.print_help()
        sys.exit(1)
