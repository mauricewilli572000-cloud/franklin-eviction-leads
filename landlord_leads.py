"""Aggregate eviction filings by plaintiff (landlord) and score "tired landlord" likelihood.

Thesis: landlords who repeatedly file evictions, especially small/individual owners with
recent activity and multiple units, are the most likely to be burned out and open to a
discounted sale. Scores are a prioritization aid, not a verdict.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import config
from parse_reports import Filing

_PUNCT_RE = re.compile(r"[^\w\s&]")
_SPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    n = _PUNCT_RE.sub(" ", name.upper())
    n = n.replace("&", " AND ")
    return _SPACE_RE.sub(" ", n).strip()


def normalize_street(street: str) -> str:
    s = _PUNCT_RE.sub(" ", street.upper())
    for a, b in (("STREET", "ST"), ("AVENUE", "AVE"), ("ROAD", "RD"), ("DRIVE", "DR"),
                 ("LANE", "LN"), ("COURT", "CT"), ("BOULEVARD", "BLVD"), ("PLACE", "PL")):
        s = re.sub(rf"\b{a}\b", b, s)
    return _SPACE_RE.sub(" ", s).strip()


def street_name(addr: str) -> str:
    """'4321 N GREEN ARBOR LN, COLUMBUS, OH 43232' -> 'GREEN ARBOR LN|43232' (house number/direction dropped)."""
    zip5 = re.search(r"\b(\d{5})\b\s*$", addr.strip())
    first = addr.split(",")[0]
    s = _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", first.upper())).strip()
    s = re.sub(r"^\d+[A-Z]?\s+", "", s)
    s = re.sub(r"^(N|S|E|W|NE|NW|SE|SW)\s+", "", s)
    return f"{s}|{zip5.group(1) if zip5 else ''}"


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def classify_owner(norm_name: str, is_person: bool | None = None) -> str:
    """institutional | manager | llc | company | trust | individual

    is_person comes from the Clerk's split name columns: a name that sits in the COMPANY
    column is never an individual, even without an LLC/INC suffix ("LIFE AT GREEN ARBOR").
    """
    if _any(config.INSTITUTIONAL_PATTERNS, norm_name):
        return "institutional"
    if _any(config.MANAGER_PATTERNS, norm_name):
        return "manager"
    if _any(config.LLC_PATTERNS, norm_name):
        return "llc"
    if _any(config.TRUST_PATTERNS, norm_name):
        return "trust"
    if _any(config.COMPANY_PATTERNS, norm_name) or is_person is False:
        return "company"
    return "individual"


@dataclass
class Landlord:
    key: str
    name: str
    owner_type: str
    mailing_street: str = ""
    mailing_city: str = ""
    mailing_state: str = ""
    mailing_zip: str = ""
    filings: list[Filing] = field(default_factory=list)

    # computed
    filings_window: int = 0
    filings_12m: int = 0
    filings_90d: int = 0
    last_filing: date | None = None
    first_filing: date | None = None
    properties: list[str] = field(default_factory=list)
    n_properties: int = 0
    dismissed: int = 0
    judgments: int = 0
    writs: int = 0
    score: int = 0
    tier: str = "COLD"
    signals: list[str] = field(default_factory=list)
    top_street_share: float = 0.0
    property_kind: str = "scattered"      # scattered | apartment complex
    verification: str = ""                # verified | via manager | sold since filing | plaintiff not owner of record | no parcel match | ""
    portfolio: int = 0                    # parcels the owner holds countywide (Auditor)
    via_managers: list[str] = field(default_factory=list)
    court_mailing: str = ""
    parcels_matched: int = 0
    sold_since: int = 0                   # rentals that changed hands after this landlord's eviction filing

    @property
    def lead_id(self) -> str:
        return hashlib.sha1(self.key.encode()).hexdigest()[:12]


def _landlord_key(norm_name: str, owner_type: str, zip5: str) -> str:
    # Individuals share common names; split them by mailing ZIP. Entities key on name alone.
    if owner_type == "individual" and zip5:
        return f"{norm_name}|{zip5}"
    return norm_name


def aggregate(filings: list[Filing], today: date | None = None,
              include_institutional: bool = False) -> list[Landlord]:
    today = today or date.today()
    groups: dict[str, Landlord] = {}
    for f in filings:
        nn = normalize_name(f.plaintiff_name)
        if not nn:
            continue
        otype = classify_owner(nn, f.plaintiff_is_person)
        key = _landlord_key(nn, otype, f.plaintiff_zip)
        ll = groups.get(key)
        if ll is None:
            ll = groups[key] = Landlord(key=key, name=f.plaintiff_name.strip(), owner_type=otype)
        ll.filings.append(f)

    result = []
    for ll in groups.values():
        _compute(ll, today)
        if ll.filings_window < config.MIN_FILINGS_FOR_LEAD:
            continue
        if ll.filings_12m > config.INSTITUTIONAL_FILINGS_THRESHOLD:
            ll.owner_type = "institutional"
        # shell entities mailed "C/O PROGRESS RESIDENTIAL ..." etc. belong to corporate investors
        if _any(config.INSTITUTIONAL_PATTERNS, normalize_name(ll.mailing_street)):
            ll.owner_type = "institutional"
        if ll.portfolio >= config.PORTFOLIO_INSTITUTIONAL:
            ll.owner_type = "institutional"          # owns a big slice of the county: not a tired landlord
        if ll.owner_type == "institutional" and not include_institutional:
            continue
        _score(ll, today)
        result.append(ll)
    result.sort(key=lambda l: (l.score, l.filings_12m), reverse=True)
    return result


def _compute(ll: Landlord, today: date) -> None:
    dated = sorted((f for f in ll.filings if f.file_date), key=lambda f: f.file_date)
    newest_first = sorted(ll.filings, key=lambda f: f.file_date or date.min, reverse=True)

    # Most recent plaintiff mailing address that has a street
    for f in newest_first:
        if f.plaintiff_street:
            ll.mailing_street, ll.mailing_city = f.plaintiff_street, f.plaintiff_city
            ll.mailing_state, ll.mailing_zip = f.plaintiff_state, f.plaintiff_zip
            break

    ll.filings_window = len(ll.filings)
    cutoff12, cutoff90 = today - timedelta(days=365), today - timedelta(days=90)
    ll.filings_12m = sum(1 for f in dated if f.file_date >= cutoff12)
    ll.filings_90d = sum(1 for f in dated if f.file_date >= cutoff90)
    if dated:
        ll.first_filing, ll.last_filing = dated[0].file_date, dated[-1].file_date

    seen, props = set(), []
    for f in newest_first:
        if not f.property_address:
            continue
        k = normalize_street(f.property_address)
        if k not in seen:
            seen.add(k)
            props.append(f.property_address)
    ll.properties = props
    ll.n_properties = len(props)

    # Auditor owner verification (parcels.enrich_filings) -------------------------------------
    st = Counter(f.parcel_status for f in ll.filings if f.parcel_status)
    # distinct rental ADDRESSES matched to a parcel (same unit as property_count, so "7 of 9" always makes sense)
    ll.parcels_matched = len({f.property_address for f in ll.filings
                              if f.parcel_status in ("confirmed", "mismatch", "via_manager", "sold")})
    kept = {f.property_address for f in ll.filings if f.parcel_status in ("confirmed", "via_manager")}
    ll.sold_since = len({f.property_address for f in ll.filings if f.parcel_status == "sold"} - kept)
    ll.portfolio = max((f.owner_portfolio for f in ll.filings), default=0)
    ll.via_managers = sorted({f.via_manager for f in ll.filings if f.via_manager})
    ll.court_mailing = next((f.court_mailing for f in newest_first if f.court_mailing), "")
    sold_majority = bool(ll.sold_since) and ll.sold_since / max(ll.parcels_matched, 1) >= 0.5
    if not st:
        ll.verification = ""
    elif sold_majority:
        ll.verification = "sold since filing"
    elif st["via_manager"] and st["via_manager"] >= st["confirmed"] + st["mismatch"] + st["sold"]:
        ll.verification = "via manager"
    elif st["confirmed"] and st["confirmed"] >= st["mismatch"] + st["sold"]:
        ll.verification = "verified"
    elif st["mismatch"] or st["sold"]:
        ll.verification = "plaintiff not owner of record"
    else:
        ll.verification = "no parcel match"

    # Apartment-complex detection: many filings on one street, or a community-style name.
    streets = Counter(street_name(f.property_address) for f in ll.filings if f.property_address)
    if streets:
        ll.top_street_share = streets.most_common(1)[0][1] / sum(streets.values())
    nn = normalize_name(ll.name)
    if ll.filings_window >= config.COMPLEX_MIN_FILINGS and (
        ll.top_street_share >= config.COMPLEX_TOP_STREET_SHARE or _any(config.COMPLEX_NAME_PATTERNS, nn)
    ):
        ll.property_kind = "apartment complex"

    c = Counter()
    for f in ll.filings:
        d = f.disposition.lower()
        if "dismiss" in d:
            c["dismissed"] += 1
        if "judgment" in d or "jgmt" in d:
            c["judgments"] += 1
        if "writ" in d or "restitution" in d:
            c["writs"] += 1
    ll.dismissed, ll.judgments, ll.writs = c["dismissed"], c["judgments"], c["writs"]


def _score(ll: Landlord, today: date) -> None:
    """0-100 'tired landlord' priority. Sweet spot = small/mid owners with scattered rentals.
    Complexes and managers are capped below HOT (you can't buy a complex from a wholesale
    flyer, and a manager's mailing address is not the owner's)."""
    s, sig = 0, []
    n12 = ll.filings_12m

    if n12 >= 50:
        sig.append(f"{n12} filings in 12 mo (large operator)")
    elif n12 >= config.LARGE_OPERATOR_12M:
        s += 10; sig.append(f"{n12} filings in 12 mo (large operator)")
    elif n12 >= 15:
        s += 25; sig.append(f"{n12} filings in 12 mo")
    elif n12 >= 5:
        s += 35; sig.append(f"{n12} filings in 12 mo")
    elif n12 >= 3:
        s += 25; sig.append(f"{n12} filings in 12 mo")
    elif n12 >= 2:
        s += 15; sig.append(f"{n12} filings in 12 mo")

    if ll.filings_90d >= 3:
        s += 15; sig.append(f"{ll.filings_90d} filings in last 90 days")
    elif ll.filings_90d >= 1:
        s += 10; sig.append("active in last 90 days")

    if ll.last_filing and (today - ll.last_filing).days <= 30:
        s += 10; sig.append("filed within 30 days")

    if ll.property_kind == "scattered":
        if 3 <= ll.n_properties < 15:
            s += 10; sig.append(f"{ll.n_properties} distinct rental addresses")
        elif ll.n_properties == 2:
            s += 5; sig.append("2 rental addresses")
        elif ll.n_properties >= 15:
            sig.append(f"{ll.n_properties} distinct rental addresses")
    else:
        sig.append(f"apartment complex / community ({ll.top_street_share:.0%} of filings on one street)")

    if ll.owner_type == "individual":
        s += 15; sig.append("individual owner")
    elif ll.owner_type in ("llc", "trust"):
        s += 8; sig.append(f"{ll.owner_type.upper()} owner")
    elif ll.owner_type == "company":
        s += 5; sig.append("company owner")
    elif ll.owner_type == "manager":
        sig.append("manager/agent - verify owner")

    if ll.mailing_state and ll.mailing_state != "OH":
        s += 10; sig.append(f"out-of-state mailing ({ll.mailing_state})")
    elif ll.mailing_zip and ll.properties and not any(ll.mailing_zip in p for p in ll.properties):
        s += 3; sig.append("mailing ZIP differs from rental ZIPs")

    if ll.portfolio:
        if ll.portfolio <= 10:
            s += 10; sig.append(f"owns {ll.portfolio} parcel{'s' if ll.portfolio != 1 else ''} countywide (small owner)")
        elif ll.portfolio <= 40:
            s += 5; sig.append(f"owns {ll.portfolio} parcels countywide")
        else:
            sig.append(f"owns {ll.portfolio} parcels countywide (large owner)")
    if ll.verification == "verified":
        sig.append("owner verified on Auditor records")
    elif ll.verification == "via manager":
        sig.append(f"owner of record found via manager ({', '.join(ll.via_managers[:2])})")
    elif ll.verification == "plaintiff not owner of record":
        sig.append("plaintiff is not the owner of record - verify before outreach")
    exited = False
    if ll.sold_since:
        exited = ll.verification == "sold since filing"
        sig.append(f"{ll.sold_since} of {ll.parcels_matched} matched rentals sold since the eviction filing"
                   + (" - landlord may have exited" if exited else ""))
        if exited:
            s = max(s - 20, 0)

    if ll.dismissed >= 2:
        s += 5; sig.append(f"{ll.dismissed} dismissals (tenant churn)")
    if ll.writs >= 1:
        s += 5; sig.append(f"{ll.writs} writ/restitution")

    s = min(s, 100)
    if ll.property_kind != "scattered" or ll.owner_type == "manager" or exited:
        s = min(s, config.HOT_SCORE - 1)       # never HOT
    ll.score = s
    ll.tier = "HOT" if ll.score >= config.HOT_SCORE else "WARM" if ll.score >= config.WARM_SCORE else "COLD"
    ll.signals = sig
