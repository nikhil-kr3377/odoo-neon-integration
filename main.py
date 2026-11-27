
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

    url = f"{odoo_url}/json/2/product.template/search_read"

    headers = {
        "Authorization": f"bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "odoo-neon-integration"
    }

 payload = {
    "domain": [],
    "limit": 5,
    "fields": [
        "id",
        "name",
        "default_code",
        "list_price",
        "active"
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

@app.get("/test-odoo-attributes")
def test_odoo_attributes(product_id: int = 1):
    odoo_url = os.getenv("ODOO_URL", "").rstrip("/")
    api_key = os.getenv("ODOO_API_KEY")

    headers = {
        "Authorization": f"bearer {api_key}",
        "Content-Type": "application/json"
    }

    def odoo_call(model, method, payload):
        url = f"{odoo_url}/json/2/{model}/{method}"

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        if not response.ok:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Odoo {model} error: {response.text[:1000]}"
            )

        return response.json()

    try:
        # 1. Retrieve product details
        products = odoo_call(
            "product.template",
            "read",
            {
                "ids": [product_id],
                "fields": [
                    "id",
                    "name",
                    "default_code",
                    "list_price"
                ]
            }
        )

        if not products:
            return {
                "status": "NOT_FOUND",
                "product_id": product_id
            }

        product = products[0]

        # 2. Retrieve attributes assigned to this product
        attribute_lines = odoo_call(
            "product.template.attribute.line",
            "search_read",
            {
                "domain": [
                    ["product_tmpl_id", "=", product_id]
                ],
                "fields": [
                    "attribute_id",
                    "value_ids"
                ],
                "limit": 200
            }
        )

        # 3. Collect all attribute value IDs
        value_ids = list({
            value_id
            for line in attribute_lines
            for value_id in line.get("value_ids", [])
        })

        # 4. Retrieve attribute value names
        values = []

        if value_ids:
            values = odoo_call(
                "product.attribute.value",
                "read",
                {
                    "ids": value_ids,
                    "fields": [
                        "id",
                        "name",
                        "attribute_id"
                    ]
                }
            )

        value_map = {
            value["id"]: value
            for value in values
        }

        # 5. Build dynamic attribute JSON
        attributes = []

        for line in attribute_lines:
            attribute = line.get("attribute_id")

            if not attribute:
                continue

            selected_values = []

            for value_id in line.get("value_ids", []):
                value = value_map.get(value_id)

                if value:
                    selected_values.append(value["name"])

            attributes.append({
                "attribute_id": attribute[0],
                "name": attribute[1],
                "values": selected_values
            })

        return {
            "status": "SUCCESS",
            "product": {
                "id": product["id"],
                "name": product["name"],
                "default_code": product.get("default_code"),
                "list_price": product["list_price"],
                "attributes": attributes
            }
        }

    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail=str(error)
        )
