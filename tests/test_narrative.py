"""Tests for the narrative layer's prompt construction and no-key fallback.

The live API call isn't exercised here (no key in CI); what matters is
that the prompt contains only our data, formatted the way the results
page formats it.
"""

from datetime import date

from app.narrative.generator import build_prompt, generate
from app.underwriting.analysis import run_analysis

TODAY = date(2026, 7, 13)


def test_prompt_contains_real_figures_and_grounding_data(db):
    result = run_analysis(db, "Chicon St", asking_price=6_500_000, today=TODAY)
    prompt = build_prompt(result)

    # Every comp's address and adjusted PSF must be in the prompt — the
    # model can only cite what we give it.
    for comp in result.comps:
        assert comp.prop.address in prompt
        assert f"${comp.adjusted_psf:,.2f}/SF" in prompt

    assert f"${result.summary.value_mid:,.0f}" in prompt
    assert "Asking price" in prompt
    # Non-disclosure fallbacks are flagged so the narrative can discuss them.
    if any(c.basis == "appraised" for c in result.comps):
        assert "APPRAISED VALUE basis" in prompt


def test_generate_without_api_key_degrades_gracefully(db, monkeypatch):
    monkeypatch.setattr("app.narrative.generator.ANTHROPIC_API_KEY", "")
    result = run_analysis(db, "Chicon St", today=TODAY)
    narrative, error = generate(result)
    assert narrative is None
    assert "ANTHROPIC_API_KEY" in error
