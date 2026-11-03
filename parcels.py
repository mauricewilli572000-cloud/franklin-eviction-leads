"""Franklin County Auditor parcel enrichment: who OWNS the rental the eviction was filed on?

The Clerk's eviction report gives the *plaintiff* (the party that filed) and the *defendant's
address* (the rental). The plaintiff can be a property manager, an attorney, or the owner. The
Auditor's monthly Parcel.csv (https://apps.franklincountyauditor.com/Parcel_CSV/) lists every
parcel with its owner of record and the owner's tax-mailing address, so we:

  1. match each rental address to a parcel,
  2. compare the owner of record with the plaintiff  -> confirmed / mismatch / unmatched,
  3. count how many parcels that owner holds countywide (real portfolio size),
  4. for filings made by a property MANAGER, re-attribute the filing to the owner of record
     (with the owner's mailing address) - the owner is who a wholesaler actually wants to call.

Column names are the ones confirmed in the foreclosure scraper (NAME1, STADDR, USPS_CITY, ZIPCODE,
MAILAD3, MAILAD4); the generic fallbacks guard against a header rename.
"""
from __future__ import annotations

import csv
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import config

OWNER_COLUMNS = ("NAME1", "OWNER", "OWN1", "OWNERNAME", "OWNER_NAME", "OWNER1")
SITE_ADDR_COLUMNS = ("STADDR", "SITE_ADDR", "SITEADDR", "SITE_ADDRE", "PROPADDR", "SITEADDRESS")
SITE_ZIP_COLUMNS = ("ZIPCODE", "SITE_ZIP", "SITEZIP", "PROPZIP", "SITEZIPCODE")
MAIL_STREET_COLUMNS = ("MAILAD3", "OWNER_ADD1", "ADDR_1", "MAILADR1", "MAIL_ADDR1", "MAILADDR")
MAIL_CSZ_COLUMNS = ("MAILAD4", "OWNER_ADD2")
PARCEL_ID_COLUMNS = ("PARCEL ID", "PARCELID", "PARCEL_ID", "PARCELNO", "PARCEL NO")

_PUNCT = re.compile(r"[^A-Z0-9 ]")
_SPACE = re.compile(r"\s+")
_UNIT = re.compile(r"\s+(?:APT|APARTMENT|UNIT|STE|SUITE|BLDG|BUILDING|LOT|RM|ROOM|FL|FLOOR)\b.*$|\s+#.*$")
_DIRECTION = re.compile(r"\b(N|S|E|W|NE|NW|SE|SW|NORTH|SOUTH|EAST|WEST)\b")

_SUFFIXES = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "BOULEVARD": "BLVD", "PLACE": "PL", "CIRCLE": "CIR", "PARKWAY": "PKWY", "TERRACE": "TER",
    "HIGHWAY": "HWY", "TRAIL": "TRL", "WAY": "WAY", "SQUARE": "SQ", "ALLEY": "ALY",
}
_DIR_WORDS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
              "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}


# --------------------------------------------------------------------------- address keys
def zip5(value: str) -> str:
    m = re.search(r"\d{5}", value or "")
    return m.group(0) if m else ""


def _street_tokens(street: str) -> str:
    s = _PUNCT.sub(" ", (street or "").upper())
    s = _SPACE.sub(" ", s).strip()
    s = _UNIT.sub("", s)
    toks = []
    for t in s.split():
        t = _DIR_WORDS.get(t, t)
        t = _SUFFIXES.get(t, t)
        toks.append(t)
    return " ".join(toks)


def address_keys(street: str, zipcode: str) -> tuple[str, str]:
    """(exact_key, no_direction_key) for matching 'street + ZIP5'. Unit numbers are ignored."""
    z = zip5(zipcode)
    base = _street_tokens(street)
    if not base or not z:
        return "", ""
    nodir = _SPACE.sub(" ", _DIRECTION.sub("", base)).strip()
    return f"{base}|{z}", f"{nodir}|{z}"


def street_of(full_address: str) -> str:
    """'9 OAK ST APT 2, COLUMBUS, OH 43201' -> '9 OAK ST APT 2' (everything before the first comma)."""
    return (full_address or "").split(",")[0].strip()


# --------------------------------------------------------------------------- name matching
_ENTITY_STOP = {
    "LLC", "INC", "CORP", "CO", "LTD", "LP", "LLP", "LLLP", "PLLC", "THE", "OF", "AND", "TRUST", "TRUSTEE",
    "TRUSTEES", "ET", "AL", "ETAL", "UX", "ESTATE", "JR", "SR", "II", "III", "IV", "COMPANY", "CORPORATION",
    "FBO", "TR", "LIFE", "EST", "REVOCABLE", "LIVING", "FAMILY", "TIC",
}


def normalize_owner(name: str) -> str:
    n = _PUNCT.sub(" ", (name or "").upper().replace("&", " AND "))
    return _SPACE.sub(" ", n).strip()


def _tokens(name: str) -> set[str]:
    return {t for t in normalize_owner(name).split() if len(t) > 1 and t not in _ENTITY_STOP}


def names_match(a: str, b: str) -> bool:
    """Loose same-owner test. 'DOE, JANE Q' ~ 'DOE JANE Q & JOHN'; 'ACME HOLDINGS LLC' ~ 'ACME HOLDINGS'."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    small = min(len(ta), len(tb))
    return inter / small >= 0.75 and (inter >= 2 or small == 1)


def split_city_state_zip(text: str) -> tuple[str, str, str]:
    text = (text or "").strip()
    m = re.match(r"^(.*?)\s+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$", text)
    if m:
        return m.group(1).strip(), m.group(2).upper(), m.group(3)
    return text, "", ""


# --------------------------------------------------------------------------- download
def _get_field(row: dict, candidates: tuple) -> str:
    for key in candidates:
        for actual in row:
            if (actual or "").strip().upper() == key:
                v = row.get(actual)
                if v not in (None, ""):
                    return str(v).strip()
    return ""


def _list_entries(session: requests.Session, url: str) -> list[str]:
    resp = session.get(url, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        name = a["href"].strip("/").split("/")[-1]
        if name and name not in ("..", "."):
            out.append(name)
    return out


def find_parcel_csv_url(session: requests.Session) -> str:
    """Newest Parcel_CSV/{year}/{month}/Parcel.csv. PARCEL_CSV_URL env var overrides discovery."""
    override = os.environ.get("PARCEL_CSV_URL") or config.PARCEL_CSV_URL
    if override:
        return override
    base = config.PARCEL_CSV_INDEX_URL
    root = _list_entries(session, base)
    print(f"[parcels] index entries: {root[:40]}")
    years = sorted((e for e in root if re.fullmatch(r"\d{4}", e)), reverse=True)
    checked = 0
    for year in years[:3]:
        entries = _list_entries(session, f"{base}{year}/")
        months = [e for e in entries if re.fullmatch(r"\d{1,2}", e)]
        print(f"[parcels] {year}: entries={entries[:40]}")
        for month in sorted(months, key=int, reverse=True):
            checked += 1
            files = _list_entries(session, f"{base}{year}/{month}/")
            print(f"[parcels] {year}/{month}: files={files[:20]}")
            for f in files:
                if f.lower() == "parcel.csv":
                    return f"{base}{year}/{month}/{f}"
    raise RuntimeError(f"No Parcel.csv found under {base} ({checked} folders checked)")


def download_parcel_csv(session: requests.Session, url: str) -> Path:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.CACHE_DIR / "Parcel.csv"
    print(f"[parcels] downloading {url}")
    t0 = time.time()
    with session.get(url, stream=True, timeout=(config.REQUEST_TIMEOUT, 300)) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    print(f"[parcels] saved {dest.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f}s")
    return dest


GIS_FIELDS = ("OBJECTID", "PARCELID", "SITEADDRESS", "ZIPCD", "OWNERNME1", "PSTLADDRES", "PSTLCITYSTZIP", "SALEDATE")
GIS_CSV_HEADER = ["PARCEL ID", "NAME1", "STADDR", "ZIPCODE", "MAILAD3", "MAILAD4"]   # same names as the old Parcel.csv


def _gis_page(session: requests.Session, url: str, last_oid: int) -> dict:
    params = {
        "where": f"OBJECTID>{last_oid}", "outFields": ",".join(GIS_FIELDS), "returnGeometry": "false",
        "orderByFields": "OBJECTID", "resultRecordCount": config.PARCEL_GIS_PAGE, "f": "json",
    }
    last_exc: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = session.get(f"{url}/query", params=params, timeout=(config.REQUEST_TIMEOUT, 180))
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            return data
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last_exc = exc
            time.sleep(2 * attempt)
    raise RuntimeError(f"parcel service page after OBJECTID {last_oid} failed: {last_exc}")


def download_gis_parcels(session: requests.Session, url: str | None = None) -> tuple[Path, str]:
    """Page through the county's LIVE tax-parcel layer and write a CSV that uses the old Parcel.csv
    column names, so build_index() works unchanged. Returns (csv_path, newest_sale_date_iso)."""
    from datetime import datetime, timezone

    url = url or config.PARCEL_GIS_URL
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.CACHE_DIR / "Parcel_gis.csv"
    t0, last_oid, rows, newest_sale = time.time(), 0, 0, 0
    with dest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(GIS_CSV_HEADER)
        for page in range(config.PARCEL_GIS_MAX_PAGES):
            data = _gis_page(session, url, last_oid)
            feats = data.get("features") or []
            if not feats:
                break
            for f in feats:
                a = f.get("attributes") or {}
                last_oid = max(last_oid, int(a.get("OBJECTID") or 0))
                newest_sale = max(newest_sale, int(a.get("SALEDATE") or 0))
                w.writerow([a.get("PARCELID") or "", a.get("OWNERNME1") or "", a.get("SITEADDRESS") or "",
                            a.get("ZIPCD") or "", a.get("PSTLADDRES") or "", a.get("PSTLCITYSTZIP") or ""])
                rows += 1
            time.sleep(0.1)
        else:
            raise RuntimeError(f"parcel service paging did not finish within {config.PARCEL_GIS_MAX_PAGES} pages")
    if rows < config.PARCEL_GIS_MIN_ROWS:   # county has ~495k parcels
        raise RuntimeError(f"parcel service returned only {rows:,} rows - refusing to use a partial download")
    asof = (datetime.fromtimestamp(newest_sale / 1000, timezone.utc).date().isoformat() if newest_sale else "?")
    print(f"[parcels] downloaded {rows:,} parcels from county GIS in {time.time() - t0:.0f}s "
          f"(newest sale on file: {asof})")
    return dest, asof


# --------------------------------------------------------------------------- index
@dataclass
class Parcel:
    parcel_id: str
    owner: str
    site_street: str
    mail_street: str
    mail_city: str
    mail_state: str
    mail_zip: str


@dataclass
class ParcelIndex:
    by_exact: dict[str, Parcel] = field(default_factory=dict)
    by_nodir: dict[str, Parcel] = field(default_factory=dict)
    owner_counts: Counter = field(default_factory=Counter)   # normalized owner -> parcels countywide
    rows_read: int = 0
    source: str = ""
    samples: list[str] = field(default_factory=list)          # first few site addresses, for the run log

    def __bool__(self) -> bool:
        return bool(self.by_exact or self.by_nodir)

    def lookup(self, street: str, zipcode: str) -> Parcel | None:
        k1, k2 = address_keys(street, zipcode)
        if not k1:
            return None
        return self.by_exact.get(k1) or self.by_nodir.get(k2)

    def portfolio(self, owner: str) -> int:
        return self.owner_counts.get(normalize_owner(owner), 0)


def build_index(csv_path: Path, wanted: set[tuple[str, str]]) -> ParcelIndex:
    """One streaming pass over Parcel.csv. Only parcels at `wanted` (street, zip) addresses are kept
    in memory; owner portfolio counts are tallied for every parcel in the county."""
    wanted_exact: set[str] = set()
    wanted_nodir: set[str] = set()
    for street, z in wanted:
        k1, k2 = address_keys(street, z)
        if k1:
            wanted_exact.add(k1)
            wanted_nodir.add(k2)

    idx = ParcelIndex()
    with Path(csv_path).open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = {(h or "").strip().upper() for h in (reader.fieldnames or [])}
        if not any(c in headers for c in OWNER_COLUMNS) or not any(c in headers for c in SITE_ADDR_COLUMNS):
            raise RuntimeError(
                f"Parcel.csv headers not recognised (need an owner and a site-address column). "
                f"Saw: {reader.fieldnames}"
            )
        for row in reader:
            idx.rows_read += 1
            owner = _get_field(row, OWNER_COLUMNS)
            if not owner:
                continue
            idx.owner_counts[normalize_owner(owner)] += 1
            site = _get_field(row, SITE_ADDR_COLUMNS)
            if not site:
                continue
            if len(idx.samples) < 5:
                idx.samples.append(f"{site} | {_get_field(row, SITE_ZIP_COLUMNS)}")
            k1, k2 = address_keys(site, _get_field(row, SITE_ZIP_COLUMNS))
            if not k1:
                continue
            hit_exact, hit_nodir = k1 in wanted_exact, k2 in wanted_nodir
            if not (hit_exact or hit_nodir):
                continue
            city, state, z = split_city_state_zip(_get_field(row, MAIL_CSZ_COLUMNS))
            parcel = Parcel(
                parcel_id=_get_field(row, PARCEL_ID_COLUMNS), owner=owner, site_street=site,
                mail_street=_get_field(row, MAIL_STREET_COLUMNS), mail_city=city,
                mail_state=state or "OH", mail_zip=z,
            )
            if hit_exact:
                idx.by_exact.setdefault(k1, parcel)
            if hit_nodir:
                idx.by_nodir.setdefault(k2, parcel)
    return idx


def load_index(wanted: set[tuple[str, str]], csv_path: Path | None = None) -> ParcelIndex:
    """Index county parcel data. Order: a local file if given; else the LIVE county GIS service; else the
    Auditor's (older) Parcel.csv. Never raises: with no parcel source the pipeline runs unenriched."""
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    source = "local file"
    try:
        if csv_path is None:
            try:
                csv_path, asof = download_gis_parcels(session)
                source = f"county GIS (live, newest sale {asof})"
            except Exception as exc:  # noqa: BLE001
                print(f"[parcels] WARN live county GIS unavailable ({exc}); falling back to Auditor Parcel.csv")
                csv_path = download_parcel_csv(session, find_parcel_csv_url(session))
                source = "Auditor Parcel.csv (may be months old)"
        idx = build_index(csv_path, wanted)
        idx.source = source
        print(f"[parcels] source: {source}")
        print(f"[parcels] site-address samples (street | zip): {idx.samples}")
        print(f"[parcels] {idx.rows_read:,} parcels read; {len(idx.owner_counts):,} distinct owners; "
              f"{len(idx.by_exact):,} rental addresses matched exactly (+{len(idx.by_nodir):,} loose)")
        return idx
    except Exception as exc:  # noqa: BLE001 - enrichment is optional
        print(f"[parcels] WARN owner enrichment unavailable: {exc}")
        return ParcelIndex()


# --------------------------------------------------------------------------- enrichment
def enrich_filings(filings: list, index: ParcelIndex) -> dict:
    """Annotate each Filing with parcel status / owner of record / portfolio; re-attribute manager
    filings to the owner of record. Returns stats. Filings are replaced in place (list mutated)."""
    from landlord_leads import classify_owner, normalize_name   # local import: avoids a cycle

    stats = Counter()
    unmatched_samples: list[str] = []
    if not index:
        return {"enabled": False}
    for i, f in enumerate(filings):
        parcel = index.lookup(street_of(f.property_address), f.property_zip)
        stats["filings"] += 1
        if parcel is None:
            f.parcel_status = "unmatched"
            stats["unmatched"] += 1
            if len(unmatched_samples) < 8:
                unmatched_samples.append(f"{street_of(f.property_address)} | {f.property_zip}")
            continue
        stats["matched"] += 1
        f.parcel_owner = parcel.owner
        f.owner_portfolio = index.portfolio(parcel.owner)
        f.parcel_id = parcel.parcel_id
        auditor_mail = (parcel.mail_street, parcel.mail_city, parcel.mail_state, parcel.mail_zip)

        if names_match(f.plaintiff_name, parcel.owner):
            f.parcel_status = "confirmed"
            stats["confirmed"] += 1
            if parcel.mail_street:      # tax mailing address beats the court's address
                f.court_mailing = ", ".join(x for x in (f.plaintiff_street, f.plaintiff_city,
                                                        f"{f.plaintiff_state} {f.plaintiff_zip}".strip()) if x)
                f.plaintiff_street, f.plaintiff_city, f.plaintiff_state, f.plaintiff_zip = (
                    parcel.mail_street, parcel.mail_city, parcel.mail_state, zip5(parcel.mail_zip))
            continue

        otype = classify_owner(normalize_name(f.plaintiff_name), f.plaintiff_is_person)
        if otype == "manager" and parcel.mail_street:
            stats["via_manager"] += 1
            filings[i] = replace(
                f, via_manager=f.plaintiff_name, parcel_status="via_manager",
                plaintiff_name=parcel.owner, plaintiff_is_person=None,
                court_mailing=", ".join(x for x in (f.plaintiff_street, f.plaintiff_city,
                                                    f"{f.plaintiff_state} {f.plaintiff_zip}".strip()) if x),
                plaintiff_street=parcel.mail_street, plaintiff_city=parcel.mail_city,
                plaintiff_state=parcel.mail_state, plaintiff_zip=zip5(parcel.mail_zip),
            )
        else:
            f.parcel_status = "mismatch"
            stats["mismatch"] += 1
    out = dict(stats)
    out["enabled"] = True
    if unmatched_samples:
        print(f"[parcels] unmatched rental samples (street | zip): {unmatched_samples}")
    out["match_rate"] = round(stats["matched"] / stats["filings"], 3) if stats["filings"] else 0.0
    return out
