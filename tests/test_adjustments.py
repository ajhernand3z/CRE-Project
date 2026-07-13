"""Unit tests for the adjustment math, checked against hand-computed
numbers so a reviewer can follow every figure."""

from datetime import date

import pytest

from app.models import Property
from app.underwriting.adjustments import (
    AdjustedComp,
    SubjectProperty,
    adjust_comp,
    choose_basis,
    estimate_cap_rate,
    summarize,
)

TODAY = date(2026, 7, 13)


def make_comp(**overrides) -> Property:
    defaults = dict(
        parcel_id="TEST",
        address="100 Test St",
        zip_code="78702",
        submarket="East Austin",
        property_type="office",
        building_sqft=20_000,
        year_built=2010,
        last_sale_price=5_000_000.0,
        last_sale_date=date(2026, 1, 15),
        market_value=4_500_000.0,
    )
    defaults.update(overrides)
    return Property(**defaults)


def make_subject(**overrides) -> SubjectProperty:
    defaults = dict(
        label="Subject", property_type="office", building_sqft=20_000, year_built=2010
    )
    defaults.update(overrides)
    return SubjectProperty(**defaults)


class TestSizeAdjustment:
    def test_smaller_comp_adjusted_down(self):
        # Comp 10,000 SF @ $100/SF vs 20,000 SF subject.
        # Size factor = (10000/20000)^0.10 = 0.5^0.1 = 0.93303
        # => adjusted PSF = 100 * 0.93303 = 93.30 (small buildings trade at a
        # premium PSF, so the comp is adjusted DOWN to the subject's size).
        comp = make_comp(building_sqft=10_000, last_sale_price=1_000_000)
        adj = adjust_comp(make_subject(), comp, TODAY)
        assert adj.raw_psf == pytest.approx(100.0)
        assert adj.size_adj_pct == pytest.approx(-0.06697, abs=1e-4)
        assert adj.adjusted_psf == pytest.approx(93.30, abs=0.01)

    def test_identical_size_no_adjustment(self):
        adj = adjust_comp(make_subject(), make_comp(), TODAY)
        assert adj.size_adj_pct == pytest.approx(0.0)

    def test_size_adjustment_capped(self):
        # 5x size ratio => factor 5^0.1 = 1.1746, capped at +10%.
        comp = make_comp(building_sqft=100_000, last_sale_price=10_000_000)
        adj = adjust_comp(make_subject(), comp, TODAY)
        assert adj.size_adj_pct == pytest.approx(0.10)


class TestAgeAdjustment:
    def test_older_comp_adjusted_up(self):
        # Comp built 1990 vs subject 2010: 20 years older
        # => +20 * 0.5% = +10%. Raw $250/SF -> $275/SF.
        comp = make_comp(year_built=1990)
        adj = adjust_comp(make_subject(year_built=2010), comp, TODAY)
        assert adj.age_adj_pct == pytest.approx(0.10)
        assert adj.adjusted_psf == pytest.approx(250.0 * 1.10)

    def test_newer_comp_adjusted_down(self):
        comp = make_comp(year_built=2020)
        adj = adjust_comp(make_subject(year_built=2010), comp, TODAY)
        assert adj.age_adj_pct == pytest.approx(-0.05)

    def test_age_adjustment_capped(self):
        # 40-year difference would be 20%; capped at 15%.
        comp = make_comp(year_built=1970)
        adj = adjust_comp(make_subject(year_built=2010), comp, TODAY)
        assert adj.age_adj_pct == pytest.approx(0.15)

    def test_unknown_year_built_skips_adjustment(self):
        comp = make_comp(year_built=None)
        adj = adjust_comp(make_subject(), comp, TODAY)
        assert adj.age_adj_pct is None
        assert any("Year built unknown" in n for n in adj.notes)


class TestBasisSelection:
    def test_recent_sale_used(self):
        price, basis, _ = choose_basis(make_comp(), TODAY)
        assert (price, basis) == (5_000_000.0, "sale")

    def test_stale_sale_falls_back_to_appraised(self):
        comp = make_comp(last_sale_date=date(2023, 1, 1))  # > 24 months ago
        price, basis, notes = choose_basis(comp, TODAY)
        assert (price, basis) == (4_500_000.0, "appraised")
        assert any("outside" in n for n in notes)

    def test_no_sale_falls_back_to_appraised(self):
        comp = make_comp(last_sale_price=None, last_sale_date=None)
        price, basis, notes = choose_basis(comp, TODAY)
        assert (price, basis) == (4_500_000.0, "appraised")
        assert any("non-disclosure" in n for n in notes)

    def test_no_price_at_all_is_unusable(self):
        comp = make_comp(last_sale_price=None, last_sale_date=None, market_value=None)
        assert choose_basis(comp, TODAY) is None
        assert adjust_comp(make_subject(), comp, TODAY) is None

    def test_missing_sqft_is_unusable(self):
        assert adjust_comp(make_subject(), make_comp(building_sqft=None), TODAY) is None


class TestWeighting:
    def test_identical_recent_comp_outweighs_dissimilar_stale_one(self):
        twin = make_comp(last_sale_date=date(2026, 6, 1))
        distant = make_comp(
            year_built=1985, building_sqft=26_000, last_sale_date=date(2024, 9, 1)
        )
        adj_twin = adjust_comp(make_subject(), twin, TODAY)
        adj_far = adjust_comp(make_subject(), distant, TODAY)
        assert adj_twin.weight > adj_far.weight

    def test_appraised_basis_weighs_less_than_sale(self):
        sold = make_comp()
        unsold = make_comp(last_sale_price=None, last_sale_date=None)
        assert (
            adjust_comp(make_subject(), sold, TODAY).weight
            > adjust_comp(make_subject(), unsold, TODAY).weight
        )


class TestCapRate:
    def test_office_cap_rate_hand_computed(self):
        # Office assumptions: $32/SF rent, 16% vacancy, 42% expense ratio.
        # NOI = 10,000 * 32 * 0.84 * 0.58 = $155,904
        # Cap = 155,904 / 3,000,000 = 5.197%
        cap = estimate_cap_rate("office", 10_000, 3_000_000)
        assert cap == pytest.approx(0.051968, abs=1e-6)

    def test_unknown_type_returns_none(self):
        assert estimate_cap_rate("stadium", 10_000, 1_000_000) is None


class TestSummarize:
    def _fake(self, psf: float, weight: float) -> AdjustedComp:
        return AdjustedComp(
            prop=None, basis_price=0, basis="sale", raw_psf=psf, age_adj_pct=0,
            size_adj_pct=0, adjusted_psf=psf, months_since_sale=1, weight=weight,
            cap_rate_est=None, price_per_unit=None,
        )

    def test_weighted_mean_and_range(self):
        # PSF 100 (w=1) and 200 (w=3):
        #   mean = (100*1 + 200*3) / 4 = 175
        #   var  = (1*75^2 + 3*25^2) / 4 = 1875  =>  std = 43.301
        subject = make_subject(building_sqft=10_000)
        s = summarize(subject, [self._fake(100, 1), self._fake(200, 3)])
        assert s.weighted_psf == pytest.approx(175.0)
        assert s.psf_low == pytest.approx(175 - 43.301, abs=0.01)
        assert s.value_mid == pytest.approx(1_750_000)

    def test_asking_price_comparison(self):
        subject = make_subject(building_sqft=10_000, asking_price=2_100_000)
        s = summarize(subject, [self._fake(100, 1), self._fake(200, 3)])
        # asking 2.1M vs 1.75M midpoint = +20%
        assert s.asking_vs_mid_pct == pytest.approx(0.20)
        assert s.subject_cap_rate_est is not None
