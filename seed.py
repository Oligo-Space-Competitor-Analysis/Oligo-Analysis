"""Load the Week-4 competitor research (data/seed_research.json) into the database.

    python seed.py            # upsert bus_catalog + historical price evidence
    python seed.py --dry-run  # just report what would be written

`load_seed()` has no database dependency, so the Streamlit app also uses it for
demo mode when no DATABASE_URL is configured.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

SEED_PATH = Path(__file__).parent / "data" / "seed_research.json"
SUPPLIER_OVERRIDE = {"nano-spinlaunch-meridian": "Kongsberg NanoAvionics"}


def source_type(kind: str) -> str:
    """Human label for where a price came from."""
    if kind == "Bus price":
        return "Public pricing (configurator)"
    if kind == "Contract context":
        return "Contract announcement"
    if kind in ("Hosted fee", "Annual service"):
        return "Published service fee"
    if kind.startswith("Workbook"):
        return "Research workbook estimate"
    return kind


def _lohi(x):
    return (x[0], x[1]) if x else (None, None)


def load_seed(path: Path = SEED_PATH):
    """Return (bus_rows, price_rows) ready for the DB layer."""
    d = json.loads(path.read_text())
    buses = {b["id"]: b for b in d["buses"]}

    bus_rows = []
    for b in d["buses"]:
        pl, ph = _lohi(b["payload"]); wl, wh = _lohi(b["power"])
        pb, pbest = _lohi(b["pointing"]); l0, l1 = _lohi(b["lead"])
        bus_rows.append(dict(
            bus_id=b["id"], company=b["company"], model=b["model"], size_class=b["cls"],
            payload_lo=pl, payload_hi=ph, power_lo=wl, power_hi=wh,
            pointing_base=pb, pointing_best=pbest, lead_min=l0, lead_max=l1,
            propulsion=b["prop"] or "", flags=b.get("flags") or [],
            form_factors=b.get("formFactors") or [], confidence=b.get("confidence")))

    price_rows = []
    for e in d["price_evidence"]:
        low, high = e.get("low"), e.get("high")
        point = low if high is None else high if low is None else (low + high) / 2
        if not point:
            continue
        kind = e["kind"]
        name = e["config"] if kind == "Bus price" else f'{e["config"]} · {kind}'
        day = e.get("accessed") or e.get("recorded") or e.get("published") or d["snapshot"]
        bus = buses.get(e.get("bus"))
        price_rows.append(dict(
            timestamp=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
            observed_on=day, state="ALL", item_name=name, price=point, currency=e["currency"],
            price_low=low, price_high=high, bus_id=e.get("bus"),
            supplier=(bus["company"] if bus else SUPPLIER_OVERRIDE.get(e["id"]) or e.get("supplier")),
            kind=kind, price_basis=e.get("priceBasis") or "unknown", quantity=e.get("quantity"),
            nre=e.get("nre") or "unknown", confidence=e["confidence"],
            source_url=e.get("source") or "", specs=e.get("specs") or {},
            source_title=e.get("sourceTitle") or "", source_type=source_type(kind)))
    return bus_rows, price_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    bus_rows, price_rows = load_seed()
    print(f"{len(bus_rows)} buses, {len(price_rows)} price observations")
    if args.dry_run:
        return
    from scraper import connect, upsert_buses, upsert_prices  # lazy: needs psycopg2
    with connect() as conn:
        upsert_buses(conn, bus_rows)
        upsert_prices(conn, price_rows)
    print("Seed complete.")


if __name__ == "__main__":
    main()
