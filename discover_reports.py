"""Reconnaissance helper (like the Cuyahoga discover_*.py scripts).

    python discover_reports.py                 # list CSV links found on the Clerk's page
    python discover_reports.py path/to/file.csv   # show how its headers map; NO row data printed

Paste the output back if a header isn't mapped so config.HEADER_OVERRIDES can be set.
"""
import sys
from pathlib import Path

import fetch_reports
import parse_reports


def main() -> None:
    if len(sys.argv) > 1:
        headers, rows = parse_reports.read_csv_rows(Path(sys.argv[1]))
        mapping = parse_reports.map_headers(headers)
        print(f"{len(rows):,} data rows; {len(headers)} columns\n")
        inverse = {v: k for k, v in mapping.items()}
        for h in headers:
            print(f"  {h!r:50} -> {inverse.get(h, 'UNMAPPED')}")
        needed = [(None, "case_number"), ("defendant", "street")]
        miss = [n for n in needed if n not in mapping]
        if not any(k in mapping for k in (("plaintiff", "name"), ("plaintiff", "company"), ("plaintiff", "last"))):
            miss.append(("plaintiff", "name|company|last"))
        print("\nREQUIRED MISSING:" if miss else "\nAll required columns mapped.", miss or "")
        return

    links = fetch_reports.discover_report_links()
    print(f"{len(links)} report links found")
    for l in links:
        print(f"  {l['start']} to {l['end']}  {l['url']}")


if __name__ == "__main__":
    main()
