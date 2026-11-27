
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
@app.get("/db-check")
def db_check():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user")
                result = cur.fetchone()

        return {
            "database_status": "Connected",
            "database": result[0],
            "user": result[1]
        }

    except Exception:
        logging.exception("Database connection test failed")
        raise HTTPException(
            status_code=500,
            detail="Database connection failed. Check Render logs."
        )

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


import os
import requests
from fastapi import HTTPException

@app.get("/test-odoo")
def test_odoo():
    odoo_url = os.getenv("ODOO_URL", "").rstrip("/")
    api_key = os.getenv("ODOO_API_KEY")

    if not odoo_url or not api_key:
        raise HTTPException(
            status_code=500,
            detail="Odoo environment variables are missing"
        )

    url = f"{odoo_url}/json/2/product.template/read"

    headers = {
        "Authorization": f"bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "odoo-neon-integration"
    }

    payload = {
        "ids": [1],
        "fields": [
            "id",
            "name",
            "default_code",
            "list_price",
            "description_sale",
            "active",
            "write_date"
        ]
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        if not response.ok:
            raise HTTPException(
                status_code=response.status_code,
                detail="Odoo API error: " + response.text[:1000]
            )

        return {
            "status": "SUCCESS",
            "message": "Connected to Odoo successfully",
            "product_data": response.json()
        }

    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail=str(error)
        )
