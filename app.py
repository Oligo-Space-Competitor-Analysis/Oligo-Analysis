"""Oligo Space · Pricing Bot dashboard (Streamlit).

Tab 1  Pricing optimizer : mission spec + strategy -> recommended price, competitor fit
Tab 2  Market intelligence: price history, every observation with obtained time and source
       link, and press/investor items with obtained time and source link.
Buttons: Refresh data (reload from the database), Scrape now (runs scraper.py).
Reads Supabase through DATABASE_URL (env var or st.secrets). Without it the app runs in
demo mode on data/seed_research.json.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from pricing_engine import MissionSpec, Strategy, analyze, scenarios, to_musd, PROPULSION_LABELS
from seed import load_seed

ROOT = Path(__file__).parent
st.set_page_config(page_title="Oligo Space · Pricing Bot", page_icon="🛰️", layout="wide")


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def _db_url() -> str | None:
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        return os.environ.get("DATABASE_URL")


def _query(conn, sql: str) -> list[dict]:
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


@st.cache_data(ttl=300, show_spinner="Loading data…")
def load_data():
    """Returns (buses_rows, prices_df, press_df, mode)."""
    url = _db_url()
    if url:
        try:
            import psycopg2
            with psycopg2.connect(url, connect_timeout=10) as conn:
                buses = _query(conn, "select * from bus_catalog")
                prices = _query(conn, 'select * from price_trends order by "timestamp"')
                press = _query(conn, "select * from press_releases order by "
                                     "coalesce(date_published, scraped_at::date) desc, id desc limit 200")
            if buses and prices:
                return buses, pd.DataFrame(prices), pd.DataFrame(press), "live"
            st.warning("Database is connected but empty: run `python seed.py`. Showing demo data.")
        except Exception as exc:                                   # noqa: BLE001
            st.warning(f"Database unavailable ({type(exc).__name__}). Showing demo data.")
    buses, prices = load_seed()
    return buses, pd.DataFrame(prices), pd.DataFrame(
        columns=["date_published", "source", "title", "url", "content", "alert_flag",
                 "alert_keywords", "scraped_at", "category"]), "demo"


def run_scrape() -> tuple[int, str]:
    """Run the scraper as a subprocess so its output and errors show in the page."""
    env = dict(os.environ)
    env["DATABASE_URL"] = _db_url() or ""
    try:
        proc = subprocess.run([sys.executable, "scraper.py", "--task", "all"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=900)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "Scrape stopped after 15 minutes."


def _gh_config() -> tuple[str | None, str | None]:
    try:
        return st.secrets["GITHUB_TOKEN"], st.secrets["GITHUB_REPO"]
    except Exception:
        return os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")


def trigger_github_scrape() -> tuple[bool, str]:
    """Ask GitHub Actions to run the scrape workflow (works from the hosted app)."""
    import requests
    token, repo = _gh_config()
    if not token or not repo:
        return False, "GITHUB_TOKEN and GITHUB_REPO are not set in the app secrets."
    url = f"https://api.github.com/repos/{repo}/actions/workflows/scrape_schedule.yml/dispatches"
    try:
        r = requests.post(url, json={"ref": "main"}, timeout=20,
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json"})
    except requests.RequestException as exc:
        return False, f"Could not reach GitHub: {exc}"
    if r.status_code == 204:
        return True, f"Scrape started on GitHub. Follow it at https://github.com/{repo}/actions"
    return False, f"GitHub returned {r.status_code}: {r.text[:300]}"


def num(x):
    return None if x is None or pd.isna(x) else float(x)


def fmt_time(ts) -> str:
    if ts is None or pd.isna(ts):
        return "unknown"
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%d %H:%M UTC") if pd.Timestamp(ts).tzinfo \
        else pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")


def link_or_none(u: str | None) -> str | None:
    return u if isinstance(u, str) and u.startswith("http") else None


def engine_buses(rows: list[dict]) -> list[dict]:
    return [dict(id=r["bus_id"], company=r["company"], model=r["model"], cls=r["size_class"],
                 payload=[num(r["payload_lo"]), num(r["payload_hi"])],
                 power=[num(r["power_lo"]), num(r["power_hi"])],
                 pointing=[num(r["pointing_base"]), num(r["pointing_best"])],
                 lead=[num(r["lead_min"]), num(r["lead_max"])], prop=r["propulsion"] or "",
                 flags=list(r["flags"] or [])) for r in rows]


def engine_obs(prices: pd.DataFrame) -> list[dict]:
    """Latest observation per item and source, used for the benchmark."""
    df = prices.sort_values("timestamp").groupby(["item_name", "source_url"], as_index=False).tail(1)
    return [dict(bus=r.bus_id, config=r.item_name, low=num(r.price_low), high=num(r.price_high),
                 currency=r.currency, kind=r.kind, quantity=None if pd.isna(r.quantity) else int(r.quantity),
                 confidence=r.confidence, specs=r.specs if isinstance(r.specs, dict) else {},
                 source=r.source_url, nre=r.nre) for r in df.itertuples()]


# --------------------------------------------------------------------------- #
# Page setup
# --------------------------------------------------------------------------- #
buses_rows, prices, press, mode = load_data()
prices = prices.copy()
for col in ("source_title", "source_type"):
    if col not in prices:
        prices[col] = ""
prices[["source_title", "source_type"]] = prices[["source_title", "source_type"]].fillna("")
prices["timestamp"] = pd.to_datetime(prices["timestamp"], utc=True)
press = press.copy()
for col in ("category", "scraped_at"):
    if col not in press:
        press[col] = None
press["scraped_at"] = pd.to_datetime(press["scraped_at"], utc=True, errors="coerce")
bus_list = engine_buses(buses_rows)

st.title("🛰️ Oligo Space · Pricing Bot")
last_price = prices["timestamp"].max()
last_press = press["scraped_at"].max() if not press.empty else None
st.caption(("Demo data (no database configured)" if mode == "demo" else "Live data from Supabase")
           + f" · prices last obtained {fmt_time(last_price)}"
           + (f" · press last checked {fmt_time(last_press)}" if last_press is not None else ""))

b1, b2, b3 = st.columns([1, 1, 4])
if b1.button("Refresh data", help="Reload from the database now"):
    st.cache_data.clear()
    st.rerun()
if b2.button("Scrape now", disabled=mode != "live",
             help="Starts the scrapers on GitHub (hosted app) or runs them here (your computer)."):
    token, repo = _gh_config()
    if token and repo:
        with st.spinner("Asking GitHub to start the scrape…"):
            ok, msg = trigger_github_scrape()
        st.session_state["scrape_output"] = (0 if ok else 1, msg, "github")
    else:
        with st.spinner("Scraping prices and press releases (1–3 minutes)…"):
            code, output = run_scrape()
        st.session_state["scrape_output"] = (code, output, "local")
    st.cache_data.clear()
if "scrape_output" in st.session_state:
    code, output, where = st.session_state["scrape_output"]
    if where == "github":
        (st.info if code == 0 else st.error)(output)
        if code == 0:
            st.caption("The job takes 2–3 minutes. Click Refresh data after it finishes.")
    else:
        (st.success if code == 0 else st.error)("Scrape finished." if code == 0 else "Scrape finished with errors.")
        with st.expander("Scrape log", expanded=code != 0):
            st.code(output[-6000:] or "(no output)")

# --------------------------------------------------------------------------- #
# Sidebar: mission + strategy
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Mission spec")
    payload = st.number_input("Payload mass (kg)", 0.0, value=50.0)
    power = st.number_input("Payload power (W, orbit-avg)", 0.0, value=150.0)
    pointing = st.number_input("Pointing needed (°, smaller = tighter; blank = any)", 0.0, value=0.05,
                               step=0.001, format="%.4f")
    prop = st.selectbox("Propulsion", ["any", *PROPULSION_LABELS],
                        format_func=lambda k: "Any / none" if k == "any" else PROPULSION_LABELS[k])
    lead = st.number_input("Max lead time (months, blank = any)", 0.0, value=15.0)
    qty = st.number_input("Spacecraft quantity", 1, value=1, step=1)

    st.header("Pricing strategy")
    bench = st.selectbox("Benchmark", ["median", "low", "average"],
                         format_func={"median": "Median of compatible", "low": "Lowest compatible",
                                      "average": "Average of compatible"}.get)
    mode_sel = st.radio("Strategy", ["undercut", "match"], horizontal=True, format_func=str.title)
    und = st.slider("Undercut by (%)", 0, 50, 10)
    margin = st.slider("Min gross margin (%)", 0, 80, 25)
    cost = st.number_input("Oligo est. cost to build ($M, optional)", 0.0, value=None, step=0.1,
                           placeholder="e.g. 4.2")

    st.header("Evidence")
    est = st.checkbox("Include workbook estimates", True,
                      help="Estimated / inferred prices carry lower weight than published ones.")
    rev = st.checkbox("Include 'needs review' fits (half weight)", True)
    fx = st.number_input("EUR→USD rate (assumption)", 0.5, 2.0, 1.08, 0.01)

spec = MissionSpec(payload or None, power or None, pointing or None, prop, lead or None, int(qty))
strat = Strategy(bench, mode_sel, und, margin, cost or None, fx, est, rev)
a = analyze(bus_list, engine_obs(prices), spec, strat)
fm = lambda v: "—" if v is None else f"${v:,.2f}M"
obtained_by_item = prices.groupby("item_name")["timestamp"].max()

tab_opt, tab_mkt = st.tabs(["Pricing optimizer", "Market intelligence"])

# --------------------------------------------------------------------------- #
# Tab 1: optimizer
# --------------------------------------------------------------------------- #
with tab_opt:
    st.subheader("Recommended price")
    c = st.columns(4)
    c[0].metric("Best Oligo price", fm(a.recommended))
    c[1].metric("Market median", fm(a.median), help=f"{a.n_priced} priced of {a.n_compatible} compatible buses")
    c[2].metric("Margin floor", fm(a.floor))
    c[3].metric("Gross margin", "—" if a.margin_pct is None else f"{a.margin_pct:.1f}%")
    c = st.columns(4)
    c[0].metric("Market low", fm(a.low)); c[1].metric("Market high", fm(a.high))
    c[2].metric("Price match", fm(a.match)); c[3].metric("Undercut", fm(a.undercut))
    for w in a.warnings:
        st.warning(w)

    priced = [b for b in a.buses if b["price_musd"] is not None]
    if priced:
        chart_df = pd.DataFrame([{"name": b["name"], "price": b["price_musd"], "who": "Competitor"} for b in priced]
                                + [{"name": "Oligo (recommended)", "price": a.recommended, "who": "Oligo"}])
        base = alt.Chart(chart_df).mark_bar().encode(
            y=alt.Y("name:N", sort="x", title=None), x=alt.X("price:Q", title="USD $M"),
            color=alt.Color("who:N", scale=alt.Scale(domain=["Competitor", "Oligo"], range=["#8c8f97", "#e8541f"]),
                            legend=None), tooltip=["name", alt.Tooltip("price:Q", format=",.2f")])
        rules = alt.Chart(pd.DataFrame({"p": [a.match, a.undercut], "l": ["match", "undercut"]})).mark_rule(
            strokeDash=[4, 3]).encode(x="p:Q", color=alt.value("#12805c"), tooltip=["l", alt.Tooltip("p:Q", format=",.2f")])
        st.altair_chart((base + rules).properties(height=max(120, 26 * len(chart_df))), width="stretch")
        st.caption("Dashed lines: price match and undercut. Prices use one weighted-median value per bus.")

    sc = scenarios(a)
    if sc:
        with st.expander("What-if: undercut vs. margin"):
            st.dataframe(pd.DataFrame(sc).rename(columns={
                "undercut_pct": "Undercut %", "price_musd": "Price $M", "margin_pct": "Margin %",
                "held_at_floor": "Held at floor"}), hide_index=True, width="stretch")

    st.subheader("Competitor fit")
    fit_df = pd.DataFrame([{
        "Bus": b["name"], "Class": b["class"],
        "Fit": {"pass": "✅ Meets", "review": "🟡 Review", "fail": "❌ Fails"}[b["status"]],
        "Price $M": b["price_musd"], "Evidence rows": b["n_obs"],
        "Why": "; ".join(b["fail"] or b["review"]) or "Meets numeric screen",
        "Notes": "; ".join(b["notes"])} for b in a.buses]).sort_values(["Fit", "Price $M"])
    st.dataframe(fit_df, hide_index=True, width="stretch",
                 column_config={"Price $M": st.column_config.NumberColumn(format="%.2f")})

    with st.expander(f"Evidence used ({len(a.used)}) and skipped ({len(a.skipped)})"):
        if a.used:
            used = pd.DataFrame(a.used)
            used["obtained"] = used["config"].map(lambda k: fmt_time(obtained_by_item.get(k)))
            used["link"] = used["source"].map(link_or_none)
            st.dataframe(used[["config", "price_musd", "weight", "fit", "confidence", "kind", "obtained", "source", "link"]],
                         hide_index=True, width="stretch",
                         column_config={"link": st.column_config.LinkColumn("Open source"),
                                        "price_musd": st.column_config.NumberColumn("Price $M", format="%.2f"),
                                        "weight": st.column_config.NumberColumn("Weight", format="%.2f")})
        st.dataframe(pd.DataFrame(a.skipped), hide_index=True, width="stretch")
    st.download_button("Download analysis (CSV)", fit_df.to_csv(index=False), "oligo-pricing-analysis.csv")

# --------------------------------------------------------------------------- #
# Tab 2: price history, price table, press
# --------------------------------------------------------------------------- #
with tab_mkt:
    st.subheader("Price trends")
    f1, f2, f3 = st.columns(3)
    states = sorted(prices["state"].unique())
    sel_states = f1.multiselect("State / region", states, default=states)
    kinds = sorted(prices["kind"].unique())
    sel_kinds = f2.multiselect("Price type", kinds, default=[k for k in kinds if k == "Bus price"] or kinds)
    suppliers = sorted(prices["supplier"].dropna().unique())
    sel_sup = f3.multiselect("Supplier", suppliers, default=suppliers)

    t = prices[prices["state"].isin(sel_states) & prices["kind"].isin(sel_kinds)
               & prices["supplier"].isin(sel_sup)].copy()
    t["usd_m"] = [to_musd(p, c, fx) for p, c in zip(t["price"].astype(float), t["currency"])]
    t = t.dropna(subset=["usd_m"])
    if t.empty:
        st.info("No price observations match these filters.")
    else:
        line = alt.Chart(t).mark_line(point=True).encode(
            x=alt.X("timestamp:T", title="Obtained"),
            y=alt.Y("usd_m:Q", title="Price (USD $M)", scale=alt.Scale(zero=False)),
            color=alt.Color("item_name:N", title="Item"),
            tooltip=["item_name", "supplier", "state", alt.Tooltip("usd_m:Q", format=",.3f", title="USD $M"),
                     alt.Tooltip("price:Q", format=",.0f", title="Native price"), "currency", "confidence",
                     "source_type", alt.Tooltip("timestamp:T", title="Obtained")],
        ).interactive().properties(height=380)
        st.altair_chart(line, width="stretch")
        st.caption(f"EUR converted at {fx:.2f}. Lines appear once the same item has been obtained on more than one day. "
                   "Workbook rows are the research file's own dates, not scrape dates.")

        st.subheader("Every price observation")
        table = pd.DataFrame({
            "Item": t["item_name"], "Supplier": t["supplier"],
            "USD $M": t["usd_m"].round(3),
            "Native price": t.apply(lambda r: f"{r['price']:,.0f} {r['currency']}", axis=1),
            "Obtained (UTC)": t["timestamp"].map(fmt_time),
            "Source type": t["source_type"].replace("", "not recorded"),
            "Source": t["source_title"].replace("", "not recorded"),
            "Confidence": t["confidence"],
            "Link": t["source_url"].map(link_or_none),
        }).sort_values("Obtained (UTC)", ascending=False)
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={"Link": st.column_config.LinkColumn("Open source"),
                                    "USD $M": st.column_config.NumberColumn(format="%.3f")})

    st.divider()
    st.subheader("Press releases and investor items")
    if press.empty:
        st.info("No items yet. Click Scrape now, or run `python scraper.py --task press`.")
    else:
        cats = sorted(press["category"].fillna("Press release").unique())
        sel_cats = st.multiselect("Source category", cats, default=cats)
        only_alerts = st.toggle("Alerts only", False)
        shown = press[press["category"].fillna("Press release").isin(sel_cats)]
        if only_alerts:
            shown = shown[shown["alert_flag"]]
        for r in shown.head(40).itertuples():
            when = r.date_published if pd.notna(r.date_published) else "date n/a"
            badge = "🔔 " if r.alert_flag else ""
            with st.expander(f"{badge}{when} · {r.source} · {r.title}"):
                st.caption(f"Category: {r.category or 'Press release'} · Obtained: {fmt_time(r.scraped_at)}")
                if r.alert_flag:
                    st.markdown("**Matched:** " + ", ".join(r.alert_keywords or []))
                st.markdown(f"[Open source]({r.url})")
                st.write((r.content or "")[:1500] + ("…" if r.content and len(r.content) > 1500 else ""))
