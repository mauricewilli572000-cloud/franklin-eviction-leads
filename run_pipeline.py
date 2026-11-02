"""Run the Franklin County eviction (tired landlord) pipeline.

    python run_pipeline.py                      # download from the Clerk, parse, score, export
    python run_pipeline.py --from-dir ./csvs    # use CSVs you downloaded manually
    python run_pipeline.py --no-download        # re-process what is already in data/raw_eviction_csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import config
import exporters
import fetch_reports
import landlord_leads
import parcels
import parse_reports


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-dir", type=Path, help="parse CSVs from this folder instead of downloading")
    ap.add_argument("--no-download", action="store_true", help="skip download; use data/raw_eviction_csv")
    ap.add_argument("--force", action="store_true", help="re-download every month")
    ap.add_argument("--include-institutional", action="store_true", help="keep housing authorities / mega-filers")
    ap.add_argument("--min-filings", type=int, default=config.MIN_FILINGS_FOR_LEAD)
    ap.add_argument("--no-parcels", action="store_true", help="skip Auditor owner matching")
    ap.add_argument("--parcel-csv", type=Path, help="use this local Parcel.csv instead of downloading it")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = ap.parse_args(argv)

    config.MIN_FILINGS_FOR_LEAD = args.min_filings

    if args.from_dir:
        src_dir = args.from_dir
    else:
        src_dir = config.RAW_DIR
        if not args.no_download:
            fetch_reports.download_reports(today=args.today, force=args.force)

    if not list(Path(src_dir).glob("*.csv")) and list(config.MANUAL_DIR.glob("*.csv")):
        print(f"[fetch] no CSVs downloaded; falling back to {config.MANUAL_DIR}")
        src_dir = config.MANUAL_DIR
    if not list(Path(src_dir).glob("*.csv")):
        print(f"No CSV files found in {src_dir} (and none in {config.MANUAL_DIR})", file=sys.stderr)
        return 1

    filings = parse_reports.parse_directory(src_dir)
    print(f"[parse] {len(filings):,} unique eviction cases")

    if not args.no_parcels:
        wanted = {(parcels.street_of(f.property_address), f.property_zip) for f in filings if f.property_address}
        index = parcels.load_index(wanted, csv_path=args.parcel_csv)
        pstats = parcels.enrich_filings(filings, index)
        print(f"[parcels] enrichment: {pstats}")

    landlords = landlord_leads.aggregate(filings, today=args.today,
                                         include_institutional=args.include_institutional)
    tiers = {t: sum(1 for l in landlords if l.tier == t) for t in ("HOT", "WARM", "COLD")}
    print(f"[leads] {len(landlords):,} landlords with >= {args.min_filings} filings  {tiers}")

    stats = exporters.merge_and_save(landlords, today=args.today)
    print(f"[export] {stats}")
    print(f"[export] {config.RECORDS_JSON}\n[export] {config.GHL_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
