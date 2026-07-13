"""Integration tests: comp search + full analysis against the seed data."""

from datetime import date

import pytest

from app.underwriting.analysis import AnalysisError, run_analysis
from app.underwriting.comps import find_candidates

TODAY = date(2026, 7, 13)


class TestCompSearch:
    def test_east_austin_office_search(self, db):
        candidates, _ = find_candidates(db, "office", 20_000, "East Austin")
        assert len(candidates) >= 5
        for c in candidates:
            assert c.property_type in {"office", "commercial"}
            assert 14_000 <= c.building_sqft <= 26_000  # +/- 30%

    def test_thin_submarket_widens_to_county(self, db):
        # Only 2 CBD offices in the seed data -> search must widen and say so.
        candidates, notes = find_candidates(db, "office", 20_000, "CBD / Downtown")
        assert len(candidates) > 2
        assert any("widened" in n for n in notes)


class TestRunAnalysis:
    def test_matched_subject_full_run(self, db):
        result = run_analysis(db, "Chicon St", today=TODAY)
        assert result.matched_property is not None
        assert result.matched_property.parcel_id not in [
            c.prop.parcel_id for c in result.comps  # subject never comps itself
        ]
        assert 5 <= len(result.comps) <= 10
        assert result.summary.value_low < result.summary.value_mid < result.summary.value_high
        # Comps come back ranked most-reliable first.
        weights = [c.weight for c in result.comps]
        assert weights == sorted(weights, reverse=True)

    def test_unmatched_subject_with_manual_inputs(self, db):
        result = run_analysis(
            db,
            "9999 Nowhere Ln",
            building_sqft=50_000,
            year_built=2005,
            zip_code="78744",
            property_type="industrial",
            asking_price=8_000_000,
            today=TODAY,
        )
        assert result.subject.submarket == "Southeast"
        assert result.summary.subject_cap_rate_est is not None
        assert any("not found" in n for n in result.notes)

    def test_unmatched_subject_without_sqft_errors(self, db):
        with pytest.raises(AnalysisError, match="square footage"):
            run_analysis(db, "9999 Nowhere Ln", property_type="office", today=TODAY)

    def test_no_sale_comps_use_appraised_basis(self, db):
        result = run_analysis(db, "Chicon St", today=TODAY)
        basis_values = {c.basis for c in result.comps}
        # Seed data includes non-disclosure parcels, so both bases appear.
        assert "sale" in basis_values
        assert "appraised" in basis_values
        for comp in result.comps:
            if comp.basis == "appraised":
                assert any("appraised value" in n for n in comp.notes)
