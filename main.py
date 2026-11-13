
import os
import re
import hmac
import logging

import psycopg
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
logging.basicConfig(level=logging.INFO)

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

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

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

                # Confirm that the manufacturer exists
                cur.execute("""
                    SELECT 1
                    FROM marketplace.manufacturers
                    WHERE manufacturer_id = %s
                """, (manufacturer_id,))

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Manufacturer does not exist in Neon"
                    )

                # First: update using the Odoo product ID
                cur.execute("""
                    UPDATE marketplace.products
                    SET manufacturer_id = %s,
                        product_name = %s,
                        product_code = %s,
                        description = %s,
                        price = %s,
                        status = %s
                    WHERE odoo_product_id = %s
                """, (
                    manufacturer_id, name, product_code,
                    description, price, status, odoo_id
                ))

                # Second: if not found, match an existing Neon product
                if cur.rowcount == 0:
                    cur.execute("""
                        UPDATE marketplace.products
                        SET odoo_product_id = %s,
                            product_name = %s,
                            description = %s,
                            price = %s,
                            status = %s
                        WHERE manufacturer_id = %s
                          AND product_code = %s
                    """, (
                        odoo_id, name, description, price, status,
                        manufacturer_id, product_code
                    ))

                # Third: insert if no existing product was found
                if cur.rowcount == 0:
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
                    """, (
                        manufacturer_id, odoo_id, name,
                        product_code, description, price, status
                    ))

        return {
            "success": True,
            "odoo_product_id": odoo_id,
            "product_name": name,
            "message": "Product synchronized to Neon"
        }

    except HTTPException:
        raise
    except Exception:
        logging.exception("Product synchronization failed")
        raise HTTPException(
            status_code=500,
            detail="Product synchronization failed. Check server logs."
        )
