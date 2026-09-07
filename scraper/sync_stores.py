#!/usr/bin/env python3
"""Monthly Home Depot store discovery and sync.

Finds Home Depot store location pages published by Home Depot for the US
(homedepot.com) and Canada (stores.homedepot.ca), compares their store
numbers against stores.json, and appends any genuinely new stores in the
file's existing sort order (country: USA, CAN, MEX; then state/province;
then streetAddress). Every run also cross-checks OpenStreetMap's Overpass
API (free, no key, not behind Akamai) for Home Depot locations not yet in
stores.json -- a candidate with a usable store number (an OSM `ref` tag)
gets added the same way; one without gets reported for manual lookup
rather than guessed at, since OSM is not the authority on Home Depot's own
internal store numbers.

This intentionally does NOT touch existing entries -- it only adds ones
whose storeNumber isn't already a key in stores.json. It also does not
attempt to discover Mexico stores via homedepot.com itself;
homedepot.com.mx's site structure has not been verified -- Mexico
coverage currently comes only from the OpenStreetMap cross-check.

Home Depot's human-facing pages sit behind Akamai Bot Manager, confirmed
(via real captured evidence, not assumption -- a soft "Oops!! Something
went wrong" error page, window.digitalData reporting pageName "error
page", and Akamai bot-sensor beacon URLs in network captures) to block
every automated technique tried against it: plain HTTP clients, Playwright,
and even puppeteer-extra-plugin-stealth with a full rendered browser. So
this script does NOT try a headless browser here -- that was tried for
real, with stronger stealth tooling than this project has, and still hit
the same wall. It only tries a plain HTTP request, which works for
machine-readable endpoints (sitemaps) that sites don't typically wall off,
and treats an individual store page it can't fetch as a manual-lookup
item (we still know the store NUMBER from the URL itself, just not the
address) rather than silently dropping it.

Exit codes:
  0 - ran fine, report written (new_stores/needs_manual_lookup may be empty)
  2 - Home Depot's own site returned nothing AND the OpenStreetMap
      cross-check also came back empty/failed -- almost certainly means
      something broke (site structure changed, both sources blocked)
      rather than that zero stores exist anywhere. The workflow treats
      this as a hard failure and sends an alert instead of opening a PR.
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

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TitanDataStoreSync/1.0; +https://titanwft.net)"}
REQUEST_DELAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 20

COUNTRY_ORDER = {"USA": 0, "CAN": 1, "MEX": 2}

US_STORE_URL_RE = re.compile(r"/l/[^/]+/[A-Z]{2}/[^/]+/[\w-]+/(\d{3,5})(?:/|$)")
CA_STORE_URL_RE = re.compile(r"-([a-z]{2})-hs(\d{3,5})\.html$")


def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY_SECONDS)
    return resp.text


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
    public directory as a fallback if the sitemap has moved or changed shape.
    Both are plain HTTP -- see the module docstring on why no browser fallback."""
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
    Google itself reads. This is still a plain HTTP GET, so it can fail
    even when the sitemap listing it came from didn't -- Akamai can wall
    off individual pages more aggressively than the sitemap itself.
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


OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="US"][admin_level=2]->.us;
area["ISO3166-1"="CA"][admin_level=2]->.ca;
area["ISO3166-1"="MX"][admin_level=2]->.mx;
(
  nwr["shop"="doityourself"]["name"~"Home Depot",i](area.us);
  nwr["shop"="doityourself"]["name"~"Home Depot",i](area.ca);
  nwr["shop"="doityourself"]["name"~"Home Depot",i](area.mx);
);
out center tags;
"""
OSM_COUNTRY_MAP = {"US": "USA", "CA": "CAN", "MX": "MEX"}


def discover_osm_candidates(failures: list[str]) -> list[dict]:
    """Cross-reference against OpenStreetMap's Overpass API: free, no key or
    billing, not behind Akamai, and runs regardless of whether Home Depot's
    own site cooperated this run. Coverage of the actual Home Depot store
    number (an addr `ref` tag some mappers add) is inconsistent -- a
    candidate without one is reported for manual lookup, never guessed at.
    """
    try:
        resp = requests.post(OVERPASS_URL, data={"data": OVERPASS_QUERY}, timeout=200)
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except Exception as e:  # noqa: BLE001
        failures.append(f"OpenStreetMap/Overpass cross-check failed ({e})")
        return []

    candidates = []
    for el in elements:
        tags = el.get("tags", {})
        street = " ".join(p for p in [tags.get("addr:housenumber"), tags.get("addr:street")] if p).strip()
        if not street or not tags.get("addr:postcode"):
            continue
        country_code = (tags.get("addr:country") or "").strip().upper()
        ref = re.sub(r"\D", "", tags.get("ref", "") or "")
        candidates.append(
            {
                "storeNumber": ref.zfill(4) if ref else None,
                "storeName": tags.get("name", "The Home Depot"),
                "streetAddress": street,
                "city": (tags.get("addr:city") or "").strip(),
                "state": (tags.get("addr:state") or "").strip(),
                "zip": (tags.get("addr:postcode") or "").strip(),
                "country": OSM_COUNTRY_MAP.get(country_code, country_code or None),
                "url": None,
            }
        )
    return candidates


def _addr_key(entry: dict) -> tuple:
    return (entry.get("zip", "").strip().lower(), entry.get("streetAddress", "").strip().lower())


def _hd_new_store_candidates(
    urls: set[str], url_re: re.Pattern, number_group: int, country: str, existing_numbers: set[str]
) -> tuple[dict, list]:
    """For each candidate URL not already in stores.json, try to pull the
    full address. If Home Depot blocks that individual page, we still know
    the real store NUMBER (it's in the URL itself, confirmed straight from
    Home Depot's own sitemap) -- report that as a manual-lookup item with a
    link, instead of silently discarding a confirmed new store."""
    found: dict[str, dict] = {}
    lookups: list[dict] = []

    for url in sorted(urls):
        m = url_re.search(url)
        if not m:
            continue
        store_number = m.group(number_group).zfill(4)
        if store_number in existing_numbers:
            continue

        parsed = parse_store_page(url)
        if parsed and parsed["state"] and parsed["zip"]:
            parsed.update(storeNumber=store_number, country=country)
            found[store_number] = parsed
        else:
            lookups.append(
                {
                    "storeNumber": store_number,
                    "storeName": "The Home Depot",
                    "streetAddress": "",
                    "city": "",
                    "state": "",
                    "zip": "",
                    "country": country,
                    "url": url,
                }
            )

    return found, lookups


def main() -> None:
    data = load_stores()
    existing_numbers = set(data.keys())
    failures: list[str] = []
    new_entries: dict[str, dict] = {}
    needs_manual_lookup: list[dict] = []

    us_urls = discover_us_store_urls(failures)
    ca_urls = discover_ca_store_urls(failures)

    us_found, us_lookups = _hd_new_store_candidates(us_urls, US_STORE_URL_RE, 1, "USA", existing_numbers)
    new_entries.update(us_found)
    needs_manual_lookup.extend(us_lookups)

    ca_found, ca_lookups = _hd_new_store_candidates(ca_urls, CA_STORE_URL_RE, 2, "CAN", existing_numbers)
    new_entries.update(ca_found)
    needs_manual_lookup.extend(ca_lookups)

    # OpenStreetMap cross-check: runs every time, independent of whether
    # Home Depot's own site cooperated above. Never overwrites a store
    # Home Depot's own site already found this run -- that data has a
    # confirmed real store number, OSM's ref tag is a distant second best.
    existing_addr_keys = {_addr_key(v) for v in data.values()}
    new_entry_addr_keys = {_addr_key(v) for v in new_entries.values()}

    for cand in discover_osm_candidates(failures):
        key = _addr_key(cand)
        if key in existing_addr_keys or key in new_entry_addr_keys:
            continue  # already tracked, or already found via Home Depot itself this run
        if cand["storeNumber"] and cand["country"] and cand["storeNumber"] not in existing_numbers:
            new_entries[cand["storeNumber"]] = {k: v for k, v in cand.items() if k != "url"}
            new_entry_addr_keys.add(key)
        else:
            needs_manual_lookup.append(cand)

    if new_entries:
        data.update(new_entries)
        save_stores(data)

    REPORT_PATH.write_text(
        json.dumps(
            {
                "new_stores": new_entries,
                "needs_manual_lookup": needs_manual_lookup,
                "failures": failures,
                "us_urls_scanned": len(us_urls),
                "ca_urls_scanned": len(ca_urls),
            },
            indent=2,
        )
    )

    print(
        f"Found {len(new_entries)} new store(s), "
        f"{len(needs_manual_lookup)} candidate(s) needing manual lookup, "
        f"{len(failures)} failure(s) logged."
    )

    if not us_urls and not ca_urls and not new_entries and not needs_manual_lookup and failures:
        # Home Depot's own site failed AND OSM found nothing usable either --
        # treat as "something broke" rather than "zero stores exist", so the
        # workflow alerts instead of silently no-opping forever.
        sys.exit(2)


if __name__ == "__main__":
    main()
