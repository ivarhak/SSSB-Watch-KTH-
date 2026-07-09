#!/usr/bin/env python3
"""
SSSB → KTH Commute Monitor
===========================

Logs into SSSB (minasidor.sssb.se), scrapes currently available student
housing listings, works out how far each SSSB area is from KTH (both a
rough straight-line estimate and a real public-transit time via Trafiklab's
Resrobot API), diffs against the last run to spot newly-published listings,
fires a desktop notification when something new shows up, and serves it
all to the dashboard (sssb_kth_dashboard.html) over a tiny local API.

WHY SELENIUM: SSSB's listings page renders its content client-side after
login (the raw HTML is just template placeholders until their JS app runs),
and there is no documented public API for it, so a real browser is used to
render the page before parsing it.

BEFORE YOU RUN THIS: the CSS selectors in `scrape_listings()` and `login()`
are my best guess at SSSB's markup — I can't load minasidor.sssb.se myself
to verify them (it's behind login and outside what I can reach from here).
Run once with `--debug` first; see README.md "Fixing the selectors" section.

CREDENTIALS: your SSSB username/password are never stored in this project
folder. The first time they're needed, you'll be prompted for them, with
the option to save them in your OS's own keychain (macOS Keychain / Windows
Credential Locker / Linux Secret Service via the `keyring` library) instead
of any file that could end up in a git commit. See `--login` / `--forget-login`.

Usage:
    python sssb_kth_monitor.py --login          # store SSSB credentials in your OS keychain
    python sssb_kth_monitor.py --forget-login   # remove them again
    python sssb_kth_monitor.py --once           # one scrape, save + notify, exit
    python sssb_kth_monitor.py --serve          # run scrape + start local API/dashboard server
    python sssb_kth_monitor.py --debug          # also dump rendered HTML to debug_page.html
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
    """Log into minasidor.sssb.se.

    CONFIGURABLE: field selectors below are a best guess (SSSB commonly uses
    a personnummer + password form). If login fails, run with --debug, open
    debug_page.html, right-click the username/password fields → Inspect, and
    update the `By.CSS_SELECTOR` values below to match.
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

    housing_type, queue_days, rent_sek, size_sqm = _parse_card_fields(card_text)

    return {
        "id": url,
        "area": area,
        "raw_text": card_text[:300],
        "type": housing_type,
        "queue_days": queue_days,
        "rent_sek": rent_sek,
        "size_sqm": size_sqm,
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
    """Returns (housing_type, queue_days, rent_sek, size_sqm), any of which
    may be None if the card text doesn't match the expected shape (falls
    back to the older, looser regexes so a format change degrades rather
    than silently returning nothing).
    """
    housing_type = None
    type_match = _CARD_TYPE_RE.match(card_text)
    if type_match and type_match.group(1).strip():
        housing_type = _translate_housing_type(type_match.group(1))

    values_match = _CARD_VALUES_RE.search(card_text)
    if values_match:
        size_sqm = int(values_match.group("size"))
        rent_sek = int(re.sub(r"\s", "", values_match.group("rent")))
        queue_days = int(re.sub(r"\s", "", values_match.group("queue")))
        return housing_type, queue_days, rent_sek, size_sqm

    # Fallback: older free-text heuristics, in case SSSB's card layout has
    # drifted from the labeled-table format confirmed above.
    queue_match = re.search(r"(\d[\d\s]{0,6})\s*(day|days|dag|dagar)", card_text, re.IGNORECASE)
    queue_days = int(re.sub(r"\s", "", queue_match.group(1))) if queue_match else None

    rent_match = re.search(r"(\d[\d\s]{2,6})\s*kr", card_text)
    rent_sek = int(re.sub(r"\s", "", rent_match.group(1))) if rent_match else None

    size_match = re.search(r"(\d{1,3})\s*m²", card_text)
    size_sqm = int(size_match.group(1)) if size_match else None

    return housing_type, queue_days, rent_sek, size_sqm


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
    from bs4 import BeautifulSoup
    import re

    driver.get(LISTINGS_URL)

    # Wait until the Angular/Vue template placeholders have been replaced
    # with real numbers (the un-rendered page literally contains "{{alla}}").
    wait = WebDriverWait(driver, 25)
    try:
        wait.until(lambda d: "{{" not in d.page_source)
    except Exception:
        pass  # proceed anyway; page may just have 0 listings right now

    time.sleep(2)  # small buffer for any trailing async rendering

    # SSSB shows "Shown X - Y of Z vacant homes" (or, on the Swedish URL we
    # now use, "Visas X - Y av Z lediga bostäder") — grab Z if we can, just
    # to tell you at the end whether we actually got everything.
    expected_total = None
    try:
        m = re.search(r"(?:of|av)\s+(\d+)\s+(?:vacant|lediga)", driver.page_source, re.IGNORECASE)
        if m:
            expected_total = int(m.group(1))
    except Exception:
        pass

    all_links_by_url = {}
    for page_num in range(1, 26):  # hard cap so a broken "next" click can't loop forever
        soup = BeautifulSoup(driver.page_source, "html.parser")
        page_links = [a for a in soup.select("a[href]") if "refid=" in a["href"]]

        new_count = 0
        for a in page_links:
            href = a["href"]
            url = href if href.startswith("http") else "https://minasidor.sssb.se" + href
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

    listing_links = list(all_links_by_url.items())
    print(f"  found {len(listing_links)} unique link(s) containing 'refid=' across all pages")

    listings = [_parse_listing_from_link(link, url) for url, link in listing_links]
    unknown_area_count = sum(1 for l in listings if l["area"] == "Unknown")

    print(f"  parsed {len(listings)} listing(s):")
    for l in listings:
        print(f"    [{l['area']}] queue_days={l['queue_days']} rent={l['rent_sek']} "
              f"size={l['size_sqm']} :: {l['raw_text'][:90]}")

    if len(listing_links) == 0:
        print("  ! No 'refid=' links found at all — either 0 listings are published right "
              "now, or SSSB's link format differs from the example you gave me. Run --debug "
              "and grep debug_page.html for 'refid=' to confirm which.")
    if expected_total and len(listing_links) < expected_total:
        print(f"  ! Only found {len(listing_links)} of an expected ~{expected_total} — the "
              "next/load-more click isn't fully working. Run --debug and watch the browser "
              "to see what the pagination control actually looks like.")
    if unknown_area_count:
        print(f"  ! {unknown_area_count} listing(s) didn't match a known area name — the "
              "surrounding-text heuristic may be grabbing the wrong ancestor for those. Check "
              "the raw_text above; paste one here and I can adjust it.")

    return listings



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


def run_scrape(debug: bool = False) -> dict:
    with _scrape_lock:
        return _run_scrape_impl(debug=debug)


def _run_scrape_impl(debug: bool = False) -> dict:
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

    print("launching browser + logging in...")
    driver = init_driver(headless=not debug)
    try:
        login(driver)
        print("scraping listings...")
        listings = scrape_listings(driver, debug=debug)
    finally:
        driver.quit()

    new_listings = [l for l in listings if l["id"] not in previous_ids]
    print(f"found {len(listings)} listings ({len(new_listings)} new)")

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

def _background_poll_loop(interval_minutes: float):
    """Runs for the lifetime of `--serve`, re-scraping on its own so you
    don't have to sit there clicking Refresh. Any failure (SSSB hiccup,
    network blip) is logged and skipped rather than killing the loop.
    """
    while True:
        time.sleep(interval_minutes * 60)
        try:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] auto-check...")
            run_scrape()
        except SystemExit as e:
            print(f"  ! auto-check stopped early: {e}")
        except Exception as e:
            print(f"  ! auto-check failed, will retry next interval: {e}")


def serve(interval_minutes: float):
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
        return jsonify(run_scrape())

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        return jsonify(run_scrape())

    threading.Thread(target=_background_poll_loop, args=(interval_minutes,), daemon=True).start()

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
    parser.add_argument("--debug", action="store_true", help="run visible browser + dump debug_page.html")
    args = parser.parse_args()

    if args.forget_login:
        forget_credentials()
    elif args.login:
        _prompt_and_store()
        print("Done — future runs will use this automatically.")
    elif args.once:
        run_scrape(debug=args.debug)
    elif args.serve:
        if args.interval < 5:
            parser.error("--interval below 5 minutes isn't a great idea — see README on rate limiting.")
        if not CURRENT_FILE.exists():
            run_scrape(debug=args.debug)
        serve(interval_minutes=args.interval)
    else:
        parser.print_help()
        sys.exit(1)
