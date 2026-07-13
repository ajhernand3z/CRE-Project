"""Tests for the TCAD PACS fixed-width ingest.

Fixture records are built by placing the known values of two real 2026
export parcels (prop 100008 "Odd Duck" and 100012 "Gibson Flats", both on
S Lamar Blvd) at the layout offsets — so these tests fail if the offset
tables drift from the values verified against the actual export.
"""

import io
import zipfile

import pytest

from app.ingest.tcad import (
    DETAIL,
    INFO,
    INFO_RECORD_LENGTH,
    ingest_pacs_archive,
    load_drafts,
    pass_improvements,
    pass_properties,
)
from app.models import Property


# Fields that are zero-padded right-justified numerics in the real export;
# everything else (including street numbers) is space-padded text.
ZERO_PADDED = {"prop_id", "acreage", "market_val", "land_val", "imprv_val",
               "yr_built", "area", "value"}


def make_record(length: int, table: dict, values: dict[str, str]) -> str:
    """Place field values at their layout offsets in a space-padded record."""
    rec = [" "] * length
    for name, value in values.items():
        start, end = table[name]
        width = end - start
        padded = value.rjust(width, "0") if name in ZERO_PADDED else value.ljust(width)
        assert len(padded) == width, f"{name}: {value!r} doesn't fit"
        rec[start:end] = padded
    return "".join(rec)


def info_record(prop_id, state_cd, market, acreage, num, street, suffix="BLVD",
                prefix="S", zip_code="78704", deed="12-31-2013") -> str:
    return make_record(INFO_RECORD_LENGTH, INFO, {
        "prop_id": prop_id.rjust(12, "0"),
        "prop_type": "R",
        "year": "2026",
        "situs_num": num,
        "situs_prefix": prefix,
        "situs_street": street,
        "situs_suffix": suffix,
        "situs_zip": zip_code,
        "acreage": acreage,
        "market_val": market,
        "deed_date": deed,
        "state_cd": state_cd,
    })


def detail_record(prop_id, type_cd, desc, year, area, value) -> str:
    return make_record(622, DETAIL, {
        "prop_id": prop_id.rjust(12, "0"),
        "type_cd": type_cd,
        "desc": desc,
        "yr_built": year,
        "area": area,   # e.g. "00002986.000"
        "value": value,
    })


@pytest.fixture()
def sample_zip(tmp_path):
    """A miniature export archive mirroring the real 2026 file structure."""
    info_lines = "\r\n".join([
        # Odd Duck: F1 commercial, $4,336,640, 0.5399 ac, 1201 S Lamar Blvd
        info_record("100008", "F1", "4336640", "5399", "1201", "LAMAR"),
        # Gibson Flats: B1 multifamily, $55,410,000, 2.3553 ac, 1219 S Lamar
        info_record("100012", "B1", "55410000", "23553", "1219", "LAMAR",
                    deed="11-22-2011"),
        # A single-family home (A1) that must be filtered out
        info_record("200000", "A1", "750000", "2000", "500", "MAPLE", suffix="AVE"),
    ]) + "\r\n"

    detail_lines = "\r\n".join([
        detail_record("100008", "551", "PAVED AREA", "1984", "00017100.000", "0000000.000000"),
        detail_record("100008", "1ST", "1st Floor", "2013", "00002986.000", "0084611.000000"),
        detail_record("100008", "501", "CANOPY", "2013", "00000402.000", "0001518.000000"),
        detail_record("100012", "1ST", "1st Floor", "2015", "00040000.000", "9000000.000000"),
        detail_record("100012", "2ND", "2nd Floor", "2015", "00040000.000", "9000000.000000"),
    ]) + "\r\n"

    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("2026-07-08_2026_APPRAISAL_INFO.TXT", info_lines)
        zf.writestr("2026-07-08_2026_APPRAISAL_IMPROVEMENT_DETAIL.TXT", detail_lines)
    return path


def test_pass_properties_extracts_verified_fields(sample_zip):
    zf = zipfile.ZipFile(sample_zip)
    drafts = pass_properties(zf, zf.infolist()[0])

    assert set(drafts) == {"000000100008", "000000100012"}  # A1 home excluded
    odd_duck = drafts["000000100008"]
    assert odd_duck.parcel_id == "100008"
    assert odd_duck.address == "1201 S Lamar Blvd"
    assert odd_duck.zip_code == "78704"
    assert odd_duck.property_type == "commercial"
    assert odd_duck.market_value == 4_336_640
    # 0.5399 acres -> square feet
    assert odd_duck.land_sqft == pytest.approx(0.5399 * 43_560)
    assert odd_duck.deed_date.isoformat() == "2013-12-31"
    assert drafts["000000100012"].property_type == "multifamily"


def test_pass_improvements_sums_floors_only(sample_zip):
    zf = zipfile.ZipFile(sample_zip)
    drafts = pass_properties(zf, zf.infolist()[0])
    pass_improvements(zf, zf.infolist()[1], drafts)

    # Odd Duck: only the 1st-floor segment counts (2,986 SF), not the
    # 17,100 SF of paving or the canopy.
    assert drafts["000000100008"].building_sqft == pytest.approx(2986.0)
    assert drafts["000000100008"].year_built == 2013
    # Gibson Flats: two floors sum.
    assert drafts["000000100012"].building_sqft == pytest.approx(80_000.0)


def test_full_archive_ingest_writes_properties(sample_zip, db):
    count = ingest_pacs_archive(sample_zip, db)
    assert count == 2
    row = db.query(Property).filter_by(parcel_id="100008").one()
    assert row.source == "tcad"
    assert row.building_sqft == pytest.approx(2986.0)
    assert row.last_sale_price is None          # non-disclosure state
    assert row.last_sale_date is not None       # deed date only
    assert row.submarket == "South Central"     # 78704
