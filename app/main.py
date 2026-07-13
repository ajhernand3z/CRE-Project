"""FastAPI app: input form, comp analysis results page, and a JSON API.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_session, init_db
from app.models import Property
from app.narrative import generator
from app.underwriting.analysis import AnalysisError, run_analysis

app = FastAPI(title="Travis County CRE Comp Analysis")

BASE = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

# Display filters used throughout the templates.
templates.env.filters["money"] = lambda v: f"${v:,.0f}" if v is not None else "—"
templates.env.filters["psf"] = lambda v: f"${v:,.2f}" if v is not None else "—"
templates.env.filters["pct"] = lambda v: f"{v:+.1%}" if v is not None else "—"
templates.env.filters["num"] = lambda v: f"{v:,.0f}" if v is not None else "—"

PROPERTY_TYPES = ["office", "retail", "industrial", "multifamily", "mixed_use", "commercial"]


@app.on_event("startup")
def startup() -> None:
    init_db()


def _form_context(session: Session) -> dict:
    """Data the input form needs: property types plus a data-inventory line
    so users can see what's actually in the local database."""
    counts = (
        session.query(Property.property_type, func.count())
        .group_by(Property.property_type)
        .all()
    )
    return {
        "property_types": PROPERTY_TYPES,
        "inventory": sorted(counts),
        "total": sum(c for _, c in counts),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "index.html", {**_form_context(session), "error": None, "form": {}}
    )


def _parse_optional_float(raw: str) -> float | None:
    raw = raw.replace(",", "").replace("$", "").strip()
    return float(raw) if raw else None


@app.post("/analyze", response_class=HTMLResponse)
def analyze(
    request: Request,
    query: str = Form(...),
    property_type: str = Form(""),
    building_sqft: str = Form(""),
    year_built: str = Form(""),
    asking_price: str = Form(""),
    zip_code: str = Form(""),
    session: Session = Depends(get_session),
):
    form = dict(
        query=query, property_type=property_type, building_sqft=building_sqft,
        year_built=year_built, asking_price=asking_price, zip_code=zip_code,
    )
    try:
        sqft = _parse_optional_float(building_sqft)
        year = int(year_built) if year_built.strip() else None
        asking = _parse_optional_float(asking_price)
    except ValueError:
        return templates.TemplateResponse(
            request, "index.html",
            {**_form_context(session), "form": form,
             "error": "Square footage, year built, and price must be numbers."},
        )

    try:
        result = run_analysis(
            session, query,
            building_sqft=sqft, year_built=year, asking_price=asking,
            zip_code=zip_code.strip() or None,
            property_type=property_type or None,
        )
    except AnalysisError as e:
        return templates.TemplateResponse(
            request, "index.html",
            {**_form_context(session), "form": form, "error": str(e)},
        )

    result.narrative, narrative_error = generator.generate(result)
    return templates.TemplateResponse(
        request, "results.html",
        {"r": result, "narrative_error": narrative_error},
    )


@app.get("/api/analyze")
def analyze_api(
    query: str,
    property_type: str | None = None,
    building_sqft: float | None = None,
    year_built: int | None = None,
    asking_price: float | None = None,
    zip_code: str | None = None,
    session: Session = Depends(get_session),
):
    """JSON version of the analysis (no AI narrative — that call is slow
    and costs money, so the API returns the quantitative result only)."""
    try:
        r = run_analysis(
            session, query,
            building_sqft=building_sqft, year_built=year_built,
            asking_price=asking_price, zip_code=zip_code, property_type=property_type,
        )
    except AnalysisError as e:
        return {"error": str(e)}

    return {
        "subject": vars(r.subject),
        "notes": r.notes,
        "valuation": vars(r.summary),
        "comps": [
            {
                "parcel_id": c.prop.parcel_id,
                "address": c.prop.address,
                "submarket": c.prop.submarket,
                "property_type": c.prop.property_type,
                "building_sqft": c.prop.building_sqft,
                "year_built": c.prop.year_built,
                "basis": c.basis,
                "basis_price": c.basis_price,
                "sale_date": c.prop.last_sale_date.isoformat()
                if c.basis == "sale" else None,
                "raw_psf": round(c.raw_psf, 2),
                "age_adj_pct": c.age_adj_pct,
                "size_adj_pct": round(c.size_adj_pct, 4),
                "adjusted_psf": round(c.adjusted_psf, 2),
                "weight": round(c.weight, 3),
                "cap_rate_est": round(c.cap_rate_est, 4) if c.cap_rate_est else None,
                "price_per_unit": c.price_per_unit,
                "notes": c.notes,
            }
            for c in r.comps
        ],
    }
