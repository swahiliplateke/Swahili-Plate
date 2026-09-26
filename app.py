import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, create_engine, insert, select, text, update

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("swahili-plate")

ROOT = Path(__file__).resolve().parent
APP_ENV = os.getenv("APP_ENV", "development")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./swahili_plate.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgres://")
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgresql://")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
if not ADMIN_TOKEN and APP_ENV != "production":
    ADMIN_TOKEN = "1234"
ORDER_NOTIFICATION_WEBHOOK = os.getenv("ORDER_NOTIFICATION_WEBHOOK", "")
BRANCH_NOTIFICATION_TARGETS = {
    "CBD": {"phone": os.getenv("CBD_BRANCH_PHONE", "0711726448")},
    "Sarit Centre": {"phone": os.getenv("SARIT_BRANCH_PHONE", "0773497954")},
}
ALLOWED_MODES = {"Dine in", "Delivery", "Take out", "Pick up"}
ALLOWED_ORDER_STATUSES = {
    "New", "Confirmed", "Preparing", "Ready", "Out for delivery", "Completed", "Cancelled"
}
ALLOWED_DELIVERY_STATUSES = {
    "Waiting for rider", "Accepted", "Picked up", "On the way", "Delivered", "Delayed"
}

app = FastAPI(title="Swahili Plate Orders", version="1.0.0")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {},
)
metadata = MetaData()
orders_table = Table(
    "orders",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
settings_table = Table(
    "app_settings",
    metadata,
    Column("setting_key", String, primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
reviews_table = Table(
    "customer_reviews",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin access is not configured")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Admin authentication required")


def validate_order(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise HTTPException(status_code=400, detail="Order must be a JSON object")
    required = ("customer_name", "customer_phone", "branch", "ordering_mode")
    if any(not isinstance(record.get(field), str) or not record[field].strip() for field in required):
        raise HTTPException(status_code=422, detail="Name, phone, branch, and ordering mode are required")
    if record["branch"] not in BRANCH_NOTIFICATION_TARGETS:
        raise HTTPException(status_code=422, detail="Choose a valid branch")
    if record["ordering_mode"] not in ALLOWED_MODES:
        raise HTTPException(status_code=422, detail="Choose a valid ordering mode")
    if len(record["customer_name"].strip()) > 120 or len(record["customer_phone"].strip()) > 40:
        raise HTTPException(status_code=422, detail="Name or phone number is too long")
    if record.get("customer_email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", record["customer_email"]):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    raw_items = record.get("cart_items", [])
    if record["ordering_mode"] == "Dine in" and raw_items == "Table reservation":
        items = raw_items
    else:
        try:
            items = json.loads(raw_items) if isinstance(raw_items, str) else raw_items
        except (TypeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=422, detail="Order items are invalid") from error
    if record["ordering_mode"] != "Dine in" and not isinstance(items, list):
        raise HTTPException(status_code=422, detail="Order items are invalid")
    if isinstance(items, list) and len(items) > 100:
        raise HTTPException(status_code=422, detail="Too many items in one order")
    clean = dict(record)
    clean.pop("id", None)
    clean["customer_name"] = record["customer_name"].strip()
    clean["customer_phone"] = record["customer_phone"].strip()
    clean["customer_email"] = (record.get("customer_email") or "").strip()
    clean["id"] = str(uuid.uuid4())
    clean["order_timestamp"] = datetime.now(timezone.utc).isoformat()
    clean["order_status"] = "New"
    clean["delivery_status"] = "Waiting for rider" if record["ordering_mode"] == "Delivery" else ""
    return clean


def validate_setting(setting_key: str, value: Any) -> dict[str, Any]:
    if setting_key not in {"menu", "branches"} or not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Shared setting is invalid")
    if setting_key == "menu":
        if not isinstance(value.get("food"), list) or not isinstance(value.get("drinks"), list):
            raise HTTPException(status_code=422, detail="Menu must include food and drinks lists")
        items = value["food"] + value["drinks"]
        if len(items) > 1000 or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("category"), str)
            or not isinstance(item.get("price"), (int, float))
            or isinstance(item.get("price"), bool)
            or item["price"] < 0
            for item in items
        ):
            raise HTTPException(status_code=422, detail="Menu contains an invalid item")
    else:
        if not isinstance(value.get("business"), dict) or not isinstance(value.get("branches"), dict):
            raise HTTPException(status_code=422, detail="Branch settings must include business and branches")
        if not value["branches"] or any(not isinstance(branch, dict) for branch in value["branches"].values()):
            raise HTTPException(status_code=422, detail="Branch settings are invalid")
    return value


@app.on_event("startup")
def initialize_database() -> None:
    if APP_ENV == "production" and not ADMIN_TOKEN:
        raise RuntimeError("ADMIN_TOKEN must be configured in production")
    metadata.create_all(engine)


@app.get("/api/health")
def health() -> dict[str, str]:
    with engine.connect() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "swahili-plate-orders"}


@app.post("/api/orders", status_code=201)
async def create_order(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    order = validate_order(raw)
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(insert(orders_table).values(id=order["id"], payload=order, created_at=now, updated_at=now))
    await notify_branch(order)
    return order


@app.get("/api/orders", dependencies=[Depends(require_admin)])
def list_orders() -> list[dict[str, Any]]:
    with engine.connect() as db:
        rows = db.execute(select(orders_table.c.payload).order_by(orders_table.c.created_at.desc())).fetchall()
    return [row[0] for row in rows]


@app.patch("/api/orders/{order_id}", dependencies=[Depends(require_admin)])
async def update_order(order_id: str, request: Request) -> dict[str, Any]:
    try:
        changes = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    allowed = {"order_status", "rider_name", "rider_phone", "delivery_status", "delivery_traffic", "delivery_update", "delivery_update_timestamp"}
    if not isinstance(changes, dict) or not changes or set(changes) - allowed:
        raise HTTPException(status_code=422, detail="Order update contains unsupported fields")
    if "order_status" in changes and changes["order_status"] not in ALLOWED_ORDER_STATUSES:
        raise HTTPException(status_code=422, detail="Order status is invalid")
    if changes.get("delivery_status") and changes["delivery_status"] not in ALLOWED_DELIVERY_STATUSES:
        raise HTTPException(status_code=422, detail="Delivery status is invalid")
    with engine.begin() as db:
        row = db.execute(select(orders_table.c.payload).where(orders_table.c.id == order_id).with_for_update()).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        updated = {**row[0], **changes}
        db.execute(update(orders_table).where(orders_table.c.id == order_id).values(payload=updated, updated_at=datetime.now(timezone.utc)))
    return updated


@app.get("/api/settings/{setting_key}")
def get_setting(setting_key: str) -> dict[str, Any]:
    if setting_key not in {"menu", "branches"}:
        raise HTTPException(status_code=404, detail="Setting not found")
    with engine.connect() as db:
        row = db.execute(select(settings_table.c.payload).where(settings_table.c.setting_key == setting_key)).fetchone()
    return {"value": row[0] if row else None}


@app.put("/api/settings/{setting_key}", dependencies=[Depends(require_admin)])
async def save_setting(setting_key: str, request: Request) -> dict[str, Any]:
    try:
        value = validate_setting(setting_key, await request.json())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        exists = db.execute(
            select(settings_table.c.setting_key).where(settings_table.c.setting_key == setting_key)
        ).first()
        if exists:
            db.execute(
                update(settings_table)
                .where(settings_table.c.setting_key == setting_key)
                .values(payload=value, updated_at=now)
            )
        else:
            db.execute(insert(settings_table).values(setting_key=setting_key, payload=value, updated_at=now))
    return {"value": value}


@app.post("/api/reviews", status_code=201)
async def create_review(request: Request) -> dict[str, Any]:
    try:
        review = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    if not isinstance(review, dict):
        raise HTTPException(status_code=400, detail="Review must be a JSON object")
    name = review.get("name")
    branch = review.get("branch")
    rating = review.get("rating")
    message = review.get("message")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise HTTPException(status_code=422, detail="Enter a valid name")
    if branch not in BRANCH_NOTIFICATION_TARGETS:
        raise HTTPException(status_code=422, detail="Choose a valid branch")
    if not isinstance(rating, int) or isinstance(rating, bool) or rating < 1 or rating > 5:
        raise HTTPException(status_code=422, detail="Choose a rating from 1 to 5")
    if not isinstance(message, str) or len(message.strip()) < 3 or len(message) > 2000:
        raise HTTPException(status_code=422, detail="Review must be between 3 and 2000 characters")
    saved = {
        "id": str(uuid.uuid4()),
        "name": name.strip(),
        "branch": branch,
        "rating": rating,
        "message": message.strip(),
        "submittedAt": datetime.now(timezone.utc).isoformat(),
    }
    with engine.begin() as db:
        db.execute(insert(reviews_table).values(id=saved["id"], payload=saved, created_at=datetime.now(timezone.utc)))
    return saved


@app.get("/api/reviews", dependencies=[Depends(require_admin)])
def list_reviews() -> list[dict[str, Any]]:
    with engine.connect() as db:
        rows = db.execute(select(reviews_table.c.payload).order_by(reviews_table.c.created_at.desc())).fetchall()
    return [row[0] for row in rows]


@app.get("/api/deliveries/{phone}")
def track_delivery(phone: str) -> dict[str, Any]:
    normalized = re.sub(r"\D", "", phone)
    if len(normalized) < 7:
        raise HTTPException(status_code=400, detail="Enter the phone number used for the order")
    with engine.connect() as db:
        rows = db.execute(select(orders_table.c.payload).order_by(orders_table.c.created_at.desc())).fetchall()
    match = next(
        (row[0] for row in rows if row[0].get("ordering_mode") == "Delivery" and re.sub(r"\D", "", row[0].get("customer_phone", "")) == normalized),
        None,
    )
    if not match:
        raise HTTPException(status_code=404, detail="No delivery order was found for that phone number")
    return {
        "order_status": match.get("order_status", "New"),
        "delivery_status": match.get("delivery_status", "Waiting for rider"),
        "delivery_traffic": match.get("delivery_traffic", "Not reported"),
        "delivery_update": match.get("delivery_update", "Your order has been received and is waiting for a rider."),
        "order_timestamp": match.get("order_timestamp"),
    }


async def notify_branch(order: dict[str, Any]) -> None:
    if not ORDER_NOTIFICATION_WEBHOOK:
        return
    payload = {
        "event": "new_order",
        "recipient": {"branch": order["branch"], **BRANCH_NOTIFICATION_TARGETS[order["branch"]]},
        "order": order,
    }
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(ORDER_NOTIFICATION_WEBHOOK, json=payload)
            response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Branch notification webhook failed for order %s", order["id"])


@app.get("/")
def customer_site() -> FileResponse:
    return FileResponse(ROOT / "Customer Site.html")


@app.get("/admin")
def admin_site() -> FileResponse:
    return FileResponse(ROOT / "admin.html")


app.mount("/brand-assets", StaticFiles(directory=ROOT / "Assets( Swahili plate)"), name="brand-assets")