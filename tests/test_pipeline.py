"""Offline tests using synthetic data (no real people)."""
import csv
import json
from datetime import date

import pytest

import config
import exporters
import fetch_reports
import landlord_leads
import parse_reports
from run_pipeline import main

TODAY = date(2026, 10, 31)

HEADER = [
    "Case Number", "File Date", "Last Disposition Date", "Last Disposition",
    "First Plaintiff", "Plaintiff Address", "Plaintiff City", "Plaintiff State", "Plaintiff Zip",
    "First Defendant", "Defendant Address", "Defendant City", "Defendant State", "Defendant Zip",
]


def row(case, fdate, dispo, pname, paddr, pcity, pstate, pzip, daddr, dzip="43201"):
    return [case, fdate, "", dispo, pname, paddr, pcity, pstate, pzip,
            "TENANT PERSON", daddr, "COLUMBUS", "OH", dzip]


def write_csv(path, rows, header=HEADER, encoding="utf-8-sig"):
    with open(path, "w", newline="", encoding=encoding) as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture
def csv_dir(tmp_path):
    d = tmp_path / "csvs"
    d.mkdir()
    rows = [
        # Individual landlord, 4 filings, 3 distinct units, out-of-state mailing
        row("2026 CVG 1", "10/20/2026", "Dismissed", "SMITH, JOHN", "12 Elm Street", "Tampa", "FL", "33601", "100 Oak Ave"),
        row("2026 CVG 2", "10/05/2026", "Dismissed", "SMITH, JOHN", "12 Elm St.", "Tampa", "FL", "33601", "102 Oak Ave"),
        row("2026 CVG 3", "08/11/2026", "Judgment for Plaintiff - Writ", "SMITH, JOHN", "12 Elm St", "Tampa", "FL", "33601", "104 Oak Ave"),
        row("2026 CVG 4", "03/02/2026", "", "SMITH, JOHN", "12 Elm St", "Tampa", "FL", "33601", "100 Oak Ave"),
        # LLC, 2 filings
        row("2026 CVG 5", "09/15/2026", "", "Buckeye Rentals LLC", "PO Box 9", "Columbus", "OH", "43215", "9 Pine Rd"),
        row("2026 CVG 6", "07/15/2026", "", "BUCKEYE RENTALS, L.L.C.", "PO Box 9", "Columbus", "OH", "43215", "11 Pine Rd"),
        # One-off filer -> excluded (below min filings)
        row("2026 CVG 7", "09/01/2026", "", "Lone Filer", "1 Main St", "Columbus", "OH", "43215", "5 Main St"),
        # Housing authority -> excluded as institutional
        row("2026 CVG 8", "09/01/2026", "", "Columbus Metropolitan Housing Authority", "880 E 11th", "Columbus", "OH", "43211", "1 A St"),
        row("2026 CVG 9", "09/02/2026", "", "Columbus Metropolitan Housing Authority", "880 E 11th", "Columbus", "OH", "43211", "2 A St"),
        # Filer with no mailing address -> pending
        row("2026 CVG 10", "10/01/2026", "", "NO ADDRESS PERSON", "", "", "", "", "7 B St"),
        row("2026 CVG 11", "10/02/2026", "", "NO ADDRESS PERSON", "", "", "", "", "8 B St"),
        # Duplicate case across files should collapse
    ]
    write_csv(d / "2026-10-01_2026-10-31.csv", rows)
    write_csv(d / "2026-09-01_2026-09-30.csv", [rows[0]])
    return d


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "RAW_DIR", data / "raw")
    monkeypatch.setattr(config, "RECORDS_JSON", data / "records.json")
    monkeypatch.setattr(config, "PENDING_JSON", data / "pending.json")
    monkeypatch.setattr(config, "DASHBOARD_DIR", data / "dash")
    monkeypatch.setattr(config, "DASHBOARD_JSON", data / "dash" / "evictions.json")
    monkeypatch.setattr(config, "GHL_CSV", data / "dash" / "ghl.csv")
    monkeypatch.setattr(config, "MANUAL_DIR", data / "manual")
    import parcels
    _real_load = parcels.load_index

    def _offline_load(wanted, csv_path=None):          # never touch the network in tests
        return _real_load(wanted, csv_path) if csv_path else parcels.ParcelIndex()

    monkeypatch.setattr(parcels, "_real_load_index", _real_load, raising=False)   # for tests that exercise it
    monkeypatch.setattr(parcels, "load_index", _offline_load)
    monkeypatch.setattr(config, "MIN_FILINGS_FOR_LEAD", 2)


def test_header_mapping():
    m = parse_reports.map_headers(HEADER)
    assert m[("plaintiff", "name")] == "First Plaintiff"
    assert m[("plaintiff", "street")] == "Plaintiff Address"
    assert m[("defendant", "street")] == "Defendant Address"
    assert m[(None, "case_number")] == "Case Number"
    assert m[(None, "file_date")] == "File Date"
    assert m[(None, "disposition")] == "Last Disposition"
    assert m[(None, "disposition_date")] == "Last Disposition Date"


def test_missing_required_columns_raises(tmp_path):
    p = tmp_path / "bad.csv"
    write_csv(p, [["x", "y"]], header=["foo", "bar"])
    with pytest.raises(parse_reports.ParseError):
        parse_reports.parse_file(p)


def test_dedupe_and_no_tenant_names(csv_dir):
    filings = parse_reports.parse_directory(csv_dir)
    assert len(filings) == 11  # CVG 1 appears in two files
    assert not any(hasattr(f, "defendant_name") for f in filings)
    assert "TENANT" not in repr(filings)


def test_owner_classification():
    c = landlord_leads.classify_owner
    n = landlord_leads.normalize_name
    assert c(n("Columbus Metropolitan Housing Authority")) == "institutional"
    assert c(n("ABC Property Management Inc")) == "manager"
    assert c(n("BUCKEYE RENTALS, L.L.C.")) == "llc" or c(n("Buckeye Rentals LLC")) == "llc"
    assert c(n("Jane Q Public")) == "individual"
    assert c(n("Doe Family Trust")) == "trust"


def test_aggregate_and_score(csv_dir):
    filings = parse_reports.parse_directory(csv_dir)
    lls = {l.name: l for l in landlord_leads.aggregate(filings, today=TODAY)}
    assert "Lone Filer" not in lls
    assert not any("HOUSING" in n.upper() for n in lls)
    smith = lls["SMITH, JOHN"]
    assert smith.filings_window == 4
    assert smith.filings_90d == 3
    assert smith.n_properties == 3            # 100 Oak deduped
    assert smith.owner_type == "individual"
    assert smith.dismissed == 2 and smith.writs == 1
    assert smith.tier in ("HOT", "WARM")
    assert any("out-of-state" in s for s in smith.signals)
    # sorted best-first
    ordered = landlord_leads.aggregate(filings, today=TODAY)
    assert ordered[0].name == "SMITH, JOHN"


def test_institutional_included_on_request(csv_dir):
    filings = parse_reports.parse_directory(csv_dir)
    lls = landlord_leads.aggregate(filings, today=TODAY, include_institutional=True)
    assert any(l.owner_type == "institutional" for l in lls)


def test_end_to_end_and_accumulation(csv_dir, capsys):
    assert main(["--from-dir", str(csv_dir), "--today", TODAY.isoformat()]) == 0
    records = json.loads(config.RECORDS_JSON.read_text())["records"]
    pending = json.loads(config.PENDING_JSON.read_text())["records"]
    names = {r["owner_name"] for r in records}
    assert "SMITH, JOHN" in names and "NO ADDRESS PERSON" not in names
    assert [r["owner_name"] for r in pending] == ["NO ADDRESS PERSON"]

    with config.GHL_CSV.open(encoding="utf-8-sig") as fh:
        out = list(csv.DictReader(fh))
    assert out[0]["Full Name"] == "SMITH, JOHN"
    assert out[0]["First Name"] == "John" and out[0]["Last Name"] == "Smith"
    assert "eviction-landlord" in out[0]["Tags"]
    assert all(r["Mailing Address"] for r in out)

    # second run later: date_added is preserved, no duplicates
    main(["--from-dir", str(csv_dir), "--today", "2026-11-02"])
    records2 = json.loads(config.RECORDS_JSON.read_text())["records"]
    assert len(records2) == len(records)
    smith = next(r for r in records2 if r["owner_name"] == "SMITH, JOHN")
    assert smith["date_added"] == "2026-10-31"
    assert smith["last_updated"] == "2026-11-02"


def test_latin1_encoding(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    write_csv(d / "x.csv", [row("2026 CVG 1", "10/01/2026", "", "MUÑOZ, ANA", "1 A St", "Columbus", "OH", "43215", "2 B St")],
              encoding="cp1252")
    f = parse_reports.parse_directory(d)
    assert f[0].plaintiff_name == "MUÑOZ, ANA"


def test_single_column_full_address_split(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    hdr = ["Case Number", "File Date", "First Plaintiff", "Plaintiff Address", "First Defendant", "Defendant Address"]
    write_csv(d / "x.csv", [["1", "2026-10-01", "A B", "5 Elm St, Dayton, OH 45402", "T", "9 Oak St, Columbus, OH 43201-1234"]], header=hdr)
    f = parse_reports.parse_directory(d)[0]
    assert f.plaintiff_state == "OH" and f.plaintiff_zip == "45402"
    assert f.property_zip == "43201"
    assert f.plaintiff_street == "5 Elm St" and f.plaintiff_city == "Dayton"
    assert f.property_address == "9 Oak St, Columbus, OH 43201"


def test_fallback_links_cover_13_months():
    links = fetch_reports.fallback_links(date(2026, 10, 31))
    assert len(links) == 13
    assert links[0]["start"] == "2026-10-01" and links[0]["end"] == "2026-10-31"
    assert links[-1]["start"] == "2025-10-01"
    assert "2026-02-28" in {l["end"] for l in links}
    assert "Case%20List%202026-10-01%20to%202026-10-31.csv" in links[0]["url"]


def test_discover_links_from_html(monkeypatch):
    html = ('<a href="https://www.fcmcclerk.com/storage/shared/civil-fed/FCMC%20Civil%20F.E.D.%20%28Eviction%29%20'
            'Case%20List%202026-10-01%20to%202026-10-31.csv?885869">x</a><a href="/other.pdf">y</a>')

    class R:
        status_code = 200
        text = html

    monkeypatch.setattr(fetch_reports, "_get", lambda s, u: R())
    links = fetch_reports.discover_report_links()
    assert links == [{"url": links[0]["url"], "start": "2026-10-01", "end": "2026-10-31"}]


REAL_HDR = ["CASE_NUMBER", "CASE_FILE_DATE", "LAST_DISPOSITION_DATE", "LAST_DISPOSITION_DESCRIPTION",
            "FIRST_PLAINTIFF_PARTY_SEQUENCE", "FIRST_PLAINTIFF_FIRST_NAME", "FIRST_PLAINTIFF_MIDDLE_NAME",
            "FIRST_PLAINTIFF_LAST_NAME", "FIRST_PLAINTIFF_SUFFIX_NAME", "FIRST_PLAINTIFF_COMPANY_NAME",
            "FIRST_PLAINTIFF_ADDRESS_LINE_1", "FIRST_PLAINTIFF_ADDRESS_LINE_2", "FIRST_PLAINTIFF_CITY",
            "FIRST_PLAINTIFF_STATE", "FIRST_PLAINTIFF_ZIP",
            "FIRST_DEFENDANT_PARTY_SEQUENCE", "FIRST_DEFENDANT_FIRST_NAME", "FIRST_DEFENDANT_MIDDLE_NAME",
            "FIRST_DEFENDANT_LAST_NAME", "FIRST_DEFENDANT_SUFFIX_NAME", "FIRST_DEFENDANT_COMPANY_NAME",
            "FIRST_DEFENDANT_ADDRESS_LINE_1", "FIRST_DEFENDANT_ADDRESS_LINE_2", "FIRST_DEFENDANT_CITY",
            "FIRST_DEFENDANT_STATE", "FIRST_DEFENDANT_ZIP"]


def test_real_clerk_layout_split_names(tmp_path):
    """Clerk's real layout: sequence col is always '1' (not a name); names split across columns."""
    d = tmp_path / "r"
    d.mkdir()
    def row(case, first, mid, last, suf, co):
        return [case, "10/05/2026", "", "UNDISPOSED", "1", first, mid, last, suf, co,
                "5 ELM ST", "", "DAYTON", "OH", "45402", "1", "SECRET", "", "TENANT", "", "",
                "9 OAK ST", "APT 2", "COLUMBUS", "OH", "43201"]
    write_csv(d / "x.csv", [row("1", "", "", "", "", "ACME HOLDINGS LLC"),
                            row("2", "JANE", "Q", "DOE", "JR", ""),
                            row("3", "", "", "", "", "ACME HOLDINGS LLC")], header=REAL_HDR)
    fs = {f.case_number: f for f in parse_reports.parse_directory(d)}
    assert fs["1"].plaintiff_name == "ACME HOLDINGS LLC"
    assert fs["2"].plaintiff_name == "DOE, JANE Q JR"
    assert fs["1"].property_address == "9 OAK ST APT 2, COLUMBUS, OH 43201"
    assert "SECRET" not in repr(fs) and "TENANT" not in repr(fs)


def _filings(name, addrs, is_person=False, zip5="43232", pzip="45402", pstate="OH"):
    from datetime import date
    from parse_reports import Filing
    return [Filing(case_number=f"{name}-{i}", file_date=date(2026, 10, 1 + i % 20), disposition="UNDISPOSED",
                   plaintiff_name=name, plaintiff_street="1 X ST", plaintiff_city="Y", plaintiff_state=pstate,
                   plaintiff_zip=pzip, property_address=a, property_zip=zip5, plaintiff_is_person=is_person)
            for i, a in enumerate(addrs)]


def test_company_column_name_is_not_individual():
    assert landlord_leads.classify_owner("LIFE AT GREEN ARBOR", is_person=False) == "company"
    assert landlord_leads.classify_owner("DOE JANE", is_person=True) == "individual"


def test_corporate_sfr_investors_are_institutional():
    for n in ("HOME SFR BORROWER IV LLC", "FIRSTKEY HOMES OF OHIO LLC", "AH4R MANAGEMENT OH LLC"):
        assert landlord_leads.classify_owner(n) == "institutional", n


def test_apartment_complex_detected_and_never_hot():
    from datetime import date
    cx = _filings("GREEN ARBOR HOLDINGS LLC", [f"{4000 + i} GREEN ARBOR LN, COLUMBUS, OH 43232" for i in range(8)])
    out = landlord_leads.aggregate(cx, today=date(2026, 10, 31))[0]
    assert out.property_kind == "apartment complex" and out.tier != "HOT"


def test_scattered_small_landlord_can_be_hot():
    from datetime import date
    streets = ["OAK ST", "ELM AVE", "PINE RD", "MAPLE DR", "CEDAR LN", "BIRCH CT"]
    ff = _filings("DOE, JANE", [f"{10 + i} {st}, COLUMBUS, OH 43232" for i, st in enumerate(streets)],
                  is_person=True, pstate="AZ")
    out = landlord_leads.aggregate(ff, today=date(2026, 10, 31))[0]
    assert out.property_kind == "scattered" and out.owner_type == "individual" and out.tier == "HOT"


def test_dashboard_json_and_manual_fallback(csv_dir, capsys):
    import run_pipeline
    # no download, empty raw dir -> falls back to manual_csv
    config.MANUAL_DIR.mkdir(parents=True)
    for f in csv_dir.glob("*.csv"):
        (config.MANUAL_DIR / f.name).write_bytes(f.read_bytes())
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    assert run_pipeline.main(["--no-download", "--today", "2026-10-31"]) == 0
    payload = json.loads(config.DASHBOARD_JSON.read_text())
    assert payload["total_leads"] == len(payload["records"]) > 0
    assert payload["new_this_run"] == payload["total_leads"]
    assert payload["leads_with_mailing_address"] == payload["total_leads"]
    assert config.GHL_CSV.exists()
    assert "falling back" in capsys.readouterr().out


def test_shell_entity_mailed_c_o_institutional_investor_is_dropped():
    from datetime import date
    ff = _filings("YAMASA CO LTD", [f"{10 + i} {st}, COLUMBUS, OH 43232" for i, st in
                                    enumerate(["OAK ST", "ELM AVE", "PINE RD", "MAPLE DR"])])
    for f in ff:
        f.plaintiff_street = "C/O PROGRESS RESIDENTIAL MANAGEMENT SERVICES LLC PO BOX 4090"
    assert landlord_leads.aggregate(ff, today=date(2026, 10, 31)) == []


def test_empty_placeholder_month_is_skipped(tmp_path):
    """Regression: on the 1st the Clerk lists next month's CSV as a ~3-byte blank file."""
    d = tmp_path / "e"
    d.mkdir()
    write_csv(d / "2026-10-01_2026-10-31.csv", [["1", "2026-10-01", "ACME LLC", "5 Elm St", "T", "9 Oak St"]],
              header=["Case Number", "File Date", "First Plaintiff", "Plaintiff Address",
                      "First Defendant", "Defendant Address"])
    (d / "2026-11-01_2026-11-30.csv").write_bytes(b'""\r\n')
    assert [f.case_number for f in parse_reports.parse_directory(d)] == ["1"]


def test_unmappable_headers_still_fail_loudly(tmp_path):
    d = tmp_path / "u"
    d.mkdir()
    (d / "x.csv").write_text("FOO,BAR\n1,2\n")
    with pytest.raises(parse_reports.ParseError):
        parse_reports.parse_directory(d)



# ----------------------------------------------------------------------------- Auditor matching
PARCEL_HDR = ["PARCEL ID", "NAME1", "STADDR", "USPS_CITY", "ZIPCODE", "MAILAD3", "MAILAD4"]


def _parcel_csv(tmp_path, rows):
    p = tmp_path / "Parcel.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(PARCEL_HDR)
        w.writerows(rows)
    return p


def _fil(name, addr, zip5="43201", is_person=None, pstreet="1 COURT ST", pcity="COLUMBUS", pstate="OH", pzip="43215", n=0):
    from datetime import date
    from parse_reports import Filing
    return Filing(case_number=f"{name}-{addr}-{n}", file_date=date(2026, 10, 1 + n % 20), disposition="UNDISPOSED",
                  plaintiff_name=name, plaintiff_street=pstreet, plaintiff_city=pcity, plaintiff_state=pstate,
                  plaintiff_zip=pzip, property_address=f"{addr}, COLUMBUS, OH {zip5}", property_zip=zip5,
                  plaintiff_is_person=is_person)


def test_address_keys_ignore_units_suffix_and_direction():
    import parcels
    a = parcels.address_keys("10 N Oak Street Apt 2", "43201-1234")
    b = parcels.address_keys("10 N OAK ST", "43201")
    assert a == b and a[0]
    assert parcels.address_keys("10 N OAK ST", "43201")[1] == parcels.address_keys("10 OAK ST", "43201")[1]
    assert parcels.address_keys("", "43201") == ("", "")


def test_names_match():
    import parcels
    assert parcels.names_match("DOE, JANE Q", "DOE JANE Q & JOHN R")
    assert parcels.names_match("ACME HOLDINGS LLC", "ACME HOLDINGS")
    assert not parcels.names_match("ACME HOLDINGS LLC", "OTHER HOLDINGS LLC")
    assert not parcels.names_match("DOE, JANE", "ROE JANE")
    assert not parcels.names_match("", "DOE JANE")


def test_enrichment_confirmed_mismatch_unmatched_and_manager_regroup(tmp_path):
    import parcels
    from datetime import date
    pc = _parcel_csv(tmp_path, [
        ["P1", "DOE JANE Q", "10 OAK ST", "COLUMBUS", "43201", "44 ELM ST", "DAYTON OH 45402-1234"],
        ["P2", "DOE JANE Q", "11 OAK ST", "COLUMBUS", "43201", "44 ELM ST", "DAYTON OH 45402-1234"],
        ["P3", "SMITH ROBERT", "20 PINE RD", "COLUMBUS", "43206", "7 BIRCH LN", "CLEVELAND OH 44101"],
        ["P4", "SMITH ROBERT", "21 PINE RD", "COLUMBUS", "43206", "7 BIRCH LN", "CLEVELAND OH 44101"],
        ["P5", "OTHER LLC", "30 MAPLE DR", "COLUMBUS", "43211", "PO BOX 1", "AUSTIN TX 78701"],
        ["P6", "ZZZ OTHER", "99 FAR AVE", "COLUMBUS", "43211", "1 X", "COLUMBUS OH 43211"],
    ])
    ff = [
        _fil("DOE, JANE Q", "10 OAK STREET", is_person=True, n=0),
        _fil("DOE, JANE Q", "11 N OAK ST APT 2", is_person=True, n=1),       # loose (direction/unit) match
        _fil("ABC PROPERTY MANAGEMENT LLC", "20 PINE RD", "43206", n=2),
        _fil("ABC PROPERTY MANAGEMENT LLC", "21 PINE RD", "43206", n=3),
        _fil("ACME HOLDINGS LLC", "30 MAPLE DR", "43211", n=4),
        _fil("ACME HOLDINGS LLC", "31 NOWHERE ST", "43211", n=5),
    ]
    wanted = {(parcels.street_of(f.property_address), f.property_zip) for f in ff}
    idx = parcels.load_index(wanted, csv_path=pc)
    assert idx.portfolio("DOE JANE Q") == 2 and idx.rows_read == 6
    stats = parcels.enrich_filings(ff, idx)
    assert stats["confirmed"] == 2 and stats["via_manager"] == 2 and stats["mismatch"] == 1 and stats["unmatched"] == 1

    out = {l.name: l for l in landlord_leads.aggregate(ff, today=date(2026, 10, 31))}
    jane = out["DOE, JANE Q"]
    assert jane.verification == "verified" and jane.portfolio == 2
    assert (jane.mailing_street, jane.mailing_city, jane.mailing_state, jane.mailing_zip) == ("44 ELM ST", "DAYTON", "OH", "45402")
    assert jane.court_mailing.startswith("1 COURT ST")
    smith = out["SMITH ROBERT"]                      # manager's filings re-attributed to the owner of record
    assert smith.verification == "via manager" and smith.via_managers == ["ABC PROPERTY MANAGEMENT LLC"]
    assert smith.owner_type == "individual" and smith.mailing_city == "CLEVELAND" and smith.filings_window == 2
    assert "ABC PROPERTY MANAGEMENT LLC" not in out
    acme = out["ACME HOLDINGS LLC"]
    assert acme.verification == "plaintiff not owner of record"


def test_large_owner_portfolio_is_dropped_as_institutional(tmp_path, monkeypatch):
    import parcels
    from datetime import date
    monkeypatch.setattr(config, "PORTFOLIO_INSTITUTIONAL", 3)
    rows = [[f"P{i}", "BIG LANDLORD LLC", f"{i} MAIN ST", "COLUMBUS", "43201", "1 X", "COLUMBUS OH 43201"] for i in range(4)]
    pc = _parcel_csv(tmp_path, rows)
    ff = [_fil("BIG LANDLORD LLC", f"{i} MAIN ST", n=i) for i in range(2)]
    idx = parcels.load_index({(parcels.street_of(f.property_address), f.property_zip) for f in ff}, csv_path=pc)
    parcels.enrich_filings(ff, idx)
    assert landlord_leads.aggregate(ff, today=date(2026, 10, 31)) == []


def test_auditor_failure_never_breaks_pipeline(tmp_path):
    import parcels
    bad = tmp_path / "Parcel.csv"
    bad.write_text("FOO,BAR\n1,2\n")
    idx = parcels.load_index({("1 A ST", "43201")}, csv_path=bad)      # unrecognised headers -> warn, empty
    assert not idx
    assert parcels.enrich_filings([_fil("X LLC", "1 A ST")], idx) == {"enabled": False}


def test_stale_leads_are_pruned_between_runs(csv_dir, monkeypatch):
    import run_pipeline
    assert run_pipeline.main(["--from-dir", str(csv_dir), "--today", "2026-10-31"]) == 0
    first = json.loads(config.RECORDS_JSON.read_text())
    ghost = dict(first["records"][0], lead_id="ghost000000", owner_name="GHOST LLC")
    first["records"].append(ghost)
    config.RECORDS_JSON.write_text(json.dumps(first))
    assert run_pipeline.main(["--from-dir", str(csv_dir), "--today", "2026-11-01"]) == 0
    ids = {r["lead_id"] for r in json.loads(config.RECORDS_JSON.read_text())["records"]}
    assert "ghost000000" not in ids and len(ids) == len(first["records"]) - 1


class _FakeGis:
    """Minimal stand-in for the county ArcGIS layer: serves OBJECTID-ordered pages."""
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        last = int(params["where"].split(">")[1])
        page = [r for r in self.rows if r["OBJECTID"] > last][:2]          # page size 2
        class R:
            def raise_for_status(self): pass
            def json(self_inner): return {"features": [{"attributes": a} for a in page]}
        return R()


GIS_ROWS = [
    {"OBJECTID": 1, "PARCELID": "010-1", "SITEADDRESS": "101 OAK ST", "ZIPCD": "43201", "OWNERNME1": "ACME HOLDINGS LLC",
     "PSTLADDRES": "PO BOX 5", "PSTLCITYSTZIP": "SCOTTSDALE AZ 85251", "SALEDATE": 1700000000000},
    {"OBJECTID": 2, "PARCELID": "010-2", "SITEADDRESS": "55 PINE AVE", "ZIPCD": "43206", "OWNERNME1": "ACME HOLDINGS LLC",
     "PSTLADDRES": "PO BOX 5", "PSTLCITYSTZIP": "SCOTTSDALE AZ 85251", "SALEDATE": 1790000000000},
    {"OBJECTID": 3, "PARCELID": "010-3", "SITEADDRESS": "9 MAPLE DR", "ZIPCD": "43211", "OWNERNME1": "DOE JANE",
     "PSTLADDRES": "44 ELM ST", "PSTLCITYSTZIP": "DAYTON OH 45402", "SALEDATE": None},
]


def test_gis_download_pages_and_feeds_index(monkeypatch):
    import parcels
    monkeypatch.setattr(config, "CACHE_DIR", config.DATA_DIR / "cache")
    monkeypatch.setattr(config, "PARCEL_GIS_MIN_ROWS", 1)
    fake = _FakeGis(GIS_ROWS)
    path, asof = parcels.download_gis_parcels(fake, url="http://x")
    assert fake.calls == 3                      # 2 + 1 rows, then an empty page ends the loop
    assert asof == "2026-09-21"                 # newest SALEDATE, epoch ms -> date
    idx = parcels.build_index(path, {("101 OAK ST", "43201")})
    p = idx.lookup("101 OAK ST", "43201")
    assert p.owner == "ACME HOLDINGS LLC" and p.mail_state == "AZ" and p.mail_zip == "85251"
    assert idx.portfolio("ACME HOLDINGS LLC") == 2


def test_gis_partial_download_is_rejected(monkeypatch):
    import parcels
    monkeypatch.setattr(config, "CACHE_DIR", config.DATA_DIR / "cache")
    with pytest.raises(RuntimeError, match="partial"):
        parcels.download_gis_parcels(_FakeGis(GIS_ROWS), url="http://x")      # 3 rows < 100,000


def test_load_index_falls_back_to_auditor_csv_when_gis_is_down(monkeypatch, tmp_path):
    import parcels
    old = tmp_path / "Parcel.csv"
    old.write_text("NAME1,STADDR,ZIPCODE,MAILAD3,MAILAD4\nACME LLC,101 OAK ST,43201,PO BOX 5,SCOTTSDALE AZ 85251\n")
    def boom(*a, **k): raise RuntimeError("503")
    monkeypatch.setattr(parcels, "download_gis_parcels", boom)
    monkeypatch.setattr(parcels, "find_parcel_csv_url", lambda s: "http://old")
    monkeypatch.setattr(parcels, "download_parcel_csv", lambda s, u: old)
    idx = parcels._real_load_index({("101 OAK ST", "43201")})
    assert idx and "Auditor" in idx.source and idx.lookup("101 OAK ST", "43201").owner == "ACME LLC"

