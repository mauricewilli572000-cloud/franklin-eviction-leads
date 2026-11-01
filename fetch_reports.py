"""Download the Clerk's monthly F.E.D. CSV reports.

Discovers links from the reports page; falls back to the known URL pattern if the
page layout changes. Current + previous month are re-downloaded every run (the Clerk
regenerates them nightly); older months are downloaded once.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

import config

RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "text/html,text/csv,*/*"})
    return s


def _get(session: requests.Session, url: str) -> requests.Response:
    last_exc = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp
            last_exc = RuntimeError(f"HTTP {resp.status_code} for {url}")
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(config.REQUEST_DELAY_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_exc}")


def discover_report_links(session: requests.Session | None = None) -> list[dict]:
    """Return [{'url','start','end'}...] newest first, from the reports page."""
    session = session or _session()
    html = _get(session, config.REPORTS_PAGE).text
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".csv" not in href.lower():
            continue
        m = RANGE_RE.search(unquote(href))
        if not m:
            continue
        links.append({"url": urljoin(config.REPORTS_PAGE, href), "start": m.group(1), "end": m.group(2)})
    links.sort(key=lambda d: d["start"], reverse=True)
    return links


def fallback_links(today: date, months: int = config.MONTHS_BACK) -> list[dict]:
    """Build monthly URLs from the known naming pattern."""
    links = []
    y, m = today.year, today.month
    for _ in range(months):
        start = date(y, m, 1)
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        end = nxt - timedelta(days=1)
        links.append({
            "url": config.CSV_URL_TEMPLATE.format(start=start.isoformat(), end=end.isoformat()),
            "start": start.isoformat(), "end": end.isoformat(),
        })
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return links


def download_reports(today: date | None = None, force: bool = False) -> list[Path]:
    today = today or date.today()
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = _session()
    try:
        links = discover_report_links(session)
    except Exception as exc:  # layout change / transient error
        print(f"[fetch] link discovery failed ({exc}); using URL pattern fallback")
        links = []
    if not links:
        links = fallback_links(today)
    links = links[: config.MONTHS_BACK]

    saved: list[Path] = []
    for idx, link in enumerate(links):
        dest = config.RAW_DIR / f"{link['start']}_{link['end']}.csv"
        refresh = force or idx < config.ALWAYS_REFRESH_LAST_N or not dest.exists()
        if not refresh:
            saved.append(dest)
            continue
        try:
            resp = _get(session, link["url"])
        except RuntimeError as exc:
            print(f"[fetch] WARN {exc}")
            if dest.exists():
                saved.append(dest)  # keep stale copy rather than losing the month
            continue
        ctype = resp.headers.get("Content-Type", "")
        if "html" in ctype.lower():
            print(f"[fetch] WARN {link['url']} returned HTML, not CSV; skipping")
            continue
        dest.write_bytes(resp.content)
        saved.append(dest)
        print(f"[fetch] saved {dest.name} ({len(resp.content):,} bytes)")
        time.sleep(config.REQUEST_DELAY_SECONDS)
    return saved
