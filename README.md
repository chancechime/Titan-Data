# Titan Data

Titan Data is the store-data repository behind Titan.

## Purpose

This repo exists to store and maintain the normalized Home Depot store directory used by the Titan ecosystem. The data is used to turn store numbers into full location details such as store name, street address, city, state, ZIP code, and country when available.

## Contents

- `stores.json` contains the store lookup dataset keyed by store number.
- Store numbers are normalized to 4 digits so lookups stay consistent across Titan tools and APIs.

## Used By

- The Titan browser extension, which enriches WFT schedule syncs with store location data.
- `titanwft.net`, which serves Titan-related web and API experiences backed by this dataset.

## Data conventions

- `state` uses the ISO 3166-2 subdivision code for the store's country (USPS 2-letter for US, provincial 2-letter for Canada, INEGI 3-letter for Mexico), for consistency across all three.
- `country` uses ISO 3166-1 alpha-3 (`USA`, `CAN`, `MEX`).
- Entries are sorted by country (USA, then CAN, then MEX), then `state`, then `streetAddress`.

## Monthly store sync (automated)

`.github/workflows/monthly-store-sync.yml` runs on the 1st of each month (and can be run manually via workflow_dispatch). `scraper/sync_stores.py` checks three independent sources for Home Depot locations not yet in `stores.json`, opens a pull request with anything it can fully confirm, and emails a report to chance@titanwft.net either way:

1. **Home Depot's own sitemap** (`homedepot.com`/`stores.homedepot.ca`) — the single most authoritative source when it's reachable, but it sits behind Akamai Bot Manager (see below) and can be blocked.
2. **Overture Maps Foundation's public places dataset** — an open data lake (Parquet on S3, `s3://overturemaps-us-west-2`) published specifically for bulk/automated consumption, not a live API that treats crawling as abuse. In practice this is the most reliable and complete source: the large majority of its US/Canada Home Depot listings carry a `homedepot.com` store URL with the real store number embedded, obtained independently of homedepot.com itself. No API key, no billing, no rate limiting tuned against cloud CI traffic.
3. **OpenStreetMap's Overpass API** — free, no key, kept as an extra cross-check. Store numbers here depend on an optional `ref` tag some mappers add, so it's less complete than Overture.

**Required repo secrets** (Settings -> Secrets and variables -> Actions):
- `STORE_SYNC_SMTP_HOST`, `STORE_SYNC_SMTP_PORT`, `STORE_SYNC_SMTP_USER`, `STORE_SYNC_SMTP_PASS` — SMTP credentials the workflow sends the report through.
- `STORE_SYNC_PAT` — a GitHub [personal access token](https://github.com/settings/tokens) (classic, `repo` scope, or a fine-grained token with Contents + Pull requests read/write on this repo) used to open the pull request. The default `GITHUB_TOKEN` can create the PR, but GitHub blocks PRs opened with it from triggering other workflows (e.g. required CI checks) on themselves — a PAT avoids that restriction.

**Why there's no browser automation against homedepot.com:** its site runs Akamai Bot Manager, tested for real (not assumed) across a separate scraping project that tried Playwright, plain HTTP, and puppeteer-extra-plugin-stealth with a fully rendered browser — every technique got served the same soft-block error page. A browser-automation fallback would cost real CI time for effectively no chance of working, so `sync_stores.py` only ever makes plain HTTP requests to homedepot.com, which work for the sitemap (a machine-readable endpoint most sites don't wall off) even though the individual store pages sometimes still get blocked.

**How the "can't fully confirm" cases are handled, not guessed:**
- A store number is Home Depot's own internal identifier — no third-party source is authoritative on it. A candidate address without one (most commonly Mexico locations, which link only the generic homedepot.com.mx domain rather than a per-store page) is emailed under "needs manual lookup" rather than invented.
- If Home Depot's sitemap lists a store URL not in `stores.json` but the individual page is blocked, the store *number* is still confirmed (it's embedded in the URL itself) even though the address isn't — that goes into the same manual-lookup list with a direct link, since a human clicking it isn't blocked the way the automation is.
- Overture and OSM candidates are matched against existing entries by both store number and address (zip + street) — the latter because different sources format street suffixes differently (e.g. "Ave" vs "Avenue"), so number is the more reliable de-duplication key.

**Known limitation:** Mexico has no confirmed-store-number source at all yet — Overture's Mexico listings don't carry a per-store URL, and homedepot.com.mx's own structure hasn't been mapped. Its stores show up under "needs manual lookup" until a source that actually exposes their store numbers is found.

## Website

Visit `https://titanwft.net` for the Titan website.
