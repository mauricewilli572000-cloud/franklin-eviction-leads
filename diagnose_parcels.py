"""Why do rentals fail to match a parcel?  Prints COUNTS and house-number-masked examples only.

    python diagnose_parcels.py            # after run_pipeline.py (needs data/raw_eviction_csv and .cache/Parcel_gis.csv)
"""
import csv
import re
import sys
from collections import Counter, defaultdict

import config
import parcels
import parse_reports

UNIT = re.compile(r"\b(APT|UNIT|STE|SUITE|BLDG|LOT|RM)\b|#")


def head(street: str):
    """(house number, first alphabetic street token) - ignoring directions."""
    toks = parcels._PUNCT.sub(" ", street.upper()).split()
    if not toks or not toks[0].isdigit():
        return None
    rest = [t for t in toks[1:] if t not in ("N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST")]
    return (toks[0], rest[0] if rest else "")


def main() -> int:
    path = config.CACHE_DIR / "Parcel_gis.csv"
    exact, nodir, by_head = set(), set(), defaultdict(list)
    zips = Counter()
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            site, z = row["STADDR"], row["ZIPCODE"]
            zips["blank" if not zip5(z) else "ok"] += 1
            k1, k2 = parcels.address_keys(site, z)
            if k1:
                exact.add(k1); nodir.add(k2)
            h = head(site)
            if h:
                by_head[h].append((site, zip5(z)))
    print(f"[diag] parcel rows with usable ZIP: {dict(zips)}; distinct (house#,street) heads: {len(by_head):,}")

    filings = parse_reports.parse_directory(config.RAW_DIR)
    cats, examples = Counter(), defaultdict(list)
    for f in filings:
        street, z = parcels.street_of(f.property_address), f.property_zip
        k1, k2 = parcels.address_keys(street, z)
        if k1 in exact or k2 in nodir:
            cats["matched"] += 1
            continue
        if UNIT.search(street.upper()):
            cat = "unmatched: apartment/unit address"
        else:
            h = head(street)
            cands = by_head.get(h, []) if h else []
            if not h:
                cat = "unmatched: no house number"
            elif not cands:
                cat = "unmatched: no parcel with this house# + street name anywhere"
            elif any(c[1] == zip5(z) for c in cands):
                cat = "unmatched: same house#/street/ZIP exists but key differs (format)"
            else:
                cat = "unmatched: house#/street exists under a DIFFERENT ZIP"
            if len(examples[cat]) < 6 and cands:
                examples[cat].append(f"rental {parcels.mask_number(street)}|{z}  vs parcel {parcels.mask_number(cands[0][0])}|{cands[0][1]}")
        cats[cat] += 1
    total = sum(cats.values())
    for k, v in cats.most_common():
        print(f"[diag] {v:6,} ({v / total:5.1%})  {k}")
        for e in examples.get(k, []):
            print(f"[diag]          {e}")
    return 0


def zip5(v):  # local alias
    return parcels.zip5(v)


if __name__ == "__main__":
    sys.exit(main())
