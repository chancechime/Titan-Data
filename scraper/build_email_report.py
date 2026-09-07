#!/usr/bin/env python3
"""Render sync_report.json (written by sync_stores.py) into a small styled
HTML email body for the monthly store-sync GitHub Action."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

REPORT_PATH = Path("sync_report.json")
OUTPUT_PATH = Path("email_report.html")

PAGE = """<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f4f4f2;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#1b1b1b;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e5e0;">
    <div style="background:#f96302;padding:20px 24px;">
      <h1 style="margin:0;font-size:18px;color:#ffffff;">Titan Data &mdash; Monthly Store Sync</h1>
      <p style="margin:4px 0 0;font-size:13px;color:#ffe7d6;">{run_date}</p>
    </div>
    <div style="padding:24px;">
      {body}
    </div>
    <div style="padding:16px 24px;background:#fafafa;border-top:1px solid #e5e5e0;font-size:12px;color:#888;">
      Automated run of monthly-store-sync.yml on chancechime/Titan-Data.
    </div>
  </div>
</body>
</html>
"""

ROW = """<tr>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;font-family:monospace;">{storeNumber}</td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;">{streetAddress}, {city}, {state} {zip}</td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;">{country}</td>
</tr>"""

def _lookup_row(v: dict) -> str:
    if v.get("url"):
        # Found via Home Depot's own sitemap -- store number is confirmed,
        # the individual page just couldn't be fetched. A human clicking
        # the link isn't blocked the way the automation is.
        what = f'Store #{v["storeNumber"]} — <a href="{v["url"]}" style="color:#f96302;">view on homedepot.com &rarr;</a>'
    else:
        # Found via OpenStreetMap -- we have an address but no confirmed
        # Home Depot store number.
        what = f'{v["streetAddress"]}, {v["city"]}, {v["state"]} {v["zip"]}'
    return f"""<tr>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;">{what}</td>
  <td style="padding:8px 10px;border-bottom:1px solid #eee;font-size:13px;">{v.get("country") or ""}</td>
</tr>"""


def render(report: dict, commit_url: str) -> str:
    new_stores = report.get("new_stores", {})
    needs_manual_lookup = report.get("needs_manual_lookup", [])
    failures = report.get("failures", [])

    if new_stores:
        rows = "".join(ROW.format(**v) for v in new_stores.values())
        commit_line = (
            f'<p style="font-size:13px;margin-top:16px;">'
            f'<a href="{commit_url}" style="color:#f96302;">View the commit &rarr;</a></p>'
            if commit_url
            else ""
        )
        body = f"""
        <p style="font-size:14px;">Added <strong>{len(new_stores)}</strong> new Home Depot store(s) to
        <code>stores.json</code> directly (each one had every required field confirmed, so no manual
        review gate before committing):</p>
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <th style="text-align:left;padding:8px 10px;font-size:12px;color:#888;border-bottom:2px solid #eee;">Store #</th>
            <th style="text-align:left;padding:8px 10px;font-size:12px;color:#888;border-bottom:2px solid #eee;">Address</th>
            <th style="text-align:left;padding:8px 10px;font-size:12px;color:#888;border-bottom:2px solid #eee;">Country</th>
          </tr>
          {rows}
        </table>
        {commit_line}
        """
    else:
        body = (
            '<p style="font-size:14px;">No new stores found this month. '
            "<code>stores.json</code> is unchanged.</p>"
        )

    if needs_manual_lookup:
        rows = "".join(_lookup_row(v) for v in needs_manual_lookup)
        body += f"""
        <p style="font-size:14px;margin-top:20px;">Found <strong>{len(needs_manual_lookup)}</strong>
        possible new location(s) that couldn't be fully confirmed automatically -- these were
        <strong>not</strong> added to <code>stores.json</code>. Where a store number is shown, it's
        confirmed from Home Depot's own sitemap; click through to get the address. Where only an
        address is shown (from OpenStreetMap), look it up on Home Depot's store locator to get the
        store number. Then add either by hand:</p>
        <table style="width:100%;border-collapse:collapse;">
          <tr>
            <th style="text-align:left;padding:8px 10px;font-size:12px;color:#888;border-bottom:2px solid #eee;">Details</th>
            <th style="text-align:left;padding:8px 10px;font-size:12px;color:#888;border-bottom:2px solid #eee;">Country</th>
          </tr>
          {rows}
        </table>
        """

    if failures:
        items = "".join(f'<li style="font-size:13px;color:#a33;">{f}</li>' for f in failures)
        body += (
            '<p style="font-size:13px;margin-top:20px;"><strong>Needs a look:</strong></p>'
            f"<ul>{items}</ul>"
        )

    return PAGE.format(run_date=date.today().isoformat(), body=body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-url", default="")
    args = parser.parse_args()

    report = json.loads(REPORT_PATH.read_text()) if REPORT_PATH.exists() else {
        "new_stores": {},
        "failures": ["sync_report.json was missing -- sync_stores.py may have crashed before writing it"],
    }
    OUTPUT_PATH.write_text(render(report, args.commit_url))


if __name__ == "__main__":
    main()
