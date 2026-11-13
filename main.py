
import os
import re
import hmac

import psycopg
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]


def get_db_connection():
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        sslmode="require"
    )


@app.get("/")
def home():
    return {"status": "Odoo to Neon integration is running"}


@app.post("/webhook/{secret}")
async def receive_product(request: Request, secret: str):

    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()

    odoo_id = data.get("id")
    name = data.get("name")
    default_code = data.get("default_code")
    price = data.get("list_price") or 0
    description = data.get("description_sale")
    active = data.get("active", True)

    if not odoo_id or not name or not default_code:
        raise HTTPException(
            status_code=400,
            detail="Product ID, name and internal reference are required"
        )

    # Our existing product reference convention is M1-CODE, M2-CODE, etc.
    match = re.match(r"^M(\d+)-(.+)$", str(default_code))

    if not match:
        raise HTTPException(
            status_code=400,
            detail="Internal Reference must use M{manufacturer_id}-{product_code}"
        )

    manufacturer_id = int(match.group(1))
    product_code = match.group(2)

    status = "ACTIVE" if active else "INACTIVE"

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    INSERT INTO marketplace.products
                    (
                        manufacturer_id,
                        odoo_product_id,
                        product_name,
                        product_code,
                        description,
                        price,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)

                    ON CONFLICT (odoo_product_id)
                    DO UPDATE SET
                        manufacturer_id = EXCLUDED.manufacturer_id,
                        product_name = EXCLUDED.product_name,
                        product_code = EXCLUDED.product_code,
                        description = EXCLUDED.description,
                        price = EXCLUDED.price,
                        status = EXCLUDED.status
                """, (
                    manufacturer_id,
                    odoo_id,
                    name,
                    product_code,
                    description,
                    price,
                    status
                ))

        return {
            "success": True,
            "odoo_product_id": odoo_id,
            "product_name": name,
            "message": "Product synchronized to Neon"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
