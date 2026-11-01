# Franklin County (OH) Eviction -> Tired Landlord Lead Scraper

Source: Franklin County Municipal Court Clerk monthly F.E.D. (eviction) case lists
<https://www.fcmcclerk.com/reports/evictions> — 13 CSVs (current month + previous 12), regenerated nightly.
Each row: case number, file date, last disposition, first plaintiff + most recent address,
first defendant + most recent address.

## How it works
1. `fetch_reports.py` — discovers the CSV links on the reports page (falls back to the known URL pattern); re-downloads the latest 2 months every run, older months once.
2. `parse_reports.py` — header-tolerant CSV parsing -> one `Filing` per case. **Tenant (defendant) names are never kept**; only the defendant address is used as the likely rental address.
3. `landlord_leads.py` — groups filings by plaintiff (landlord), classifies owner (individual / LLC / trust / company / manager / institutional), drops housing authorities, banks, government and mega-filers (> 75 filings/yr), and scores 0-100 (HOT >= 70, WARM >= 50). Repeat filers with several rentals, individual owners and out-of-state mailing addresses score highest.
4. `exporters.py` — accumulates `data/eviction_records.json` (merge by lead_id), holds address-less leads in `data/eviction_pending_records.json` (retried each run), writes `dashboard/evictions.json` + `dashboard/evictions_ghl_export.csv` (GoHighLevel upload) — same decisions as the foreclosure scrapers.

## Run
```
pip install -r requirements.txt
python run_pipeline.py                    # download + parse + score + export
python run_pipeline.py --from-dir ./csvs  # use CSVs you downloaded by hand
python run_pipeline.py --include-institutional --min-filings 1
python -m pytest -q
```
## Automation (own repo: `franklin-eviction-leads`)
`.github/workflows/scrape.yml` runs nightly (~4:45 AM ET) and on demand (Actions -> Run workflow):
tests -> download all 13 monthly CSVs -> score landlords -> commit `data/` + `dashboard/` -> deploy `dashboard/` to GitHub Pages.

Published feed (read by the **Franklin Evictions** tab of the Foreclosure Leads dashboard):
- `https://<user>.github.io/franklin-eviction-leads/evictions.json`
- `https://<user>.github.io/franklin-eviction-leads/evictions_ghl_export.csv`

One-time setup: create the repo, push this folder, then Settings -> Pages -> Source: **GitHub Actions**.
If the Clerk ever blocks GitHub's servers, put the monthly CSVs in `manual_csv/` and the pipeline uses them.

## First run / header check
The parser was verified against real Clerk files (Jul-Oct 2026). If the Clerk renames columns, run:
```
python discover_reports.py                    # lists the CSV links found
python discover_reports.py path/to/file.csv   # shows how each header was mapped (prints no row data)
```
If a required column shows as UNMAPPED, add it to `HEADER_OVERRIDES` in `config.py`.

## Caveats
- The Clerk notes addresses are *mailing* addresses and only the **first** plaintiff/defendant is listed; the plaintiff may be a property manager or attorney rather than the owner (flagged "manager/agent - verify owner"). Confirm ownership on the Franklin County Auditor site before outreach.
- Scores prioritize; they don't prove a landlord wants to sell.
