"""Comp adjustment and valuation math.

The approach mirrors a standard sales-comparison grid, simplified to the
two physical adjustments the assignment calls for (age and size), plus a
transparent reliability weighting instead of an appraiser's subjective
weighting. Every constant is defined at the top with its rationale.

Sign convention: adjustments are applied TO THE COMP to restate its price
as if the comp shared the subject's characteristics. A comp that is
*older* than the subject sold at a discount for its extra depreciation,
so its price is adjusted UP; a comp that is *smaller* than the subject
traded at a higher $/SF (small buildings command premium unit pricing),
so its $/SF is adjusted DOWN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.config import NOI_ASSUMPTIONS, SALE_WINDOW_MONTHS
from app.models import Property

# --- Adjustment constants ----------------------------------------------------

# Age: straight-line physical depreciation. Commercial buildings are
# typically underwritten on a 50-60 year economic life => roughly 1.7-2%
# of *improvement* value per year. Because parcel price includes land
# (which doesn't depreciate) and buildings get maintained, the observable
# effect on total price is much smaller — we use 0.5% per year of age
# difference, capped so a very old comp can't be adjusted into fiction.
AGE_ADJ_PER_YEAR = 0.005
AGE_ADJ_CAP = 0.15  # max +/-15% total age adjustment

# Size: price per SF falls as buildings get larger (bigger assets have a
# thinner buyer pool and economies of scale). We model PSF ~ k * SF^(-e)
# with elasticity e = 0.10. Restating a comp's PSF at the subject's size:
#
#   comp_psf = k * comp_sf^(-e)          =>  k = comp_psf * comp_sf^e
#   psf_at_subject_size = k * subj_sf^(-e) = comp_psf * (comp_sf/subj_sf)^e
#
# So the multiplicative size factor is (comp_sf / subject_sf) ** e.
SIZE_ELASTICITY = 0.10
SIZE_ADJ_CAP = 0.10  # max +/-10% total size adjustment

# Reliability-weight penalties (see weight formula in adjust_comp):
STALENESS_PENALTY_PER_YEAR = 0.05   # older sales are weaker evidence
APPRAISED_BASIS_PENALTY = 0.25      # assessed value is weaker than a real sale
UNKNOWN_AGE_PENALTY = 0.10          # can't age-adjust => less comparable


@dataclass
class SubjectProperty:
    """The property being valued, as entered by the user."""

    label: str                       # address or parcel id for display
    property_type: str
    building_sqft: float
    year_built: int | None = None
    asking_price: float | None = None
    submarket: str = "Other Travis County"
    units: int | None = None


@dataclass
class AdjustedComp:
    """One comp with its valuation basis, adjustments, and weight."""

    prop: Property
    basis_price: float               # sale price, or appraised value fallback
    basis: str                       # 'sale' or 'appraised'
    raw_psf: float
    age_adj_pct: float | None        # None when comp or subject age unknown
    size_adj_pct: float
    adjusted_psf: float
    months_since_sale: float | None
    weight: float
    cap_rate_est: float | None       # pro forma — see NOI_ASSUMPTIONS
    price_per_unit: float | None     # multifamily only
    notes: list[str] = field(default_factory=list)


def months_between(earlier: date, later: date) -> float:
    return (later - earlier).days / 30.44  # average month length


def choose_basis(comp: Property, today: date) -> tuple[float, str, list[str]] | None:
    """Pick the price a comp contributes: a recent arm's-length sale if we
    have one, otherwise TCAD's appraised market value.

    Texas is a non-disclosure state, so most public records have no sale
    price — the appraised-value fallback is what keeps the tool usable on
    real TCAD data. Returns None if the comp has no usable price at all.
    """
    notes: list[str] = []
    if comp.last_sale_price and comp.last_sale_date:
        age_months = months_between(comp.last_sale_date, today)
        if age_months <= SALE_WINDOW_MONTHS:
            return comp.last_sale_price, "sale", notes
        notes.append(
            f"Sale on {comp.last_sale_date:%b %Y} is outside the "
            f"{SALE_WINDOW_MONTHS}-month window; using appraised value instead."
        )
    else:
        notes.append("No disclosed sale (TX non-disclosure); using appraised value.")

    if comp.market_value:
        return comp.market_value, "appraised", notes
    return None  # no sale AND no appraised value — comp is unusable


def estimate_cap_rate(prop_type: str, sqft: float | None, price: float) -> float | None:
    """Pro forma cap rate: modeled NOI / price.

    NOI = SF * market_rent_psf * (1 - vacancy) * (1 - expense_ratio)
    Gross potential rent, less vacancy allowance, less operating expenses
    as a share of effective gross income. Assumptions are market-level
    placeholders (config.NOI_ASSUMPTIONS) — clearly labeled as estimates
    in the UI because actual T-12 financials are never public.
    """
    a = NOI_ASSUMPTIONS.get(prop_type)
    if not a or not sqft or price <= 0:
        return None
    noi = sqft * a["rent_psf"] * (1 - a["vacancy"]) * (1 - a["expense_ratio"])
    return noi / price


def adjust_comp(subject: SubjectProperty, comp: Property, today: date) -> AdjustedComp | None:
    """Adjust one comp's $/SF to the subject's age and size, and score its
    reliability. Returns None when the comp lacks the minimum data
    (a usable price and building SF)."""
    picked = choose_basis(comp, today)
    if picked is None or not comp.building_sqft:
        return None
    basis_price, basis, notes = picked
    raw_psf = basis_price / comp.building_sqft

    # --- Age adjustment: +AGE_ADJ_PER_YEAR for every year the comp is
    # older than the subject (comp suffered more depreciation than the
    # subject, so its observed price understates subject value), capped.
    age_adj_pct: float | None = None
    if comp.year_built and subject.year_built:
        age_diff_years = subject.year_built - comp.year_built  # >0: comp older
        age_adj_pct = max(-AGE_ADJ_CAP, min(AGE_ADJ_CAP, age_diff_years * AGE_ADJ_PER_YEAR))
        if abs(age_diff_years * AGE_ADJ_PER_YEAR) > AGE_ADJ_CAP:
            notes.append(f"Age adjustment capped at {AGE_ADJ_CAP:.0%}.")
    else:
        notes.append("Year built unknown — no age adjustment applied.")

    # --- Size adjustment: restate comp PSF at the subject's size using the
    # elasticity model derived above, capped.
    size_factor = (comp.building_sqft / subject.building_sqft) ** SIZE_ELASTICITY
    size_adj_pct = max(-SIZE_ADJ_CAP, min(SIZE_ADJ_CAP, size_factor - 1))
    if abs(size_factor - 1) > SIZE_ADJ_CAP:
        notes.append(f"Size adjustment capped at {SIZE_ADJ_CAP:.0%}.")

    adjusted_psf = raw_psf * (1 + (age_adj_pct or 0.0)) * (1 + size_adj_pct)

    # --- Reliability weight. A comp needing big adjustments, an old sale,
    # or an appraised-value basis is weaker evidence. The weight is
    #   1 / (1 + total dissimilarity)
    # so a perfect comp gets weight 1.0 and weights fall smoothly from there.
    months_since_sale = None
    dissimilarity = abs(age_adj_pct or 0.0) + abs(size_adj_pct)
    if age_adj_pct is None:
        dissimilarity += UNKNOWN_AGE_PENALTY
    if basis == "sale":
        months_since_sale = months_between(comp.last_sale_date, today)
        dissimilarity += STALENESS_PENALTY_PER_YEAR * (months_since_sale / 12)
    else:
        dissimilarity += APPRAISED_BASIS_PENALTY
    weight = 1.0 / (1.0 + dissimilarity)

    price_per_unit = basis_price / comp.units if comp.units else None

    return AdjustedComp(
        prop=comp,
        basis_price=basis_price,
        basis=basis,
        raw_psf=raw_psf,
        age_adj_pct=age_adj_pct,
        size_adj_pct=size_adj_pct,
        adjusted_psf=adjusted_psf,
        months_since_sale=months_since_sale,
        weight=weight,
        cap_rate_est=estimate_cap_rate(comp.property_type, comp.building_sqft, basis_price),
        price_per_unit=price_per_unit,
        notes=notes,
    )


@dataclass
class ValuationSummary:
    """Weighted valuation of the subject from the adjusted comp set."""

    weighted_psf: float
    psf_low: float          # weighted mean − 1 weighted std dev
    psf_high: float         # weighted mean + 1 weighted std dev
    value_mid: float
    value_low: float
    value_high: float
    subject_cap_rate_est: float | None   # at asking price, if given
    asking_vs_mid_pct: float | None      # asking premium/(discount) to midpoint


def summarize(subject: SubjectProperty, comps: list[AdjustedComp]) -> ValuationSummary:
    """Collapse the adjusted comps into an indicated value range.

    Weighted mean, not a simple average: each comp's adjusted $/SF counts
    in proportion to its reliability weight, so a near-identical recent
    sale moves the answer more than a heavily-adjusted appraised value.
    The range is the weighted mean +/- one weighted standard deviation —
    a plain-language "most comps land in here" band.
    """
    if not comps:
        raise ValueError("summarize() requires at least one adjusted comp")

    total_w = sum(c.weight for c in comps)
    mean = sum(c.weight * c.adjusted_psf for c in comps) / total_w
    variance = sum(c.weight * (c.adjusted_psf - mean) ** 2 for c in comps) / total_w
    std = variance ** 0.5

    psf_low, psf_high = mean - std, mean + std
    value_mid = mean * subject.building_sqft

    cap = None
    ask_delta = None
    if subject.asking_price:
        cap = estimate_cap_rate(subject.property_type, subject.building_sqft, subject.asking_price)
        ask_delta = subject.asking_price / value_mid - 1

    return ValuationSummary(
        weighted_psf=mean,
        psf_low=psf_low,
        psf_high=psf_high,
        value_mid=value_mid,
        value_low=psf_low * subject.building_sqft,
        value_high=psf_high * subject.building_sqft,
        subject_cap_rate_est=cap,
        asking_vs_mid_pct=ask_delta,
    )
