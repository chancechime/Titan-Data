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

`.github/workflows/monthly-store-sync.yml` runs on the 1st of each month (and can be run manually via workflow_dispatch). It checks Home Depot's own US and Canada store location pages for store numbers not yet present in `stores.json`, opens a pull request with anything new, and emails a report to chance@titanwft.net.

**Required repo secrets** (Settings -> Secrets and variables -> Actions):
- `STORE_SYNC_SMTP_HOST`, `STORE_SYNC_SMTP_PORT`, `STORE_SYNC_SMTP_USER`, `STORE_SYNC_SMTP_PASS` — SMTP credentials the workflow sends the report through (e.g. a Gmail account with an [App Password](https://myaccount.google.com/apppasswords): host `smtp.gmail.com`, port `465`).

**Known limitations:**
- Mexico is not covered — homedepot.com.mx's site structure hasn't been mapped, so new Mexican stores won't be auto-detected yet.
- The scraper targets Home Depot's own sitemap and page structure, which isn't a published API and can change without notice. If a run can't find any stores at all, it skips the PR and sends a failure email instead of guessing.
- This has not been validated against a live run yet — trigger it manually once via workflow_dispatch and check the email/PR before trusting the schedule unattended.

## Website

Visit `https://titanwft.net` for the Titan website.
