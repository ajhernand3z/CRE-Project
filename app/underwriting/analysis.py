"""Orchestrates a full comp analysis: resolve the subject, find candidate
comps, adjust and rank them, and produce the valuation summary.

This module is the single entry point the web layer (and the narrative
layer) call into, so all "what happened during the search" context is
collected here for display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.config import MAX_COMPS, submarket_for_zip
from app.models import Property
from app.underwriting.adjustments import (
    AdjustedComp,
    SubjectProperty,
    ValuationSummary,
    adjust_comp,
    summarize,
)
from app.underwriting.comps import find_candidates


@dataclass
class AnalysisResult:
    subject: SubjectProperty
    matched_property: Property | None      # DB row the subject resolved to, if any
    comps: list[AdjustedComp]              # ranked, best first
    summary: ValuationSummary | None       # None when no usable comps
    notes: list[str] = field(default_factory=list)
    narrative: str | None = None           # filled in by the AI layer


class AnalysisError(Exception):
    """User-facing analysis failure (bad input / no data)."""


def resolve_subject(
    session: Session,
    query: str,
    building_sqft: float | None,
    year_built: int | None,
    asking_price: float | None,
    zip_code: str | None,
    property_type: str | None,
) -> tuple[SubjectProperty, Property | None, list[str]]:
    """Build the SubjectProperty from form input, enriched from the DB.

    The user may enter a parcel ID or an address. If we find the parcel in
    our data, its stored characteristics fill any fields the user left
    blank; user-entered values always win (they may know about a
    renovation or remeasurement the roll doesn't reflect).
    """
    notes: list[str] = []
    q = query.strip()
    match = (
        session.query(Property).filter(Property.parcel_id == q).first()
        or session.query(Property).filter(Property.address.ilike(f"%{q}%")).first()
    )

    if match:
        building_sqft = building_sqft or match.building_sqft
        year_built = year_built or match.year_built
        property_type = property_type or match.property_type
        submarket = match.submarket
        label = f"{match.address} (parcel {match.parcel_id})"
        notes.append(f"Matched subject to {label} in {submarket}.")
    else:
        submarket = submarket_for_zip(zip_code)
        label = q
        notes.append(
            "Subject not found in the local database — using the entered "
            f"characteristics; submarket set to {submarket}"
            + ("" if zip_code else " (no ZIP provided)") + "."
        )

    if not building_sqft or building_sqft <= 0:
        raise AnalysisError(
            "Building square footage is required (the subject wasn't found in "
            "the database, so it can't be filled in automatically)."
        )
    if not property_type:
        raise AnalysisError("Property type is required for an unmatched subject.")

    subject = SubjectProperty(
        label=label,
        property_type=property_type,
        building_sqft=float(building_sqft),
        year_built=year_built,
        asking_price=asking_price,
        submarket=submarket,
        units=match.units if match else None,
    )
    return subject, match, notes


def run_analysis(
    session: Session,
    query: str,
    building_sqft: float | None = None,
    year_built: int | None = None,
    asking_price: float | None = None,
    zip_code: str | None = None,
    property_type: str | None = None,
    today: date | None = None,
) -> AnalysisResult:
    today = today or date.today()
    subject, match, notes = resolve_subject(
        session, query, building_sqft, year_built, asking_price, zip_code, property_type
    )

    candidates, search_notes = find_candidates(
        session,
        subject.property_type,
        subject.building_sqft,
        subject.submarket,
        exclude_parcel_id=match.parcel_id if match else None,
    )
    notes.extend(search_notes)

    # Adjust every candidate, drop the unusable ones, rank by reliability
    # weight, and keep the top MAX_COMPS.
    adjusted = [a for c in candidates if (a := adjust_comp(subject, c, today))]
    adjusted.sort(key=lambda a: a.weight, reverse=True)
    comps = adjusted[:MAX_COMPS]

    if not comps:
        raise AnalysisError(
            f"No usable comps for a {subject.building_sqft:,.0f} SF "
            f"{subject.property_type} near {subject.submarket}. "
            "Try widening the inputs or ingesting more data."
        )

    dropped = len(candidates) - len(adjusted)
    if dropped:
        notes.append(f"Dropped {dropped} candidate(s) missing price or building SF.")

    return AnalysisResult(
        subject=subject,
        matched_property=match,
        comps=comps,
        summary=summarize(subject, comps),
        notes=notes,
    )
