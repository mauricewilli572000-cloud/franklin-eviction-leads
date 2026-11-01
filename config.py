"""Configuration for the Franklin County (OH) eviction -> tired-landlord lead scraper.

Source: Franklin County Municipal Court Clerk "Monthly Civil F.E.D. (Eviction) Case List"
https://www.fcmcclerk.com/reports/evictions

The Clerk publishes one CSV per month (current month + previous 12), regenerated nightly.
Each row = one F.E.D. case: case number, file date, last disposition, first plaintiff
(+ most recent address) and first defendant (+ most recent address).
"""
from pathlib import Path

BASE_URL = "https://www.fcmcclerk.com"
REPORTS_PAGE = f"{BASE_URL}/reports/evictions"
CSV_URL_TEMPLATE = (
    BASE_URL
    + "/storage/shared/civil-fed/"
    + "FCMC%20Civil%20F.E.D.%20%28Eviction%29%20Case%20List%20{start}%20to%20{end}.csv"
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw_eviction_csv"
RECORDS_JSON = DATA_DIR / "eviction_records.json"          # leads WITH a mailing address
PENDING_JSON = DATA_DIR / "eviction_pending_records.json"  # leads without one (retried each run)
DASHBOARD_DIR = ROOT / "dashboard"                         # published to GitHub Pages
DASHBOARD_JSON = DASHBOARD_DIR / "evictions.json"           # read by the Evictions tab of the combined dashboard
GHL_CSV = DASHBOARD_DIR / "evictions_ghl_export.csv"        # GoHighLevel upload (also linked from the dashboard)
MANUAL_DIR = ROOT / "manual_csv"                            # fallback: CSVs dropped here are used if the Clerk blocks downloads

# --- Polite fetching -------------------------------------------------------
USER_AGENT = "Mozilla/5.0 (compatible; FranklinEvictionLeads/1.0; public-records research)"
REQUEST_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
MONTHS_BACK = 13               # the Clerk publishes current month + 12 previous
ALWAYS_REFRESH_LAST_N = 2      # the current and previous month change nightly; older months rarely do

# --- Lead qualification ----------------------------------------------------
MIN_FILINGS_FOR_LEAD = 2                 # landlords with fewer filings in the window are ignored
INSTITUTIONAL_FILINGS_THRESHOLD = 75     # more filings than this in 12 months -> professional filer
HOT_SCORE = 70
WARM_SCORE = 50

# Plaintiffs that are not "tired landlord" targets (regex, matched on normalized UPPER name).
INSTITUTIONAL_PATTERNS = [
    r"HOUSING AUTHORITY",
    r"METROPOLITAN HOUSING",
    r"\bCMHA\b",
    r"\bCITY OF\b",
    r"\bCOUNTY OF\b",
    r"\bSTATE OF\b",
    r"UNIVERSITY",
    r"\bBANK\b",
    r"CREDIT UNION",
    r"\bREIT\b",
    r"HABITAT FOR HUMANITY",
    # Corporate single-family-rental investors (not "tired landlords")
    r"\bBORROWER\b", r"FIRSTKEY", r"PROGRESS RESIDENTIAL", r"\bAH4R\b", r"AMERICAN HOMES 4 RENT",
    r"INVITATION HOMES", r"\bHOME SFR\b", r"\bSFR\b", r"\bBAF ASSETS\b", r"ALTO ASSET",
    r"VINEBROOK", r"MAIN STREET RENEWAL", r"TRICON", r"PRETIUM", r"\bCERBERUS\b", r"\bBLACKSTONE\b",
]

# Names that signal an apartment community (many units, one site) rather than scattered houses.
COMPLEX_NAME_PATTERNS = [
    r"\bVILLAGE\b", r"\bAPARTMENTS?\b", r"\bCOMMONS\b", r"\bRESIDENCES\b", r"\bTOWNHOMES?\b",
    r"\bLIFE AT\b", r"\b(RETREAT|LAKESIDE|ARBORS?|CROSSING|PLACE|PARK|MANOR|GARDENS|TERRACE) AT\b",
    r"\bAT THE\b", r"\bTOWNE? CENTER\b", r"\bOWNER (LLC|LP)\b",
    r"\b(SQUARE|RUN|TRACE|POINTE?|GLEN|CROSSING|PLACE|PARK|WOODS|RIDGE|COMMUNITIES|ASSOCIATES|TIC \d+)\b",
]
COMPLEX_MIN_FILINGS = 5
COMPLEX_TOP_STREET_SHARE = 0.35   # >= this share of filings on one street (+ZIP) -> likely apartment complex
LARGE_OPERATOR_12M = 30           # >= this many filings/yr: professionally run, little size credit

# Plaintiff looks like a manager/agent rather than an owner -> verify owner before outreach.
MANAGER_PATTERNS = [
    r"\bMANAGEMENT\b",
    r"\bMGMT\b",
    r"\bREALTY\b",
    r"\bREAL ESTATE\b",
    r"PROPERTY (SERVICES|MANAGERS?)",
    r"\bRENTALS? (SERVICES|MANAGEMENT)\b",
]

# Entity suffix classification (after normalization).
LLC_PATTERNS = [r"\bLLC\b", r"\bL L C\b", r"\bLTD\b", r"\bLP\b", r"\bLLP\b", r"\bLLLP\b", r"\bPLLC\b"]
COMPANY_PATTERNS = [r"\bINC\b", r"\bCORP\b", r"\bCORPORATION\b", r"\bCO\b", r"\bCOMPANY\b",
                    r"\bPROPERTIES\b", r"\bHOLDINGS\b", r"\bGROUP\b", r"\bINVESTMENTS?\b",
                    r"\bENTERPRISES?\b", r"\bPARTNERS\b", r"\bAPARTMENTS?\b"]
TRUST_PATTERNS = [r"\bTRUST\b", r"\bTRUSTEE\b", r"\bESTATE\b"]

# Optional manual header overrides if the Clerk renames columns:
# {"Exact Header In CSV": ("plaintiff"|"defendant"|None, "name"|"street"|"street2"|"city"|"state"|"zip"|
#                          "case_number"|"file_date"|"disposition"|"disposition_date")}
HEADER_OVERRIDES: dict = {}
