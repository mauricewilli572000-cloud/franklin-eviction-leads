"""Parse the Clerk's monthly F.E.D. CSVs into normalized Filing records.

Header-tolerant: columns are classified by keywords (plaintiff/defendant side + field),
so small renames by the Clerk don't break the run. Use discover_reports.py to see how
a real file's headers were mapped.

Privacy design: tenant (defendant) NAMES are never read into the output. Only the
defendant's address is kept, as the likely rental-property address.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import config

DATE_FORMATS = (
    "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
)
STATE_ZIP_RE = re.compile(r"\b([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?\s*$")


class ParseError(Exception):
    pass


@dataclass
class Filing:
    case_number: str
    file_date: date | None
    disposition: str
    plaintiff_name: str
    plaintiff_street: str
    plaintiff_city: str
    plaintiff_state: str
    plaintiff_zip: str
    property_address: str      # defendant's most recent address (likely the rental unit)
    property_zip: str
    source_file: str = ""
    plaintiff_is_person: bool | None = None   # True/False when the Clerk splits person vs company columns; None = unknown


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def classify_header(header: str):
    """Return (side, field) for a header, or (None, None) if unrecognised."""
    if header in config.HEADER_OVERRIDES:
        return config.HEADER_OVERRIDES[header]
    n = _norm_header(header)
    side = "plaintiff" if "plaintiff" in n else "defendant" if "defendant" in n else None

    if side is None:
        if "dispo" in n:
            return (None, "disposition_date" if "date" in n else "disposition")
        if "case" in n and any(k in n for k in ("number", "num", "no", "id")):
            return (None, "case_number")
        if n in ("case", "casenumber"):
            return (None, "case_number")
        if ("file" in n or "filing" in n) and "date" in n:
            return (None, "file_date")
        if n in ("filed", "datefiled", "filedate"):
            return (None, "file_date")
        return (None, None)

    if "zip" in n or "postal" in n:
        return (side, "zip")
    if "city" in n:
        return (side, "city")
    if "state" in n:
        return (side, "state")
    if any(k in n for k in ("address2", "addr2", "line2", "apt", "unit", "suite")):
        return (side, "street2")
    if any(k in n for k in ("address", "addr", "street")):
        return (side, "street")
    # Split-name layouts, e.g. FIRST_PLAINTIFF_FIRST_NAME / _LAST_NAME / _COMPANY_NAME
    if "sequence" in n:
        return (side, "seq")            # a counter (always 1), NOT a name
    if n.endswith("companyname") or n.endswith("company"):
        return (side, "company")
    if n.endswith("firstname"):
        return (side, "first")
    if n.endswith("middlename") or n.endswith("middleinitial"):
        return (side, "middle")
    if n.endswith("lastname"):
        return (side, "last")
    if n.endswith("suffixname") or n.endswith("suffix"):
        return (side, "suffix")
    return (side, "name")  # "First Plaintiff", "Plaintiff Name", ...


def map_headers(headers: Iterable[str]) -> dict:
    """Return {(side, field): header}. First match wins."""
    mapping: dict = {}
    for h in headers:
        key = classify_header(h)
        if key != (None, None) and key not in mapping:
            mapping[key] = h
    return mapping


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def read_csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def _clean(s) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _split_state_zip(street: str, state: str, zipc: str) -> tuple[str, str, str, str]:
    """If street holds a full 'addr, CITY, OH 43215' string, peel off city/state/zip."""
    city = ""
    if (not state or not zipc) and street:
        m = STATE_ZIP_RE.search(street)
        if m:
            state = state or m.group(1).upper()
            zipc = zipc or m.group(2)
            street = street[: m.start()].rstrip(", ")
            # "123 MAIN ST, COLUMBUS" -> peel the city off after the last comma
            if "," in street:
                street, _, city = street.rpartition(",")
                street, city = street.strip(), city.strip()
    return street, city, state, zipc


def _zip5(z: str) -> str:
    m = re.search(r"\d{5}", z or "")
    return m.group(0) if m else ""


def parse_file(path: Path) -> list[Filing]:
    headers, rows = read_csv_rows(path)
    mapping = map_headers(headers)
    required = [(None, "case_number"), ("defendant", "street")]
    missing = [r for r in required if r not in mapping]
    if not any(k in mapping for k in (("plaintiff", "name"), ("plaintiff", "company"), ("plaintiff", "last"))):
        missing.append(("plaintiff", "name|company|last"))
    if missing:
        raise ParseError(
            f"{Path(path).name}: could not map required columns {missing}. "
            f"Headers seen: {headers}. Add entries to config.HEADER_OVERRIDES."
        )

    def get(row, side, fld) -> str:
        h = mapping.get((side, fld))
        return _clean(row.get(h, "")) if h else ""

    def plaintiff_name(get, row) -> tuple[str, bool | None]:
        """(name, is_person). Company name if present; else 'LAST, FIRST MIDDLE SUFFIX'; else a single name column."""
        company = get(row, "plaintiff", "company")
        if company:
            return company, False
        last = get(row, "plaintiff", "last")
        if last:
            rest = " ".join(x for x in (get(row, "plaintiff", "first"), get(row, "plaintiff", "middle"),
                                        get(row, "plaintiff", "suffix")) if x)
            return (f"{last}, {rest}" if rest else last), True
        return get(row, "plaintiff", "name"), None

    filings: list[Filing] = []
    for row in rows:
        case_no = get(row, None, "case_number")
        pname, is_person = plaintiff_name(get, row)
        if not case_no or not pname:
            continue
        p_street = get(row, "plaintiff", "street")
        p_street2 = get(row, "plaintiff", "street2")
        if p_street2:
            p_street = f"{p_street} {p_street2}".strip()
        p_city = get(row, "plaintiff", "city")
        p_state = get(row, "plaintiff", "state").upper()
        p_zip = get(row, "plaintiff", "zip")
        p_street, _pc, p_state, p_zip = _split_state_zip(p_street, p_state, p_zip)
        p_city = p_city or _pc

        d_street = get(row, "defendant", "street")
        d_street2 = get(row, "defendant", "street2")
        if d_street2:
            d_street = f"{d_street} {d_street2}".strip()
        d_city = get(row, "defendant", "city")
        d_state = get(row, "defendant", "state").upper()
        d_zip = get(row, "defendant", "zip")
        d_street, _dc, d_state, d_zip = _split_state_zip(d_street, d_state, d_zip)
        d_city = d_city or _dc
        prop = ", ".join(x for x in (d_street, d_city, " ".join(x for x in (d_state, d_zip) if x)) if x)

        dispo = " | ".join(x for x in (get(row, None, "disposition"),) if x)
        filings.append(
            Filing(
                case_number=case_no,
                file_date=parse_date(get(row, None, "file_date")),
                disposition=dispo,
                plaintiff_name=pname,
                plaintiff_street=p_street,
                plaintiff_city=p_city,
                plaintiff_state=p_state,
                plaintiff_zip=_zip5(p_zip),
                property_address=prop,
                property_zip=_zip5(d_zip),
                source_file=Path(path).name,
                plaintiff_is_person=is_person,
            )
        )
    return filings


def parse_directory(directory: Path) -> list[Filing]:
    """Parse every CSV in a directory; de-duplicate by case number (newest file wins)."""
    by_case: dict[str, Filing] = {}
    for path in sorted(Path(directory).glob("*.csv")):
        for f in parse_file(path):
            by_case[f.case_number] = f
    return list(by_case.values())
