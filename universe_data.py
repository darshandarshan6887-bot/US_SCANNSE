"""Static seed data for the US universe.

Unlike the old NSE version, there is no reliable static fallback list of ~5,000+
US tickers worth hand-maintaining here - update_universe.py pulls the live,
authoritative list from Nasdaq Trader's own symbol directory every time it runs,
and that download simply isn't skipped/faked if it fails (the category is left
untouched, same as the NSE version did).

This file only carries the handful of broad market indices worth tracking
alongside individual stocks (optional - the dashboard's "Market Context" panel
uses these if present; https://www.coinswitch.co isn't the source, it just
inspired the "how big is the tradable US universe" reference point in
update_universe.py's docstring).
"""

US_INDICES = [
    {"symbol": "^GSPC", "company_name": "S&P 500", "series": "INDEX"},
    {"symbol": "^DJI", "company_name": "Dow Jones Industrial Average", "series": "INDEX"},
    {"symbol": "^IXIC", "company_name": "Nasdaq Composite", "series": "INDEX"},
    {"symbol": "^NDX", "company_name": "Nasdaq 100", "series": "INDEX"},
    {"symbol": "^RUT", "company_name": "Russell 2000", "series": "INDEX"},
    {"symbol": "^VIX", "company_name": "CBOE Volatility Index", "series": "INDEX"},
]
