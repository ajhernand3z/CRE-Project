"""Fallback ingest: Regrid parcel API (https://regrid.com).

Used when the TCAD export isn't practical (e.g. running in an environment
that can't reach traviscad.org). Requires a Regrid API token:

    export REGRID_API_TOKEN=...
    python -m app.ingest.regrid --zip 78702 78704 --limit 500

Notes:
* Regrid normalizes county assessor data into one schema; the field names
  used below ('parcelnumb', 'yearbuilt', 'saleprice', ...) are Regrid's
  standard schema columns.
* Even Regrid usually has NO sale price for Texas parcels (non-disclosure
  state) — expect saleprice to be null and the app to fall back to
  assessed market value, exactly as with TCAD data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import httpx

from app.config import REGRID_API_BASE, REGRID_API_TOKEN, submarket_for_zip
from app.database import SessionLocal, init_db
from app.models import Property

# Regrid 'usedesc' values → our normalized property types. Anything not
# matched is skipped (residential, vacant land, exempt, ...).
USEDESC_TO_TYPE = {
    "office": "office",
    "retail": "retail",
    "commercial": "commercial",
    "industrial": "industrial",
    "warehouse": "industrial",
    "apartment": "multifamily",
    "multi-family": "multifamily",
    "multifamily": "multifamily",
}


def classify(usedesc: str | None) -> str | None:
    if not usedesc:
        return None
    text = usedesc.lower()
    for keyword, prop_type in USEDESC_TO_TYPE.items():
        if keyword in text:
            return prop_type
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def fetch_zip(client: httpx.Client, zip_code: str, limit: int) -> list[dict]:
    """Pull parcel records for one ZIP from Regrid's query endpoint."""
    resp = client.get(
        f"{REGRID_API_BASE}/parcels/query",
        params={
            "parcel[szip]": zip_code,
            "parcel[scounty]": "travis",
            "parcel[sstate]": "TX",
            "limit": limit,
            "token": REGRID_API_TOKEN,
        },
        timeout=60,
    )
    resp.raise_for_status()
    features = resp.json().get("parcels", {}).get("features", [])
    return [f.get("properties", {}).get("fields", {}) for f in features]


def ingest_records(records: list[dict], session) -> int:
    ingested = 0
    for f in records:
        prop_type = classify(f.get("usedesc"))
        parcel_id = f.get("parcelnumb")
        address = f.get("address")
        market_value = f.get("parval")  # assessed/appraised parcel value
        if not (prop_type and parcel_id and address and market_value):
            continue

        zip_code = (f.get("szip") or "")[:5] or None
        record = dict(
            address=address,
            city=f.get("scity"),
            zip_code=zip_code,
            submarket=submarket_for_zip(zip_code),
            property_type=prop_type,
            state_category_code=None,
            building_sqft=f.get("improvementsqft") or f.get("sqft"),
            land_sqft=f.get("ll_gissqft"),
            year_built=f.get("yearbuilt"),
            units=f.get("numunits"),
            market_value=float(market_value),
            last_sale_price=float(f["saleprice"]) if f.get("saleprice") else None,
            last_sale_date=_parse_date(f.get("saledate")),
            source="regrid",
        )
        existing = session.query(Property).filter_by(parcel_id=parcel_id).first()
        if existing:
            for k, v in record.items():
                setattr(existing, k, v)
        else:
            session.add(Property(parcel_id=parcel_id, **record))
        ingested += 1
    session.commit()
    return ingested


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", nargs="+", required=True, help="ZIP codes to pull")
    parser.add_argument("--limit", type=int, default=500, help="Max parcels per ZIP")
    args = parser.parse_args(argv)

    if not REGRID_API_TOKEN:
        raise SystemExit("Set REGRID_API_TOKEN to use the Regrid fallback.")

    init_db()
    session = SessionLocal()
    total = 0
    try:
        with httpx.Client() as client:
            for zip_code in args.zip:
                records = fetch_zip(client, zip_code, args.limit)
                count = ingest_records(records, session)
                print(f"{zip_code}: ingested {count} commercial parcels")
                total += count
    finally:
        session.close()
    print(f"Done: {total} parcels total.")


if __name__ == "__main__":
    main(sys.argv[1:])
