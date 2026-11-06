"""Oligo Space pricing engine.

Pure Python (standard library only), so it can be unit-tested and reused by the
Streamlit app, the scraper, or a notebook.

Algorithm = the Pricing Optimizer's strategy layer
    benchmark (median / lowest / average of compatible competitors)
    -> price match or undercut %
    -> margin floor = cost / (1 - min margin)
    -> recommended price = max(target, floor)
fed by the Competitor Research file's better evidence handling
    * range-aware screening: pass / review / fail instead of a hard pass-fail
    * only real bus prices count (no hosted fees, annual fees or contract totals)
    * price basis and quantity must match the requested mission
    * evidence confidence weights the benchmark (published > workbook estimate)
    * currencies are never mixed silently (EUR -> USD uses an explicit rate)

All prices inside the engine are USD millions ($M).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
CONFIDENCE_WEIGHT = {
    "Published": 1.0,
    "Quoted": 1.0,
    "Workbook quoted": 0.6,
    "Estimated": 0.4,
    "Inferred": 0.3,
}
FIT_WEIGHT = {"pass": 1.0, "review": 0.5}
PRICE_KINDS = {"Bus price", "Workbook base range", "Workbook volume proxy"}
ESTIMATE_CONFIDENCE = {"Workbook quoted", "Estimated", "Inferred"}
PROPULSION_LABELS = {"E": "Electric", "C": "Chemical", "G": "Green monoprop", "N": "Cold gas"}


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #
@dataclass
class MissionSpec:
    payload_kg: Optional[float] = 50
    power_w: Optional[float] = 150
    pointing_deg: Optional[float] = 0.05      # smaller = tighter; None/0 = any
    propulsion: str = "any"                   # any | E | C | G | N
    max_lead_months: Optional[float] = 15     # None/0 = any
    quantity: int = 1


@dataclass
class Strategy:
    benchmark: str = "median"                 # median | low | average
    mode: str = "undercut"                    # undercut | match
    undercut_pct: float = 10.0
    min_margin_pct: float = 25.0
    est_cost_musd: Optional[float] = None     # Oligo build cost, $M
    eur_usd: float = 1.08                     # ASSUMPTION: set to today's rate
    include_estimates: bool = True            # workbook estimates / inferred prices
    include_review: bool = True               # "needs review" fits, at half weight


@dataclass
class Fit:
    status: str                               # pass | review | fail
    fail: list = field(default_factory=list)
    review: list = field(default_factory=list)
    notes: list = field(default_factory=list)


@dataclass
class Analysis:
    spec: MissionSpec
    strategy: Strategy
    buses: list            # one dict per catalog bus (fit + price)
    used: list             # price observations that fed the benchmark
    skipped: list          # observations excluded, with the reason
    low: Optional[float] = None
    median: Optional[float] = None
    average: Optional[float] = None
    high: Optional[float] = None
    benchmark: Optional[float] = None
    match: Optional[float] = None
    undercut: Optional[float] = None
    floor: Optional[float] = None
    recommended: Optional[float] = None
    margin_pct: Optional[float] = None
    estimate_share: Optional[float] = None
    warnings: list = field(default_factory=list)

    @property
    def n_priced(self) -> int:
        return sum(1 for b in self.buses if b["price_musd"] is not None)

    @property
    def n_compatible(self) -> int:
        return sum(1 for b in self.buses if b["status"] in ("pass", "review"))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def weighted_median(pairs: Iterable[tuple]) -> Optional[float]:
    """pairs = [(value, weight)]. Equal weights give the ordinary median."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        return None
    half = sum(w for _, w in pairs) / 2
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if math.isclose(acc, half, rel_tol=1e-9):
            return (v + pairs[i + 1][0]) / 2 if i + 1 < len(pairs) else v
        if acc > half:
            return v
    return pairs[-1][0]


def _pair(x):
    """Normalise a [lo, hi] spec field. Returns (lo, hi) with None for unknown."""
    if x is None:
        return (None, None)
    lo, hi = (x[0], x[1]) if len(x) > 1 else (x[0], x[0])
    lo = None if lo is None else float(lo)
    hi = None if hi is None else float(hi)
    if lo is None:
        lo = hi
    if hi is None:
        hi = lo
    return (lo, hi)


def to_musd(value, currency: str, eur_usd: float) -> Optional[float]:
    if value is None:
        return None
    if currency == "USD":
        return value / 1e6
    if currency == "EUR":
        return value * eur_usd / 1e6
    return None  # GBP etc: never guess a rate


def point_price(low, high) -> Optional[float]:
    """Midpoint of a range. Open-ended '$4.0M+' keeps its floor value."""
    if low is None and high is None:
        return None
    if high is None:
        return float(low)
    if low is None:
        return float(high)
    return (float(low) + float(high)) / 2


# --------------------------------------------------------------------------- #
# Screening (range-aware, from the research file)
# --------------------------------------------------------------------------- #
def screen(bus: dict, spec: MissionSpec, installed_prop: Optional[str] = None) -> Fit:
    """Compare one bus (or one priced configuration) with the mission.

    bus keys: payload [lo,hi] kg | power [lo,hi] W | pointing [standard,best] deg |
              lead [min,max] months | prop 'EG' | flags [...]
    installed_prop: propulsion of the exact priced configuration, if known
                    ('E', 'C', 'G', 'N' or 'none').
    fail   -> cannot meet the requirement even with options
    review -> might meet it (upgrade, unknown value, range overlaps deadline)
    notes  -> information only (family-level specs, conflicting sources...)
    """
    fail, review, notes = [], [], []
    flags = bus.get("flags") or []

    for key, label, need in (
        ("payload", "Payload capacity", spec.payload_kg),
        ("power", "Payload power", spec.power_w),
    ):
        if not need:
            continue
        lo, hi = _pair(bus.get(key))
        if lo is None:
            review.append(f"{label} not recorded on a comparable basis")
        elif need > hi:
            fail.append(f"{label} below requirement")
        elif need > lo:
            review.append(f"{label} needs a larger or upgraded configuration")

    if spec.pointing_deg:
        base, best = _pair(bus.get("pointing"))
        if base is None:
            review.append("Pointing not recorded")
        else:
            base, best = max(base, best), min(base, best)  # base = looser figure
            if best > spec.pointing_deg:
                fail.append("Pointing error exceeds requirement")
            elif base > spec.pointing_deg:
                review.append("Pointing needs the optional precision package")
            if "pointing" in flags:
                review.append("Pointing quoted at 1σ; confirm a comparable definition")

    if spec.max_lead_months:
        lmin, lmax = _pair(bus.get("lead"))
        if lmax is None:
            review.append("Lead time not recorded")
        elif lmax > spec.max_lead_months:
            if lmin is not None and lmin <= spec.max_lead_months:
                review.append("Lead-time range overlaps the deadline")
            else:
                fail.append("Earliest delivery misses the deadline")

    if spec.propulsion != "any":
        if installed_prop:
            if installed_prop != spec.propulsion:
                fail.append("Priced configuration has different propulsion")
        else:
            prop = bus.get("prop") or ""
            if not prop:
                review.append("Propulsion capability not confirmed")
            elif spec.propulsion not in prop:
                fail.append("Required propulsion not offered")
            else:
                review.append("Confirm propulsion is included in the priced configuration")

    for flag, text in (
        ("family", "Family-level specs; confirm a single configuration"),
        ("conflict", "Source specs conflict"),
        ("development", "Flight readiness needs reconfirmation"),
        ("scope", "Commercial scope needs confirmation"),
    ):
        if flag in flags:
            notes.append(text)

    return Fit("fail" if fail else "review" if review else "pass", fail, review, notes)


def _config_bus(bus: dict, specs: dict) -> tuple:
    """Overlay the exact priced configuration's known specs on the bus record."""
    merged = dict(bus)
    for key in ("payload", "power", "lead"):
        if specs.get(key) is not None:
            merged[key] = [specs[key], specs[key]]
    if specs.get("pointing") is not None:
        merged["pointing"] = [specs["pointing"], specs["pointing"]]
    return merged, specs.get("prop")


# --------------------------------------------------------------------------- #
# Observation eligibility
# --------------------------------------------------------------------------- #
def eligible(obs: dict, spec: MissionSpec, strat: Strategy) -> Optional[str]:
    """Return None if the observation may feed the benchmark, else the reason."""
    kind = obs.get("kind") or "Bus price"
    if kind not in PRICE_KINDS:
        return f"{kind} is not a bus price"
    if obs.get("confidence") not in CONFIDENCE_WEIGHT:
        return "unrecognised confidence level"
    if kind != "Bus price" and not strat.include_estimates:
        return "workbook estimates switched off"
    qty = max(int(spec.quantity or 1), 1)
    if kind == "Workbook base range" and qty != 1:
        return "single-unit price, mission needs a volume buy"
    if kind == "Workbook volume proxy" and qty == 1:
        return "volume proxy, mission is a single unit"
    if kind == "Bus price" and obs.get("quantity") not in (None, qty):
        return f"priced for {obs.get('quantity')} units, mission needs {qty}"
    if obs.get("currency") not in ("USD", "EUR"):
        return f"no conversion rate for {obs.get('currency')}"
    if point_price(obs.get("low"), obs.get("high")) is None:
        return "no price value"
    return None


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def analyze(buses: list, observations: list, spec: MissionSpec, strat: Strategy) -> Analysis:
    """buses: catalog dicts. observations: price dicts with keys
    bus, config, low, high, currency, kind, quantity, confidence, specs, source."""
    by_id = {b["id"]: b for b in buses}
    per_bus: dict = {b["id"]: [] for b in buses}
    used, skipped = [], []

    for obs in observations:
        label = obs.get("config") or obs.get("item_name") or "?"
        bus = by_id.get(obs.get("bus"))
        if bus is None:
            skipped.append({"config": label, "reason": "not linked to a catalog bus"})
            continue
        reason = eligible(obs, spec, strat)
        if reason:
            skipped.append({"config": label, "reason": reason})
            continue
        merged, installed = _config_bus(bus, obs.get("specs") or {})
        fit = screen(merged, spec, installed_prop=installed)
        if fit.status == "fail":
            skipped.append({"config": label, "reason": "fails: " + "; ".join(fit.fail)})
            continue
        if fit.status == "review" and not strat.include_review:
            skipped.append({"config": label, "reason": "needs review (review fits excluded)"})
            continue
        price = to_musd(point_price(obs.get("low"), obs.get("high")), obs["currency"], strat.eur_usd)
        weight = CONFIDENCE_WEIGHT[obs["confidence"]] * FIT_WEIGHT[fit.status]
        row = {
            "bus": obs["bus"], "config": label, "price_musd": price, "weight": weight,
            "fit": fit.status, "review": fit.review, "currency": obs["currency"],
            "confidence": obs["confidence"], "kind": obs.get("kind"),
            "source": obs.get("source"), "nre": obs.get("nre"),
        }
        used.append(row)
        per_bus[obs["bus"]].append(row)

    # One price per bus (so a supplier with many configs doesn't dominate)
    bus_rows, bus_prices = [], []
    for b in buses:
        fit = screen(b, spec)
        rows = per_bus[b["id"]]
        price = weight = None
        if rows:
            price = weighted_median([(r["price_musd"], r["weight"]) for r in rows])
            weight = max(r["weight"] for r in rows)
            bus_prices.append((price, weight))
        bus_rows.append({
            "id": b["id"], "name": f'{b["company"]} {b["model"]}', "class": b.get("cls"),
            "status": fit.status, "fail": fit.fail, "review": fit.review, "notes": fit.notes,
            "price_musd": price, "weight": weight, "n_obs": len(rows),
        })

    a = Analysis(spec, strat, bus_rows, used, skipped)
    if bus_prices:
        vals = [p for p, _ in bus_prices]
        total_w = sum(w for _, w in bus_prices)
        a.low, a.high = min(vals), max(vals)
        a.median = weighted_median(bus_prices)
        a.average = sum(p * w for p, w in bus_prices) / total_w
        a.benchmark = {"low": a.low, "average": a.average}.get(strat.benchmark, a.median)
        est_w = sum(r["weight"] for r in used if r["confidence"] in ESTIMATE_CONFIDENCE)
        a.estimate_share = est_w / sum(r["weight"] for r in used)

        a.match = a.benchmark
        a.undercut = a.benchmark * (1 - strat.undercut_pct / 100)
        if strat.est_cost_musd and strat.min_margin_pct < 100:
            a.floor = strat.est_cost_musd / (1 - strat.min_margin_pct / 100)
        target = a.undercut if strat.mode == "undercut" else a.match
        a.recommended = max(target, a.floor or 0)
        if strat.est_cost_musd:
            a.margin_pct = (a.recommended - strat.est_cost_musd) / a.recommended * 100

    a.warnings = _warnings(a)
    return a


def _warnings(a: Analysis) -> list:
    w = []
    s = a.strategy
    if a.benchmark is None:
        return ["No priced competitor matches this mission yet. Loosen the spec, switch on "
                "workbook estimates, or add price evidence."]
    if a.n_priced < 3:
        w.append(f"Thin evidence: only {a.n_priced} priced competitor(s) match. "
                 "Treat the benchmark as indicative.")
    if a.estimate_share is not None and a.estimate_share > 0.5:
        w.append(f"{a.estimate_share:.0%} of the benchmark weight comes from workbook estimates, "
                 "not published prices.")
    if a.floor and a.floor > a.match:
        w.append("Your margin floor is above the market benchmark. Cut build cost, lower the "
                 "margin target, or sell on customisation rather than price.")
    elif a.floor and s.mode == "undercut" and a.undercut < a.floor:
        w.append("The undercut price falls below your margin floor, so the recommendation is "
                 "held at the floor.")
    if any(r["currency"] == "EUR" for r in a.used):
        w.append(f"EUR prices converted at {s.eur_usd:.3f} USD/EUR (assumption, update it).")
    if any(r.get("nre") == "excluded" for r in a.used):
        w.append("Some competitor prices exclude NRE; compare scope before undercutting.")
    return w


def scenarios(a: Analysis, undercuts=(0, 5, 10, 15, 20)) -> list:
    """What-if table: price and margin at several undercut levels."""
    if a.benchmark is None:
        return []
    out = []
    for u in undercuts:
        p = a.benchmark * (1 - u / 100)
        p = max(p, a.floor or 0)
        m = (p - a.strategy.est_cost_musd) / p * 100 if a.strategy.est_cost_musd else None
        out.append({"undercut_pct": u, "price_musd": p, "margin_pct": m,
                    "held_at_floor": bool(a.floor and a.benchmark * (1 - u / 100) < a.floor)})
    return out
