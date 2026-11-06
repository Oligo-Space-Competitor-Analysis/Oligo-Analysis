# Oligo Space · Pricing Bot

Scrapes competitor bus prices and press releases on a schedule, stores them in Supabase,
and serves a Streamlit dashboard with the pricing optimizer. All free tiers.

```
GitHub Actions (cron, 6h) -> scraper.py -> Supabase Postgres -> app.py (Streamlit Community Cloud)
```

## Files
| File | Purpose |
|---|---|
| `pricing_engine.py` | Optimizer algorithm (benchmark → undercut/match → margin floor) + range-aware screening |
| `scraper.py` | Task A Playwright prices (state/ZIP aware), Task B BeautifulSoup press, psycopg2 upserts |
| `targets.json`, `press_sources.json` | What to scrape. **Selectors are unverified starting points** |
| `seed.py` + `data/seed_research.json` | Loads your Week-4 catalog (33 buses) and 83 price records |
| `schema.sql` | `bus_catalog`, `price_trends`, `press_releases`, `latest_prices` view, RLS on |
| `app.py` | Dashboard: optimizer tab, trend chart (state filter) + press timeline |
| `.github/workflows/scrape_schedule.yml` | Cron job |

## Setup
1. **Supabase**: new project → SQL editor → paste `schema.sql` → Run.
2. Copy the **Session pooler** connection string (Settings → Database) into `.env` as `DATABASE_URL`.
3. `pip install -r requirements.txt && playwright install chromium`
4. `python seed.py` (loads the research data).
5. Tune selectors: `python scraper.py --task prices --dry-run --headed` and
   `python scraper.py --task press --dry-run`. Fix `targets.json` / `press_sources.json` until each target prints a price / articles.
6. `python scraper.py` for a real run, then `streamlit run app.py`.
7. **GitHub**: repo → Settings → Secrets → Actions → add `DATABASE_URL`. The workflow runs every 6 h (and via "Run workflow").
8. **Dashboard**: share.streamlit.io → deploy `app.py` → add `DATABASE_URL = "..."` under Secrets.

## Free-tier gotchas
- Supabase free projects pause after ~1 week idle; the 6-hourly job keeps yours active.
- GitHub disables scheduled workflows after 60 days without repo activity; push a commit or re-enable.
- Use the pooler URI: GitHub runners have no IPv6, so Supabase's direct host fails.
- Respect each site's terms. `scraper.py` checks robots.txt, identifies itself, and rate-limits; set your contact email in `UA`.

## Regional (state / ZIP) scraping
Set `"regional": true` on a target and pass `--regions GA,CA,30332`. Each region sends the cookies,
headers and URL params from `region_templates` (`{state}`, `{zip}` placeholders) and is stored in `price_trends.state`.
Satellite bus prices are usually national, so targets default to `state = 'ALL'`.

## How the price recommendation works
1. Screen every priced configuration against the mission: **pass / review / fail** (range-aware).
2. Keep real bus prices only, matching the requested quantity; EUR→USD uses an explicit rate.
3. Weight each price by evidence (Published 1.0 … Inferred 0.3) × fit (pass 1.0, review 0.5).
4. One weighted-median price per bus → benchmark (median / lowest / average).
5. Target = benchmark × (1 − undercut %) or the benchmark; floor = cost / (1 − min margin); **recommended = max(target, floor)**.

Run tests: `pytest -q tests`
