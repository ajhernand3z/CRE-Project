"""AI narrative layer: turn the comp analysis into a plain-English summary.

Design constraints:
* The model must ONLY use figures we hand it. We serialize the exact comp
  table (addresses, PSFs, adjustment percentages, weights) into the prompt
  and instruct the model not to introduce outside data. The narrative is
  commentary on our math, never a source of new numbers.
* The app must work without a key: if ANTHROPIC_API_KEY is unset or the
  call fails, generate() returns (None, reason) and the results page still
  renders everything except the narrative.
"""

from __future__ import annotations

import anthropic

from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from app.underwriting.analysis import AnalysisResult

SYSTEM_PROMPT = """You are a commercial real estate analyst writing for a \
broker or investor reviewing a comparable-sales analysis.

Rules:
- Use ONLY the figures provided in the data below. Never invent, estimate, \
or recall market data from outside this comp set.
- Cite specific numbers (prices per square foot, adjustment percentages, \
years, square footages) when explaining, not vague language.
- Explain which 2-3 comps are most reliable and why (recency, similarity, \
sale basis vs appraised basis), and why the adjusted values differ from \
the raw values.
- Note any weaknesses in the comp set (appraised-value fallbacks, stale \
sales, missing data) plainly — do not oversell the analysis.
- Write 1-2 short paragraphs of plain English prose. No headers, no \
bullet lists, no markdown."""


def build_prompt(result: AnalysisResult) -> str:
    """Serialize the analysis into the user prompt.

    Plain labeled text rather than JSON: the model quotes figures from it
    verbatim, and formatted values ("$243/SF", "+4.5%") keep the narrative's
    numbers consistent with what the results page shows.
    """
    s = result.subject
    lines = [
        "SUBJECT PROPERTY",
        f"- {s.label}",
        f"- Type: {s.property_type}, Submarket: {s.submarket}",
        f"- Building: {s.building_sqft:,.0f} SF"
        + (f", built {s.year_built}" if s.year_built else ", year built unknown"),
    ]
    if s.asking_price:
        lines.append(f"- Asking price: ${s.asking_price:,.0f}")

    lines.append("\nCOMPARABLE SALES (adjusted to the subject's age and size)")
    for i, c in enumerate(result.comps, 1):
        p = c.prop
        age_txt = f"{c.age_adj_pct:+.1%}" if c.age_adj_pct is not None else "n/a"
        sale_txt = (
            f"sold {p.last_sale_date:%b %Y}" if c.basis == "sale"
            else "no recent sale — APPRAISED VALUE basis"
        )
        lines.append(
            f"{i}. {p.address} ({p.property_type}, {p.building_sqft:,.0f} SF, "
            f"built {p.year_built or 'unknown'}): ${c.basis_price:,.0f} ({sale_txt}); "
            f"raw ${c.raw_psf:,.2f}/SF; age adj {age_txt}, size adj {c.size_adj_pct:+.1%} "
            f"=> adjusted ${c.adjusted_psf:,.2f}/SF; reliability weight {c.weight:.2f}"
        )

    v = result.summary
    lines += [
        "\nVALUATION SUMMARY (reliability-weighted)",
        f"- Weighted adjusted value: ${v.weighted_psf:,.2f}/SF",
        f"- Indicated range for subject: ${v.value_low:,.0f} to ${v.value_high:,.0f} "
        f"(midpoint ${v.value_mid:,.0f})",
    ]
    if v.asking_vs_mid_pct is not None:
        lines.append(
            f"- Asking price is {v.asking_vs_mid_pct:+.1%} vs the comp-indicated midpoint"
        )
    if v.subject_cap_rate_est is not None:
        lines.append(
            f"- Pro forma cap rate at asking (modeled NOI, not actuals): "
            f"{v.subject_cap_rate_est:.2%}"
        )
    if result.notes:
        lines.append("\nSEARCH NOTES")
        lines += [f"- {n}" for n in result.notes]

    lines.append("\nWrite the comp analysis narrative now.")
    return "\n".join(lines)


def generate(result: AnalysisResult) -> tuple[str | None, str | None]:
    """Return (narrative, error_message) — exactly one is None."""
    if not ANTHROPIC_API_KEY:
        return None, (
            "AI narrative skipped: set the ANTHROPIC_API_KEY environment "
            "variable to enable it."
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,  # 1-2 paragraphs; deliberately short output
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(result)}],
        )
    # Most-specific first: retryable and non-retryable failures read
    # differently to the user, so keep them distinct.
    except anthropic.AuthenticationError:
        return None, "AI narrative unavailable: invalid Anthropic API key."
    except anthropic.RateLimitError:
        return None, "AI narrative unavailable: rate limited — try again shortly."
    except anthropic.APIStatusError as e:
        return None, f"AI narrative unavailable: API error {e.status_code}."
    except anthropic.APIConnectionError:
        return None, "AI narrative unavailable: could not reach the Anthropic API."

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return None, "AI narrative unavailable: model returned no text."
    return text, None
