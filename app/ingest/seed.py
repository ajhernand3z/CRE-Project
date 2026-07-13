"""Load the bundled demo dataset (data/seed_comps.json) into SQLite.

    python -m app.ingest.seed

The seed file contains 37 plausible Travis County commercial parcels with
SYNTHETIC sale prices (Texas is a non-disclosure state — real sale prices
are not public). It exists so the app runs end-to-end out of the box;
swap in real appraisal data with app/ingest/tcad.py or app/ingest/regrid.py.
"""

from __future__ import annotations

import json
from datetime import date

from app.config import DATA_DIR, submarket_for_zip
from app.database import SessionLocal, init_db
from app.models import Property

SEED_FILE = DATA_DIR / "seed_comps.json"


def load_seed(session) -> int:
    with open(SEED_FILE) as f:
        payload = json.load(f)

    count = 0
    for rec in payload["properties"]:
        rec = dict(rec)  # don't mutate the parsed payload
        sale_date = rec.pop("last_sale_date", None)
        rec["last_sale_date"] = date.fromisoformat(sale_date) if sale_date else None
        rec["submarket"] = submarket_for_zip(rec.get("zip_code"))
        rec["source"] = "seed"

        existing = session.query(Property).filter_by(parcel_id=rec["parcel_id"]).first()
        if existing:
            for k, v in rec.items():
                setattr(existing, k, v)
        else:
            session.add(Property(**rec))
        count += 1
    session.commit()
    return count


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        count = load_seed(session)
    finally:
        session.close()
    print(f"Seeded {count} demo properties into the database.")


if __name__ == "__main__":
    main()
