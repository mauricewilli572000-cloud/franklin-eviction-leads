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


REST_ROOTS = [
    "https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures",
    "https://gis.franklincountyohio.gov/hosting/rest/services",
    "https://maps.hilliardohio.gov/arcgis/rest/services/Reference/Franklin_County_Auditor/MapServer",
]
OWNERISH = re.compile(r"own|name|addr|mail|site|situs|parcel|sale|transfer|deed|date|updat", re.I)


def probe_rest(s: requests.Session) -> None:
    """ArcGIS REST: list services, then layer fields (NAMES ONLY - no record values are printed)."""
    for root in REST_ROOTS:
        try:
            j = s.get(root, params={"f": "json"}, timeout=45).json()
        except Exception as exc:  # noqa: BLE001
            print(f"[recon] REST {root} -> ERROR {exc}")
            continue
        svcs = [f"{x.get('name')}({x.get('type')})" for x in j.get("services", [])]
        layers = [(l.get("id"), l.get("name")) for l in j.get("layers", [])]
        print(f"[recon] REST {root} -> services={svcs[:40]} layers={layers[:20]} folders={j.get('folders')}")
        for lid, _ in layers[:4]:
            try:
                lj = s.get(f"{root}/{lid}", params={"f": "json"}, timeout=45).json()
                fields = [f["name"] for f in lj.get("fields", [])]
                cnt = s.get(f"{root}/{lid}/query", params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
                            timeout=60).json()
                print(f"[recon]   layer {lid} {lj.get('name')!r} count={cnt} maxRecordCount={lj.get('maxRecordCount')}")
                print(f"[recon]   fields: {fields}")
            except Exception as exc:  # noqa: BLE001
                print(f"[recon]   layer {lid} ERROR {exc}")
        for svc in j.get("services", [])[:40]:
            name, typ = svc.get("name"), svc.get("type")
            if typ not in ("MapServer", "FeatureServer"):
                continue
            url = f"https://gis.franklincountyohio.gov/hosting/rest/services/{name}/{typ}"
            try:
                sj = s.get(url, params={"f": "json"}, timeout=45).json()
                print(f"[recon]   svc {name}/{typ}: layers={[(l.get('id'), l.get('name')) for l in sj.get('layers', [])][:8]}")
            except Exception as exc:  # noqa: BLE001
                print(f"[recon]   svc {name} ERROR {exc}")


LAYERS = [
    "https://maps.hilliardohio.gov/arcgis/rest/services/Reference/Franklin_County_Auditor/MapServer/0",
    "https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures/Parcel_Features/MapServer/0",
    "https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures/TaxParcel_EDP/MapServer/0",
    "https://gis.franklincountyohio.gov/hosting/rest/services/TaxParcel_CombosSplitsSubsCondos/MapServer/0",
]


def probe_freshness(s: requests.Session) -> None:
    """Field names, count and the NEWEST value of every date field (no personal data printed)."""
    import datetime as dt
    for url in LAYERS:
        try:
            lj = s.get(url, params={"f": "json"}, timeout=45).json()
            fields = lj.get("fields", [])
            names = [f["name"] for f in fields]
            cnt = s.get(f"{url}/query", params={"where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=60).json()
            print(f"[recon] LAYER {url} name={lj.get('name')!r} count={cnt} maxRecordCount={lj.get('maxRecordCount')}")
            print(f"[recon]   has owner/mail/sale fields: {[n for n in names if re.search('own|mail|pstl|sale|rental|site|addr', n, re.I)]}")
            print(f"[recon]   ALL fields ({len(names)}): {names[:70]}")
            for f in fields:
                if f.get("type") != "esriFieldTypeDate":
                    continue
                stats = [{"statisticType": "max", "onStatisticField": f["name"], "outStatisticFieldName": "mx"}]
                r = s.get(f"{url}/query", params={"where": "1=1", "outStatistics": str(stats).replace("'", '"'),
                                                  "f": "json"}, timeout=60).json()
                try:
                    ms = r["features"][0]["attributes"]["mx"]
                    when = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat() if ms else None
                except Exception:  # noqa: BLE001
                    when = f"? {str(r)[:120]}"
                print(f"[recon]   newest {f['name']}: {when}")
        except Exception as exc:  # noqa: BLE001
            print(f"[recon] LAYER {url} ERROR {exc}")


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
    probe_rest(s)
    probe_freshness(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
