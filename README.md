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

`.github/workflows/monthly-store-sync.yml` runs on the 1st of each month (and can be run manually via workflow_dispatch). `scraper/sync_stores.py` checks Home Depot's own sitemap for US/Canada store URLs not yet in `stores.json`, cross-checks OpenStreetMap's Overpass API for Home Depot locations (a source that isn't behind Akamai) not yet in the file, opens a pull request with anything it can fully confirm, and emails a report to chance@titanwft.net either way.

**Required repo secrets** (Settings -> Secrets and variables -> Actions):
- `STORE_SYNC_SMTP_HOST`, `STORE_SYNC_SMTP_PORT`, `STORE_SYNC_SMTP_USER`, `STORE_SYNC_SMTP_PASS` — SMTP credentials the workflow sends the report through.

**Why there's no browser automation here:** Home Depot's site runs Akamai Bot Manager, and this was tested for real (not assumed) across a real, separate scraping project attempting Playwright, plain HTTP, and puppeteer-extra-plugin-stealth with a fully rendered browser — every technique got served the same soft-block error page. A browser-automation fallback would cost real CI time for effectively no chance of working, so this only ever makes plain HTTP requests, which do work for the sitemap (a machine-readable endpoint most sites don't wall off) even though the individual store pages sometimes still get blocked.

**How the "can't fully confirm" cases are handled, not guessed:**
- A store number is Home Depot's own internal identifier — no third-party source (OpenStreetMap included) is authoritative on it. A candidate address without one is emailed under "needs manual lookup" rather than invented.
- If Home Depot's sitemap lists a store URL not in `stores.json` but the individual page is blocked, the store *number* is still confirmed (it's embedded in the URL itself) even though the address isn't — that goes into the same manual-lookup list with a direct link, since a human clicking it isn't blocked the way the automation is.
- Typical month: a short "no new stores" email, since Home Depot doesn't open many new locations. Expect the occasional manual-lookup item, not a fully hands-off pipeline — that ceiling is a property of Home Depot's site, not something scriptable around it.

**Known limitation:** Mexico has no direct Home Depot source (homedepot.com.mx's structure hasn't been mapped) — its only coverage is the OpenStreetMap cross-check.

## Website

Visit `https://titanwft.net` for the Titan website.
