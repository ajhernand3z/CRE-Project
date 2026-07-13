"""Ingest parcels from the TCAD certified appraisal-roll export.

TCAD (Travis Central Appraisal District) publishes a free "certified
export" at https://traviscad.org/publicinformation — a zip of fixed-width
text files in the PACS "Appraisal Export" layout (the export header
reports 8.0.33 for the 2026 files). There is no stable API and the site
sits behind a bot filter, so the workflow is: download the zip in a
browser, then

    python -m app.ingest.tcad --file /path/to/export.zip

Files used (the archive contains ~20; the rest are tax/ARB data):

  *_APPRAISAL_INFO.TXT              one 9,922-char record per property+owner:
                                    situs address, state category code,
                                    land/improvement/market values, deed date
  *_APPRAISAL_IMPROVEMENT_DETAIL.TXT  one record per building segment:
                                    year built and segment square footage

Field positions below were derived from actual 2026 export records and
cross-checked (land value + improvement value = market value on every
sample). Layouts drift between versions — if a future export breaks, dump
a record with a position ruler and adjust the offset tables.

Two data caveats baked into the design:
* Texas is a NON-DISCLOSURE state: the export has NO sale prices. It does
  carry the last deed/transfer date. Rows load with last_sale_price=None
  and the valuation layer falls back to appraised market value.
* Building square footage lives in per-segment improvement details; we sum
  the floor-type segments (1ST, 2ND, ...) and ignore site work like paving.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.config import STATE_CODE_TO_TYPE, submarket_for_zip
from app.database import SessionLocal, init_db
from app.models import Property

# --- PACS fixed-width layout (verified against 2026 TCAD export) --------------

INFO_RECORD_LENGTH = 9922

# (start, end) character slices into an APPRAISAL_INFO record.
INFO = {
    "prop_id":      (0, 12),
    "prop_type":    (12, 17),     # 'R' = real property
    "year":         (18, 22),
    "geo_id":       (546, 556),
    "situs_prefix": (1039, 1049),  # e.g. 'S'
    "situs_street": (1049, 1099),  # e.g. 'LAMAR'
    "situs_suffix": (1099, 1139),  # e.g. 'BLVD'
    "situs_zip":    (1139, 1144),
    "acreage":      (1659, 1675),  # 4 implied decimals (00005399 = 0.5399 ac)
    "land_val":     (1810, 1825),
    "imprv_val":    (1840, 1855),
    "market_val":   (1945, 1960),
    "deed_date":    (2033, 2043),  # MM-DD-YYYY, blank if none
    "state_cd":     (2741, 2751),  # PTD state category, e.g. F1 / B1
    "situs_num":    (4448, 4463),  # street number lives in this later block
}

# Slices into an APPRAISAL_IMPROVEMENT_DETAIL record (622 chars).
DETAIL = {
    "prop_id": (0, 12),
    "type_cd": (40, 50),    # '1ST', '2ND', '551' (paving), '501' (canopy)...
    "desc":    (50, 75),    # '1st Floor', 'PAVED AREA', ...
    "yr_built": (85, 89),
    "area":    (93, 105),   # square feet, 3 implied decimals after the '.'
    "value":   (105, 119),  # segment value; 0 for site work like paving
}

# Segment types that count as building floor area (vs paving, canopies,
# terraces, pools...). Floors are coded 1ST/2ND/3RD/... in TCAD's data.
FLOOR_TYPE_RE = re.compile(r"^\d+(ST|ND|RD|TH)$")
FLOOR_DESC_RE = re.compile(r"floor|main area", re.IGNORECASE)


def _cut(record: str, name: str, table: dict) -> str:
    start, end = table[name]
    return record[start:end].strip()


def _to_int(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.strip())
    except ValueError:
        return None


@dataclass
class ParcelDraft:
    """Accumulates one property across the two export files."""

    parcel_id: str
    address: str
    zip_code: str | None
    property_type: str
    state_cd: str
    market_value: float
    land_sqft: float | None
    deed_date: date | None
    building_sqft: float = 0.0
    year_built: int | None = None
    # fallback when no floor-type segments exist: largest valued segment
    best_other_area: float = 0.0
    best_other_value: float = 0.0
    best_other_year: int | None = None


def _members(zf: zipfile.ZipFile, suffix: str) -> list[zipfile.ZipInfo]:
    return [i for i in zf.infolist() if i.filename.upper().endswith(suffix)]


def _lines(zf: zipfile.ZipFile, info: zipfile.ZipInfo):
    with zf.open(info) as raw:
        for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
            yield line.rstrip("\r\n")


def _parse_deed_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m-%d-%Y").date()
    except ValueError:
        return None


def pass_properties(zf: zipfile.ZipFile, member: zipfile.ZipInfo) -> dict[str, ParcelDraft]:
    """Scan APPRAISAL_INFO and keep the commercial universe (B1/B2/F1/F2)."""
    drafts: dict[str, ParcelDraft] = {}
    seen = 0
    warned_length = False
    for rec in _lines(zf, member):
        seen += 1
        if seen % 500_000 == 0:
            print(f"  properties scanned: {seen:,} ({len(drafts):,} commercial)")
        if len(rec) < INFO["state_cd"][1]:
            continue
        if not warned_length and len(rec) != INFO_RECORD_LENGTH:
            print(
                f"  note: record length {len(rec)} != expected {INFO_RECORD_LENGTH} — "
                "layout may have drifted; verify a few parcels after loading."
            )
            warned_length = True

        state_cd = _cut(rec, "state_cd", INFO)[:2]
        prop_type = STATE_CODE_TO_TYPE.get(state_cd)
        if prop_type is None or _cut(rec, "prop_type", INFO) != "R":
            continue

        prop_id = _cut(rec, "prop_id", INFO)
        market = _to_int(rec[slice(*INFO["market_val"])])
        if not prop_id or not market:
            continue
        if prop_id in drafts:  # one record per property+owner — dedupe
            continue

        address = " ".join(
            part for part in (
                _cut(rec, "situs_num", INFO),
                _cut(rec, "situs_prefix", INFO),
                _cut(rec, "situs_street", INFO),
                _cut(rec, "situs_suffix", INFO),
            ) if part
        )
        if not address:
            continue  # can't run location-based comps without a situs

        acreage_raw = _to_int(rec[slice(*INFO["acreage"])])
        land_sqft = (acreage_raw / 10_000) * 43_560 if acreage_raw else None

        zip_code = _cut(rec, "situs_zip", INFO) or None
        drafts[prop_id] = ParcelDraft(
            parcel_id=prop_id.lstrip("0"),
            address=address.title(),
            zip_code=zip_code,
            property_type=prop_type,
            state_cd=state_cd,
            market_value=float(market),
            land_sqft=land_sqft,
            deed_date=_parse_deed_date(rec[slice(*INFO["deed_date"])]),
        )
    print(f"  properties scanned: {seen:,} — kept {len(drafts):,} commercial parcels")
    return drafts


def pass_improvements(zf: zipfile.ZipFile, member: zipfile.ZipInfo,
                      drafts: dict[str, ParcelDraft]) -> None:
    """Scan IMPROVEMENT_DETAIL, summing floor areas and taking the earliest
    build year for each commercial parcel."""
    seen = 0
    for rec in _lines(zf, member):
        seen += 1
        if seen % 1_000_000 == 0:
            print(f"  improvement segments scanned: {seen:,}")
        draft = drafts.get(rec[0:12])
        if draft is None or len(rec) < DETAIL["value"][1]:
            continue

        type_cd = _cut(rec, "type_cd", DETAIL)
        desc = _cut(rec, "desc", DETAIL)
        area = _to_float(rec[slice(*DETAIL["area"])]) or 0.0
        value = _to_float(rec[slice(*DETAIL["value"])]) or 0.0
        year = _to_int(rec[slice(*DETAIL["yr_built"])])
        if year is not None and not (1800 <= year <= 2100):
            year = None

        if FLOOR_TYPE_RE.match(type_cd) or FLOOR_DESC_RE.search(desc):
            draft.building_sqft += area
            if year and (draft.year_built is None or year < draft.year_built):
                draft.year_built = year
        elif value > 0 and area > draft.best_other_area:
            # Remember the biggest non-floor building segment as a fallback
            # for parcels whose details don't use floor codes.
            draft.best_other_area = area
            draft.best_other_value = value
            draft.best_other_year = year


def load_drafts(drafts: dict[str, ParcelDraft], session: Session) -> int:
    ingested = 0
    for d in drafts.values():
        sqft = d.building_sqft or d.best_other_area or None
        year = d.year_built or d.best_other_year
        record = dict(
            address=d.address,
            city="Austin" if d.zip_code and d.zip_code.startswith("787") else None,
            zip_code=d.zip_code,
            submarket=submarket_for_zip(d.zip_code),
            property_type=d.property_type,
            state_category_code=d.state_cd,
            building_sqft=sqft,
            land_sqft=d.land_sqft,
            year_built=year,
            market_value=d.market_value,
            last_sale_price=None,           # non-disclosure state
            last_sale_date=d.deed_date,     # transfer date only, no price
            source="tcad",
        )
        existing = session.query(Property).filter_by(parcel_id=d.parcel_id).first()
        if existing:
            for k, v in record.items():
                setattr(existing, k, v)
        else:
            session.add(Property(parcel_id=d.parcel_id, **record))
        ingested += 1
        if ingested % 2_000 == 0:
            session.commit()
    session.commit()
    return ingested


def ingest_pacs_archive(path: Path, session: Session) -> int:
    zf = zipfile.ZipFile(path)
    info_members = _members(zf, "_APPRAISAL_INFO.TXT")
    detail_members = _members(zf, "_APPRAISAL_IMPROVEMENT_DETAIL.TXT")
    if not info_members:
        raise SystemExit("No *_APPRAISAL_INFO.TXT found in the archive.")

    print(f"Pass 1/3: reading {info_members[0].filename} "
          f"({info_members[0].file_size / 1e9:.1f} GB uncompressed)...")
    drafts = pass_properties(zf, info_members[0])

    if detail_members:
        print(f"Pass 2/3: reading {detail_members[0].filename} "
              f"({detail_members[0].file_size / 1e9:.1f} GB uncompressed)...")
        pass_improvements(zf, detail_members[0], drafts)
    else:
        print("Pass 2/3 skipped: no IMPROVEMENT_DETAIL file (no sqft/year built).")

    print("Pass 3/3: writing to the database...")
    return load_drafts(drafts, session)


# --- Legacy path: simple delimited file with a header row ---------------------
# Kept for older/alternate exports that ship as a single CSV-like file.

FIELD_ALIASES: dict[str, list[str]] = {
    "parcel_id": ["prop_id", "pid", "account", "account_num", "geo_id"],
    "address": ["situs", "situs_address", "situs_display", "situs_street"],
    "city": ["situs_city", "city"],
    "zip_code": ["situs_zip", "zip", "situs_zip_code"],
    "state_category_code": ["state_cd", "state_code", "ptd_code", "sptb_code"],
    "building_sqft": ["imprv_sqft", "living_area", "bldg_sqft", "imp_sqft", "main_area"],
    "land_sqft": ["land_sqft", "land_size"],
    "year_built": ["yr_built", "year_built", "actual_year_built", "eff_yr_built"],
    "market_value": ["market_value", "market", "total_market", "appraised_val", "market_val"],
    "last_sale_date": ["deed_dt", "deed_date", "sale_dt", "transfer_date"],
}


def parse_delimited(stream: io.TextIOBase, session: Session) -> int:
    sample = stream.read(8192)
    stream.seek(0)
    dialect = csv.Sniffer().sniff(sample, delimiters="|\t,")
    reader = csv.DictReader(stream, dialect=dialect)
    lower = {h.lower().strip(): h for h in (reader.fieldnames or [])}
    columns = {
        f: lower[a] for f, aliases in FIELD_ALIASES.items()
        for a in aliases if a in lower
    }
    missing = {"parcel_id", "state_category_code", "market_value"} - set(columns)
    if missing:
        raise SystemExit(
            f"Could not locate required columns {sorted(missing)}; header row: "
            f"{reader.fieldnames}"
        )

    ingested = 0
    for row in reader:
        get = lambda f: (row.get(columns[f]) or "").strip() if f in columns else ""
        state_cd = get("state_category_code").upper()[:2]
        prop_type = STATE_CODE_TO_TYPE.get(state_cd)
        market = _to_float(get("market_value").replace(",", "").replace("$", ""))
        if prop_type is None or not get("parcel_id") or not get("address") or not market:
            continue
        zip_code = get("zip_code")[:5] or None
        record = dict(
            address=get("address"), city=get("city") or None, zip_code=zip_code,
            submarket=submarket_for_zip(zip_code), property_type=prop_type,
            state_category_code=state_cd,
            building_sqft=_to_float(get("building_sqft")),
            land_sqft=_to_float(get("land_sqft")),
            year_built=_to_int(get("year_built")[:4]),
            market_value=market, last_sale_price=None, source="tcad",
        )
        pid = get("parcel_id")
        existing = session.query(Property).filter_by(parcel_id=pid).first()
        if existing:
            for k, v in record.items():
                setattr(existing, k, v)
        else:
            session.add(Property(parcel_id=pid, **record))
        ingested += 1
    session.commit()
    return ingested


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="Local export zip (or delimited txt/csv)")
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
        if zipfile.is_zipfile(path) and _members(zipfile.ZipFile(path), "_APPRAISAL_INFO.TXT"):
            count = ingest_pacs_archive(path, session)
        else:
            with open(path, encoding="latin-1") as stream:
                count = parse_delimited(stream, session)
        print(f"Done: ingested {count:,} commercial parcels.")
    finally:
        session.close()


if __name__ == "__main__":
    main(sys.argv[1:])
