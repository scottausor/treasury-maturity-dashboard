# 🏛️ US Treasury Debt Maturity Dashboard

Interactive dashboard tracking month-by-month US Treasury debt maturities — to understand refinancing pressure and why lower yields are critical.

## What it does

Scrapes the [Monthly Statement of Public Debt (MSPD)](https://fiscaldata.treasury.gov/datasets/monthly-statement-public-debt/monthly-statement-public-debt/) PDF published by the US Treasury each month. Extracts all outstanding marketable securities (Bills, Notes, Bonds, TIPS, FRNs), aggregates maturities by calendar month, and displays them in an interactive Streamlit dashboard with refinancing cost analysis.

## Repo structure

```
├── app.py                        # Streamlit dashboard
├── treasury_scraper.py           # PDF scraper
├── requirements.txt
├── data/
│   └── maturity_data_latest.json # Latest parsed data (auto-updated)
├── .streamlit/
│   └── config.toml               # Dark theme config
└── .github/
    └── workflows/
        └── scrape_monthly.yml    # Auto-scrape on the 6th of each month
```

## Local setup

```bash
git clone https://github.com/YOUR_USERNAME/treasury-maturity-dashboard
cd treasury-maturity-dashboard

pip install -r requirements.txt

# Run the scraper to get fresh data
python treasury_scraper.py

# Launch the app
streamlit run app.py
```

## Deploy to Streamlit Cloud (free)

1. Push this repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**
3. Select your repo, branch `main`, file `app.py`
4. Click **Deploy** — done

Streamlit Cloud automatically redeploys whenever you push to `main`. The GitHub Actions workflow runs the scraper on the 6th of each month, commits the new data, and the app updates automatically.

## Manual scraper run

```bash
# Auto-detect latest available MSPD
python treasury_scraper.py

# Specific month
python treasury_scraper.py --year 2026 --month 2

# Use a local PDF
python treasury_scraper.py --pdf path/to/file.pdf

# Output to a specific folder
python treasury_scraper.py --out data/
```

You can also trigger the GitHub Action manually from the **Actions** tab in your repo.

## Data source

[US Treasury — Monthly Statement of Public Debt](https://fiscaldata.treasury.gov/datasets/monthly-statement-public-debt/)
Published ~5th of each month. Covers all outstanding marketable Treasury securities.
