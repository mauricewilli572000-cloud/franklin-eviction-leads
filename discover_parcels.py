"""Reconnaissance: where does the Franklin County Auditor publish CURRENT parcel/owner data?

    python discover_parcels.py

Prints status + data-looking links for the Auditor's download pages. Run in CI (the Auditor's
sites are not reachable from every network). Output is links only - no personal data.
"""
import re
import sys

import requests
from bs4 import BeautifulSoup

PAGES = [
    "https://auditor.franklincountyohio.gov/Auditor/FTP",
    "https://franklincountyauditor.com/ftp",
    "https://apps.franklincountyauditor.com/Parcel_CSV/",
    "https://apps.franklincountyauditor.com/",
    "https://auditor-fca.opendata.arcgis.com/",
]
WANT = re.compile(r"csv|zip|xls|download|parcel|owner|sale|transfer|conveyance|ftp|bulk|data", re.I)


def main() -> int:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; FranklinLeadsRecon/1.0)"
    for url in PAGES:
        try:
            r = s.get(url, timeout=45, allow_redirects=True)
        except requests.RequestException as exc:
            print(f"[recon] {url} -> ERROR {exc}")
            continue
        print(f"[recon] {url} -> {r.status_code} final={r.url} bytes={len(r.content)}")
        if "html" not in r.headers.get("Content-Type", "").lower():
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        print(f"[recon]   title: {title}")
        seen = set()
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text(" ", strip=True).split())[:90]
            href = a["href"]
            if href in seen or not (WANT.search(href) or WANT.search(text)):
                continue
            seen.add(href)
            print(f"[recon]   {text!r} -> {href}")
            if len(seen) >= 60:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
