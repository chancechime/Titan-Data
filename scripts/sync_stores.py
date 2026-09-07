#!/usr/bin/env python3
"""Monthly Home Depot store discovery and sync.

Finds Home Depot store location pages published by Home Depot for the US
(homedepot.com) and Canada (stores.homedepot.ca), compares their store
numbers against stores.json, and appends any genuinely new stores in the
file's existing sort order (country: USA, CAN, MEX; then state/province;
then streetAddress).

This intentionally does NOT touch existing entries -- it only adds ones
whose storeNumber isn't already a key in stores.json. It also does not
attempt to discover Mexico stores; homedepot.com.mx's site structure has
not been verified (see the workflow's README note).

Home Depot's human-facing pages sit behind Akamai Bot Manager, which
fingerprints and blocks plain HTTP clients (confirmed: 403 "Pardon Our
Interruption" responses with _abck/bm_sz cookies) regardless of headers.
fetch() below tries a normal HTTP request first -- fine for machine-
readable endpoints like sitemaps, which sites typically don't wall off --
and only pays for a real headless-browser render when that gets blocked.
Even that isn't guaranteed: Akamai also scores IP reputation, and GitHub
Actions' runner IPs are well-known cloud ranges that can get flagged
regardless of what the browser fingerprint looks like.

Exit codes:
  0 - ran fine, report written (new_stores may be empty)
  2 - discovery came back completely empty for both countries, which
      almost certainly means Home Depot changed their site structure
      or started blocking automated requests rather than that zero
      stores exist -- the workflow treats this as a hard failure and
      sends an alert instead of opening a PR.
"""

from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STORES_PATH = Path(__file__).resolve().parent.parent / "stores.json"
REPORT_PATH = Path("sync_report.json")

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TitanDataStoreSync/1.0; +https://titanwft.net)"}
REQUEST_DELAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 20
BROWSER_TIMEOUT_MS = 30000

COUNTRY_ORDER = {"USA": 0, "CAN": 1, "MEX": 2}

US_STORE_URL_RE = re.compile(r"/l/[^/]+/[A-Z]{2}/[^/]+/[\w-]+/(\d{3,5})(?:/|$)")
CA_STORE_URL_RE = re.compile(r"-([a-z]{2})-hs(\d{3,5})\.html$")

_browser_state: dict = {"playwright": None, "browser": None}


def _get_browser():
    if _browser_state["browser"] is None:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        _browser_state["playwright"] = pw
        _browser_state["browser"] = pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
    return _browser_state["browser"]


def close_browser() -> None:
    if _browser_state["browser"] is not None:
        _browser_state["browser"].close()
        _browser_state["playwright"].stop()
        _browser_state["browser"] = None
        _browser_state["playwright"] = None


def fetch(url: str) -> str:
    """Fetch a URL as text, plain HTTP first, headless-browser render as fallback."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code < 400:
            time.sleep(REQUEST_DELAY_SECONDS)
            return resp.text
    except requests.RequestException:
        pass

    page = _get_browser().new_page(user_agent=BROWSER_USER_AGENT)
    try:
        response = page.goto(url, wait_until="networkidle", timeout=BROWSER_TIMEOUT_MS)
        if response is None or response.status >= 400:
            status = response.status if response else "no response"
            raise RuntimeError(f"browser fetch also failed for {url} (status {status})")
        return response.text()
    finally:
        page.close()


def sort_key(entry: dict) -> tuple:
    return (COUNTRY_ORDER.get(entry["country"], 99), entry["state"], entry["streetAddress"])


def load_stores() -> dict:
    with open(STORES_PATH) as f:
        return json.load(f)


def save_stores(data: dict) -> None:
    ordered = dict(sorted(data.items(), key=lambda kv: sort_key(kv[1])))
    with open(STORES_PATH, "w") as f:
        json.dump(ordered, f, indent=2)
        f.write("\n")


def discover_us_store_urls(failures: list[str]) -> set[str]:
    """Sitemap first (machine-readable, usually not walled off); crawl the
    public directory as a fallback if the sitemap has moved or changed shape."""
    urls: set[str] = set()

    try:
        root = ET.fromstring(fetch("https://www.homedepot.com/sitemap/main.xml"))
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        sub_sitemaps = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
        candidates = [u for u in sub_sitemaps if "store" in u.lower() or "location" in u.lower()]
        for sm_url in candidates:
            sm_root = ET.fromstring(fetch(sm_url))
            for loc in sm_root.findall(".//sm:loc", ns):
                if loc.text and US_STORE_URL_RE.search(loc.text):
                    urls.add(loc.text)
    except Exception as e:  # noqa: BLE001 - any failure here just means "try the fallback"
        failures.append(f"US sitemap discovery failed ({e}); fell back to directory crawl")

    if urls:
        return urls

    try:
        soup = BeautifulSoup(fetch("https://www.homedepot.com/l/storeDirectory"), "html.parser")
        state_links = [
            a["href"] for a in soup.select("a[href]") if re.match(r"^/l/[A-Za-z-]+$", a["href"])
        ]
        for state_url in state_links:
            state_soup = BeautifulSoup(fetch(f"https://www.homedepot.com{state_url}"), "html.parser")
            for a in state_soup.select("a[href]"):
                href = a["href"]
                if US_STORE_URL_RE.search(href):
                    full = href if href.startswith("http") else f"https://www.homedepot.com{href}"
                    urls.add(full)
    except Exception as e:  # noqa: BLE001
        failures.append(f"US directory crawl fallback also failed ({e}) -- no US stores discovered")

    return urls


def discover_ca_store_urls(failures: list[str]) -> set[str]:
    try:
        soup = BeautifulSoup(fetch("https://stores.homedepot.ca/sitemap.xml"), "xml")
        return {loc.text for loc in soup.find_all("loc") if loc.text and CA_STORE_URL_RE.search(loc.text)}
    except Exception as e:  # noqa: BLE001
        failures.append(f"CA sitemap discovery failed ({e}) -- no Canadian stores checked this run")
        return set()


def parse_store_page(url: str) -> dict | None:
    """Pull address data out of the store page's schema.org JSON-LD block.

    JSON-LD is what these pages already publish for Google's own use (rich
    results / the Maps knowledge panel), so it's the same structured data
    Google itself reads -- more reliable than scraping visible page text.
    """
    soup = BeautifulSoup(fetch(url), "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (TypeError, ValueError):
            continue
        for obj in payload if isinstance(payload, list) else [payload]:
            if not isinstance(obj, dict):
                continue
            addr = obj.get("address")
            if isinstance(addr, dict) and addr.get("streetAddress"):
                return {
                    "storeName": obj.get("name", "The Home Depot"),
                    "streetAddress": (addr.get("streetAddress") or "").strip(),
                    "city": (addr.get("addressLocality") or "").strip(),
                    "state": (addr.get("addressRegion") or "").strip(),
                    "zip": (addr.get("postalCode") or "").strip(),
                }
    return None


def main() -> None:
    data = load_stores()
    existing_numbers = set(data.keys())
    failures: list[str] = []
    new_entries: dict[str, dict] = {}
    us_urls: set[str] = set()
    ca_urls: set[str] = set()

    try:
        us_urls = discover_us_store_urls(failures)
        ca_urls = discover_ca_store_urls(failures)

        for url in sorted(us_urls):
            m = US_STORE_URL_RE.search(url)
            if not m:
                continue
            store_number = m.group(1).zfill(4)
            if store_number in existing_numbers:
                continue
            parsed = parse_store_page(url)
            if not parsed or not parsed["state"] or not parsed["zip"]:
                failures.append(f"Could not parse a full address for candidate new store at {url}")
                continue
            parsed.update(storeNumber=store_number, country="USA")
            new_entries[store_number] = parsed

        for url in sorted(ca_urls):
            m = CA_STORE_URL_RE.search(url)
            if not m:
                continue
            store_number = m.group(2).zfill(4)
            if store_number in existing_numbers:
                continue
            parsed = parse_store_page(url)
            if not parsed or not parsed["state"] or not parsed["zip"]:
                failures.append(f"Could not parse a full address for candidate new store at {url}")
                continue
            parsed.update(storeNumber=store_number, country="CAN")
            new_entries[store_number] = parsed
    finally:
        close_browser()

    if new_entries:
        data.update(new_entries)
        save_stores(data)

    REPORT_PATH.write_text(
        json.dumps(
            {
                "new_stores": new_entries,
                "failures": failures,
                "us_urls_scanned": len(us_urls),
                "ca_urls_scanned": len(ca_urls),
            },
            indent=2,
        )
    )

    print(f"Found {len(new_entries)} new store(s); {len(failures)} failure(s) logged.")

    if not us_urls and not ca_urls:
        # Both discovery paths came back empty -- treat as "something broke"
        # rather than "Home Depot has zero stores", so the workflow alerts
        # instead of silently no-opping forever.
        sys.exit(2)


if __name__ == "__main__":
    main()
