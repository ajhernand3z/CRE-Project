"""Database models.

One table: `properties`. Each row is a Travis County parcel with the
fields the underwriting layer needs. Sale fields are nullable on purpose —
Texas is a non-disclosure state, so many parcels will never have a public
sale price and the valuation layer must be able to fall back to the
appraisal district's market value.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Property(Base):
    __tablename__ = "properties"
    __table_args__ = (UniqueConstraint("parcel_id", name="uq_parcel_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity / location
    parcel_id: Mapped[str] = mapped_column(String(32), index=True)
    address: Mapped[str] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zip_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    submarket: Mapped[str] = mapped_column(String(64), index=True)

    # Classification
    property_type: Mapped[str] = mapped_column(String(32), index=True)
    state_category_code: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # Physical characteristics
    building_sqft: Mapped[float | None] = mapped_column(Float, nullable=True)
    land_sqft: Mapped[float | None] = mapped_column(Float, nullable=True)
    year_built: Mapped[int | None] = mapped_column(Integer, nullable=True)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Valuation inputs. last_sale_* are frequently NULL (non-disclosure);
    # market_value is TCAD's appraised market value and is the fallback basis.
    last_sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_sale_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    market_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Provenance: 'tcad', 'regrid', or 'seed' (bundled demo data)
    source: Mapped[str] = mapped_column(String(16), default="seed")
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<Property {self.parcel_id} {self.address!r} {self.property_type}>"
