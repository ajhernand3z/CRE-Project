"""Ingest parcels from the TCAD certified appraisal-roll export.

TCAD (Travis Central Appraisal District) publishes a free "certified
export" — a zip of delimited text files — at
https://traviscad.org/publicinformation. There is no stable API, and the
site sits behind a bot filter, so the intended workflow is:

    1. Download the current certified export zip in a browser.
    2. Run:  python -m app.ingest.tcad --file /path/to/export.zip
       (or --url <direct link> if you have one that allows scripted access)

Two practical caveats this module is built around:

* The column layout changes between years (TCAD migrated from PACS to
  TrueProdigy in 2024), so field names are resolved through FIELD_ALIASES
  below rather than hard-coded positions. If a year's export renames a
  column, add the new name to the alias list — no code changes needed.
* Texas is a NON-DISCLOSURE state: the export has NO sale prices. It does
  carry deed/transfer dates on some layouts. Rows are ingested with
  last_sale_price = None and the valuation layer falls back to TCAD's
  appraised market value.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import STATE_CODE_TO_TYPE, submarket_for_zip
from app.database import SessionLocal, init_db
from app.models import Property

# Map our canonical field -> candidate column names seen across TCAD export
# vintages (case-insensitive). First match wins.
FIELD_ALIASES: dict[str, list[str]] = {
    "parcel_id": ["prop_id", "pid", "account", "account_num", "geo_id"],
    "address": ["situs", "situs_address", "situs_display", "situs_street"],
    "city": ["situs_city", "city"],
    "zip_code": ["situs_zip", "zip", "situs_zip_code"],
    "state_category_code": ["state_cd", "state_code", "ptd_code", "sptb_code"],
    "building_sqft": ["imprv_sqft", "living_area", "bldg_sqft", "imp_sqft", "main_area"],
    "land_sqft": ["land_sqft", "land_size", "land_acres_sqft"],
    "year_built": ["yr_built", "year_built", "actual_year_built", "eff_yr_built"],
    "market_value": ["market_value", "market", "total_market", "appraised_val", "market_val"],
    "last_sale_date": ["deed_dt", "deed_date", "sale_dt", "transfer_date"],
}


def _resolve_columns(header: list[str]) -> dict[str, str]:
    """Match canonical fields to actual columns in this export's header."""
    lower = {h.lower().strip(): h for h in header}
    resolved = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                resolved[field] = lower[alias]
                break
    return resolved


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        cleaned = value.replace(",", "").replace("$", "").strip()
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    f = _to_float(value)
    return int(f) if f else None


def parse_export(stream: io.TextIOBase, session: Session) -> tuple[int, int]:
    """Parse a delimited TCAD property file and upsert commercial parcels.

    Returns (rows_seen, rows_ingested). Rows that aren't commercial, or are
    missing the fields the comp engine can't work without (parcel id,
    address, market value), are skipped rather than half-ingested.
    """
    sample = stream.read(8192)
    stream.seek(0)
    # Export delimiter varies by vintage (pipe, tab, comma) — sniff it.
    dialect = csv.Sniffer().sniff(sample, delimiters="|\t,")
    reader = csv.DictReader(stream, dialect=dialect)
    columns = _resolve_columns(reader.fieldnames or [])

    missing = {"parcel_id", "state_category_code", "market_value"} - set(columns)
    if missing:
        raise SystemExit(
            f"Could not locate required columns {sorted(missing)} in export header. "
            f"Add this vintage's column names to FIELD_ALIASES in {__file__}."
        )

    seen = ingested = 0
    for row in reader:
        seen += 1
        get = lambda f: (row.get(columns[f]) or "").strip() if f in columns else ""

        state_code = get("state_category_code").upper()[:2]
        prop_type = STATE_CODE_TO_TYPE.get(state_code)
        if prop_type is None:  # residential/land/exempt — not our universe
            continue

        parcel_id, address = get("parcel_id"), get("address")
        market_value = _to_float(get("market_value"))
        if not parcel_id or not address or not market_value:
            continue  # can't underwrite a comp without identity + value

        zip_code = get("zip_code")[:5] or None
        record = dict(
            address=address,
            city=get("city") or None,
            zip_code=zip_code,
            submarket=submarket_for_zip(zip_code),
            property_type=prop_type,
            state_category_code=state_code,
            building_sqft=_to_float(get("building_sqft")),
            land_sqft=_to_float(get("land_sqft")),
            year_built=_to_int(get("year_built")),
            market_value=market_value,
            last_sale_price=None,  # non-disclosure state: never in the export
            source="tcad",
        )

        existing = session.query(Property).filter_by(parcel_id=parcel_id).first()
        if existing:
            for k, v in record.items():
                setattr(existing, k, v)
        else:
            session.add(Property(parcel_id=parcel_id, **record))
        ingested += 1
        if ingested % 5000 == 0:
            session.commit()

    session.commit()
    return seen, ingested


def _open_property_file(path: Path) -> io.TextIOBase:
    """Open the property file, transparently handling a zip archive.

    In a zip we pick the largest .txt/.csv entry — in every TCAD export
    vintage that's the main property table.
    """
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        candidates = [
            i for i in zf.infolist()
            if i.filename.lower().endswith((".txt", ".csv")) and not i.is_dir()
        ]
        if not candidates:
            raise SystemExit("No .txt/.csv files found inside the zip export.")
        main = max(candidates, key=lambda i: i.file_size)
        print(f"Using {main.filename} ({main.file_size / 1e6:.1f} MB) from archive")
        return io.TextIOWrapper(zf.open(main), encoding="latin-1")
    return open(path, encoding="latin-1")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="Local export zip or txt/csv file")
    src.add_argument("--url", help="Direct download URL for the export zip")
    args = parser.parse_args(argv)

    init_db()

    path = args.file
    if args.url:
        path = Path("data/tcad_export.zip")
        print(f"Downloading {args.url} ...")
        with httpx.stream("GET", args.url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)

    session = SessionLocal()
    try:
        with _open_property_file(path) as stream:
            seen, ingested = parse_export(stream, session)
        print(f"Done: scanned {seen:,} rows, ingested {ingested:,} commercial parcels.")
    finally:
        session.close()


if __name__ == "__main__":
    main(sys.argv[1:])
