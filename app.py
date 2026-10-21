import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import uuid
from contextlib import asynccontextmanager
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import Boolean, Index, Integer, JSON, Column, DateTime, MetaData, String, Table, create_engine, delete, insert, inspect as sqlalchemy_inspect, select, text, update
from sqlalchemy.exc import IntegrityError

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
MASTER_ADMIN_TOKEN = os.getenv("MASTER_ADMIN_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
REVENUE_SETTING_KEY = "revenue_summary"
BUSINESS_TIMEZONE = ZoneInfo("Africa/Nairobi")
CUSTOMER_SESSION_COOKIE = "swahili_plate_customer"
CUSTOMER_SESSION_DURATION = timedelta(days=30)
CUSTOMER_ACCOUNT_TOKEN_DURATION = timedelta(hours=1)
CUSTOMER_ACCOUNT_TOKEN_RESEND_DELAY = timedelta(seconds=60)
CUSTOMER_GOOGLE_STATE_COOKIE = "swahili_plate_google_state"
CUSTOMER_GOOGLE_STATE_DURATION = timedelta(minutes=10)
CUSTOMER_PASSWORD_ITERATIONS = 600_000
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
ORDER_NOTIFICATION_WEBHOOK = os.getenv("ORDER_NOTIFICATION_WEBHOOK", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_WHATSAPP_CONTENT_SID = os.getenv("TWILIO_WHATSAPP_CONTENT_SID", "")
MPESA_ENV = os.getenv("MPESA_ENV", "sandbox").strip().lower()
MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY", "").strip()
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET", "").strip()
MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE", "").strip()
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY", "").strip()
MPESA_CALLBACK_TOKEN = os.getenv("MPESA_CALLBACK_TOKEN", "").strip()
MPESA_TRANSACTION_TYPE = os.getenv("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline").strip()
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

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {},
)
metadata = MetaData()


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting Swahili Plate application")
    try:
        initialize_database()
        yield
    finally:
        engine.dispose()
        logger.info("Stopped Swahili Plate application")


app = FastAPI(title="Swahili Plate Orders", version="1.0.0", lifespan=lifespan)

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
customer_accounts_table = Table(
    "customer_accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("email", String(254), nullable=False, unique=True),
    Column("phone", String(40), nullable=False),
    Column("password_hash", String(256), nullable=False),
    Column("email_verified", Boolean, nullable=False, default=True, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
customer_sessions_table = Table(
    "customer_sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("account_id", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)
customer_account_tokens_table = Table(
    "customer_account_tokens",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("account_id", String, nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)
Index(
    "uq_customer_account_token_purpose",
    customer_account_tokens_table.c.account_id,
    customer_account_tokens_table.c.purpose,
    unique=True,
)
mpesa_payment_attempts_table = Table(
    "mpesa_payment_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("order_id", String, nullable=False),
    Column("checkout_request_id", String, unique=True),
    Column("merchant_request_id", String),
    Column("status", String(24), nullable=False),
    Column("amount", Integer, nullable=False),
    Column("phone", String(20), nullable=False),
    Column("receipt_number", String(64)),
    Column("result_code", String(32)),
    Column("result_description", String(500)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index(
    "uq_mpesa_active_order_attempt",
    mpesa_payment_attempts_table.c.order_id,
    unique=True,
    sqlite_where=text("status IN ('initiating', 'pending', 'unknown')"),
    postgresql_where=text("status IN ('initiating', 'pending', 'unknown')"),
)


def authenticated_admin_role(x_admin_token: str | None = Header(default=None)) -> str:
    if not ADMIN_TOKEN and not MASTER_ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin access is not configured")
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Admin authentication required")
    if MASTER_ADMIN_TOKEN and hmac.compare_digest(x_admin_token, MASTER_ADMIN_TOKEN):
        return "master"
    if ADMIN_TOKEN and hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        return "admin"
    raise HTTPException(status_code=401, detail="Admin authentication required")


def is_strong_admin_secret(value: str) -> bool:
    lowered = value.casefold()
    return (
        len(value) >= 32
        and len(set(value)) >= 12
        and not any(marker in lowered for marker in ("replace-with", "change-me", "your-secret", "1234"))
    )


def require_admin(role: str = Depends(authenticated_admin_role)) -> None:
    return None


def require_master(role: str = Depends(authenticated_admin_role)) -> None:
    if role != "master":
        raise HTTPException(status_code=403, detail="Master account access required")


def hash_customer_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, CUSTOMER_PASSWORD_ITERATIONS)
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return f"pbkdf2_sha256${CUSTOMER_PASSWORD_ITERATIONS}${encode(salt)}${encode(digest)}"


def verify_customer_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded_hash.split("$", 3)
        iterations = int(raw_iterations)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 1_000_000:
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        salt = decode(raw_salt)
        expected = decode(raw_digest)
    except (AttributeError, TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def customer_session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def customer_account_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_customer_session(db: Any, account_id: str, now: datetime | None = None) -> str:
    created_at = now or datetime.now(timezone.utc)
    db.execute(delete(customer_sessions_table).where(customer_sessions_table.c.expires_at <= created_at))
    token = secrets.token_urlsafe(32)
    db.execute(insert(customer_sessions_table).values(
        token_hash=customer_session_hash(token),
        account_id=account_id,
        created_at=created_at,
        expires_at=created_at + CUSTOMER_SESSION_DURATION,
    ))
    return token


def create_customer_account_token(db: Any, account_id: str, purpose: str, now: datetime | None = None) -> str:
    created_at = now or datetime.now(timezone.utc)
    db.execute(delete(customer_account_tokens_table).where(
        customer_account_tokens_table.c.account_id == account_id,
        customer_account_tokens_table.c.purpose == purpose,
    ))
    token = secrets.token_urlsafe(40)
    db.execute(insert(customer_account_tokens_table).values(
        token_hash=customer_account_token_hash(token),
        account_id=account_id,
        purpose=purpose,
        created_at=created_at,
        expires_at=created_at + CUSTOMER_ACCOUNT_TOKEN_DURATION,
    ))
    return token


def customer_account_link(request: Request, route: str, token: str) -> str:
    base_url = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base_url}{route}?token={quote(token, safe='')}"


def set_customer_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        CUSTOMER_SESSION_COOKIE,
        token,
        max_age=int(CUSTOMER_SESSION_DURATION.total_seconds()),
        httponly=True,
        secure=APP_ENV == "production",
        samesite="lax",
        path="/",
    )


def google_customer_login_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)


def signed_customer_google_transaction(state: str, verifier: str) -> str:
    transaction = json.dumps(
        {"state": state, "verifier": verifier, "created_at": int(datetime.now(timezone.utc).timestamp())},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(transaction).decode("ascii").rstrip("=")
    signature = hmac.new(GOOGLE_CLIENT_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_customer_google_transaction(cookie_value: str | None, callback_state: str | None) -> str | None:
    if not cookie_value or not callback_state or not GOOGLE_CLIENT_SECRET:
        return None
    try:
        encoded, signature = cookie_value.rsplit(".", 1)
        expected_signature = hmac.new(
            GOOGLE_CLIENT_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        transaction = json.loads(raw)
        created_at = int(transaction["created_at"])
        now = int(datetime.now(timezone.utc).timestamp())
        verifier = transaction["verifier"]
        if not hmac.compare_digest(str(transaction["state"]), callback_state):
            return None
        if created_at > now or now - created_at > int(CUSTOMER_GOOGLE_STATE_DURATION.total_seconds()):
            return None
        if not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            return None
        return verifier
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def clear_customer_google_state_cookie(response: Response) -> None:
    response.delete_cookie(
        CUSTOMER_GOOGLE_STATE_COOKIE,
        path="/api/customer/google",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="lax",
    )


def customer_from_request(request: Request) -> dict[str, str] | None:
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE)
    if not token or len(token) > 512:
        return None
    token_hash = customer_session_hash(token)
    statement = (
        select(
            customer_accounts_table.c.id,
            customer_accounts_table.c.name,
            customer_accounts_table.c.email,
            customer_accounts_table.c.phone,
            customer_sessions_table.c.expires_at,
        )
        .join(customer_sessions_table, customer_sessions_table.c.account_id == customer_accounts_table.c.id)
        .where(customer_sessions_table.c.token_hash == token_hash)
    )
    with engine.connect() as db:
        row = db.execute(statement).mappings().first()
    if not row:
        return None
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        with engine.begin() as db:
            db.execute(delete(customer_sessions_table).where(customer_sessions_table.c.token_hash == token_hash))
        return None
    return {key: row[key] for key in ("id", "name", "email", "phone")}


def require_customer(request: Request) -> dict[str, str]:
    customer = customer_from_request(request)
    if customer is None:
        raise HTTPException(status_code=401, detail="Sign in to access your customer account")
    return customer


def validate_customer_account(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Account details must be a JSON object")
    name = payload.get("name")
    email = payload.get("email")
    phone = payload.get("phone")
    password = payload.get("password")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
        raise HTTPException(status_code=422, detail="Enter your name (up to 120 characters)")
    if not isinstance(email, str) or len(email.strip()) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    if not isinstance(phone, str) or not phone.strip() or len(phone.strip()) > 40:
        raise HTTPException(status_code=422, detail="Enter a valid phone number")
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        raise HTTPException(status_code=422, detail="Use a password between 10 and 128 characters")
    return {
        "name": name.strip(),
        "email": email.strip().lower(),
        "phone": phone.strip(),
        "password": password,
    }


CUSTOMER_PASSWORD_DUMMY_HASH = hash_customer_password("not-a-real-customer-password")


@app.get("/api/customer/google/start")
def start_customer_google_login() -> RedirectResponse:
    if not google_customer_login_configured():
        return RedirectResponse("/customer/?google=unavailable", status_code=303)

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    })
    response = RedirectResponse(authorization_url, status_code=302)
    response.set_cookie(
        CUSTOMER_GOOGLE_STATE_COOKIE,
        signed_customer_google_transaction(state, verifier),
        max_age=int(CUSTOMER_GOOGLE_STATE_DURATION.total_seconds()),
        httponly=True,
        secure=APP_ENV == "production",
        samesite="lax",
        path="/api/customer/google",
    )
    return response


@app.get("/api/customer/google/callback")
async def finish_customer_google_login(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    def result_redirect(result: str) -> RedirectResponse:
        response = RedirectResponse(f"/customer/?google={quote(result)}", status_code=303)
        clear_customer_google_state_cookie(response)
        return response

    if not google_customer_login_configured():
        return result_redirect("unavailable")
    verifier = verify_customer_google_transaction(request.cookies.get(CUSTOMER_GOOGLE_STATE_COOKIE), state)
    if verifier is None:
        return result_redirect("state")
    if error:
        return result_redirect("cancelled")
    if not code or len(code) > 2048:
        return result_redirect("error")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return result_redirect("error")

            profile_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            profile_response.raise_for_status()
            profile = profile_response.json()

        email = profile.get("email")
        verified_email = profile.get("email_verified")
        google_subject = profile.get("sub")
        if (
            verified_email is not True
            or not isinstance(google_subject, str)
            or not google_subject
            or not isinstance(email, str)
            or len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip())
        ):
            return result_redirect("error")

        normalized_email = email.strip().lower()
        with engine.connect() as db:
            account_row = db.execute(
                select(customer_accounts_table.c.id).where(customer_accounts_table.c.email == normalized_email)
            ).first()
        now = datetime.now(timezone.utc)
        previous_token = request.cookies.get(CUSTOMER_SESSION_COOKIE)

        if account_row:
            account_id = account_row[0]
            with engine.begin() as db:
                db.execute(update(customer_accounts_table).where(
                    customer_accounts_table.c.id == account_id
                ).values(email_verified=True))
                if previous_token:
                    db.execute(delete(customer_sessions_table).where(
                        customer_sessions_table.c.token_hash == customer_session_hash(previous_token)
                    ))
                session_token = create_customer_session(db, account_id, now)
        else:
            account_id = str(uuid.uuid4())
            display_name = profile.get("name")
            if not isinstance(display_name, str) or not display_name.strip():
                display_name = normalized_email.split("@", 1)[0]
            display_name = display_name.strip()[:120]
            # Google-only accounts still use an indistinguishable password hash so password checks stay uniform.
            password_hash = await asyncio.to_thread(hash_customer_password, secrets.token_urlsafe(48))
            try:
                with engine.begin() as db:
                    db.execute(insert(customer_accounts_table).values(
                        id=account_id,
                        name=display_name,
                        email=normalized_email,
                        phone="",
                        password_hash=password_hash,
                        email_verified=True,
                        created_at=now,
                    ))
                    if previous_token:
                        db.execute(delete(customer_sessions_table).where(
                            customer_sessions_table.c.token_hash == customer_session_hash(previous_token)
                        ))
                    session_token = create_customer_session(db, account_id, now)
            except IntegrityError:
                with engine.connect() as db:
                    account_row = db.execute(
                        select(customer_accounts_table.c.id).where(customer_accounts_table.c.email == normalized_email)
                    ).first()
                if not account_row:
                    raise
                account_id = account_row[0]
                with engine.begin() as db:
                    db.execute(update(customer_accounts_table).where(
                        customer_accounts_table.c.id == account_id
                    ).values(email_verified=True))
                    if previous_token:
                        db.execute(delete(customer_sessions_table).where(
                            customer_sessions_table.c.token_hash == customer_session_hash(previous_token)
                        ))
                    session_token = create_customer_session(db, account_id, now)

        response = result_redirect("connected")
        set_customer_session_cookie(response, session_token)
        return response
    except Exception as failure:
        logger.warning("Google customer sign-in failed (%s)", type(failure).__name__)
        return result_redirect("error")


def revenue_months(now: datetime | None = None) -> list[dict[str, str]]:
    current = (now or datetime.now(timezone.utc)).astimezone(BUSINESS_TIMEZONE)
    current_index = current.year * 12 + current.month - 1
    months = []
    for month_index in range(current_index - 5, current_index + 1):
        year, month_offset = divmod(month_index, 12)
        month = month_offset + 1
        months.append({
            "key": f"{year:04d}-{month:02d}",
            "label": datetime(year, month, 1).strftime("%B %Y"),
        })
    return months


def build_revenue_summary(rows: list[Any], now: datetime | None = None) -> dict[str, Any]:
    calculated_at = now or datetime.now(timezone.utc)
    if calculated_at.tzinfo is None:
        calculated_at = calculated_at.replace(tzinfo=BUSINESS_TIMEZONE)
    months = revenue_months(calculated_at)
    month_keys = {month["key"] for month in months}
    totals = {month["key"]: Decimal("0") for month in months}
    counts = {month["key"]: 0 for month in months}

    for payload, created_at in rows:
        if not isinstance(payload, dict) or payload.get("order_status") != "Completed":
            continue
        if payload.get("payment_method") == "M-Pesa STK Push" and payload.get("payment_state") != "paid":
            continue
        raw_timestamp = payload.get("order_timestamp")
        try:
            order_time = raw_timestamp if isinstance(raw_timestamp, datetime) else datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            order_time = created_at
        if not isinstance(order_time, datetime):
            continue
        if order_time.tzinfo is None:
            order_time = order_time.replace(tzinfo=timezone.utc)
        month_key = order_time.astimezone(BUSINESS_TIMEZONE).strftime("%Y-%m")
        if month_key not in month_keys:
            continue
        try:
            amount = Decimal(str(payload.get("grand_total", 0)))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not amount.is_finite() or amount < 0:
            continue
        totals[month_key] += amount
        counts[month_key] += 1

    def json_amount(value: Decimal) -> int | float:
        rounded = value.quantize(Decimal("0.01"))
        return int(rounded) if rounded == rounded.to_integral_value() else float(rounded)

    monthly = [
        {**month, "revenue": json_amount(totals[month["key"]]), "order_count": counts[month["key"]]}
        for month in months
    ]
    total = sum(totals.values(), Decimal("0"))
    local_now = calculated_at.astimezone(BUSINESS_TIMEZONE)
    return {
        "currency": "KES",
        "period_start": f"{months[0]['key']}-01",
        "period_end": local_now.date().isoformat(),
        "total_revenue": json_amount(total),
        "order_count": sum(counts.values()),
        "months": monthly,
        "calculated_at": calculated_at.astimezone(timezone.utc).isoformat(),
    }


def refresh_revenue_snapshot() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        rows = db.execute(select(orders_table.c.payload, orders_table.c.created_at)).fetchall()
        summary = build_revenue_summary(rows, now)
        dialect_insert = {
            "postgresql": postgres_insert,
            "sqlite": sqlite_insert,
        }.get(engine.dialect.name)
        values = {"setting_key": REVENUE_SETTING_KEY, "payload": summary, "updated_at": now}
        if dialect_insert:
            statement = dialect_insert(settings_table).values(**values).on_conflict_do_update(
                index_elements=[settings_table.c.setting_key],
                set_={"payload": summary, "updated_at": now},
            )
            db.execute(statement)
        else:
            exists = db.execute(
                select(settings_table.c.setting_key).where(settings_table.c.setting_key == REVENUE_SETTING_KEY)
            ).first()
            if exists:
                db.execute(
                    update(settings_table)
                    .where(settings_table.c.setting_key == REVENUE_SETTING_KEY)
                    .values(payload=summary, updated_at=now)
                )
            else:
                db.execute(insert(settings_table).values(**values))
    return summary


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
    payment_method = record.get("payment_method") or "Cash on collection / in person"
    allowed_payment_methods = {
        "M-Pesa STK Push",
        "Cash on collection / in person",
        "M-Pesa / payment contact",
        "Pay in person",
    }
    if not isinstance(payment_method, str) or payment_method not in allowed_payment_methods:
        raise HTTPException(status_code=422, detail="Choose a valid payment method")
    if payment_method == "M-Pesa STK Push" and not mpesa_configured():
        raise HTTPException(status_code=503, detail="M-Pesa online payment is not configured yet. Choose cash or contact the branch.")
    if len(record["customer_name"].strip()) > 120 or len(record["customer_phone"].strip()) > 40:
        raise HTTPException(status_code=422, detail="Name or phone number is too long")
    if record.get("customer_email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", record["customer_email"]):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    if "whatsapp_updates_opt_in" in record and not isinstance(record["whatsapp_updates_opt_in"], bool):
        raise HTTPException(status_code=422, detail="WhatsApp update consent must be true or false")
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
    clean.pop("customer_account_id", None)
    clean["customer_name"] = record["customer_name"].strip()
    clean["customer_phone"] = record["customer_phone"].strip()
    clean["customer_email"] = (record.get("customer_email") or "").strip()
    clean["whatsapp_updates_opt_in"] = record.get("whatsapp_updates_opt_in") is True
    clean["payment_method"] = payment_method
    clean["payment_state"] = "not_started" if payment_method == "M-Pesa STK Push" else "unpaid"
    clean["payment_status"] = "Awaiting M-Pesa payment" if payment_method == "M-Pesa STK Push" else "Payment due in person"
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
        if any(not valid_menu_image(item.get("image")) for item in items):
            raise HTTPException(status_code=422, detail="Menu images must use an HTTPS URL or a /brand-assets/ path")
    else:
        if not isinstance(value.get("business"), dict) or not isinstance(value.get("branches"), dict):
            raise HTTPException(status_code=422, detail="Branch settings must include business and branches")
        if not value["branches"] or any(not isinstance(branch, dict) for branch in value["branches"].values()):
            raise HTTPException(status_code=422, detail="Branch settings are invalid")
    normalize_setting_images(setting_key, value)
    return value


def valid_menu_image(image: Any) -> bool:
    if image is None or image == "":
        return True
    if not isinstance(image, str) or len(image) > 2048:
        return False
    image = image.strip()
    if not image:
        return True
    try:
        parsed = urlsplit(image)
    except ValueError:
        return False
    if parsed.scheme:
        return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password
    return canonical_asset_image(image) is not None


def canonical_asset_image(image: str) -> str | None:
    """Turn legacy relative asset URLs into stable /brand-assets URLs."""
    decoded = unquote(image.strip())
    if decoded.startswith("/brand-assets/"):
        relative = decoded.removeprefix("/brand-assets/")
    else:
        relative = decoded.lstrip("/")
        for prefix in ("customer/Assets( Swahili plate)/", "Assets( Swahili plate)/"):
            if relative.startswith(prefix):
                relative = relative.removeprefix(prefix)
                break
        else:
            return None
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts) or "\\" in relative:
        return None
    if relative.startswith("web-ready/"):
        relative = relative.removeprefix("web-ready/")
    if PurePosixPath(relative).suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    web_path = PurePosixPath("web-ready") / PurePosixPath(relative).with_suffix(".jpg")
    return "/brand-assets/" + quote(web_path.as_posix(), safe="/")


def normalize_setting_images(setting_key: str, value: dict[str, Any]) -> None:
    if setting_key == "menu":
        for item in value["food"] + value["drinks"]:
            image = item.get("image")
            if isinstance(image, str) and image:
                canonical = canonical_asset_image(image)
                if canonical:
                    item["image"] = canonical
    else:
        for branch in value["branches"].values():
            images = branch.get("images", [])
            if isinstance(images, list):
                branch["images"] = [
                    canonical_asset_image(image) or image if isinstance(image, str) else image
                    for image in images
                ]


def initialize_database() -> None:
    if APP_ENV == "production":
        if engine.dialect.name != "postgresql":
            raise RuntimeError("DATABASE_URL must point to durable PostgreSQL in production")
        if not is_strong_admin_secret(ADMIN_TOKEN):
            raise RuntimeError("ADMIN_TOKEN must be a unique random secret of at least 32 characters in production")
        if MASTER_ADMIN_TOKEN and not is_strong_admin_secret(MASTER_ADMIN_TOKEN):
            raise RuntimeError("MASTER_ADMIN_TOKEN must be a unique random secret of at least 32 characters")
        if PUBLIC_BASE_URL and not PUBLIC_BASE_URL.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
        if email_notifications_configured() and not PUBLIC_BASE_URL.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must be set to the HTTPS site URL when customer email is configured")
    if MASTER_ADMIN_TOKEN and ADMIN_TOKEN and hmac.compare_digest(MASTER_ADMIN_TOKEN, ADMIN_TOKEN):
        raise RuntimeError("MASTER_ADMIN_TOKEN must be different from ADMIN_TOKEN")
    metadata.create_all(engine)
    account_columns = {column["name"] for column in sqlalchemy_inspect(engine).get_columns("customer_accounts")}
    if "email_verified" not in account_columns:
        with engine.begin() as db:
            if engine.dialect.name == "postgresql":
                db.execute(text(
                    "ALTER TABLE customer_accounts ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT TRUE"
                ))
            else:
                db.execute(text(
                    "ALTER TABLE customer_accounts ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT TRUE"
                ))
    try:
        refresh_revenue_snapshot()
    except Exception:
        logger.exception("Could not initialize the stored revenue summary")


@app.get("/api/health")
def health() -> dict[str, str]:
    with engine.connect() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "swahili-plate-orders"}


@app.get("/api/menu-images", dependencies=[Depends(require_admin)])
def list_menu_images() -> dict[str, list[dict[str, str]]]:
    asset_root = ROOT / "Assets( Swahili plate)"
    drink_root = asset_root / "Swahili plate. assets"
    supported_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    drink_photo_names = {
        "Screenshot_20261010_092113_Gallery.jpg",
        "Screenshot_20261010_092114_Gallery.jpg",
        "Screenshot_20261010_092116_Gallery.jpg",
        "Screenshot_20261010_092118_Gallery.jpg",
        "Screenshot_20261010_092119_Gallery.jpg",
        "IMG-20261003-WA0000.jpg",
        "IMG-20261003-WA0001.jpg",
        "Swahili Plate Drinks Menu.pdf_20260917_082333_0000.png",
    }
    image_groups = {
        "food": list((asset_root / "menu-product-images").rglob("*")),
        "drinks": list(drink_root.glob("IMG-20261002-WA*.jpg"))
        + [drink_root / name for name in drink_photo_names],
    }
    images: list[dict[str, str]] = []
    for kind, paths in image_groups.items():
        for path in sorted(paths, key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.casefold() not in supported_extensions:
                continue
            relative = path.relative_to(asset_root).as_posix()
            images.append({
                "image": "/brand-assets/" + quote((PurePosixPath("web-ready") / PurePosixPath(relative).with_suffix(".jpg")).as_posix(), safe="/"),
                "label": path.name,
                "kind": kind,
            })
    return {"images": images}


@app.get("/api/customer-product-images")
def list_customer_product_images() -> dict[str, list[dict[str, str]]]:
    asset_root = ROOT / "Assets( Swahili plate)"
    supported_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    images: list[dict[str, str]] = []
    seen_stems: set[str] = set()

    search_dirs = [
        asset_root / "menu-product-images",
        asset_root / "Swahili plate. assets",
    ]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for path in sorted(search_dir.rglob("*"), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.casefold() not in supported_extensions:
                continue
            if "image review for fixing" in path.stem.casefold():
                continue
            stem_norm = path.stem.strip().lower()
            if stem_norm in seen_stems:
                continue
            seen_stems.add(stem_norm)
            rel_path = path.relative_to(asset_root).as_posix()
            images.append({
                "name": path.stem,
                "image": f"/brand-assets/{quote((PurePosixPath('web-ready') / PurePosixPath(rel_path).with_suffix('.jpg')).as_posix(), safe='/')}",
            })
    return {"images": images}


@app.post("/api/customer/accounts", status_code=201)
async def create_customer_account(request: Request, response: Response) -> dict[str, Any]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    account = validate_customer_account(raw)
    if not email_notifications_configured():
        raise HTTPException(status_code=503, detail="Account email verification is unavailable until SMTP is configured")
    account_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        exists = db.execute(
            select(customer_accounts_table.c.id).where(customer_accounts_table.c.email == account["email"])
        ).first()
    if exists:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in instead.")
    password_hash = await asyncio.to_thread(hash_customer_password, account["password"])
    try:
        with engine.begin() as db:
            db.execute(insert(customer_accounts_table).values(
                id=account_id,
                name=account["name"],
                email=account["email"],
                phone=account["phone"],
                password_hash=password_hash,
                email_verified=False,
                created_at=now,
            ))
            verification_token = create_customer_account_token(db, account_id, "verify", now)
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Sign in instead.") from error
    verification_link = customer_account_link(request, "/api/customer/account/verify", verification_token)
    sent = await send_transactional_email(
        account["email"],
        "Verify your Swahili Plate account",
        f"Habari {account['name']},\n\nConfirm your email address to activate your Swahili Plate account:\n{verification_link}\n\nThis link expires in one hour. If you did not create this account, you can ignore this email.\n\nKaribu Nyumbani,\nSwahili Plate",
    )
    return {
        "account": {key: account[key] for key in ("name", "email", "phone")} | {"id": account_id},
        "verification_required": True,
        "verification_sent": sent == "sent",
    }


@app.get("/api/customer/account/verify")
def verify_customer_account(token: str = "") -> RedirectResponse:
    if not token or len(token) > 256:
        return RedirectResponse("/customer/?account=verification_expired", status_code=303)
    now = datetime.now(timezone.utc)
    token_hash = customer_account_token_hash(token)
    with engine.begin() as db:
        token_row = db.execute(
            select(customer_account_tokens_table)
            .where(
                customer_account_tokens_table.c.token_hash == token_hash,
                customer_account_tokens_table.c.purpose == "verify",
            )
            .with_for_update()
        ).mappings().first()
        if not token_row:
            result = "verification_expired"
        else:
            expires_at = token_row["expires_at"]
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                db.execute(delete(customer_account_tokens_table).where(
                    customer_account_tokens_table.c.token_hash == token_hash
                ))
                result = "verification_expired"
            else:
                db.execute(update(customer_accounts_table).where(
                    customer_accounts_table.c.id == token_row["account_id"]
                ).values(email_verified=True))
                db.execute(delete(customer_account_tokens_table).where(
                    customer_account_tokens_table.c.account_id == token_row["account_id"]
                ))
                result = "verified"
    return RedirectResponse(f"/customer/?account={result}", status_code=303)


async def customer_account_email_request(request: Request, raw: Any, purpose: str) -> dict[str, bool]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Account details must be a JSON object")
    email = raw.get("email")
    if not isinstance(email, str) or len(email) > 254:
        return {"accepted": True}
    normalized_email = email.strip().lower()
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        row = db.execute(select(
            customer_accounts_table.c.id,
            customer_accounts_table.c.name,
            customer_accounts_table.c.email,
            customer_accounts_table.c.email_verified,
        ).where(customer_accounts_table.c.email == normalized_email).with_for_update()).mappings().first()
        if (
            not row
            or (purpose == "verify" and row["email_verified"])
            or (purpose == "password_reset" and not row["email_verified"])
            or not email_notifications_configured()
        ):
            return {"accepted": True}
        recent = db.execute(select(customer_account_tokens_table.c.created_at).where(
            customer_account_tokens_table.c.account_id == row["id"],
            customer_account_tokens_table.c.purpose == purpose,
        )).first()
        if recent:
            created_at = recent[0]
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if now - created_at < CUSTOMER_ACCOUNT_TOKEN_RESEND_DELAY:
                return {"accepted": True}
        token = create_customer_account_token(db, row["id"], purpose, now)
    if purpose == "verify":
        link = customer_account_link(request, "/api/customer/account/verify", token)
        subject = "Verify your Swahili Plate account"
        body = f"Habari {row['name']},\n\nConfirm your email address to activate your Swahili Plate account:\n{link}\n\nThis link expires in one hour.\n\nSwahili Plate"
    else:
        link = customer_account_link(request, "/customer/", token)
        link = f"{link}&reset=1"
        subject = "Reset your Swahili Plate password"
        body = f"Habari {row['name']},\n\nUse this link to set a new password for your account:\n{link}\n\nThis link expires in one hour. If you did not request a reset, you can ignore this email.\n\nSwahili Plate"
    await send_transactional_email(row["email"], subject, body)
    return {"accepted": True}


@app.post("/api/customer/account/verification/resend")
async def resend_customer_verification(request: Request) -> dict[str, bool]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    return await customer_account_email_request(request, raw, "verify")


@app.post("/api/customer/password-reset/request")
async def request_customer_password_reset(request: Request) -> dict[str, bool]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    return await customer_account_email_request(request, raw, "password_reset")


@app.post("/api/customer/password-reset")
async def reset_customer_password(request: Request) -> dict[str, bool]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Password reset details must be a JSON object")
    token = raw.get("token")
    password = raw.get("password")
    if not isinstance(token, str) or not token or len(token) > 256:
        raise HTTPException(status_code=400, detail="Password reset link is invalid or expired")
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        raise HTTPException(status_code=422, detail="Use a password between 10 and 128 characters")
    now = datetime.now(timezone.utc)
    with engine.connect() as db:
        token_row = db.execute(select(customer_account_tokens_table).where(
            customer_account_tokens_table.c.token_hash == customer_account_token_hash(token),
            customer_account_tokens_table.c.purpose == "password_reset",
        )).mappings().first()
    if not token_row:
        raise HTTPException(status_code=400, detail="Password reset link is invalid or expired")
    expires_at = token_row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        with engine.begin() as db:
            db.execute(delete(customer_account_tokens_table).where(
                customer_account_tokens_table.c.token_hash == token_row["token_hash"]
            ))
        raise HTTPException(status_code=400, detail="Password reset link is invalid or expired")
    password_hash = await asyncio.to_thread(hash_customer_password, password)
    with engine.begin() as db:
        current_token = db.execute(select(customer_account_tokens_table.c.account_id).where(
            customer_account_tokens_table.c.token_hash == token_row["token_hash"],
            customer_account_tokens_table.c.purpose == "password_reset",
        ).with_for_update()).first()
        if not current_token:
            raise HTTPException(status_code=400, detail="Password reset link is invalid or expired")
        account_id = current_token[0]
        db.execute(update(customer_accounts_table).where(
            customer_accounts_table.c.id == account_id
        ).values(password_hash=password_hash))
        db.execute(delete(customer_account_tokens_table).where(
            customer_account_tokens_table.c.account_id == account_id
        ))
        db.execute(delete(customer_sessions_table).where(customer_sessions_table.c.account_id == account_id))
    return {"success": True}


@app.post("/api/customer/session")
async def create_customer_session_endpoint(request: Request, response: Response) -> dict[str, Any]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Sign-in details must be a JSON object")
    email = raw.get("email")
    password = raw.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or len(password) > 128:
        await asyncio.to_thread(verify_customer_password, "invalid", CUSTOMER_PASSWORD_DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    normalized_email = email.strip().lower()
    with engine.connect() as db:
        row = db.execute(
            select(
                customer_accounts_table.c.id,
                customer_accounts_table.c.name,
                customer_accounts_table.c.email,
                customer_accounts_table.c.phone,
                customer_accounts_table.c.password_hash,
                customer_accounts_table.c.email_verified,
            ).where(customer_accounts_table.c.email == normalized_email)
        ).mappings().first()
    password_hash = row["password_hash"] if row else CUSTOMER_PASSWORD_DUMMY_HASH
    password_valid = await asyncio.to_thread(verify_customer_password, password, password_hash)
    if not password_valid or not row:
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    if not row["email_verified"]:
        raise HTTPException(status_code=403, detail="Verify your email before signing in. You can resend the verification email.")
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        previous_token = request.cookies.get(CUSTOMER_SESSION_COOKIE)
        if previous_token:
            db.execute(delete(customer_sessions_table).where(
                customer_sessions_table.c.token_hash == customer_session_hash(previous_token)
            ))
        session_token = create_customer_session(db, row["id"], now)
    set_customer_session_cookie(response, session_token)
    return {"account": {key: row[key] for key in ("id", "name", "email", "phone")}}


@app.delete("/api/customer/session", status_code=204)
def delete_customer_session(request: Request, response: Response) -> Response:
    token = request.cookies.get(CUSTOMER_SESSION_COOKIE)
    if token:
        with engine.begin() as db:
            db.execute(delete(customer_sessions_table).where(
                customer_sessions_table.c.token_hash == customer_session_hash(token)
            ))
    response.delete_cookie(
        CUSTOMER_SESSION_COOKIE,
        path="/",
        secure=APP_ENV == "production",
        httponly=True,
        samesite="lax",
    )
    response.status_code = 204
    return response


@app.get("/api/customer/account")
def get_customer_account(customer: dict[str, str] = Depends(require_customer)) -> dict[str, Any]:
    order_payload = orders_table.c.payload
    statement = (
        select(order_payload)
        .where(order_payload["customer_account_id"].as_string() == customer["id"])
        .order_by(order_payload["order_timestamp"].as_string().desc())
    )
    with engine.connect() as db:
        rows = db.execute(statement).fetchall()
    allowed_order_fields = (
        "id", "order_timestamp", "ordering_mode", "branch", "cart_items", "grand_total",
        "order_status", "delivery_status", "delivery_traffic", "delivery_update", "rider_name", "rider_phone",
        "payment_method", "payment_state", "payment_status", "mpesa_receipt_number",
    )
    orders = [
        {key: payload[key] for key in allowed_order_fields if key in payload}
        for (payload,) in rows if isinstance(payload, dict)
    ]
    return {"account": customer, "orders": orders}


@app.post("/api/orders", status_code=201)
async def create_order(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    order = validate_order(raw)
    customer = customer_from_request(request)
    if customer:
        order["customer_account_id"] = customer["id"]
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(insert(orders_table).values(id=order["id"], payload=order, created_at=now, updated_at=now))
    try:
        refresh_revenue_snapshot()
    except Exception:
        logger.exception("Could not refresh the stored revenue summary after order creation")
    await notify_branch(order)
    await send_customer_notifications(order, "New")
    return {key: value for key, value in order.items() if key != "customer_account_id"}


@app.get("/api/orders", dependencies=[Depends(require_admin)])
def list_orders() -> list[dict[str, Any]]:
    with engine.connect() as db:
        rows = db.execute(select(orders_table.c.payload).order_by(orders_table.c.created_at.desc())).fetchall()
    return [row[0] for row in rows]


@app.get("/api/admin/session")
def admin_session(role: str = Depends(authenticated_admin_role)) -> dict[str, str]:
    return {"role": role}


@app.get("/api/admin/revenue", dependencies=[Depends(require_master)])
def total_revenue() -> dict[str, Any]:
    return refresh_revenue_snapshot()


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
        previous = dict(row[0])
        updated = {**previous, **changes}
        if (updated.get("rider_name") or updated.get("rider_phone")) and not (updated.get("rider_name") and updated.get("rider_phone")):
            raise HTTPException(status_code=422, detail="Rider name and phone number must both be provided")
        if updated.get("order_status") == "Out for delivery" and (
            updated.get("ordering_mode") != "Delivery" or not updated.get("rider_name") or not updated.get("rider_phone")
        ):
            raise HTTPException(status_code=422, detail="Assign a rider name and phone before dispatching a delivery")
        db.execute(update(orders_table).where(orders_table.c.id == order_id).values(payload=updated, updated_at=datetime.now(timezone.utc)))
    try:
        refresh_revenue_snapshot()
    except Exception:
        logger.exception("Could not refresh the stored revenue summary after order update")
    event = changed_notification_event(previous, updated)
    if event:
        await send_customer_notifications(updated, event)
    return updated


@app.get("/api/settings/{setting_key}")
def get_setting(setting_key: str) -> dict[str, Any]:
    if setting_key not in {"menu", "branches"}:
        raise HTTPException(status_code=404, detail="Setting not found")
    with engine.connect() as db:
        row = db.execute(select(settings_table.c.payload).where(settings_table.c.setting_key == setting_key)).fetchone()
    if not row:
        return {"value": None}
    value = dict(row[0])
    normalize_setting_images(setting_key, value)
    return {"value": value}


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
        "id": match.get("id"),
        "customer_name": match.get("customer_name", "Valued Guest"),
        "customer_phone": match.get("customer_phone", ""),
        "branch": match.get("branch", ""),
        "order_status": match.get("order_status", "New"),
        "delivery_status": match.get("delivery_status", "Waiting for rider"),
        "delivery_traffic": match.get("delivery_traffic", "Not reported"),
        "delivery_update": match.get("delivery_update", "Your order has been received and is waiting for a rider."),
        "delivery_address": match.get("delivery_address", ""),
        "delivery_area": match.get("delivery_area", ""),
        "rider_name": match.get("rider_name", ""),
        "rider_phone": match.get("rider_phone", ""),
        "grand_total": match.get("grand_total", 0),
        "payment_method": match.get("payment_method", ""),
        "payment_state": match.get("payment_state", "not_started"),
        "payment_status": match.get("payment_status", "Payment pending"),
        "mpesa_receipt_number": match.get("mpesa_receipt_number", ""),
        "order_timestamp": match.get("order_timestamp"),
    }


@app.post("/api/orders/{order_id}/notify", dependencies=[Depends(require_admin)])
async def notify_customer(order_id: str) -> dict[str, Any]:
    with engine.connect() as db:
        row = db.execute(select(orders_table.c.payload).where(orders_table.c.id == order_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        order = dict(row[0])
    event = order.get("order_status") or "New"
    results = await send_customer_notifications(order, event)
    return {"status": "processed", "order_id": order_id, "event": event, "results": results}


def email_notifications_configured() -> bool:
    return all(has_configured_value(value) for value in (
        SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM_EMAIL,
    ))


def whatsapp_notifications_configured() -> bool:
    return bool(
        has_configured_value(TWILIO_ACCOUNT_SID)
        and has_configured_value(TWILIO_AUTH_TOKEN)
        and has_configured_value(TWILIO_WHATSAPP_FROM)
        and has_configured_value(TWILIO_WHATSAPP_CONTENT_SID)
    )


def mpesa_configured() -> bool:
    return bool(
        MPESA_ENV in {"sandbox", "production"}
        and has_configured_value(MPESA_CONSUMER_KEY)
        and has_configured_value(MPESA_CONSUMER_SECRET)
        and has_configured_value(MPESA_SHORTCODE)
        and has_configured_value(MPESA_PASSKEY)
        and len(MPESA_CALLBACK_TOKEN) >= 32
        and has_configured_value(MPESA_CALLBACK_TOKEN)
        and PUBLIC_BASE_URL.startswith("https://")
        and "your-domain.example" not in PUBLIC_BASE_URL.casefold()
        and MPESA_TRANSACTION_TYPE in {"CustomerPayBillOnline", "CustomerBuyGoodsOnline"}
    )


def has_configured_value(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return False
    return not any(marker in normalized for marker in (
        "replace-with", "example.com", "xxxxxxxx", "use-a-google-app-password",
    ))


def mpesa_api_root() -> str:
    if MPESA_ENV == "production":
        return "https://api.safaricom.co.ke"
    return "https://sandbox.safaricom.co.ke"


async def mpesa_access_token(client: httpx.AsyncClient) -> str:
    response = await client.get(
        f"{mpesa_api_root()}/oauth/v1/generate?grant_type=client_credentials",
        auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Daraja did not return an access token")
    return token


def update_order_payment(order_id: str, payment_state: str, payment_status: str, receipt: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    with engine.begin() as db:
        row = db.execute(
            select(orders_table.c.payload).where(orders_table.c.id == order_id).with_for_update()
        ).first()
        if not row:
            return
        payload = dict(row[0])
        if payload.get("payment_state") == "paid" and payment_state != "paid":
            return
        payload["payment_state"] = payment_state
        payload["payment_status"] = payment_status
        if receipt:
            payload["mpesa_receipt_number"] = receipt
        db.execute(update(orders_table).where(orders_table.c.id == order_id).values(
            payload=payload,
            updated_at=now,
        ))


def current_mpesa_payment(order_id: str) -> dict[str, Any] | None:
    with engine.connect() as db:
        row = db.execute(
            select(mpesa_payment_attempts_table)
            .where(mpesa_payment_attempts_table.c.order_id == order_id)
            .order_by(mpesa_payment_attempts_table.c.created_at.desc())
            .limit(1)
        ).mappings().first()
    return dict(row) if row else None


@app.get("/api/payment/config")
def payment_configuration() -> dict[str, bool]:
    return {"mpesa_stk_push": mpesa_configured()}


@app.post("/api/orders/{order_id}/payments/mpesa", status_code=202)
async def start_mpesa_payment(order_id: str) -> dict[str, Any]:
    if not mpesa_configured():
        raise HTTPException(status_code=503, detail="M-Pesa online payment is not configured yet.")
    with engine.connect() as db:
        row = db.execute(select(orders_table.c.payload).where(orders_table.c.id == order_id)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    order = dict(row[0])
    if order.get("payment_method") != "M-Pesa STK Push":
        raise HTTPException(status_code=422, detail="This order does not use M-Pesa STK Push")
    if order.get("payment_state") == "paid":
        return {"payment_state": "paid", "message": "Payment is already complete."}

    active_statuses = ("initiating", "pending", "unknown")
    with engine.connect() as db:
        active = db.execute(
            select(mpesa_payment_attempts_table)
            .where(
                mpesa_payment_attempts_table.c.order_id == order_id,
                mpesa_payment_attempts_table.c.status.in_(active_statuses),
            )
            .order_by(mpesa_payment_attempts_table.c.created_at.desc())
            .limit(1)
        ).mappings().first()
    if active:
        return {
            "payment_state": active["status"],
            "message": "An M-Pesa request is already active. Check your phone before trying again.",
        }

    phone = format_customer_phone(str(order.get("customer_phone") or ""))
    if not phone:
        raise HTTPException(status_code=422, detail="Enter a valid Kenyan phone number for M-Pesa")
    try:
        amount_value = Decimal(str(order.get("grand_total", "")))
        if not amount_value.is_finite() or amount_value <= 0:
            raise InvalidOperation
        amount = int(amount_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Order total is invalid for M-Pesa payment")
    if amount < 1:
        raise HTTPException(status_code=422, detail="M-Pesa payment must be at least KES 1")

    now = datetime.now(timezone.utc)
    attempt_id = str(uuid.uuid4())
    try:
        with engine.begin() as db:
            db.execute(insert(mpesa_payment_attempts_table).values(
                id=attempt_id,
                order_id=order_id,
                checkout_request_id=None,
                merchant_request_id=None,
                status="initiating",
                amount=amount,
                phone=phone,
                receipt_number=None,
                result_code=None,
                result_description=None,
                created_at=now,
                updated_at=now,
            ))
            db.execute(update(orders_table).where(orders_table.c.id == order_id).values(
                payload={**order, "payment_state": "initiating", "payment_status": "Starting M-Pesa prompt"},
                updated_at=now,
            ))
    except IntegrityError:
        active = current_mpesa_payment(order_id)
        return {
            "payment_state": active["status"] if active else "unknown",
            "message": "An M-Pesa request is already active. Check your phone before trying again.",
        }

    timestamp = datetime.now(BUSINESS_TIMEZONE).strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode("utf-8")).decode("ascii")
    callback_url = f"{PUBLIC_BASE_URL}/api/payments/mpesa/callback/{quote(MPESA_CALLBACK_TOKEN, safe='')}"
    request_payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": MPESA_TRANSACTION_TYPE,
        "Amount": amount,
        "PartyA": phone.removeprefix("+"),
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone.removeprefix("+"),
        "CallBackURL": callback_url,
        "AccountReference": f"SP{order_id.replace('-', '')[-10:]}"[:12],
        "TransactionDesc": "Swahili Plate order",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            access_token = await mpesa_access_token(client)
            response = await client.post(
                f"{mpesa_api_root()}/mpesa/stkpush/v1/processrequest",
                json=request_payload,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            response.raise_for_status()
            result = response.json()
        if str(result.get("ResponseCode")) != "0" or not result.get("CheckoutRequestID"):
            description = str(result.get("ResponseDescription") or result.get("errorMessage") or "M-Pesa did not accept the request")[:500]
            with engine.begin() as db:
                db.execute(update(mpesa_payment_attempts_table).where(
                    mpesa_payment_attempts_table.c.id == attempt_id
                ).values(status="failed", result_description=description, updated_at=datetime.now(timezone.utc)))
            update_order_payment(order_id, "failed", "M-Pesa prompt could not start. Retry or contact the branch.")
            raise HTTPException(status_code=502, detail="M-Pesa could not start the payment prompt. Please retry or choose cash.")
        checkout_request_id = str(result["CheckoutRequestID"])
        merchant_request_id = str(result.get("MerchantRequestID") or "")
        with engine.begin() as db:
            db.execute(update(mpesa_payment_attempts_table).where(
                mpesa_payment_attempts_table.c.id == attempt_id
            ).values(
                checkout_request_id=checkout_request_id,
                merchant_request_id=merchant_request_id,
                status="pending",
                result_description=str(result.get("CustomerMessage") or "Prompt sent")[:500],
                updated_at=datetime.now(timezone.utc),
            ))
        update_order_payment(order_id, "pending", "M-Pesa prompt sent. Complete the request on your phone.")
        return {"payment_state": "pending", "message": "Check your phone and enter your M-Pesa PIN to complete payment."}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        with engine.begin() as db:
            db.execute(update(mpesa_payment_attempts_table).where(
                mpesa_payment_attempts_table.c.id == attempt_id
            ).values(status="unknown", result_description="Provider response timed out", updated_at=datetime.now(timezone.utc)))
        update_order_payment(order_id, "unknown", "M-Pesa response is delayed. Check your phone before retrying.")
        return {"payment_state": "unknown", "message": "M-Pesa did not confirm the request. Check your phone before retrying."}
    except httpx.HTTPStatusError as error:
        status = "unknown" if error.response.status_code >= 500 else "failed"
        with engine.begin() as db:
            db.execute(update(mpesa_payment_attempts_table).where(
                mpesa_payment_attempts_table.c.id == attempt_id
            ).values(status=status, result_description="Daraja rejected the request", updated_at=datetime.now(timezone.utc)))
        update_order_payment(order_id, status, "M-Pesa request needs attention. Check your phone or contact the branch.")
        logger.warning("Daraja returned HTTP %s for order %s", error.response.status_code, order_id)
        raise HTTPException(status_code=502, detail="M-Pesa could not start the payment prompt. Please retry or choose cash.") from error
    except (httpx.NetworkError, ValueError, KeyError):
        with engine.begin() as db:
            db.execute(update(mpesa_payment_attempts_table).where(
                mpesa_payment_attempts_table.c.id == attempt_id
            ).values(status="unknown", result_description="Daraja response could not be confirmed", updated_at=datetime.now(timezone.utc)))
        update_order_payment(order_id, "unknown", "M-Pesa response is delayed. Check your phone before retrying.")
        logger.exception("Could not confirm Daraja payment initiation for order %s", order_id)
        return {"payment_state": "unknown", "message": "M-Pesa did not confirm the request. Check your phone before retrying."}


@app.get("/api/orders/{order_id}/payment")
def get_order_payment(order_id: str) -> dict[str, Any]:
    with engine.connect() as db:
        order_row = db.execute(select(orders_table.c.payload).where(orders_table.c.id == order_id)).first()
    if not order_row:
        raise HTTPException(status_code=404, detail="Order not found")
    order = order_row[0]
    return {
        "payment_state": order.get("payment_state", "not_started"),
        "payment_status": order.get("payment_status", "Payment pending"),
        "receipt_number": order.get("mpesa_receipt_number"),
    }


@app.post("/api/payments/mpesa/callback/{callback_token}", include_in_schema=False)
async def receive_mpesa_callback(callback_token: str, request: Request) -> dict[str, Any]:
    if not MPESA_CALLBACK_TOKEN or not hmac.compare_digest(callback_token, MPESA_CALLBACK_TOKEN):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Callback body must be valid JSON") from error
    callback = raw.get("Body", {}).get("stkCallback", {}) if isinstance(raw, dict) else {}
    checkout_request_id = callback.get("CheckoutRequestID") if isinstance(callback, dict) else None
    if not isinstance(checkout_request_id, str) or not checkout_request_id or len(checkout_request_id) > 128:
        raise HTTPException(status_code=422, detail="Callback request ID is invalid")
    result_code = str(callback.get("ResultCode", ""))
    result_description = str(callback.get("ResultDesc") or "Payment result received")[:500]
    metadata_items: dict[str, Any] = {}
    callback_metadata = callback.get("CallbackMetadata", {})
    if isinstance(callback_metadata, dict):
        items = callback_metadata.get("Item", [])
        if isinstance(items, list):
            metadata_items = {
                item.get("Name"): item.get("Value")
                for item in items
                if isinstance(item, dict) and isinstance(item.get("Name"), str)
            }
    receipt = metadata_items.get("MpesaReceiptNumber")
    receipt = str(receipt)[:64] if receipt is not None else None
    now = datetime.now(timezone.utc)
    order_id: str | None = None
    final_state = "failed"
    with engine.begin() as db:
        attempt = db.execute(
            select(mpesa_payment_attempts_table)
            .where(mpesa_payment_attempts_table.c.checkout_request_id == checkout_request_id)
            .with_for_update()
        ).mappings().first()
        if not attempt:
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        order_id = attempt["order_id"]
        if attempt["status"] == "paid":
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        if result_code == "0":
            try:
                callback_amount = Decimal(str(metadata_items.get("Amount", "")))
                callback_phone = format_customer_phone(str(metadata_items.get("PhoneNumber", "")))
                payment_matches = callback_amount == Decimal(attempt["amount"]) and callback_phone == attempt["phone"]
            except (InvalidOperation, TypeError, ValueError):
                payment_matches = False
            if payment_matches and receipt:
                final_state = "paid"
            else:
                final_state = "unknown"
                result_description = "Payment result did not match the expected amount or phone; review required"
        db.execute(update(mpesa_payment_attempts_table).where(
            mpesa_payment_attempts_table.c.id == attempt["id"]
        ).values(
            status=final_state,
            receipt_number=receipt if final_state == "paid" else None,
            result_code=result_code[:32],
            result_description=result_description,
            updated_at=now,
        ))
        latest = db.execute(
            select(mpesa_payment_attempts_table.c.id)
            .where(mpesa_payment_attempts_table.c.order_id == order_id)
            .order_by(mpesa_payment_attempts_table.c.created_at.desc())
            .limit(1)
        ).first()
        if final_state == "paid":
            db.execute(update(mpesa_payment_attempts_table).where(
                mpesa_payment_attempts_table.c.order_id == order_id,
                mpesa_payment_attempts_table.c.id != attempt["id"],
                mpesa_payment_attempts_table.c.status.in_(("initiating", "pending", "unknown")),
            ).values(status="cancelled", result_description="Another attempt completed successfully", updated_at=now))
        if final_state == "paid" or (latest and latest[0] == attempt["id"]):
            order_row = db.execute(
                select(orders_table.c.payload).where(orders_table.c.id == order_id).with_for_update()
            ).first()
            if order_row:
                order_payload = dict(order_row[0])
                order_payload["payment_state"] = final_state
                order_payload["payment_status"] = (
                    f"Paid — receipt {receipt}" if final_state == "paid" else
                    "Payment result is being reviewed. Contact the branch before retrying."
                    if final_state == "unknown" else
                    "M-Pesa payment was not completed. Retry or choose cash."
                )
                if final_state == "paid":
                    order_payload["mpesa_receipt_number"] = receipt
                db.execute(update(orders_table).where(orders_table.c.id == order_id).values(
                    payload=order_payload,
                    updated_at=now,
                ))
    if final_state == "paid":
        try:
            refresh_revenue_snapshot()
        except Exception:
            logger.exception("Could not refresh revenue after M-Pesa confirmation")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


def format_customer_phone(phone: str) -> str | None:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9:
        digits = "254" + digits
    if not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def customer_update_text(order: dict[str, Any], event: str) -> tuple[str, str]:
    short_id = str(order.get("id", ""))[-6:].upper()
    status_messages = {
        "New": "We have received your order and sent it to the branch team.",
        "Confirmed": "The branch team has confirmed your order and is acting on it.",
        "Preparing": "Your order is now being prepared by the branch team.",
        "Ready": "Your order is ready.",
        "Out for delivery": "Your order has been dispatched and is on its way.",
        "Accepted": "A rider has accepted your delivery.",
        "Picked up": "Your rider has picked up the order.",
        "On the way": "Your order is on the way to you.",
        "Delayed": "Your delivery is delayed. The branch team will keep you updated.",
        "Delivered": "Your order has been delivered. Thank you for ordering with us.",
        "Completed": "Your order is complete. Thank you for ordering with us.",
        "Cancelled": "Your order has been cancelled. Please contact the branch if you need help.",
    }
    message = status_messages.get(event, f"Your order status is now {event}.")
    subject = f"Swahili Plate order #{short_id}: {event}"
    lines = [
        f"Habari {order.get('customer_name') or 'there'},",
        "",
        f"{message}",
        "",
        f"Order: #{short_id}",
        f"Status: {event}",
        f"Branch: {order.get('branch') or 'Swahili Plate'}",
    ]
    if order.get("rider_name"):
        lines.extend([f"Rider: {order['rider_name']}", f"Rider phone: {order.get('rider_phone') or 'Contact the branch'}"])
    if order.get("delivery_address"):
        lines.append(f"Delivery address: {order['delivery_address']}")
    lines.extend(["", "You can check the latest delivery status on our website using the phone number from checkout.", "", "Karibu Nyumbani,", "Swahili Plate"])
    return subject, "\n".join(lines)


async def send_email_notification(order: dict[str, Any], subject: str, body: str) -> str:
    return await send_transactional_email(order["customer_email"], subject, body)


async def send_transactional_email(recipient: str, subject: str, body: str) -> str:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM_EMAIL
    message["To"] = recipient
    message.set_content(body)

    def deliver() -> None:
        smtp_class = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
        with smtp_class(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if not SMTP_USE_SSL:
                server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)

    try:
        await asyncio.to_thread(deliver)
        return "sent"
    except Exception:
        logger.exception("Transactional email delivery failed")
        return "failed"


async def send_whatsapp_notification(order: dict[str, Any], event: str, status_message: str) -> str:
    phone = format_customer_phone(order.get("customer_phone", ""))
    if not phone:
        return "invalid_phone"
    sender = TWILIO_WHATSAPP_FROM if TWILIO_WHATSAPP_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_FROM}"
    variables = {
        "1": order.get("customer_name") or "there",
        "2": str(order.get("id", ""))[-6:].upper(),
        "3": status_message,
        "4": order.get("rider_name") or "Not assigned",
        "5": order.get("rider_phone") or "Not assigned",
        "6": order.get("branch") or "Swahili Plate",
        "7": event,
    }
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                data={
                    "From": sender,
                    "To": f"whatsapp:{phone}",
                    "ContentSid": TWILIO_WHATSAPP_CONTENT_SID,
                    "ContentVariables": json.dumps(variables),
                },
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            )
            response.raise_for_status()
        return "sent"
    except httpx.HTTPError:
        logger.exception("Customer WhatsApp notification failed for order %s", order.get("id"))
        return "failed"


def changed_notification_event(previous: dict[str, Any], updated: dict[str, Any]) -> str | None:
    if previous.get("order_status") != updated.get("order_status"):
        return updated.get("order_status") or "New"
    if updated.get("order_status") == "Out for delivery" and any(
        previous.get(field) != updated.get(field) for field in ("rider_name", "rider_phone")
    ):
        return "Out for delivery"
    if previous.get("delivery_status") != updated.get("delivery_status"):
        return updated.get("delivery_status") or None
    return None


async def send_customer_notifications(order: dict[str, Any], event: str) -> dict[str, str]:
    subject, body = customer_update_text(order, event)
    results: dict[str, str] = {}
    if not order.get("customer_email"):
        results["email"] = "skipped_missing_address"
    elif not email_notifications_configured():
        results["email"] = "not_configured"
    else:
        results["email"] = await send_email_notification(order, subject, body)

    if not order.get("whatsapp_updates_opt_in"):
        results["whatsapp"] = "skipped_no_consent"
    elif not order.get("customer_phone"):
        results["whatsapp"] = "skipped_missing_number"
    elif not whatsapp_notifications_configured():
        results["whatsapp"] = "not_configured"
    else:
        results["whatsapp"] = await send_whatsapp_notification(order, event, body.split("\n\n", 1)[1].split("\n\nOrder:", 1)[0])

    record = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channels": results,
    }
    order.setdefault("notification_history", []).append(record)
    order["notification_history"] = order["notification_history"][-30:]
    with engine.begin() as db:
        row = db.execute(select(orders_table.c.payload).where(orders_table.c.id == order["id"])).fetchone()
        if row:
            persisted = dict(row[0])
            persisted.setdefault("notification_history", []).append(record)
            persisted["notification_history"] = persisted["notification_history"][-30:]
            db.execute(
                update(orders_table)
                .where(orders_table.c.id == order["id"])
                .values(payload=persisted, updated_at=datetime.now(timezone.utc))
            )
            order["notification_history"] = persisted["notification_history"]
    return results


@app.get("/api/notifications/status", dependencies=[Depends(require_admin)])
def notification_status() -> dict[str, Any]:
    return {
        "email_configured": email_notifications_configured(),
        "whatsapp_configured": whatsapp_notifications_configured(),
        "mpesa_stk_push_configured": mpesa_configured(),
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
def customer_site_redirect() -> RedirectResponse:
    return RedirectResponse("/customer/", status_code=307)


@app.get("/customer")
def customer_site_clean_redirect() -> RedirectResponse:
    return RedirectResponse("/customer/", status_code=307)


@app.get("/customer/")
def customer_site() -> FileResponse:
    return FileResponse(ROOT / "Customer Site.html")


@app.get("/admin")
def admin_site_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/", status_code=307)


@app.get("/admin/")
def admin_site() -> FileResponse:
    return FileResponse(ROOT / "admin.html")


@app.get("/customer/manifest.webmanifest", include_in_schema=False)
def customer_manifest() -> FileResponse:
    return FileResponse(ROOT / "customer.webmanifest", media_type="application/manifest+json")


@app.get("/customer/service-worker.js", include_in_schema=False)
def customer_service_worker() -> FileResponse:
    return FileResponse(ROOT / "customer-sw.js", media_type="application/javascript")


@app.get("/admin/manifest.webmanifest", include_in_schema=False)
def admin_manifest() -> FileResponse:
    return FileResponse(ROOT / "admin.webmanifest", media_type="application/manifest+json")


@app.get("/admin/service-worker.js", include_in_schema=False)
def admin_service_worker() -> FileResponse:
    return FileResponse(ROOT / "admin-sw.js", media_type="application/javascript")


brand_assets = StaticFiles(directory=ROOT / "Assets( Swahili plate)")
app.mount("/brand-assets", brand_assets, name="brand-assets")
# Relative asset URLs work from both the customer page and local file previews.
app.mount("/customer/Assets( Swahili plate)", brand_assets, name="customer-brand-assets")
app.mount("/customer/Assets(%20Swahili%20plate)", brand_assets, name="customer-brand-assets-encoded")
app.mount("/Assets( Swahili plate)", brand_assets, name="root-brand-assets")
app.mount("/Assets(%20Swahili%20plate)", brand_assets, name="root-brand-assets-encoded")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
