#!/usr/bin/env python3
"""Monthly Home Depot store discovery and sync.

Finds Home Depot store location pages published by Home Depot for the US
(homedepot.com) and Canada (stores.homedepot.ca), compares their store
numbers against stores.json, and appends any genuinely new stores in the
file's existing sort order (country: USA, CAN, MEX; then state/province;
then streetAddress).

Every run also cross-checks two independent, non-Akamai-protected sources
that don't care about automated/bulk access the way Home Depot's own site
does:
  - Overture Maps Foundation's public places dataset (Parquet on S3, no
    API key, published specifically for bulk/automated consumption). In
    practice this is the most complete source: most US/Canada entries
    carry a homedepot.com store URL with the real store number embedded,
    sourced independently of homedepot.com itself.
  - OpenStreetMap's Overpass API (free, no key). Store numbers here come
    from an optional `ref` tag some mappers add -- less consistent than
    Overture, kept as an extra cross-check.
A candidate with a confirmed store number gets added the same way a
Home Depot-sourced find does; one without gets reported for manual lookup
rather than guessed at, since neither source is authoritative on Home
Depot's own internal store numbers.

This intentionally does NOT touch existing entries -- it only adds ones
whose storeNumber isn't already a key in stores.json. It also does not
attempt to discover Mexico stores via homedepot.com itself (its site
structure has not been verified) or via Overture (Mexico listings there
only link the generic homedepot.com.mx domain, no store number) -- Mexico
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

US_STORE_URL_RE = re.compile(r"/l/[^/]+/[A-Z]{2}/[^/]+/[\w-]+/(\d{3,5})(?:[/?]|$)")
CA_STORE_URL_RE = re.compile(r"-([a-z]{2})-(?:hs)?(\d{3,5})\.html")


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


def _parse_sitemap_locs(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]


def discover_sitemap_url_from_robots(base_url: str) -> list[str]:
    """robots.txt is where sites actually declare their sitemap location --
    more robust than guessing a path, since it's meant for exactly this."""
    try:
        text = fetch(f"{base_url}/robots.txt")
    except Exception:  # noqa: BLE001
        return []
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.lower().startswith("sitemap:")]


# Confirmed real path (from a working scraper that actually parsed it), not a
# guess -- Home Depot's "store" sitemap is filed under LocalCityPages, which
# doesn't contain the words "store" or "location" so a substring filter over
# the sitemap index misses it entirely.
US_KNOWN_SITEMAP_URLS = ["https://www.homedepot.com/sitemap/LocalCityPages/LCP-0.xml"]


def discover_us_store_urls(failures: list[str]) -> set[str]:
    """Known-good sitemap URL first; robots.txt-declared sitemaps second
    (in case Home Depot moves it again); directory crawl as a last resort.
    All plain HTTP -- see the module docstring on why no browser fallback."""
    urls: set[str] = set()

    sitemap_candidates = list(US_KNOWN_SITEMAP_URLS)
    sitemap_candidates += [
        u for u in discover_sitemap_url_from_robots("https://www.homedepot.com") if u not in sitemap_candidates
    ]

    for sitemap_url in sitemap_candidates:
        try:
            locs = _parse_sitemap_locs(fetch(sitemap_url))
        except Exception as e:  # noqa: BLE001
            failures.append(f"US sitemap fetch failed for {sitemap_url} ({e})")
            continue

        direct_hits = [loc for loc in locs if US_STORE_URL_RE.search(loc)]
        if direct_hits:
            urls.update(direct_hits)
            continue

        # Not a leaf sitemap -- likely a sitemap index. Recurse one level.
        for child_url in locs:
            if not child_url.endswith(".xml"):
                continue
            try:
                for loc in _parse_sitemap_locs(fetch(child_url)):
                    if US_STORE_URL_RE.search(loc):
                        urls.add(loc)
            except Exception as e:  # noqa: BLE001
                failures.append(f"US child sitemap fetch failed for {child_url} ({e})")

    if urls:
        return urls

    failures.append("US sitemap discovery found no store URLs from any known/declared sitemap; falling back to directory crawl")

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
    candidates = ["https://stores.homedepot.ca/sitemap.xml"]
    candidates += [
        u for u in discover_sitemap_url_from_robots("https://stores.homedepot.ca") if u not in candidates
    ]

    urls: set[str] = set()
    errors: list[str] = []
    for sitemap_url in candidates:
        try:
            soup = BeautifulSoup(fetch(sitemap_url), "xml")
            hits = {loc.text for loc in soup.find_all("loc") if loc.text and CA_STORE_URL_RE.search(loc.text)}
            if hits:
                urls.update(hits)
                break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sitemap_url} ({e})")

    if not urls and errors:
        failures.append(f"CA sitemap discovery failed -- no Canadian stores checked this run: {'; '.join(errors)}")

    return urls


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
        # Overpass's public instance returns 406 for requests that don't
        # look like they come from an identified client -- a bare
        # requests.post() with no headers was hitting exactly that.
        resp = requests.post(
            OVERPASS_URL,
            data={"data": OVERPASS_QUERY},
            headers=HEADERS,
            timeout=200,
        )
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


OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_MIN_CONFIDENCE = 0.7


def discover_overture_release() -> str | None:
    """Overture Maps publishes a new dated release monthly to a public S3
    bucket -- list it directly (plain, unauthenticated HTTPS GET against
    S3's REST API) rather than hardcode a release string that goes stale."""
    try:
        resp = requests.get(
            f"https://{OVERTURE_BUCKET}.s3.amazonaws.com/",
            params={"list-type": "2", "prefix": "release/", "delimiter": "/"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        prefixes = [
            p.find("s3:Prefix", ns).text
            for p in root.findall("s3:CommonPrefixes", ns)
            if p.find("s3:Prefix", ns) is not None
        ]
        releases = sorted(p.strip("/").split("/")[-1] for p in prefixes if p)
        return releases[-1] if releases else None
    except Exception:  # noqa: BLE001
        return None


OVERTURE_COUNTRY_MAP = {"US": "USA", "CA": "CAN", "MX": "MEX"}


def discover_overture_candidates(failures: list[str]) -> list[dict]:
    """Cross-reference against Overture Maps Foundation's public places
    dataset: an open data lake (Parquet on S3/Azure) explicitly published
    for bulk/automated consumption -- no API key, no per-IP throttling like
    a live API, not something that treats crawling as abuse. Far more
    complete than OpenStreetMap for this brand in practice: most US/Canada
    entries carry a homedepot.com store URL with the real store number
    embedded, sourced independently of Home Depot's own (Akamai-protected)
    site. Mexico entries typically only link the generic homedepot.com.mx
    domain, no store number -- those go to manual lookup like everything
    else without a confirmed number.
    """
    try:
        import pyarrow.dataset as pa_ds
        import pyarrow.fs as pa_fs
        import pyarrow.compute as pa_compute
    except ImportError as e:
        failures.append(f"Overture cross-check skipped -- pyarrow not installed ({e})")
        return []

    release = discover_overture_release()
    if not release:
        failures.append("Overture cross-check failed -- could not determine the latest release version")
        return []

    try:
        fs = pa_fs.S3FileSystem(region="us-west-2", anonymous=True)
        path = f"{OVERTURE_BUCKET}/release/{release}/theme=places/type=place"
        dataset = pa_ds.dataset(path, filesystem=fs, format="parquet", partitioning="hive")
        scanner = dataset.scanner(
            columns=["names", "addresses", "websites", "confidence"],
            filter=(pa_compute.field("names", "primary") == "The Home Depot"),
        )
        table = scanner.to_table()
    except Exception as e:  # noqa: BLE001
        failures.append(f"Overture cross-check failed ({e})")
        return []

    best_by_number: dict[str, dict] = {}
    unconfirmed: list[dict] = []

    for row in table.to_pylist():
        addr_list = row.get("addresses") or []
        if not addr_list or not addr_list[0].get("freeform"):
            continue
        addr = addr_list[0]
        confidence = row.get("confidence") or 0.0
        country_code = (addr.get("country") or "").upper()
        country = OVERTURE_COUNTRY_MAP.get(country_code)
        websites = row.get("websites") or []

        store_number = None
        for url in websites:
            m = US_STORE_URL_RE.search(url) or CA_STORE_URL_RE.search(url)
            if m:
                store_number = m.group(m.lastindex).zfill(4)
                break

        candidate = {
            "storeNumber": store_number,
            "storeName": row.get("names", {}).get("primary") or "The Home Depot",
            "streetAddress": addr["freeform"].strip(),
            "city": (addr.get("locality") or "").strip(),
            "state": (addr.get("region") or "").strip(),
            "zip": (addr.get("postcode") or "").split("-")[0].strip(),
            "country": country,
            "confidence": confidence,
            "url": websites[0] if websites else None,
        }

        if not candidate["state"] or not candidate["zip"] or not country:
            continue

        if store_number and confidence >= OVERTURE_MIN_CONFIDENCE:
            existing = best_by_number.get(store_number)
            if existing is None or confidence > existing["confidence"]:
                best_by_number[store_number] = candidate
        else:
            unconfirmed.append(candidate)

    return list(best_by_number.values()) + unconfirmed


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

    STORE_FIELDS = {"storeNumber", "storeName", "streetAddress", "city", "state", "zip", "country"}

    for cand in discover_overture_candidates(failures) + discover_osm_candidates(failures):
        key = _addr_key(cand)
        already_tracked = (
            key in existing_addr_keys
            or key in new_entry_addr_keys
            or cand["storeNumber"] in existing_numbers  # address text can differ (Ave vs Avenue) even
            or cand["storeNumber"] in new_entries  # when the store number itself already matches
        )
        if already_tracked:
            continue
        if cand["storeNumber"] and cand["country"]:
            new_entries[cand["storeNumber"]] = {k: v for k, v in cand.items() if k in STORE_FIELDS}
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
