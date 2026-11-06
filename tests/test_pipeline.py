import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import pytest
from scraper import find_prices, pick_price, classify, listing_links, extract_text, parse_date
from pricing_engine import MissionSpec, Strategy, analyze, weighted_median, scenarios, screen
from seed import load_seed


@pytest.mark.parametrize("text,expected", [
    ("Base price $3,600,000", [(3_600_000, "USD")]),
    ("€450,960 excl. NRE", [(450_960, "EUR")]),
    ("from $1.5M to $2.5M", [(1_500_000, "USD"), (2_500_000, "USD")]),
    ("starting at $150k", [(150_000, "USD")]),
    ("450.960,00 €", [(450_960, "EUR")]),
    ("USD 4.5 million", [(4_500_000, "USD")]),
    ("1.250.000 EUR", [(1_250_000, "EUR")]),
    ("175 W and 100 kg", []),
])
def test_find_prices(text, expected):
    assert find_prices(text) == expected


def test_pick_price_bounds_and_currency():
    t = "Save $50 today! Platform from $3,600,000 or €4,000,000"
    assert pick_price(t, 1e6, 1e7, "USD") == (3_600_000, "USD")
    assert pick_price(t, 1e6, 1e7, "EUR") == (4_000_000, "EUR")
    assert pick_price("nothing", None, None, None) is None


def test_press_parsing():
    from bs4 import BeautifulSoup
    listing = """<main><a href="/news/big-contract-win-announced">Company wins big contract award</a>
      <a href="/about">About us page link</a><a href="/news/">All news items listing</a></main>"""
    links = listing_links(listing, {"url": "https://x.com/news/", "url_contains": "/news/"})
    assert [l["url"] for l in links] == ["https://x.com/news/big-contract-win-announced"]
    soup = BeautifulSoup("<article><time datetime='2026-10-12'></time><p>Won a contract</p>"
                         "<nav>menu</nav></article>", "html.parser")
    assert str(parse_date(soup, None)) == "2026-10-12"
    assert extract_text(soup, "article") == "Won a contract"
    assert classify("Wins CONTRACT for ESPA bus", ["contract", "espa", "ipo"]) == ["contract", "espa"]


def test_weighted_median():
    assert weighted_median([(1, 1), (3, 1)]) == 2
    assert weighted_median([(1, 1), (2, 1), (9, 1)]) == 2
    assert weighted_median([(1, 1), (2, 5), (9, 1)]) == 2


@pytest.fixture(scope="module")
def data():
    bus_rows, price_rows = load_seed()
    import json
    d = json.load(open(pathlib.Path(__file__).parent.parent / "data/seed_research.json"))
    obs = [dict(bus=e["bus"], config=e["config"], low=e["low"], high=e["high"], currency=e["currency"],
                kind=e["kind"], quantity=e.get("quantity"), confidence=e["confidence"],
                specs=e.get("specs") or {}, source=e["source"], nre=e["nre"]) for e in d["price_evidence"]]
    return d["buses"], obs, price_rows


def test_seed_keys_unique(data):
    _, _, rows = data
    keys = [(r["item_name"], r["state"], r["source_url"], r["observed_on"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_optimizer_default_mission(data):
    buses, obs, _ = data
    a = analyze(buses, obs, MissionSpec(), Strategy(est_cost_musd=1.5))
    assert a.n_priced >= 3 and a.benchmark
    assert a.undercut == pytest.approx(a.benchmark * 0.9)
    assert a.recommended >= a.floor
    assert a.margin_pct >= 25 - 1e-6
    # hosted fees / contract totals never feed the benchmark
    assert all(r["kind"] in {"Bus price", "Workbook base range", "Workbook volume proxy"} for r in a.used)
    assert all(not r["config"].startswith("Shared Satellite") for r in a.used)


def test_precision_config_filter(data):
    buses, obs, _ = data
    a = analyze(buses, obs, MissionSpec(pointing_deg=0.05, propulsion="any", max_lead_months=None,
                                        payload_kg=50, power_w=150), Strategy(include_estimates=False))
    used = {r["config"] for r in a.used}
    assert "LEO Aries · base" not in used          # 0.119 deg misses 0.05
    assert "LEO Aries · precision GNC" in used     # 0.022 deg meets it


def test_floor_holds_and_matching(data):
    buses, obs, _ = data
    a = analyze(buses, obs, MissionSpec(), Strategy(est_cost_musd=50))
    assert a.recommended == pytest.approx(a.floor) and any("margin floor" in w for w in a.warnings)
    m = analyze(buses, obs, MissionSpec(), Strategy(mode="match"))
    assert m.recommended == pytest.approx(m.match)
    assert len(scenarios(m)) == 5


def test_volume_mission_uses_volume_proxies(data):
    buses, obs, _ = data
    a = analyze(buses, obs, MissionSpec(quantity=10, pointing_deg=None, max_lead_months=None),
                Strategy())
    assert a.used and all(r["kind"] == "Workbook volume proxy" for r in a.used)


def test_screen_range_logic():
    bus = {"payload": [20, 100], "power": [100, 100], "pointing": [0.3, 0.02], "lead": [6, 12], "prop": "E"}
    assert screen(bus, MissionSpec(payload_kg=50, power_w=100, pointing_deg=0.05, max_lead_months=15)).status == "review"
    assert screen(bus, MissionSpec(payload_kg=200)).status == "fail"
    assert screen(bus, MissionSpec(payload_kg=10, power_w=50, pointing_deg=0.5, max_lead_months=12,
                                   propulsion="any")).status == "pass"
