"""Oligo Space scraping pipeline.

Task A  prices  : Playwright -> price per target x region (state / ZIP) -> price_trends
Task B  press   : requests + BeautifulSoup -> new articles only -> press_releases

    python scraper.py                         # both tasks, regions from targets.json
    python scraper.py --task prices --regions GA,CA,30332
    python scraper.py --task press --dry-run  # print, don't write
Env: DATABASE_URL (Supabase *session pooler* URI).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse
from urllib.robotparser import RobotFileParser

log = logging.getLogger("scraper")
ROOT = Path(__file__).parent
UA = "OligoSpacePricingBot/1.0 (+competitor research; contact: set-your-email@example.com)"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


# =========================================================================== #
# Price parsing
# =========================================================================== #
_CUR = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}
_AMOUNT = r"\d[\d.,\s]*\d|\d"
_MULTI = r"(?:million|thousand|[kKmM](?![a-zA-Z]))"
_PRE = re.compile(rf"(?P<cur>US\$|USD|EUR|\$|€|£)\s?(?P<num>{_AMOUNT})\s?(?P<mult>{_MULTI})?", re.I)
_POST = re.compile(rf"(?P<num>{_AMOUNT})\s?(?P<mult>{_MULTI})?\s?(?P<cur>USD|EUR|€)", re.I)


def _to_number(raw: str, has_mult: bool) -> float | None:
    s = re.sub(r"\s", "", raw)
    try:
        if "," in s and "." in s:                      # last separator is the decimal one
            dec = "," if s.rfind(",") > s.rfind(".") else "."
            s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
        elif "," in s:
            s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
        elif "." in s and not has_mult and re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")                     # 1.250.000 -> thousands (no k/M suffix)
        return float(s)
    except ValueError:
        return None


def find_prices(text: str) -> list[tuple[float, str]]:
    """All (value, currency) pairs in text, in order. Needs a currency marker."""
    hits = []
    for rx in (_PRE, _POST):
        for m in rx.finditer(text):
            mult = (m.group("mult") or "").lower()
            num = _to_number(m.group("num"), bool(mult))
            if num is None:
                continue
            hits.append((m.start(), num * _MULT.get(mult, 1), _CUR[m.group("cur").lower()]))
    hits.sort()
    seen, out = set(), []
    for pos, v, c in hits:
        if (v, c) not in seen:
            seen.add((v, c)); out.append((v, c))
    return out


def pick_price(text: str, lo: float | None, hi: float | None, currency: str | None):
    """First price inside the sanity bounds (rejects stray numbers on the page)."""
    for v, c in find_prices(text):
        if currency and c != currency:
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            continue
        return v, c
    return None


def price_snippet(text: str, value: float, currency: str, width: int = 60) -> str:
    """Return the text around the first occurrence of the matched price, for verification."""
    for rx in (_PRE, _POST):
        for m in rx.finditer(text):
            mult = (m.group("mult") or "").lower()
            num = _to_number(m.group("num"), bool(mult))
            if num is not None and abs(num * _MULT.get(mult, 1) - value) < 1e-6:
                s = max(0, m.start() - width); e = min(len(text), m.end() + width)
                return re.sub(r"\s+", " ", text[s:e]).strip()
    return ""


# =========================================================================== #
# Helpers
# =========================================================================== #
def load_json(name: str, override: str | None = None) -> dict:
    return json.loads(Path(override or ROOT / name).read_text())


def _fill(obj, ctx: dict):
    """Recursively substitute {state} / {zip} in strings."""
    if isinstance(obj, str):
        for k, v in ctx.items():
            obj = obj.replace("{" + k + "}", v)
        return obj
    if isinstance(obj, list):
        return [_fill(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, ctx) for k, v in obj.items()}
    return obj


_robots: dict[str, RobotFileParser] = {}


def robots_ok(url: str) -> bool:
    """Respect robots.txt. If it can't be fetched we allow (standard behaviour)."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = RobotFileParser(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA, url)


def region_for(token: str) -> dict:
    token = token.strip()
    if re.fullmatch(r"\d{5}", token):
        return {"state": "ZIP-" + token, "zip": token}
    return {"state": token.upper(), "zip": ""}


# =========================================================================== #
# Task A - prices (Playwright)
# =========================================================================== #
def scrape_prices(cfg: dict, region_tokens: list[str], headless: bool = True) -> list[dict]:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    defaults = cfg.get("defaults", {})
    tmpl = cfg.get("region_templates", {})
    rows: list[dict] = []
    now = datetime.now(timezone.utc)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        for t in cfg["targets"]:
            if not t.get("enabled", True):
                log.info("skip (disabled): %s", t["item_name"]); continue
            t = {**defaults, **t}
            regions = [region_for(x) for x in region_tokens] if t.get("regional") else [{"state": "ALL", "zip": ""}]
            for reg in regions:
                ctx_vars = {"state": reg["state"], "zip": reg["zip"]}
                url = t["url"].replace("{state}", reg["state"]).replace("{zip}", reg["zip"])
                if t.get("regional") and tmpl.get("query"):       # regional URL parameters
                    q = {k: v for k, v in _fill(tmpl["query"], ctx_vars).items() if v}
                    pu = urlparse(url)
                    url = urlunparse(pu._replace(query=urlencode(parse_qsl(pu.query) + list(q.items()))))
                if not robots_ok(url):
                    log.warning("robots.txt disallows %s - skipped", url); continue

                price = None
                for attempt in range(1, 4):
                    context = browser.new_context(
                        user_agent=UA, locale="en-US",
                        extra_http_headers=_fill(tmpl.get("headers", {}), ctx_vars) if t.get("regional") else {})
                    try:
                        if t.get("regional") and tmpl.get("cookies"):
                            context.add_cookies([{**c, "url": url} for c in _fill(tmpl["cookies"], ctx_vars)])
                        page = context.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        for act in t.get("actions", []):          # e.g. click a configurator option
                            if "click" in act: page.click(act["click"], timeout=10_000)
                            if "fill" in act: page.fill(act["fill"]["selector"], _fill(act["fill"]["value"], ctx_vars))
                            if "wait_ms" in act: page.wait_for_timeout(act["wait_ms"])
                        sel = t.get("selector", "body")
                        page.wait_for_selector(sel, timeout=15_000)
                        text = " ".join(page.locator(sel).all_inner_texts())
                        price = pick_price(text, t.get("min_price"), t.get("max_price"), t.get("currency"))
                        last_text = text
                        if price: break
                        log.warning("no in-range price (try %d) %s [%s]", attempt, t["item_name"], reg["state"])
                    except PWTimeout:
                        log.warning("timeout (try %d) %s [%s]", attempt, t["item_name"], reg["state"])
                    except Exception as exc:                      # network / navigation errors
                        log.warning("error (try %d) %s: %s", attempt, t["item_name"], exc)
                    finally:
                        context.close()
                    time.sleep(2 * attempt)
                time.sleep(t.get("delay_s", 3))                    # be polite

                if not price:
                    log.error("FAILED %s [%s] - check selector / bounds", t["item_name"], reg["state"]); continue
                value, cur = price
                rows.append(dict(
                    timestamp=now, observed_on=now.date().isoformat(), state=reg["state"],
                    item_name=t["item_name"], price=value, currency=cur, price_low=value, price_high=value,
                    bus_id=t.get("bus_id"), supplier=t.get("supplier"), kind=t["kind"],
                    price_basis=t["price_basis"], quantity=t.get("quantity"), nre=t["nre"],
                    confidence=t["confidence"], source_url=url, specs=t.get("specs", {}),
                    source_title=t.get("source_title", t["item_name"]),
                    source_type=t.get("source_type", "Public pricing (configurator)")))
                log.info("OK %-45s %-8s %s %s", t["item_name"], reg["state"], f"{value:,.0f}", cur)
                log.info("   matched text: ...%s...", price_snippet(last_text, value, cur))
        browser.close()
    return rows


# =========================================================================== #
# Task B - press releases (BeautifulSoup)
# =========================================================================== #
def classify(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return sorted({k for k in keywords if re.search(r"\b" + re.escape(k.lower()), low)})


def parse_date(soup, selector: str | None):
    from dateutil import parser as dp
    cands = []
    if selector:
        el = soup.select_one(selector)
        if el: cands += [el.get("datetime"), el.get_text(" ", strip=True)]
    t = soup.find("time")
    if t: cands += [t.get("datetime"), t.get_text(" ", strip=True)]
    m = soup.find("meta", attrs={"property": "article:published_time"})
    if m: cands.append(m.get("content"))
    for c in cands:
        if not c: continue
        try: return dp.parse(c, fuzzy=True).date()
        except (ValueError, OverflowError): continue
    return None


def extract_text(soup, selector: str) -> str:
    node = soup.select_one(selector) or soup.body or soup
    for bad in node.select("script, style, nav, footer, header, form, aside"):
        bad.decompose()
    return re.sub(r"\n{3,}", "\n\n", node.get_text("\n", strip=True))[:20_000]


def listing_links(html: str, src: dict) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    must = src.get("url_contains")
    seen, out = set(), []
    for a in soup.select(src.get("link_selector", "article a[href], main a[href]")):
        href = urljoin(src["url"], a["href"]).split("#")[0]
        title = a.get_text(" ", strip=True)
        if href in seen or href.rstrip("/") == src["url"].rstrip("/") or len(title) < 12:
            continue
        if must and must not in href:
            continue
        excerpt = ""
        if src.get("title_selector"):
            el = a.select_one(src["title_selector"])
            if el: title = el.get_text(" ", strip=True)
        if src.get("excerpt_selector"):
            el = a.select_one(src["excerpt_selector"])
            if el: excerpt = el.get_text(" ", strip=True)
        seen.add(href); out.append({"url": href, "title": title, "excerpt": excerpt})
    return out[: src.get("max_articles", 10)]


def scrape_press(cfg: dict, known_urls: set[str]) -> list[dict]:
    import requests
    from bs4 import BeautifulSoup

    keywords = cfg.get("alert_keywords", [])
    rows = []
    for src in cfg["sources"]:
        if not src.get("enabled", True): continue
        if not robots_ok(src["url"]):
            log.warning("robots.txt disallows %s", src["url"]); continue
        try:
            r = requests.get(src["url"], headers=HEADERS, timeout=30); r.raise_for_status()
        except requests.RequestException as exc:
            log.error("listing failed %s: %s", src["name"], exc); continue
        links = listing_links(r.text, src)
        new = [l for l in links if l["url"] not in known_urls]
        log.info("%s: %d links, %d new", src["name"], len(links), len(new))
        if not links:
            log.warning("%s: 0 links - check link_selector", src["name"])
        for l in new:
            time.sleep(1.5)
            try:
                ar = requests.get(l["url"], headers=HEADERS, timeout=30); ar.raise_for_status()
            except requests.RequestException as exc:
                log.warning("article failed %s: %s", l["url"], exc); continue
            soup = BeautifulSoup(ar.text, "html.parser")
            if src.get("title_selector"):
                title = l["title"]                       # card title; avoids sitewide <h1>
            else:
                h1 = soup.find("h1")
                title = h1.get_text(" ", strip=True) if h1 else l["title"]
            title = title[:300]
            pub = parse_date(soup, src.get("date_selector"))
            body = extract_text(soup, src.get("body_selector", "article, main"))
            # alerts use the headline and summary only, so sitewide menus can't trigger them
            hits = classify(f"{title} {l.get('excerpt', '')}", keywords)
            rows.append(dict(date_published=pub, source=src["name"], title=title, url=l["url"],
                             category=src.get("category", "Press release"),
                             content=body, alert_flag=bool(hits), alert_keywords=hits))
            known_urls.add(l["url"])
    return rows


# =========================================================================== #
# Database sync (psycopg2)
# =========================================================================== #
def connect():
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (use the Supabase session-pooler connection string).")
    return psycopg2.connect(url, connect_timeout=15)


_PRICE_COLS = ["timestamp", "observed_on", "state", "item_name", "price", "currency", "price_low",
               "price_high", "bus_id", "supplier", "kind", "price_basis", "quantity", "nre",
               "confidence", "source_url", "specs",
               "source_title", "source_type"]


def upsert_prices(conn, rows: list[dict]) -> int:
    from psycopg2.extras import Json, execute_values
    # ON CONFLICT cannot touch the same row twice in one statement -> dedupe first
    uniq = {(r["item_name"], r["state"], r["source_url"], str(r["observed_on"])): r for r in rows}
    vals = [tuple(Json(r[c]) if c == "specs" else r[c] for c in _PRICE_COLS) for r in uniq.values()]
    if not vals: return 0
    sets = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in _PRICE_COLS
                     if c not in ("item_name", "state", "source_url", "observed_on"))
    sql = (f'INSERT INTO price_trends ({", ".join(chr(34)+c+chr(34) for c in _PRICE_COLS)}) VALUES %s '
           f"ON CONFLICT (item_name, state, source_url, observed_on) DO UPDATE SET {sets}")
    with conn.cursor() as cur:
        execute_values(cur, sql, vals)
    conn.commit()
    return len(vals)


def upsert_buses(conn, rows: list[dict]) -> int:
    from psycopg2.extras import Json, execute_values
    cols = list(rows[0].keys())
    vals = [tuple(Json(r[c]) if c in ("flags", "form_factors") else r[c] for c in cols) for r in rows]
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "bus_id") + ", updated_at = now()"
    sql = f"INSERT INTO bus_catalog ({', '.join(cols)}) VALUES %s ON CONFLICT (bus_id) DO UPDATE SET {sets}"
    with conn.cursor() as cur:
        execute_values(cur, sql, vals)
    conn.commit()
    return len(vals)


def known_press_urls(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT url FROM press_releases")
        return {r[0] for r in cur.fetchall()}


def upsert_press(conn, rows: list[dict]) -> int:
    from psycopg2.extras import execute_values
    if not rows: return 0
    cols = ["date_published", "source", "title", "url", "content", "alert_flag", "alert_keywords", "category"]
    vals = [tuple(r[c] for c in cols) for r in {r["url"]: r for r in rows}.values()]
    sql = (f"INSERT INTO press_releases ({', '.join(cols)}) VALUES %s ON CONFLICT (url) DO UPDATE SET "
           "title = EXCLUDED.title, content = EXCLUDED.content, alert_flag = EXCLUDED.alert_flag, "
           "alert_keywords = EXCLUDED.alert_keywords, category = EXCLUDED.category, date_published = COALESCE(EXCLUDED.date_published, "
           "press_releases.date_published)")
    with conn.cursor() as cur:
        execute_values(cur, sql, vals)
    conn.commit()
    return len(vals)


# =========================================================================== #
# CLI
# =========================================================================== #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=["prices", "press", "all"], default="all")
    ap.add_argument("--regions", help="comma list of state codes / ZIPs, e.g. GA,CA,30332")
    ap.add_argument("--targets"); ap.add_argument("--sources")
    ap.add_argument("--dry-run", action="store_true", help="scrape and print, do not write to the DB")
    ap.add_argument("--headed", action="store_true", help="show the browser (debugging selectors)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    conn = None if a.dry_run else connect()
    failures = 0
    try:
        if a.task in ("prices", "all"):
            cfg = load_json("targets.json", a.targets)
            tokens = [x for x in (a.regions or ",".join(cfg.get("regions", ["ALL"]))).split(",") if x]
            rows = scrape_prices(cfg, tokens, headless=not a.headed)
            n = len(rows) if a.dry_run else upsert_prices(conn, rows)
            log.info("prices: %d scraped, %d %s", len(rows), n, "printed" if a.dry_run else "upserted")
            if a.dry_run:
                for r in rows: print(json.dumps({k: str(v) for k, v in r.items() if k != "specs"}))
            enabled = sum(1 for t in cfg["targets"] if t.get("enabled", True))
            if enabled and not rows: failures += 1

        if a.task in ("press", "all"):
            cfg = load_json("press_sources.json", a.sources)
            rows = scrape_press(cfg, set() if a.dry_run else known_press_urls(conn))
            n = len(rows) if a.dry_run else upsert_press(conn, rows)
            log.info("press: %d new articles, %d alerts", n, sum(r["alert_flag"] for r in rows))
            if a.dry_run:
                for r in rows: print(r["date_published"], r["source"], "|", r["title"], r["alert_keywords"])
    finally:
        if conn: conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
