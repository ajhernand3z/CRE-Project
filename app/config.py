"""Application configuration.

Everything an appraiser or reviewer might want to tune lives here:
data-source settings, submarket definitions, and the underwriting
assumptions used for pro forma NOI / cap-rate estimation.
"""

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = os.environ.get("CRE_DB_PATH", str(DATA_DIR / "comps.db"))
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# --- Data sources ----------------------------------------------------------

# TCAD publishes a certified appraisal-roll export (zipped delimited text)
# at https://traviscad.org/publicinformation. The exact file name changes
# each year, so the ingest script takes the URL / local path as an argument.
TCAD_EXPORT_URL = os.environ.get("TCAD_EXPORT_URL", "")

# Regrid parcel API fallback (https://regrid.com). Requires an API token.
REGRID_API_TOKEN = os.environ.get("REGRID_API_TOKEN", "")
REGRID_API_BASE = "https://app.regrid.com/api/v2"

# Anthropic API for the narrative layer.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# --- Comp search parameters --------------------------------------------------

# Comps must be within +/- this fraction of the subject's building SF.
SQFT_TOLERANCE = 0.30
# Sales older than this are excluded from the sale-based comp set.
SALE_WINDOW_MONTHS = 24
MIN_COMPS = 5
MAX_COMPS = 10

# --- Austin / Travis County submarkets ---------------------------------------
#
# TCAD data does not carry a "submarket" field, so we approximate the
# submarkets brokers actually use (CoStar-style) with ZIP-code groupings.
# Coarse, but transparent and easy to defend: a comp in the same ZIP group
# shares the same general location drivers (rents, land values, tenant base).

SUBMARKETS: dict[str, list[str]] = {
    "CBD / Downtown": ["78701"],
    "East Austin": ["78702", "78721", "78722", "78723"],
    "South Central": ["78704", "78745", "78748"],
    "Southeast": ["78741", "78744", "78747"],
    "North Central": ["78751", "78752", "78753", "78756", "78757"],
    "Northwest / Domain": ["78758", "78759", "78727", "78729"],
    "West / Westlake": ["78703", "78731", "78733", "78746"],
    "Round Rock / Pflugerville": ["78660", "78664", "78681"],
    "Far Northwest / Cedar Park": ["78613", "78717", "78750"],
    "Airport / Del Valle": ["78617", "78719", "78742"],
}

ZIP_TO_SUBMARKET: dict[str, str] = {
    z: name for name, zips in SUBMARKETS.items() for z in zips
}


def submarket_for_zip(zip_code: str | None) -> str:
    """Map a ZIP code to a named submarket; unknown ZIPs get a catch-all."""
    if not zip_code:
        return "Other Travis County"
    return ZIP_TO_SUBMARKET.get(zip_code.strip()[:5], "Other Travis County")


# --- Texas PTAD state category codes -----------------------------------------
#
# Appraisal rolls classify parcels with Comptroller "state category" codes.
# We only ingest the commercial ones and normalize them to plain-English
# property types the rest of the app filters on.

STATE_CODE_TO_TYPE: dict[str, str] = {
    "B1": "multifamily",   # multifamily (5+ units)
    "B2": "multifamily",   # duplex/triplex/fourplex
    "F1": "commercial",    # commercial real property (office/retail/etc.)
    "F2": "industrial",    # industrial real property
}

# --- Pro forma NOI assumptions ------------------------------------------------
#
# Texas is a NON-DISCLOSURE state: neither TCAD nor Regrid publishes actual
# rents or operating statements, so cap rates here are PRO FORMA ESTIMATES.
# NOI is modeled as:
#
#     NOI = building_sf * market_rent_psf * (1 - vacancy) * (1 - expense_ratio)
#
# i.e. gross potential rent, less a vacancy allowance, less operating
# expenses as a share of effective gross income. The assumptions below are
# placeholder market-level figures for Austin; in a real underwriting they
# would be replaced with the property's trailing-12 financials.

NOI_ASSUMPTIONS: dict[str, dict[str, float]] = {
    # rent_psf = annual market rent per building SF (NNN-equivalent gross-up)
    "office":      {"rent_psf": 32.0, "vacancy": 0.16, "expense_ratio": 0.42},
    "retail":      {"rent_psf": 28.0, "vacancy": 0.06, "expense_ratio": 0.30},
    "industrial":  {"rent_psf": 14.0, "vacancy": 0.08, "expense_ratio": 0.25},
    "multifamily": {"rent_psf": 21.0, "vacancy": 0.07, "expense_ratio": 0.45},
    "commercial":  {"rent_psf": 26.0, "vacancy": 0.10, "expense_ratio": 0.35},
    "mixed_use":   {"rent_psf": 26.0, "vacancy": 0.10, "expense_ratio": 0.38},
}
