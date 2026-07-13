"""Comp search: find candidate comparables for a subject property.

Filter hierarchy (each step is relaxed only if the previous one can't
produce enough comps, and every relaxation is reported back to the user):

    1. Same property type (with 'commercial' as a compatible generic)
    2. Building SF within +/- SQFT_TOLERANCE (30%) of the subject
    3. Same submarket  ->  widened to all of Travis County if thin
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import SQFT_TOLERANCE, MIN_COMPS
from app.models import Property

# TCAD's F1 code lumps office/retail/mixed-use together as generic
# 'commercial', so a generic comp can stand in for a specific type (and
# vice versa) when the data doesn't distinguish them.
COMPATIBLE_TYPES: dict[str, set[str]] = {
    "office": {"office", "commercial"},
    "retail": {"retail", "commercial"},
    "mixed_use": {"mixed_use", "commercial", "office", "retail"},
    "commercial": {"commercial", "office", "retail", "mixed_use"},
    "industrial": {"industrial"},
    "multifamily": {"multifamily"},
}


def find_candidates(
    session: Session,
    property_type: str,
    building_sqft: float,
    submarket: str,
    exclude_parcel_id: str | None = None,
) -> tuple[list[Property], list[str]]:
    """Return (candidates, search_notes).

    Candidates are unadjusted DB rows; the adjustment layer scores and
    ranks them. Notes record any widening so the results page can be
    honest about search quality.
    """
    notes: list[str] = []
    types = COMPATIBLE_TYPES.get(property_type, {property_type})
    lo = building_sqft * (1 - SQFT_TOLERANCE)
    hi = building_sqft * (1 + SQFT_TOLERANCE)

    base = (
        session.query(Property)
        .filter(Property.property_type.in_(types))
        .filter(Property.building_sqft.isnot(None))
        .filter(Property.building_sqft.between(lo, hi))
    )
    if exclude_parcel_id:
        base = base.filter(Property.parcel_id != exclude_parcel_id)

    candidates = base.filter(Property.submarket == submarket).all()

    if len(candidates) < MIN_COMPS:
        # Thin submarket — widen to the whole county rather than return a
        # comp set too small to mean anything.
        county_wide = base.all()
        added = [c for c in county_wide if c not in candidates]
        if added:
            notes.append(
                f"Only {len(candidates)} comps found in {submarket}; widened the "
                f"search to all of Travis County (+{len(added)} candidates)."
            )
            candidates = candidates + added

    if len(candidates) < MIN_COMPS:
        notes.append(
            f"Comp set is thin ({len(candidates)} candidates county-wide). "
            "Treat the valuation range with caution."
        )
    return candidates, notes
