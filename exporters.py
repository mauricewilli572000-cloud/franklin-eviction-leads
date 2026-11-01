"""Persist leads: accumulate in JSON (merge by lead_id), hold address-less leads as
pending, and export a GoHighLevel-ready CSV. Mirrors the Franklin/Cuyahoga foreclosure
scrapers' decision: only leads with a mailing address reach records/dashboard/CSV.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

import config
from landlord_leads import Landlord

LEAD_TYPE = "Tired Landlord - Eviction Filings"
SOURCE = "FCMC Clerk F.E.D. Case List"
MAX_PROPERTIES_LISTED = 15

GHL_COLUMNS = [
    "Full Name", "First Name", "Last Name", "Business Name",
    "Mailing Address", "City", "State", "Zip",
    "Rental Property Addresses", "Rental Property Count", "Property Profile",
    "Filings 12mo", "Filings 90d", "Last Filing", "First Filing In Window",
    "Score", "Tier", "Owner Type", "Signals", "Case Numbers",
    "Lead Type", "Source", "Date Added", "Tags",
]


def _record(ll: Landlord, today: date) -> dict:
    return {
        "lead_id": ll.lead_id,
        "owner_name": ll.name,
        "owner_type": ll.owner_type,
        "mailing_street": ll.mailing_street,
        "mailing_city": ll.mailing_city,
        "mailing_state": ll.mailing_state,
        "mailing_zip": ll.mailing_zip,
        "property_addresses": ll.properties[:MAX_PROPERTIES_LISTED],
        "property_count": ll.n_properties,
        "property_kind": ll.property_kind,
        "filings_window": ll.filings_window,
        "filings_12m": ll.filings_12m,
        "filings_90d": ll.filings_90d,
        "first_filing": ll.first_filing.isoformat() if ll.first_filing else "",
        "last_filing": ll.last_filing.isoformat() if ll.last_filing else "",
        "dismissed": ll.dismissed,
        "judgments": ll.judgments,
        "writs": ll.writs,
        "score": ll.score,
        "tier": ll.tier,
        "signals": ll.signals,
        "case_numbers": sorted({f.case_number for f in ll.filings}),
        "lead_type": LEAD_TYPE,
        "source": SOURCE,
        "date_added": today.isoformat(),
        "last_updated": today.isoformat(),
    }


def _load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {r["lead_id"]: r for r in data.get("records", [])}


def _save(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records.values(), key=lambda r: (r["score"], r["filings_12m"]), reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(ordered),
        "records": ordered,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def merge_and_save(landlords: list[Landlord], today: date | None = None) -> dict:
    """Merge today's leads into records.json / pending_records.json. Returns run stats."""
    today = today or date.today()
    records = _load(config.RECORDS_JSON)
    pending = _load(config.PENDING_JSON)
    stats = {"new": 0, "updated": 0, "pending": 0, "promoted": 0}

    for ll in landlords:
        rec = _record(ll, today)
        has_address = bool(ll.mailing_street)
        prior = records.get(rec["lead_id"]) or pending.get(rec["lead_id"])
        if prior:
            rec["date_added"] = prior.get("date_added", rec["date_added"])
            rec["case_numbers"] = sorted(set(prior.get("case_numbers", [])) | set(rec["case_numbers"]))
            # keep rental addresses seen in earlier runs (CSV window rolls off old months)
            merged = list(rec["property_addresses"])
            for p in prior.get("property_addresses", []):
                if p not in merged:
                    merged.append(p)
            rec["property_addresses"] = merged[:MAX_PROPERTIES_LISTED]
            rec["property_count"] = max(rec["property_count"], prior.get("property_count", 0))

        if has_address:
            if rec["lead_id"] in pending:
                pending.pop(rec["lead_id"]); stats["promoted"] += 1
            elif rec["lead_id"] in records:
                stats["updated"] += 1
            else:
                stats["new"] += 1
            records[rec["lead_id"]] = rec
        else:
            if rec["lead_id"] not in pending:
                stats["pending"] += 1
            pending[rec["lead_id"]] = rec

    _save(config.RECORDS_JSON, records)
    _save(config.PENDING_JSON, pending)
    export_ghl_csv(list(records.values()))
    export_dashboard_json(list(records.values()), stats["new"])
    stats["total_records"] = len(records)
    stats["total_pending"] = len(pending)
    return stats


def export_dashboard_json(records: list[dict], new_this_run: int) -> None:
    """dashboard/evictions.json - consumed by the Evictions tab (and anyone else who wants the data)."""
    config.DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda r: (r["score"], r["filings_12m"]), reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_leads": len(records),
        "leads_with_mailing_address": sum(1 for r in records if r["mailing_street"]),
        "new_this_run": new_this_run,
        "records": records,
    }
    config.DASHBOARD_JSON.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _split_name(rec: dict) -> tuple[str, str, str]:
    """(first, last, business). Individuals are 'LAST, FIRST' or 'FIRST LAST'; entities -> business."""
    name = rec["owner_name"].strip()
    if rec["owner_type"] != "individual":
        return "", "", name
    if "," in name:
        last, _, first = name.partition(",")
        return first.strip().title(), last.strip().title(), ""
    parts = name.split()
    if len(parts) >= 2:
        return parts[0].title(), parts[-1].title(), ""
    return "", name.title(), ""


def export_ghl_csv(records: list[dict]) -> None:
    config.GHL_CSV.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda r: (r["score"], r["filings_12m"]), reverse=True)
    with config.GHL_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=GHL_COLUMNS)
        w.writeheader()
        for r in records:
            first, last, biz = _split_name(r)
            tags = ["eviction-landlord", f"tier-{r['tier'].lower()}", f"owner-{r['owner_type']}"]
            w.writerow({
                "Full Name": r["owner_name"], "First Name": first, "Last Name": last, "Business Name": biz,
                "Mailing Address": r["mailing_street"], "City": r["mailing_city"],
                "State": r["mailing_state"], "Zip": r["mailing_zip"],
                "Rental Property Addresses": "; ".join(r["property_addresses"]),
                "Rental Property Count": r["property_count"],
                "Property Profile": r.get("property_kind", ""),
                "Filings 12mo": r["filings_12m"], "Filings 90d": r["filings_90d"],
                "Last Filing": r["last_filing"], "First Filing In Window": r["first_filing"],
                "Score": r["score"], "Tier": r["tier"], "Owner Type": r["owner_type"],
                "Signals": "; ".join(r["signals"]), "Case Numbers": "; ".join(r["case_numbers"]),
                "Lead Type": r["lead_type"], "Source": r["source"], "Date Added": r["date_added"],
                "Tags": ", ".join(tags),
            })
