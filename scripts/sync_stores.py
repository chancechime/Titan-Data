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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TitanDataStoreSync/1.0; +https://titanwft.net)"
}
REQUEST_DELAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 20

COUNTRY_ORDER = {"USA": 0, "CAN": 1, "MEX": 2}

US_STORE_URL_RE = re.compile(r"/l/[^/]+/[A-Z]{2}/[^/]+/[\w-]+/(\d{3,5})(?:/|$)")
CA_STORE_URL_RE = re.compile(r"-([a-z]{2})-hs(\d{3,5})\.html$")


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


def get(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp


def discover_us_store_urls(failures: list[str]) -> set[str]:
    """Sitemap first (cheap, meant for bots); crawl the public directory as a fallback."""
    urls: set[str] = set()

    try:
        root = ET.fromstring(get("https://www.homedepot.com/sitemap.xml").content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        sub_sitemaps = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
        candidates = [u for u in sub_sitemaps if "store" in u.lower() or "location" in u.lower()]
        for sm_url in candidates:
            sm_root = ET.fromstring(get(sm_url).content)
            for loc in sm_root.findall(".//sm:loc", ns):
                if loc.text and US_STORE_URL_RE.search(loc.text):
                    urls.add(loc.text)
    except Exception as e:  # noqa: BLE001 - any failure here just means "try the fallback"
        failures.append(f"US sitemap discovery failed ({e}); fell back to directory crawl")

    if urls:
        return urls

    try:
        soup = BeautifulSoup(get("https://www.homedepot.com/l/storeDirectory").text, "html.parser")
        state_links = [
            a["href"] for a in soup.select("a[href]") if re.match(r"^/l/[A-Za-z-]+$", a["href"])
        ]
        for state_url in state_links:
            state_soup = BeautifulSoup(get(f"https://www.homedepot.com{state_url}").text, "html.parser")
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
        soup = BeautifulSoup(get("https://stores.homedepot.ca/sitemap.xml").text, "xml")
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
    soup = BeautifulSoup(get(url).text, "html.parser")
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
