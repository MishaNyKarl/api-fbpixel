import hashlib
import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import redis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine, func
from sqlalchemy.orm import Session, declarative_base, sessionmaker


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api-pixel")

load_dotenv()

API_PUBLIC_KEY = os.getenv("API_PUBLIC_KEY", "")
META_PIXEL_ID = os.getenv("META_PIXEL_ID", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_TEST_EVENT_CODE = os.getenv("META_TEST_EVENT_CODE", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DB_PATH = os.getenv("DB_PATH", "/opt/api-pixel/api-pixel.db")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
STRICT_DOMAIN_CHECK = os.getenv("STRICT_DOMAIN_CHECK", "false").lower() in {"1", "true", "yes", "on"}
CORS_ALLOW_ORIGINS = os.getenv("CORS_ALLOW_ORIGINS", "*")
DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "600"))
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Moscow")
WHALE_TIKTOK_SECRET = os.getenv("WHALE_TIKTOK_SECRET", "")
TIKTOK_ALLOWED_STATUSES = {
    item.strip().lower()
    for item in os.getenv("TIKTOK_ALLOWED_STATUSES", "Approved,Paid").split(",")
    if item.strip()
}
TIKTOK_SEND_WITHOUT_TTCLID = os.getenv("TIKTOK_SEND_WITHOUT_TTCLID", "true").lower() in {"1", "true", "yes", "on"}
TIKTOK_EVENTS_API_URL = os.getenv("TIKTOK_EVENTS_API_URL", "https://business-api.tiktok.com/open_api/v1.3/event/track/")
TIKTOK_TIMEOUT_SECONDS = float(os.getenv("TIKTOK_TIMEOUT_SECONDS", "10"))

META_LOG_KEY = "log:meta"
META_EVENT_LOG_KEY_PREFIX = "log:meta:event:"
META_CLICK_LOG_KEY_PREFIX = "log:meta:clickid:"
TIKTOK_LOG_KEY = "log:tiktok"
TIKTOK_EVENT_LOG_KEY_PREFIX = "log:tiktok:event:"
TIKTOK_CLICK_LOG_KEY_PREFIX = "log:tiktok:clickid:"
META_LOG_MAX = int(os.getenv("META_LOG_MAX", "10000"))
META_EVENT_LOG_MAX = int(os.getenv("META_EVENT_LOG_MAX", "5000"))
META_CLICK_LOG_MAX = int(os.getenv("META_CLICK_LOG_MAX", "200"))
TIKTOK_LOG_MAX = int(os.getenv("TIKTOK_LOG_MAX", str(META_LOG_MAX)))
TIKTOK_EVENT_LOG_MAX = int(os.getenv("TIKTOK_EVENT_LOG_MAX", str(META_EVENT_LOG_MAX)))
TIKTOK_CLICK_LOG_MAX = int(os.getenv("TIKTOK_CLICK_LOG_MAX", str(META_CLICK_LOG_MAX)))
ADMIN_LOG_LIMIT_MAX = int(os.getenv("ADMIN_LOG_LIMIT_MAX", "1000"))
CLICK_TTL = 60 * 60 * 24 * 11

try:
    APP_TZ = ZoneInfo(APP_TIMEZONE)
except ZoneInfoNotFoundError:
    if APP_TIMEZONE == "Europe/Moscow":
        log.warning("System timezone Europe/Moscow is unavailable, falling back to fixed MSK UTC+3")
        APP_TZ = timezone(timedelta(hours=3), "MSK")
    else:
        log.warning("Unknown APP_TIMEZONE=%s, falling back to UTC", APP_TIMEZONE)
        APP_TIMEZONE = "UTC"
        APP_TZ = timezone.utc


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def app_now() -> datetime:
    return utc_now().astimezone(APP_TZ)


def naive_app_now() -> datetime:
    return app_now().replace(tzinfo=None)


app = FastAPI(title="API Pixel MVP (Multi Pixel CAPI + Lemonad)")
security = HTTPBasic()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

allow_origins = ["*"] if CORS_ALLOW_ORIGINS.strip() == "*" else [
    item.strip() for item in CORS_ALLOW_ORIGINS.split(",") if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- Database ----------
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class Pixel(Base):
    __tablename__ = "pixels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    buyer_name = Column(String(255), nullable=True)
    meta_pixel_id = Column(String(255), nullable=False)
    meta_access_token = Column(Text, nullable=False)
    allowed_domains = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=naive_app_now)
    updated_at = Column(DateTime, nullable=False, default=naive_app_now, onupdate=naive_app_now)


class TikTokPixel(Base):
    __tablename__ = "tiktok_pixels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    buyer_name = Column(String(255), nullable=True)
    dataset_id = Column(String(255), nullable=False)
    access_token = Column(Text, nullable=False)
    event_name = Column(String(128), nullable=False, default="CompletePayment")
    currency = Column(String(16), nullable=False, default="USD")
    allowed_statuses = Column(Text, nullable=True)
    flow_ids = Column(Text, nullable=True)
    test_event_code = Column(String(255), nullable=True)
    send_without_ttclid = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=naive_app_now)
    updated_at = Column(DateTime, nullable=False, default=naive_app_now, onupdate=naive_app_now)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    buyer_name = Column(String(255), nullable=True)
    role = Column(String(32), nullable=False, default="buyer")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=naive_app_now)
    updated_at = Column(DateTime, nullable=False, default=naive_app_now, onupdate=naive_app_now)


Base.metadata.create_all(bind=engine)


def format_app_datetime(value: datetime) -> str:
    return value.astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_app_timestamp(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return format_app_datetime(datetime.fromtimestamp(int(ts), timezone.utc))


def app_date_bounds(date_from: Optional[str], date_to: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    if not date_from and not date_to:
        return None, None

    start_ts = None
    end_ts = None
    try:
        if date_from:
            start_dt = datetime.fromisoformat(date_from).replace(tzinfo=APP_TZ)
            start_ts = int(start_dt.astimezone(timezone.utc).timestamp())
        if date_to:
            end_dt = datetime.fromisoformat(date_to).replace(tzinfo=APP_TZ) + timedelta(days=1)
            end_ts = int(end_dt.astimezone(timezone.utc).timestamp())
    except ValueError:
        return None, None
    return start_ts, end_ts


def app_date_shortcuts() -> Dict[str, str]:
    today = app_now().date()
    return {
        "today": today.isoformat(),
        "yesterday": (today - timedelta(days=1)).isoformat(),
        "week": (today - timedelta(days=6)).isoformat(),
    }


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str) -> str:
    rounds = 260_000
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, rounds_raw, salt, expected = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds).hex()
        return secrets.compare_digest(digest, expected)
    except Exception:
        return False


def bootstrap_admin_user():
    if not ADMIN_PASSWORD:
        return
    db = SessionLocal()
    try:
        if db.query(AdminUser).first():
            return
        user = AdminUser(
            username=ADMIN_USERNAME,
            password_hash=hash_password(ADMIN_PASSWORD),
            buyer_name=ADMIN_USERNAME,
            role="admin",
            is_active=True,
            created_at=naive_app_now(),
            updated_at=naive_app_now(),
        )
        db.add(user)
        db.commit()
        log.info("Bootstrapped admin user from ADMIN_USERNAME")
    finally:
        db.close()


bootstrap_admin_user()


def generate_public_id() -> str:
    return "px_" + secrets.token_urlsafe(8)


def generate_tiktok_public_id() -> str:
    return "tt_" + secrets.token_urlsafe(8)


def generate_unique_public_id(db: Session) -> str:
    for _ in range(10):
        public_id = generate_public_id()
        if not db.query(Pixel).filter(Pixel.public_id == public_id).first():
            return public_id
    raise RuntimeError("failed_to_generate_public_id")


def generate_unique_tiktok_public_id(db: Session) -> str:
    for _ in range(10):
        public_id = generate_tiktok_public_id()
        if not db.query(TikTokPixel).filter(TikTokPixel.public_id == public_id).first():
            return public_id
    raise RuntimeError("failed_to_generate_tiktok_public_id")


def get_pixel_by_public_id(public_id: Optional[str], db: Session) -> Pixel:
    if not public_id:
        raise HTTPException(status_code=400, detail="tracker_pixel_id_required")

    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=400, detail="unknown_tracker_pixel_id")
    if not pixel.is_active:
        raise HTTPException(status_code=403, detail="pixel_disabled")
    if not pixel.meta_pixel_id or not pixel.meta_access_token:
        raise HTTPException(status_code=500, detail="pixel_not_configured")
    return pixel


def split_config_values(value: Optional[str]) -> Set[str]:
    if not value:
        return set()
    return {item.strip().lower() for item in value.replace("\n", ",").split(",") if item.strip()}


def get_tiktok_pixel_by_alias(alias: Optional[str], db: Session, flow_id: Optional[str] = None) -> TikTokPixel:
    normalized_alias = (alias or "").strip()
    normalized_flow = (flow_id or "").strip().lower()
    if not normalized_alias and not normalized_flow:
        raise HTTPException(status_code=400, detail="tiktok_pixel_id_required")

    pixel = None
    if normalized_alias:
        pixel = db.query(TikTokPixel).filter(TikTokPixel.public_id == normalized_alias).first()
        if not pixel:
            pixel = db.query(TikTokPixel).filter(TikTokPixel.dataset_id == normalized_alias).first()
    if not pixel and normalized_flow:
        candidates = db.query(TikTokPixel).all()
        for candidate in candidates:
            if normalized_flow in split_config_values(candidate.flow_ids):
                pixel = candidate
                break
    if not pixel:
        raise HTTPException(status_code=400, detail="unknown_tiktok_pixel_id")
    if not pixel.is_active:
        raise HTTPException(status_code=403, detail="tiktok_pixel_disabled")
    if not pixel.dataset_id or not pixel.access_token:
        raise HTTPException(status_code=500, detail="tiktok_pixel_not_configured")
    return pixel


# ---------- Redis ----------
rds = redis.Redis.from_url(REDIS_URL, decode_responses=True)


def cache_fp(
    clickid: Optional[str],
    tracker_pixel_id: Optional[str],
    fbclid: Optional[str],
    fbp: Optional[str],
    fbc: Optional[str],
    ip: Optional[str],
    ua: Optional[str],
    em_hash: Optional[str],
    ph_hash: Optional[str],
):
    if not clickid:
        return
    data = {
        **({"tracker_pixel_id": tracker_pixel_id} if tracker_pixel_id else {}),
        **({"fbclid": fbclid} if fbclid else {}),
        **({"fbp": fbp} if fbp else {}),
        **({"fbc": fbc} if fbc else {}),
        **({"client_ip_address": ip} if ip else {}),
        **({"client_user_agent": ua} if ua else {}),
        **({"em": [em_hash]} if em_hash else {}),
        **({"ph": [ph_hash]} if ph_hash else {}),
    }
    rds.setex(f"click:{clickid}", CLICK_TTL, json.dumps(data))


def load_fp(clickid: str) -> Dict[str, Any]:
    raw = rds.get(f"click:{clickid}")
    return json.loads(raw) if raw else {}


def log_index_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in normalized)
    return safe[:160]


def push_log_entry(key: str, payload: str, max_len: int):
    rds.lpush(key, payload)
    rds.ltrim(key, 0, max(0, max_len - 1))


def log_meta_to_redis(entry: Dict[str, Any]):
    try:
        entry.setdefault("_ts", int(time.time()))
        entry.setdefault("when", format_app_timestamp(entry.get("_ts")))
        payload = json.dumps(entry, ensure_ascii=False)
        push_log_entry(META_LOG_KEY, payload, META_LOG_MAX)

        event_index = log_index_value(entry.get("event_name"))
        if event_index:
            push_log_entry(f"{META_EVENT_LOG_KEY_PREFIX}{event_index}", payload, META_EVENT_LOG_MAX)

        clickid_index = log_index_value(entry.get("clickid"))
        if clickid_index:
            push_log_entry(f"{META_CLICK_LOG_KEY_PREFIX}{clickid_index}", payload, META_CLICK_LOG_MAX)
    except Exception as e:
        log.warning("Failed to write meta log to Redis: %s", e)


def log_tiktok_to_redis(entry: Dict[str, Any]):
    try:
        entry.setdefault("_ts", int(time.time()))
        entry.setdefault("when", format_app_timestamp(entry.get("_ts")))
        payload = json.dumps(entry, ensure_ascii=False)
        push_log_entry(TIKTOK_LOG_KEY, payload, TIKTOK_LOG_MAX)

        event_index = log_index_value(entry.get("event_name"))
        if event_index:
            push_log_entry(f"{TIKTOK_EVENT_LOG_KEY_PREFIX}{event_index}", payload, TIKTOK_EVENT_LOG_MAX)

        clickid_index = log_index_value(entry.get("clickid"))
        if clickid_index:
            push_log_entry(f"{TIKTOK_CLICK_LOG_KEY_PREFIX}{clickid_index}", payload, TIKTOK_CLICK_LOG_MAX)
    except Exception as e:
        log.warning("Failed to write TikTok log to Redis: %s", e)


def parse_log_rows(rows: List[str]) -> List[Dict[str, Any]]:
    result = []
    for row in rows:
        try:
            parsed = json.loads(row)
            if isinstance(parsed, dict) and parsed.get("_ts"):
                parsed["when"] = format_app_timestamp(parsed.get("_ts"))
            result.append(parsed)
        except Exception:
            result.append({"raw": row})
    return result


def load_meta_logs_from_key(key: str, limit: int) -> List[Dict[str, Any]]:
    rows = rds.lrange(key, 0, max(0, limit - 1))
    return parse_log_rows(rows)


def load_meta_logs(limit: int = 100):
    return load_meta_logs_from_key(META_LOG_KEY, min(limit, META_LOG_MAX))


def load_all_meta_logs():
    return load_meta_logs(META_LOG_MAX)


def load_event_meta_logs(event_name: str):
    event_index = log_index_value(event_name)
    if not event_index:
        return []
    return load_meta_logs_from_key(f"{META_EVENT_LOG_KEY_PREFIX}{event_index}", META_EVENT_LOG_MAX)


def load_clickid_meta_logs(clickid: str):
    clickid_index = log_index_value(clickid)
    if not clickid_index:
        return []
    return load_meta_logs_from_key(f"{META_CLICK_LOG_KEY_PREFIX}{clickid_index}", META_CLICK_LOG_MAX)


def load_tiktok_logs_from_key(key: str, limit: int) -> List[Dict[str, Any]]:
    rows = rds.lrange(key, 0, max(0, limit - 1))
    return parse_log_rows(rows)


def load_all_tiktok_logs():
    return load_tiktok_logs_from_key(TIKTOK_LOG_KEY, TIKTOK_LOG_MAX)


def load_event_tiktok_logs(event_name: str):
    event_index = log_index_value(event_name)
    if not event_index:
        return []
    return load_tiktok_logs_from_key(f"{TIKTOK_EVENT_LOG_KEY_PREFIX}{event_index}", TIKTOK_EVENT_LOG_MAX)


def load_clickid_tiktok_logs(clickid: str):
    clickid_index = log_index_value(clickid)
    if not clickid_index:
        return []
    return load_tiktok_logs_from_key(f"{TIKTOK_CLICK_LOG_KEY_PREFIX}{clickid_index}", TIKTOK_CLICK_LOG_MAX)


def make_fbc_from_fbclid(fbclid: Optional[str]) -> Optional[str]:
    if not fbclid:
        return None
    return f"fb.1.{int(time.time() * 1000)}.{fbclid}"


def dedup_key(tracker_pixel_id: str, event_name: str, event_id: str) -> str:
    raw = f"{tracker_pixel_id}|{event_name}|{event_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"dedup:meta:{digest}"


def mark_event_for_send(tracker_pixel_id: str, event_name: str, event_id: str) -> bool:
    if DEDUP_TTL_SECONDS <= 0:
        return True
    return bool(rds.set(dedup_key(tracker_pixel_id, event_name, event_id), "1", nx=True, ex=DEDUP_TTL_SECONDS))


def tiktok_dedup_key(dataset_id: str, event_name: str, event_id: str) -> str:
    raw = f"{dataset_id}|{event_name}|{event_id}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"dedup:tiktok:{digest}"


def mark_tiktok_event_for_send(dataset_id: str, event_name: str, event_id: str) -> bool:
    if DEDUP_TTL_SECONDS <= 0:
        return True
    return bool(rds.set(tiktok_dedup_key(dataset_id, event_name, event_id), "1", nx=True, ex=DEDUP_TTL_SECONDS))


def log_duplicate_event(pixel: Pixel, event_name: str, event_id: str, context: Dict[str, Any]):
    log.warning(
        "Duplicate Meta event skipped: tracker_pixel_id=%s buyer=%s pixel_name=%s name=%s id=%s clickid=%s fbclid=%s",
        pixel.public_id,
        pixel.buyer_name,
        pixel.name,
        event_name,
        event_id,
        context.get("clickid"),
        context.get("fbclid"),
    )
    log_meta_to_redis({
        "tracker_pixel_id": pixel.public_id,
        "buyer_name": pixel.buyer_name,
        "pixel_name": pixel.name,
        "status_code": 208,
        "event_name": event_name,
        "event_id": event_id,
        "clickid": context.get("clickid"),
        "fbclid": context.get("fbclid"),
        "fbp": context.get("fbp"),
        "fbc": context.get("fbc"),
        "event_source_url": context.get("event_source_url"),
        "source_domain": extract_domain_from_url(context.get("event_source_url")),
        "skipped_duplicate": True,
        "details": "duplicate_event_id_skipped",
    })


def log_rejected_event(
    *,
    event_name: Optional[str],
    tracker_pixel_id: Optional[str],
    status_code: int,
    reason: str,
    context: Dict[str, Any],
    pixel: Optional[Pixel] = None,
    event_id: Optional[str] = None,
    details: Optional[str] = None,
):
    log.warning(
        "Meta event rejected before send: tracker_pixel_id=%s buyer=%s pixel_name=%s name=%s id=%s clickid=%s reason=%s",
        tracker_pixel_id,
        pixel.buyer_name if pixel else None,
        pixel.name if pixel else None,
        event_name,
        event_id,
        context.get("clickid"),
        reason,
    )
    log_meta_to_redis({
        "tracker_pixel_id": tracker_pixel_id,
        "buyer_name": pixel.buyer_name if pixel else None,
        "pixel_name": pixel.name if pixel else None,
        "status_code": status_code,
        "event_name": event_name,
        "event_id": event_id,
        "clickid": context.get("clickid"),
        "fbclid": context.get("fbclid"),
        "fbp": context.get("fbp"),
        "fbc": context.get("fbc"),
        "event_source_url": context.get("event_source_url"),
        "source_domain": extract_domain_from_url(context.get("event_source_url")),
        "rejected": True,
        "details": details or reason,
    })


class TrackEvent(BaseModel):
    tracker_pixel_id: Optional[str] = None
    event_name: str
    clickid: Optional[str] = None
    event_time: Optional[int] = None
    event_source_url: Optional[str] = None
    fbclid: Optional[str] = None
    fbp: Optional[str] = None
    fbc: Optional[str] = None
    ua: Optional[str] = None
    ip: Optional[str] = None
    user_data_raw: Optional[Dict[str, Any]] = None
    currency: Optional[str] = None
    value: Optional[float] = None
    order_id: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class WhaleTikTokPostback(BaseModel):
    source: Optional[str] = None
    event: Optional[str] = None
    event_id: str
    status: str
    payout: Optional[Any] = None
    offer_id: Optional[str] = None
    flow_id: Optional[str] = None
    source_id: Optional[str] = None
    click_uuid: Optional[str] = None
    ip: Optional[str] = None
    ttclid: Optional[str] = None
    pixel_id: Optional[str] = None
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    adgroup_id: Optional[str] = None
    adgroup_name: Optional[str] = None
    creative_id: Optional[str] = None
    creative_name: Optional[str] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None
    user_agent: Optional[str] = None


def sha256_norm_email(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def sha256_norm_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return hashlib.sha256(digits.encode("utf-8")).hexdigest()


def get_client_ip(req: Request, fallback: Optional[str]) -> Optional[str]:
    xff = req.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return fallback or (req.client.host if req.client else None)


def build_event_id(clickid: Optional[str], name: str, order_id: Optional[str]) -> str:
    base = clickid if clickid else f"anon-{int(time.time() * 1000)}"
    return ":".join([p for p in [base, name, order_id] if p])


def normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    domain = domain.replace("https://", "").replace("http://", "")
    domain = domain.split("/")[0]
    domain = domain.split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def extract_domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path.split("/")[0]
    return normalize_domain(domain) if domain else None


def parse_allowed_domains(raw: Optional[str]) -> Set[str]:
    if not raw:
        return set()
    parts = raw.replace("\r", "\n").replace(",", "\n").split("\n")
    return {normalize_domain(part) for part in parts if normalize_domain(part)}


def ensure_domain_allowed(pixel: Pixel, event_source_url: Optional[str]):
    allowed = parse_allowed_domains(pixel.allowed_domains)
    if not allowed:
        return

    event_domain = extract_domain_from_url(event_source_url)
    if event_domain in allowed:
        return

    log.warning(
        "Domain not allowed: tracker_pixel_id=%s buyer=%s pixel_name=%s domain=%s allowed=%s",
        pixel.public_id,
        pixel.buyer_name,
        pixel.name,
        event_domain,
        ",".join(sorted(allowed)),
    )
    if STRICT_DOMAIN_CHECK:
        raise HTTPException(status_code=403, detail="domain_not_allowed")


def ensure_whale_tiktok_secret(request: Request, query_secret: Optional[str]):
    if not WHALE_TIKTOK_SECRET:
        return
    bearer = request.headers.get("authorization", "")
    header_secret = request.headers.get("x-postback-secret", "")
    candidates = [query_secret or "", header_secret]
    if bearer.lower().startswith("bearer "):
        candidates.append(bearer.split(" ", 1)[1].strip())
    if any(secrets.compare_digest(candidate, WHALE_TIKTOK_SECRET) for candidate in candidates if candidate):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def meta_payload_for_log(payload: Dict[str, Any]) -> str:
    logged_payload = dict(payload)
    if logged_payload.get("access_token"):
        logged_payload["access_token"] = mask_token(str(logged_payload["access_token"]))
    return json.dumps(logged_payload, ensure_ascii=False, separators=(",", ":"))


def public_meta_result(status_code: Optional[int], parsed: Any, text: str) -> Dict[str, Any]:
    fbtrace_id = None
    events_received = None
    messages = None
    error_user_title = None
    error_user_msg = None

    if isinstance(parsed, dict):
        fbtrace_id = parsed.get("fbtrace_id") or (parsed.get("error") or {}).get("fbtrace_id")
        events_received = parsed.get("events_received")
        messages = parsed.get("messages")
        if "error" in parsed:
            err = parsed["error"]
            error_user_title = err.get("error_user_title")
            error_user_msg = err.get("error_user_msg")

    return {
        "status_code": status_code,
        "fbtrace_id": fbtrace_id,
        "events_received": events_received,
        "messages": messages,
        "error_user_title": error_user_title,
        "error_user_msg": error_user_msg,
        "body": parsed if parsed is not None else text,
    }


def parse_float(value: Optional[Any]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def parse_event_timestamp(*values: Optional[Any]) -> int:
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            raw = int(value)
            return raw // 1000 if raw > 10_000_000_000 else raw
        text = str(value).strip()
        if not text:
            continue
        try:
            if text.isdigit():
                raw = int(text)
                return raw // 1000 if raw > 10_000_000_000 else raw
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=APP_TZ)
            return int(parsed.astimezone(timezone.utc).timestamp())
        except Exception:
            continue
    return int(time.time())


def is_macro_or_empty(value: Optional[str]) -> bool:
    if not value:
        return True
    text = value.strip()
    if not text:
        return True
    return text.startswith("__") and text.endswith("__")


def tiktok_allowed_statuses(pixel: TikTokPixel) -> Set[str]:
    configured = split_config_values(pixel.allowed_statuses)
    return configured or TIKTOK_ALLOWED_STATUSES


def tiktok_payload_for_log(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def public_tiktok_result(status_code: Optional[int], parsed: Any, text: str) -> Dict[str, Any]:
    return {
        "status_code": status_code,
        "code": parsed.get("code") if isinstance(parsed, dict) else None,
        "message": parsed.get("message") if isinstance(parsed, dict) else None,
        "request_id": parsed.get("request_id") if isinstance(parsed, dict) else None,
        "body": parsed if parsed is not None else text,
    }


def build_tiktok_payload(pb: WhaleTikTokPostback, pixel: TikTokPixel, request: Request) -> Dict[str, Any]:
    event_name = pixel.event_name or pb.event or "CompletePayment"
    value = parse_float(pb.payout)
    query = {
        "campaign_id": pb.campaign_id,
        "campaign_name": pb.campaign_name,
        "adgroup_id": pb.adgroup_id,
        "adgroup_name": pb.adgroup_name,
        "creative_id": pb.creative_id,
        "creative_name": pb.creative_name,
        "offer_id": pb.offer_id,
        "flow_id": pb.flow_id,
        "source_id": pb.source_id,
        "click_uuid": pb.click_uuid,
        "status": pb.status,
        "source": pb.source,
    }
    user = {
        **({"ttclid": pb.ttclid.strip()} if pb.ttclid and not is_macro_or_empty(pb.ttclid) else {}),
        **({"ip": pb.ip} if pb.ip else {}),
        **({"user_agent": pb.user_agent or request.headers.get("user-agent")} if (pb.user_agent or request.headers.get("user-agent")) else {}),
    }
    query_payload = {key: val for key, val in query.items() if val not in {None, ""}}
    properties = {
        "currency": pixel.currency or "USD",
        **({"value": value} if value is not None else {}),
        "content_type": "product",
        "description": "Whale conversion",
        "query": json.dumps(query_payload, ensure_ascii=False, separators=(",", ":")),
    }
    event = {
        "event": event_name,
        "event_time": parse_event_timestamp(pb.updated_at, pb.created_at),
        "event_id": pb.event_id,
        "user": user,
        "properties": properties,
    }
    payload = {
        "event_source": "web",
        "event_source_id": pixel.dataset_id,
        "data": [event],
    }
    if pixel.test_event_code:
        payload["test_event_code"] = pixel.test_event_code
    return payload


async def send_to_tiktok(
    payload: Dict[str, Any],
    pixel: TikTokPixel,
    context: Dict[str, Any],
) -> Dict[str, Any]:
    headers = {
        "Access-Token": pixel.access_token,
        "Content-Type": "application/json",
    }
    event = (payload.get("data") or [{}])[0]
    event_name = event.get("event")
    event_id = event.get("event_id")
    logged_payload = tiktok_payload_for_log(payload)
    last_status = None
    parsed = None
    text = ""

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIKTOK_TIMEOUT_SECONDS) as client:
                resp = await client.post(TIKTOK_EVENTS_API_URL, headers=headers, json=payload)
            last_status = resp.status_code
            text = resp.text
            try:
                parsed = resp.json()
            except Exception:
                parsed = None
            if resp.status_code not in {429} and resp.status_code < 500:
                break
        except Exception as e:
            if attempt >= 1:
                log.exception(
                    "TikTok Events API send failed: tiktok_pixel_id=%s buyer=%s dataset_id=%s event=%s id=%s clickid=%s",
                    pixel.public_id,
                    pixel.buyer_name,
                    pixel.dataset_id,
                    event_name,
                    event_id,
                    context.get("clickid"),
                )
                log_tiktok_to_redis({
                    "tiktok_pixel_id": pixel.public_id,
                    "buyer_name": pixel.buyer_name,
                    "pixel_name": pixel.name,
                    "dataset_id": pixel.dataset_id,
                    "status_code": 0,
                    "event_name": event_name,
                    "event_id": event_id,
                    "clickid": context.get("clickid"),
                    "ttclid": context.get("ttclid"),
                    "flow_id": context.get("flow_id"),
                    "sended": logged_payload,
                    "send_failed": True,
                    "details": f"tiktok_send_failed: {e}",
                })
                raise HTTPException(status_code=502, detail="tiktok_send_failed")
        if attempt == 0:
            await asyncio.sleep(0.35)

    result = public_tiktok_result(last_status, parsed, text)
    log.info(
        "TikTok Events API resp: tiktok_pixel_id=%s buyer=%s dataset_id=%s event=%s id=%s clickid=%s ttclid=%s code=%s request_id=%s msg=%s",
        pixel.public_id,
        pixel.buyer_name,
        pixel.dataset_id,
        event_name,
        event_id,
        context.get("clickid"),
        context.get("ttclid"),
        result.get("status_code"),
        result.get("request_id"),
        result.get("message"),
    )
    log_tiktok_to_redis({
        "tiktok_pixel_id": pixel.public_id,
        "buyer_name": pixel.buyer_name,
        "pixel_name": pixel.name,
        "dataset_id": pixel.dataset_id,
        "status_code": result.get("status_code"),
        "event_name": event_name,
        "event_id": event_id,
        "clickid": context.get("clickid"),
        "ttclid": context.get("ttclid"),
        "flow_id": context.get("flow_id"),
        "status": context.get("status"),
        "sended": logged_payload,
        "code": result.get("code"),
        "message": result.get("message"),
        "request_id": result.get("request_id"),
        "body": result.get("body"),
    })
    if result.get("status_code") in {429} or (result.get("status_code") and result["status_code"] >= 500):
        raise HTTPException(status_code=502, detail="tiktok_temporary_error")
    return result


async def send_to_meta(
    event: dict,
    meta_pixel_id: str,
    meta_access_token: str,
    pixel: Optional[Pixel] = None,
    context: Optional[Dict[str, Any]] = None,
) -> dict:
    if not meta_pixel_id or not meta_access_token:
        return {"skipped": "meta_not_configured"}

    url = f"https://graph.facebook.com/v21.0/{meta_pixel_id}/events"
    payload = {"data": [event], "access_token": meta_access_token}
    if META_TEST_EVENT_CODE:
        payload["test_event_code"] = META_TEST_EVENT_CODE
    sended = meta_payload_for_log(payload)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
    except Exception as e:
        tracker_pixel_id = pixel.public_id if pixel else None
        buyer_name = pixel.buyer_name if pixel else None
        pixel_name = pixel.name if pixel else None
        context = context or {}
        log.exception(
            "Meta CAPI send failed: tracker_pixel_id=%s buyer=%s pixel_name=%s name=%s id=%s clickid=%s",
            tracker_pixel_id,
            buyer_name,
            pixel_name,
            event.get("event_name"),
            event.get("event_id"),
            context.get("clickid"),
        )
        log_meta_to_redis({
            "tracker_pixel_id": tracker_pixel_id,
            "buyer_name": buyer_name,
            "pixel_name": pixel_name,
            "status_code": 0,
            "event_name": event.get("event_name"),
            "event_id": event.get("event_id"),
            "clickid": context.get("clickid"),
            "fbclid": context.get("fbclid"),
            "fbp": context.get("fbp"),
            "fbc": context.get("fbc"),
            "event_source_url": context.get("event_source_url") or event.get("event_source_url"),
            "source_domain": extract_domain_from_url(context.get("event_source_url") or event.get("event_source_url")),
            "sended": sended,
            "send_failed": True,
            "details": f"meta_send_failed: {e}",
        })
        raise HTTPException(status_code=502, detail="meta_send_failed")

    try:
        parsed = resp.json()
    except Exception:
        parsed = None

    meta = public_meta_result(resp.status_code, parsed, resp.text)
    tracker_pixel_id = pixel.public_id if pixel else None
    buyer_name = pixel.buyer_name if pixel else None
    pixel_name = pixel.name if pixel else None
    context = context or {}

    log.info(
        "Meta CAPI resp: tracker_pixel_id=%s buyer=%s pixel_name=%s name=%s id=%s clickid=%s fbclid=%s code=%s fbtrace_id=%s events=%s msg=%s err_title=%s err_msg=%s",
        tracker_pixel_id,
        buyer_name,
        pixel_name,
        event.get("event_name"),
        event.get("event_id"),
        context.get("clickid"),
        context.get("fbclid"),
        resp.status_code,
        meta.get("fbtrace_id"),
        meta.get("events_received"),
        meta.get("messages"),
        meta.get("error_user_title"),
        meta.get("error_user_msg"),
    )

    log_meta_to_redis({
        "tracker_pixel_id": tracker_pixel_id,
        "buyer_name": buyer_name,
        "pixel_name": pixel_name,
        "status_code": resp.status_code,
        "event_name": event.get("event_name"),
        "event_id": event.get("event_id"),
        "clickid": context.get("clickid"),
        "fbclid": context.get("fbclid"),
        "fbp": context.get("fbp"),
        "fbc": context.get("fbc"),
        "event_source_url": context.get("event_source_url") or event.get("event_source_url"),
        "source_domain": extract_domain_from_url(context.get("event_source_url") or event.get("event_source_url")),
        "sended": sended,
        "fbtrace_id": meta.get("fbtrace_id"),
        "events_received": meta.get("events_received"),
        "messages": meta.get("messages"),
        "error_user_title": meta.get("error_user_title"),
        "error_user_msg": meta.get("error_user_msg"),
        "body": meta.get("body"),
    })

    return meta


async def parse_form(request: Request) -> Dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def auth_failed():
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="admin_auth_required",
        headers={"WWW-Authenticate": "Basic"},
    )


def require_user(
    credentials: HTTPBasicCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    user = db.query(AdminUser).filter(AdminUser.username == credentials.username).first()
    if user and user.is_active and verify_password(credentials.password, user.password_hash):
        return {
            "username": user.username,
            "buyer_name": user.buyer_name or user.username,
            "role": user.role,
            "is_admin": user.role == "admin",
            "source": "db",
        }

    if ADMIN_PASSWORD:
        username_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
        password_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
        if username_ok and password_ok:
            return {
                "username": ADMIN_USERNAME,
                "buyer_name": ADMIN_USERNAME,
                "role": "admin",
                "is_admin": True,
                "source": "env",
            }

    auth_failed()


def require_admin_user(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def row_matches_filter(row: Dict[str, Any], key: str, expected: Optional[str]) -> bool:
    if not expected:
        return True
    return str(row.get(key) or "").lower() == expected.lower()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def pct(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(part * 100 / total, 1)


def selected_buyer_for_user(user: Dict[str, Any], buyer: Optional[str]) -> str:
    selected = buyer or "mine"
    if not user.get("is_admin"):
        return user["buyer_name"]
    if selected == "mine":
        return user["buyer_name"]
    return selected


def filter_meta_logs(
    logs: List[Dict[str, Any]],
    user: Dict[str, Any],
    buyer: Optional[str] = "mine",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    event: Optional[str] = None,
    tracker_pixel_id: Optional[str] = None,
    clickid: Optional[str] = None,
    domain: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], str]:
    selected_buyer = selected_buyer_for_user(user, buyer)
    start_ts, end_ts = app_date_bounds(date_from, date_to)
    if start_ts is not None:
        logs = [row for row in logs if safe_int(row.get("_ts")) >= start_ts]
    if end_ts is not None:
        logs = [row for row in logs if safe_int(row.get("_ts")) < end_ts]
    if event:
        logs = [row for row in logs if str(row.get("event_name", "")).lower() == event.lower()]
    if tracker_pixel_id:
        logs = [row for row in logs if row.get("tracker_pixel_id") == tracker_pixel_id]
    if selected_buyer != "__all__":
        logs = [row for row in logs if row_matches_filter(row, "buyer_name", selected_buyer)]
    if clickid:
        logs = [row for row in logs if row_matches_filter(row, "clickid", clickid)]
    if domain:
        logs = [row for row in logs if row_matches_filter(row, "source_domain", normalize_domain(domain))]
    return logs, selected_buyer


def filter_tiktok_logs(
    logs: List[Dict[str, Any]],
    user: Dict[str, Any],
    buyer: Optional[str] = "mine",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    event: Optional[str] = None,
    tiktok_pixel_id: Optional[str] = None,
    clickid: Optional[str] = None,
    ttclid: Optional[str] = None,
    flow_id: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], str]:
    selected_buyer = selected_buyer_for_user(user, buyer)
    start_ts, end_ts = app_date_bounds(date_from, date_to)
    if start_ts is not None:
        logs = [row for row in logs if safe_int(row.get("_ts")) >= start_ts]
    if end_ts is not None:
        logs = [row for row in logs if safe_int(row.get("_ts")) < end_ts]
    if event:
        logs = [row for row in logs if str(row.get("event_name", "")).lower() == event.lower()]
    if tiktok_pixel_id:
        logs = [row for row in logs if row.get("tiktok_pixel_id") == tiktok_pixel_id]
    if selected_buyer != "__all__":
        logs = [row for row in logs if row_matches_filter(row, "buyer_name", selected_buyer)]
    if clickid:
        logs = [row for row in logs if row_matches_filter(row, "clickid", clickid)]
    if ttclid:
        logs = [row for row in logs if row_matches_filter(row, "ttclid", ttclid)]
    if flow_id:
        logs = [row for row in logs if row_matches_filter(row, "flow_id", flow_id)]
    return logs, selected_buyer


def available_buyers() -> List[str]:
    return sorted({row.get("buyer_name") for row in load_all_meta_logs() if row.get("buyer_name")})


def available_tiktok_buyers() -> List[str]:
    return sorted({row.get("buyer_name") for row in load_all_tiktok_logs() if row.get("buyer_name")})


def is_error_log(row: Dict[str, Any]) -> bool:
    status_code = row.get("status_code")
    return bool(
        row.get("rejected")
        or row.get("send_failed")
        or safe_int(status_code, 200) == 0
        or safe_int(status_code, 200) >= 400
    )


def is_weak_matching(row: Dict[str, Any]) -> bool:
    return not row.get("fbc") and not row.get("fbp")


def build_quality_dashboard(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    pixels: Dict[str, Dict[str, Any]] = {}
    totals = {
        "events": 0,
        "pageviews": 0,
        "leads": 0,
        "purchases": 0,
        "duplicates": 0,
        "errors": 0,
        "weak_matching": 0,
        "missing_fbclid": 0,
        "missing_fbc": 0,
        "missing_fbp": 0,
    }

    for row in logs:
        key = row.get("tracker_pixel_id") or "unknown"
        pixel = pixels.setdefault(
            key,
            {
                "tracker_pixel_id": key,
                "pixel_name": row.get("pixel_name") or "",
                "buyer_name": row.get("buyer_name") or "",
                "events": 0,
                "pageviews": 0,
                "leads": 0,
                "purchases": 0,
                "duplicates": 0,
                "errors": 0,
                "weak_matching": 0,
                "missing_fbclid": 0,
                "missing_fbc": 0,
                "missing_fbp": 0,
                "last_seen": row.get("when") or "",
            },
        )

        event_name = str(row.get("event_name") or "")
        event_key = event_name.lower()
        pixel["pixel_name"] = pixel["pixel_name"] or row.get("pixel_name") or ""
        pixel["buyer_name"] = pixel["buyer_name"] or row.get("buyer_name") or ""
        pixel["events"] += 1
        totals["events"] += 1
        if event_key == "pageview":
            pixel["pageviews"] += 1
            totals["pageviews"] += 1
        elif event_key == "lead":
            pixel["leads"] += 1
            totals["leads"] += 1
        elif event_key == "purchase":
            pixel["purchases"] += 1
            totals["purchases"] += 1

        if row.get("skipped_duplicate"):
            pixel["duplicates"] += 1
            totals["duplicates"] += 1
        if is_error_log(row):
            pixel["errors"] += 1
            totals["errors"] += 1
        if is_weak_matching(row):
            pixel["weak_matching"] += 1
            totals["weak_matching"] += 1
        if not row.get("fbclid"):
            pixel["missing_fbclid"] += 1
            totals["missing_fbclid"] += 1
        if not row.get("fbc"):
            pixel["missing_fbc"] += 1
            totals["missing_fbc"] += 1
        if not row.get("fbp"):
            pixel["missing_fbp"] += 1
            totals["missing_fbp"] += 1

    for item in pixels.values():
        total = item["events"]
        item["duplicate_pct"] = pct(item["duplicates"], total)
        item["error_pct"] = pct(item["errors"], total)
        item["weak_matching_pct"] = pct(item["weak_matching"], total)
        item["missing_fbclid_pct"] = pct(item["missing_fbclid"], total)
        item["missing_fbc_pct"] = pct(item["missing_fbc"], total)
        item["missing_fbp_pct"] = pct(item["missing_fbp"], total)

    totals["duplicate_pct"] = pct(totals["duplicates"], totals["events"])
    totals["error_pct"] = pct(totals["errors"], totals["events"])
    totals["weak_matching_pct"] = pct(totals["weak_matching"], totals["events"])
    totals["missing_fbclid_pct"] = pct(totals["missing_fbclid"], totals["events"])
    totals["missing_fbc_pct"] = pct(totals["missing_fbc"], totals["events"])
    totals["missing_fbp_pct"] = pct(totals["missing_fbp"], totals["events"])

    return {
        "totals": totals,
        "pixels": sorted(
            pixels.values(),
            key=lambda item: (item["errors"], item["weak_matching"], item["events"]),
            reverse=True,
        ),
    }


def build_click_diagnostics(logs: List[Dict[str, Any]], clickid: str) -> Dict[str, Any]:
    timeline = sorted(logs, key=lambda row: safe_int(row.get("_ts")))
    event_order = ["PageView", "Lead", "Purchase"]
    present = {str(row.get("event_name") or "") for row in timeline}
    missing_steps = [name for name in event_order if name not in present]
    first = timeline[0] if timeline else {}
    last = timeline[-1] if timeline else {}
    issues: List[str] = []

    if not timeline:
        issues.append("По этому clickid нет событий в Redis-логах.")
    if timeline and not any(row.get("fbclid") for row in timeline):
        issues.append("В событиях нет fbclid. Атрибуция клика в Meta может быть слабее.")
    if timeline and not any(row.get("fbc") for row in timeline):
        issues.append("В событиях нет fbc. Meta хуже сопоставляет событие с кликом.")
    if timeline and not any(row.get("fbp") for row in timeline):
        issues.append("В событиях нет fbp. Browser matching слабее.")
    if any(row.get("skipped_duplicate") for row in timeline):
        issues.append("Есть дубли, часть событий была пропущена защитой от дублей.")
    if any(is_error_log(row) for row in timeline):
        issues.append("Есть ошибки или события, отклоненные до отправки в Meta.")
    if "Lead" in missing_steps:
        issues.append("Lead по этому clickid не найден в логах.")
    if "Purchase" in missing_steps:
        issues.append("Purchase по этому clickid не найден в логах.")

    return {
        "clickid": clickid,
        "timeline": timeline,
        "missing_steps": missing_steps,
        "issues": issues,
        "summary": {
            "events": len(timeline),
            "tracker_pixel_id": first.get("tracker_pixel_id") or "",
            "buyer_name": first.get("buyer_name") or "",
            "pixel_name": first.get("pixel_name") or "",
            "first_seen": first.get("when") or "",
            "last_seen": last.get("when") or "",
            "duplicates": sum(1 for row in timeline if row.get("skipped_duplicate")),
            "errors": sum(1 for row in timeline if is_error_log(row)),
            "weak_matching": sum(1 for row in timeline if is_weak_matching(row)),
        },
    }


def normalize_role(role: Optional[str]) -> str:
    return "admin" if role == "admin" else "buyer"


def pixel_query_for_user(db: Session, user: Dict[str, Any]):
    query = db.query(Pixel)
    if user.get("is_admin"):
        return query
    buyer_name = str(user.get("buyer_name") or "").lower()
    return query.filter(func.lower(Pixel.buyer_name) == buyer_name)


def tiktok_pixel_query_for_user(db: Session, user: Dict[str, Any]):
    query = db.query(TikTokPixel)
    if user.get("is_admin"):
        return query
    buyer_name = str(user.get("buyer_name") or "").lower()
    return query.filter(func.lower(TikTokPixel.buyer_name) == buyer_name)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/admin/pixels", response_class=HTMLResponse)
async def admin_pixels(request: Request, db: Session = Depends(get_db), user: Dict[str, Any] = Depends(require_user)):
    pixels = pixel_query_for_user(db, user).order_by(Pixel.created_at.desc()).all()
    return templates.TemplateResponse(
        name="pixels_list.html",
        request=request,
	context={"pixels": pixels, "mask_token": mask_token, "current_user": user},
    )


@app.get("/admin/pixels/new", response_class=HTMLResponse)
async def admin_pixel_new(request: Request, user: Dict[str, Any] = Depends(require_admin_user)):
    return templates.TemplateResponse(
        name="pixel_form.html",
        request=request,
	context={"pixel": None, "mode": "new", "masked_token": "", "current_user": user},
    )


@app.post("/admin/pixels/new")
async def admin_pixel_create(request: Request, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    form = await parse_form(request)
    pixel = Pixel(
        public_id=generate_unique_public_id(db),
        name=form.get("name", "").strip(),
        buyer_name=form.get("buyer_name", "").strip() or None,
        meta_pixel_id=form.get("meta_pixel_id", "").strip(),
        meta_access_token=form.get("meta_access_token", "").strip(),
        allowed_domains=form.get("allowed_domains", "").strip() or None,
        is_active=form.get("is_active") == "on",
        created_at=naive_app_now(),
        updated_at=naive_app_now(),
    )
    if not pixel.name or not pixel.meta_pixel_id or not pixel.meta_access_token:
        raise HTTPException(status_code=400, detail="name_pixel_id_and_token_required")
    db.add(pixel)
    db.commit()
    return RedirectResponse(f"/admin/pixels/{pixel.public_id}/edit?created=1", status_code=303)


@app.get("/admin/pixels/{public_id}/edit", response_class=HTMLResponse)
async def admin_pixel_edit(
    public_id: str,
    request: Request,
    created: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(require_admin_user),
):
    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    return templates.TemplateResponse(
        name="pixel_form.html",
        request=request,
	context=
	{
            "pixel": pixel,
            "mode": "edit",
            "created": bool(created),
            "masked_token": mask_token(pixel.meta_access_token),
            "current_user": user,
        },
    )


@app.post("/admin/pixels/{public_id}/edit")
async def admin_pixel_update(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(require_admin_user),
):
    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="pixel_not_found")

    form = await parse_form(request)
    pixel.name = form.get("name", "").strip()
    pixel.buyer_name = form.get("buyer_name", "").strip() or None
    pixel.meta_pixel_id = form.get("meta_pixel_id", "").strip()
    new_token = form.get("meta_access_token", "").strip()
    if new_token:
        pixel.meta_access_token = new_token
    pixel.allowed_domains = form.get("allowed_domains", "").strip() or None
    pixel.is_active = form.get("is_active") == "on"
    pixel.updated_at = naive_app_now()
    if not pixel.name or not pixel.meta_pixel_id or not pixel.meta_access_token:
        raise HTTPException(status_code=400, detail="name_pixel_id_and_token_required")
    db.commit()
    return RedirectResponse(f"/admin/pixels/{pixel.public_id}/edit?saved=1", status_code=303)


@app.post("/admin/pixels/{public_id}/disable")
async def admin_pixel_disable(public_id: str, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    pixel.is_active = False
    pixel.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/pixels", status_code=303)


@app.post("/admin/pixels/{public_id}/enable")
async def admin_pixel_enable(public_id: str, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    pixel.is_active = True
    pixel.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/pixels", status_code=303)


@app.get("/admin/tiktok/pixels", response_class=HTMLResponse)
async def admin_tiktok_pixels(request: Request, db: Session = Depends(get_db), user: Dict[str, Any] = Depends(require_user)):
    pixels = tiktok_pixel_query_for_user(db, user).order_by(TikTokPixel.created_at.desc()).all()
    return templates.TemplateResponse(
        name="tiktok_pixels_list.html",
        request=request,
        context={"pixels": pixels, "mask_token": mask_token, "current_user": user},
    )


@app.get("/admin/tiktok/pixels/new", response_class=HTMLResponse)
async def admin_tiktok_pixel_new(request: Request, user: Dict[str, Any] = Depends(require_admin_user)):
    return templates.TemplateResponse(
        name="tiktok_pixel_form.html",
        request=request,
        context={"pixel": None, "mode": "new", "masked_token": "", "current_user": user},
    )


@app.post("/admin/tiktok/pixels/new")
async def admin_tiktok_pixel_create(request: Request, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    form = await parse_form(request)
    pixel = TikTokPixel(
        public_id=form.get("public_id", "").strip() or generate_unique_tiktok_public_id(db),
        name=form.get("name", "").strip(),
        buyer_name=form.get("buyer_name", "").strip() or None,
        dataset_id=form.get("dataset_id", "").strip(),
        access_token=form.get("access_token", "").strip(),
        event_name=form.get("event_name", "").strip() or "CompletePayment",
        currency=(form.get("currency", "").strip() or "USD").upper(),
        allowed_statuses=form.get("allowed_statuses", "").strip() or None,
        flow_ids=form.get("flow_ids", "").strip() or None,
        test_event_code=form.get("test_event_code", "").strip() or None,
        send_without_ttclid=form.get("send_without_ttclid") == "on",
        is_active=form.get("is_active") == "on",
        created_at=naive_app_now(),
        updated_at=naive_app_now(),
    )
    if not pixel.name or not pixel.public_id or not pixel.dataset_id or not pixel.access_token:
        raise HTTPException(status_code=400, detail="name_public_id_dataset_id_and_token_required")
    db.add(pixel)
    db.commit()
    return RedirectResponse(f"/admin/tiktok/pixels/{pixel.public_id}/edit?created=1", status_code=303)


@app.get("/admin/tiktok/pixels/{public_id}/edit", response_class=HTMLResponse)
async def admin_tiktok_pixel_edit(
    public_id: str,
    request: Request,
    created: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(require_admin_user),
):
    pixel = db.query(TikTokPixel).filter(TikTokPixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="tiktok_pixel_not_found")
    return templates.TemplateResponse(
        name="tiktok_pixel_form.html",
        request=request,
        context={
            "pixel": pixel,
            "mode": "edit",
            "created": bool(created),
            "masked_token": mask_token(pixel.access_token),
            "current_user": user,
        },
    )


@app.post("/admin/tiktok/pixels/{public_id}/edit")
async def admin_tiktok_pixel_update(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(require_admin_user),
):
    pixel = db.query(TikTokPixel).filter(TikTokPixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="tiktok_pixel_not_found")

    form = await parse_form(request)
    pixel.name = form.get("name", "").strip()
    pixel.buyer_name = form.get("buyer_name", "").strip() or None
    pixel.dataset_id = form.get("dataset_id", "").strip()
    new_token = form.get("access_token", "").strip()
    if new_token:
        pixel.access_token = new_token
    pixel.event_name = form.get("event_name", "").strip() or "CompletePayment"
    pixel.currency = (form.get("currency", "").strip() or "USD").upper()
    pixel.allowed_statuses = form.get("allowed_statuses", "").strip() or None
    pixel.flow_ids = form.get("flow_ids", "").strip() or None
    pixel.test_event_code = form.get("test_event_code", "").strip() or None
    pixel.send_without_ttclid = form.get("send_without_ttclid") == "on"
    pixel.is_active = form.get("is_active") == "on"
    pixel.updated_at = naive_app_now()
    if not pixel.name or not pixel.dataset_id or not pixel.access_token:
        raise HTTPException(status_code=400, detail="name_dataset_id_and_token_required")
    db.commit()
    return RedirectResponse(f"/admin/tiktok/pixels/{pixel.public_id}/edit?saved=1", status_code=303)


@app.post("/admin/tiktok/pixels/{public_id}/disable")
async def admin_tiktok_pixel_disable(public_id: str, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    pixel = db.query(TikTokPixel).filter(TikTokPixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="tiktok_pixel_not_found")
    pixel.is_active = False
    pixel.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/tiktok/pixels", status_code=303)


@app.post("/admin/tiktok/pixels/{public_id}/enable")
async def admin_tiktok_pixel_enable(public_id: str, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    pixel = db.query(TikTokPixel).filter(TikTokPixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="tiktok_pixel_not_found")
    pixel.is_active = True
    pixel.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/tiktok/pixels", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: Session = Depends(get_db), user: Dict[str, Any] = Depends(require_admin_user)):
    users = db.query(AdminUser).order_by(AdminUser.username.asc()).all()
    return templates.TemplateResponse(
        name="users_list.html",
        request=request,
        context={"users": users, "current_user": user},
    )


@app.get("/admin/users/new", response_class=HTMLResponse)
async def admin_user_new(request: Request, user: Dict[str, Any] = Depends(require_admin_user)):
    return templates.TemplateResponse(
        name="user_form.html",
        request=request,
        context={"managed_user": None, "mode": "new", "current_user": user},
    )


@app.post("/admin/users/new")
async def admin_user_create(request: Request, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    form = await parse_form(request)
    username = form.get("username", "").strip()
    password = form.get("password", "")
    buyer_name = form.get("buyer_name", "").strip() or username
    role = normalize_role(form.get("role"))

    if not username or not password:
        raise HTTPException(status_code=400, detail="username_and_password_required")
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        raise HTTPException(status_code=400, detail="username_already_exists")

    managed_user = AdminUser(
        username=username,
        password_hash=hash_password(password),
        buyer_name=buyer_name,
        role=role,
        is_active=form.get("is_active") == "on",
        created_at=naive_app_now(),
        updated_at=naive_app_now(),
    )
    db.add(managed_user)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/users/{username}/edit", response_class=HTMLResponse)
async def admin_user_edit(
    username: str,
    request: Request,
    db: Session = Depends(get_db),
    user: Dict[str, Any] = Depends(require_admin_user),
):
    managed_user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not managed_user:
        raise HTTPException(status_code=404, detail="user_not_found")
    return templates.TemplateResponse(
        name="user_form.html",
        request=request,
        context={"managed_user": managed_user, "mode": "edit", "current_user": user},
    )


@app.post("/admin/users/{username}/edit")
async def admin_user_update(
    username: str,
    request: Request,
    db: Session = Depends(get_db),
    _: Dict[str, Any] = Depends(require_admin_user),
):
    managed_user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not managed_user:
        raise HTTPException(status_code=404, detail="user_not_found")

    form = await parse_form(request)
    new_username = form.get("username", "").strip()
    if not new_username:
        raise HTTPException(status_code=400, detail="username_required")
    existing = db.query(AdminUser).filter(AdminUser.username == new_username, AdminUser.id != managed_user.id).first()
    if existing:
        raise HTTPException(status_code=400, detail="username_already_exists")

    managed_user.username = new_username
    managed_user.buyer_name = form.get("buyer_name", "").strip() or new_username
    managed_user.role = normalize_role(form.get("role"))
    managed_user.is_active = form.get("is_active") == "on"
    new_password = form.get("password", "")
    if new_password:
        managed_user.password_hash = hash_password(new_password)
    managed_user.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{username}/disable")
async def admin_user_disable(username: str, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    managed_user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not managed_user:
        raise HTTPException(status_code=404, detail="user_not_found")
    managed_user.is_active = False
    managed_user.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{username}/enable")
async def admin_user_enable(username: str, db: Session = Depends(get_db), _: Dict[str, Any] = Depends(require_admin_user)):
    managed_user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not managed_user:
        raise HTTPException(status_code=404, detail="user_not_found")
    managed_user.is_active = True
    managed_user.updated_at = naive_app_now()
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=ADMIN_LOG_LIMIT_MAX),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    event: Optional[str] = Query(default=None),
    tracker_pixel_id: Optional[str] = Query(default=None),
    buyer: Optional[str] = Query(default="mine"),
    clickid: Optional[str] = Query(default=None),
    fbclid: Optional[str] = Query(default=None),
    domain: Optional[str] = Query(default=None),
    user: Dict[str, Any] = Depends(require_user),
):
    if clickid:
        logs = load_clickid_meta_logs(clickid)
    elif event:
        logs = load_event_meta_logs(event)
    else:
        logs = load_all_meta_logs()

    logs, selected_buyer = filter_meta_logs(
        logs,
        user=user,
        buyer=buyer,
        date_from=date_from,
        date_to=date_to,
        event=event,
        tracker_pixel_id=tracker_pixel_id,
        clickid=clickid,
        domain=domain,
    )
    if status_filter:
        logs = [row for row in logs if str(row.get("status_code", "")) == status_filter]
    if fbclid:
        logs = [row for row in logs if row_matches_filter(row, "fbclid", fbclid)]
    total_filtered = len(logs)
    logs = logs[:limit]

    buyers = available_buyers()
    date_shortcuts = app_date_shortcuts()
    return templates.TemplateResponse(
        name="logs.html",
        request=request,
	context=
	{
            "logs": logs,
            "limit": limit,
            "total_filtered": total_filtered,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "today_date": date_shortcuts["today"],
            "yesterday_date": date_shortcuts["yesterday"],
            "week_date": date_shortcuts["week"],
            "app_timezone": APP_TIMEZONE,
            "status_filter": status_filter or "",
            "event": event or "",
            "tracker_pixel_id": tracker_pixel_id or "",
            "buyer": buyer or "mine",
            "resolved_buyer": selected_buyer,
            "buyers": buyers,
            "clickid": clickid or "",
            "fbclid": fbclid or "",
            "domain": domain or "",
            "current_user": user,
        },
    )


@app.get("/admin/tiktok/logs", response_class=HTMLResponse)
async def admin_tiktok_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=ADMIN_LOG_LIMIT_MAX),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    event: Optional[str] = Query(default=None),
    tiktok_pixel_id: Optional[str] = Query(default=None),
    buyer: Optional[str] = Query(default="mine"),
    clickid: Optional[str] = Query(default=None),
    ttclid: Optional[str] = Query(default=None),
    flow_id: Optional[str] = Query(default=None),
    user: Dict[str, Any] = Depends(require_user),
):
    if clickid:
        logs = load_clickid_tiktok_logs(clickid)
    elif event:
        logs = load_event_tiktok_logs(event)
    else:
        logs = load_all_tiktok_logs()

    logs, selected_buyer = filter_tiktok_logs(
        logs,
        user=user,
        buyer=buyer,
        date_from=date_from,
        date_to=date_to,
        event=event,
        tiktok_pixel_id=tiktok_pixel_id,
        clickid=clickid,
        ttclid=ttclid,
        flow_id=flow_id,
    )
    if status_filter:
        logs = [row for row in logs if str(row.get("status_code", "")) == status_filter]
    total_filtered = len(logs)
    logs = logs[:limit]

    date_shortcuts = app_date_shortcuts()
    return templates.TemplateResponse(
        name="tiktok_logs.html",
        request=request,
        context={
            "logs": logs,
            "limit": limit,
            "total_filtered": total_filtered,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "today_date": date_shortcuts["today"],
            "yesterday_date": date_shortcuts["yesterday"],
            "week_date": date_shortcuts["week"],
            "app_timezone": APP_TIMEZONE,
            "status_filter": status_filter or "",
            "event": event or "",
            "tiktok_pixel_id": tiktok_pixel_id or "",
            "buyer": buyer or "mine",
            "resolved_buyer": selected_buyer,
            "buyers": available_tiktok_buyers(),
            "clickid": clickid or "",
            "ttclid": ttclid or "",
            "flow_id": flow_id or "",
            "current_user": user,
        },
    )


@app.get("/admin/quality", response_class=HTMLResponse)
async def admin_quality_dashboard(
    request: Request,
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    buyer: Optional[str] = Query(default="mine"),
    tracker_pixel_id: Optional[str] = Query(default=None),
    user: Dict[str, Any] = Depends(require_user),
):
    logs, selected_buyer = filter_meta_logs(
        load_all_meta_logs(),
        user=user,
        buyer=buyer,
        date_from=date_from,
        date_to=date_to,
        tracker_pixel_id=tracker_pixel_id,
    )
    dashboard = build_quality_dashboard(logs)
    date_shortcuts = app_date_shortcuts()
    return templates.TemplateResponse(
        name="quality.html",
        request=request,
        context={
            "dashboard": dashboard,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "today_date": date_shortcuts["today"],
            "yesterday_date": date_shortcuts["yesterday"],
            "week_date": date_shortcuts["week"],
            "app_timezone": APP_TIMEZONE,
            "buyer": buyer or "mine",
            "resolved_buyer": selected_buyer,
            "buyers": available_buyers(),
            "tracker_pixel_id": tracker_pixel_id or "",
            "current_user": user,
        },
    )


@app.get("/admin/diagnostics", response_class=HTMLResponse)
async def admin_click_diagnostics(
    request: Request,
    clickid: Optional[str] = Query(default=None),
    buyer: Optional[str] = Query(default="mine"),
    user: Dict[str, Any] = Depends(require_user),
):
    logs: List[Dict[str, Any]] = []
    diagnostics = None
    selected_buyer = selected_buyer_for_user(user, buyer)
    if clickid:
        logs, selected_buyer = filter_meta_logs(
            load_clickid_meta_logs(clickid),
            user=user,
            buyer=buyer,
            clickid=clickid,
        )
        diagnostics = build_click_diagnostics(logs, clickid)
    return templates.TemplateResponse(
        name="diagnostics.html",
        request=request,
        context={
            "clickid": clickid or "",
            "diagnostics": diagnostics,
            "buyer": buyer or "mine",
            "resolved_buyer": selected_buyer,
            "buyers": available_buyers(),
            "current_user": user,
        },
    )


@app.post("/postbacks/whale/tiktok")
async def whale_tiktok_postback(
    pb: WhaleTikTokPostback,
    request: Request,
    secret: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    ensure_whale_tiktok_secret(request, secret)
    context = {
        "clickid": pb.click_uuid,
        "ttclid": pb.ttclid,
        "flow_id": pb.flow_id,
        "status": pb.status,
        "pixel_id": pb.pixel_id,
    }

    try:
        pixel = get_tiktok_pixel_by_alias(pb.pixel_id, db, pb.flow_id)
    except HTTPException as exc:
        log.warning(
            "TikTok postback pixel resolution failed: pixel_id=%s flow_id=%s event_id=%s status=%s reason=%s",
            pb.pixel_id,
            pb.flow_id,
            pb.event_id,
            pb.status,
            exc.detail,
        )
        log_tiktok_to_redis({
            "tiktok_pixel_id": pb.pixel_id,
            "dataset_id": None,
            "status_code": exc.status_code,
            "event_name": pb.event or "CompletePayment",
            "event_id": pb.event_id,
            "clickid": pb.click_uuid,
            "ttclid": pb.ttclid,
            "flow_id": pb.flow_id,
            "status": pb.status,
            "rejected": True,
            "details": str(exc.detail),
        })
        raise

    event_name = pixel.event_name or pb.event or "CompletePayment"
    allowed_statuses = tiktok_allowed_statuses(pixel)
    if (pb.status or "").strip().lower() not in allowed_statuses:
        log_tiktok_to_redis({
            "tiktok_pixel_id": pixel.public_id,
            "buyer_name": pixel.buyer_name,
            "pixel_name": pixel.name,
            "dataset_id": pixel.dataset_id,
            "status_code": 200,
            "event_name": event_name,
            "event_id": pb.event_id,
            "clickid": pb.click_uuid,
            "ttclid": pb.ttclid,
            "flow_id": pb.flow_id,
            "status": pb.status,
            "ignored": True,
            "details": "status_not_allowed",
        })
        return {"ok": True, "ignored": True, "reason": "status_not_allowed"}

    if is_macro_or_empty(pb.ttclid):
        log.warning(
            "TikTok postback has weak ttclid: tiktok_pixel_id=%s dataset_id=%s event_id=%s ttclid=%s",
            pixel.public_id,
            pixel.dataset_id,
            pb.event_id,
            pb.ttclid,
        )
        if not pixel.send_without_ttclid:
            log_tiktok_to_redis({
                "tiktok_pixel_id": pixel.public_id,
                "buyer_name": pixel.buyer_name,
                "pixel_name": pixel.name,
                "dataset_id": pixel.dataset_id,
                "status_code": 200,
                "event_name": event_name,
                "event_id": pb.event_id,
                "clickid": pb.click_uuid,
                "ttclid": pb.ttclid,
                "flow_id": pb.flow_id,
                "status": pb.status,
                "ignored": True,
                "weak_matching": True,
                "details": "ttclid_missing",
            })
            return {"ok": True, "ignored": True, "reason": "ttclid_missing"}

    if not mark_tiktok_event_for_send(pixel.dataset_id, event_name, pb.event_id):
        log.warning(
            "Duplicate TikTok event skipped: tiktok_pixel_id=%s dataset_id=%s event=%s id=%s clickid=%s ttclid=%s",
            pixel.public_id,
            pixel.dataset_id,
            event_name,
            pb.event_id,
            pb.click_uuid,
            pb.ttclid,
        )
        log_tiktok_to_redis({
            "tiktok_pixel_id": pixel.public_id,
            "buyer_name": pixel.buyer_name,
            "pixel_name": pixel.name,
            "dataset_id": pixel.dataset_id,
            "status_code": 208,
            "event_name": event_name,
            "event_id": pb.event_id,
            "clickid": pb.click_uuid,
            "ttclid": pb.ttclid,
            "flow_id": pb.flow_id,
            "status": pb.status,
            "skipped_duplicate": True,
            "details": "duplicate_event_id_skipped",
        })
        return {
            "ok": True,
            "accepted": False,
            "duplicate": True,
            "tiktok_pixel_id": pixel.public_id,
            "dataset_id": pixel.dataset_id,
            "event_id": pb.event_id,
            "reason": "duplicate_event_id",
        }

    payload = build_tiktok_payload(pb, pixel, request)
    tiktok_result = await send_to_tiktok(payload, pixel, context)
    return {
        "ok": True,
        "accepted": True,
        "tiktok_pixel_id": pixel.public_id,
        "dataset_id": pixel.dataset_id,
        "event_id": pb.event_id,
        "tiktok": tiktok_result,
    }


@app.post("/api/pixel/track")
async def track(
    ev: TrackEvent,
    request: Request,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
):
    fbc = ev.fbc or make_fbc_from_fbclid(ev.fbclid)
    event_id = build_event_id(ev.clickid, ev.event_name, ev.order_id)
    context = {
        "clickid": ev.clickid,
        "fbclid": ev.fbclid,
        "fbp": ev.fbp,
        "fbc": fbc,
        "event_source_url": ev.event_source_url,
    }
    if API_PUBLIC_KEY and x_api_key != API_PUBLIC_KEY:
        log_rejected_event(
            event_name=ev.event_name,
            tracker_pixel_id=ev.tracker_pixel_id,
            status_code=401,
            reason="unauthorized",
            context=context,
            event_id=event_id,
        )
        raise HTTPException(status_code=401, detail="unauthorized")

    pixel = None
    try:
        pixel = get_pixel_by_public_id(ev.tracker_pixel_id, db)
        ensure_domain_allowed(pixel, ev.event_source_url)
    except HTTPException as exc:
        log_rejected_event(
            event_name=ev.event_name,
            tracker_pixel_id=ev.tracker_pixel_id,
            status_code=exc.status_code,
            reason=str(exc.detail),
            context=context,
            pixel=pixel,
            event_id=event_id,
        )
        raise

    now = int(time.time())
    event_time = ev.event_time or now
    ua = ev.ua or request.headers.get("user-agent", "")
    ip = get_client_ip(request, ev.ip)

    em_hash = ph_hash = None
    if ev.user_data_raw:
        if isinstance(ev.user_data_raw.get("email"), str) and ev.user_data_raw["email"].strip():
            em_hash = sha256_norm_email(ev.user_data_raw["email"])
        if isinstance(ev.user_data_raw.get("phone"), str) and ev.user_data_raw["phone"].strip():
            ph_hash = sha256_norm_phone(ev.user_data_raw["phone"])

    cache_fp(ev.clickid, pixel.public_id, ev.fbclid, ev.fbp, fbc, ip, ua, em_hash, ph_hash)

    if not mark_event_for_send(pixel.public_id, ev.event_name, event_id):
        log_duplicate_event(pixel, ev.event_name, event_id, context)
        return {
            "ok": True,
            "accepted": False,
            "duplicate": True,
            "tracker_pixel_id": pixel.public_id,
            "event_id": event_id,
            "reason": "duplicate_event_id",
        }

    meta_event = {
        "event_name": ev.event_name,
        "event_time": event_time,
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": ev.event_source_url,
        "user_data": {
            **({"em": [em_hash]} if em_hash else {}),
            **({"ph": [ph_hash]} if ph_hash else {}),
            **({"client_ip_address": ip} if ip else {}),
            **({"client_user_agent": ua} if ua else {}),
            **({"fbp": ev.fbp} if ev.fbp else {}),
            **({"fbc": fbc} if fbc else {}),
        },
        "custom_data": {
            **({"currency": ev.currency} if ev.currency else {}),
            **({"value": ev.value} if ev.value is not None else {}),
            **(ev.extra or {}),
        },
    }

    meta_result = await send_to_meta(meta_event, pixel.meta_pixel_id, pixel.meta_access_token, pixel, context)

    return {
        "ok": True,
        "tracker_pixel_id": pixel.public_id,
        "event_id": event_id,
        "meta": meta_result,
    }


@app.get("/postback/lemonad")
async def lemonad_postback(
    clickid: str = Query(...),
    status: str = Query(...),
    currency: str = Query(...),
    value: str = Query(...),
    tracker_pixel_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    context = {
        "clickid": clickid,
        "fbclid": None,
        "fbp": None,
        "fbc": None,
        "event_source_url": None,
    }
    event_id = build_event_id(clickid, "Purchase", None)
    if status.lower() != "sale":
        return {"ok": True, "accepted": False, "reason": "status_ignored", "status": status}

    cur = currency.upper() if currency else None
    val: Optional[float] = None
    if value is not None:
        try:
            val = float(value)
        except Exception:
            log.warning("Postback value is not a number: %s (clickid=%s)", value, clickid)

    fp = load_fp(clickid)
    if not fp:
        log.warning("No fingerprint in Redis for clickid=%s; Purchase will have weak matching", clickid)
    else:
        context.update({
            "fbclid": fp.get("fbclid"),
            "fbp": fp.get("fbp"),
            "fbc": fp.get("fbc"),
        })

    resolved_tracker_pixel_id = tracker_pixel_id or fp.get("tracker_pixel_id")
    if not resolved_tracker_pixel_id:
        log_rejected_event(
            event_name="Purchase",
            tracker_pixel_id=tracker_pixel_id,
            status_code=422,
            reason="tracker_pixel_id_not_found",
            context=context,
            event_id=event_id,
            details="postback_without_tracker_pixel_id_or_click_fingerprint",
        )
        return {"ok": False, "accepted": False, "reason": "tracker_pixel_id_not_found"}

    try:
        pixel = get_pixel_by_public_id(resolved_tracker_pixel_id, db)
    except HTTPException as exc:
        log_rejected_event(
            event_name="Purchase",
            tracker_pixel_id=resolved_tracker_pixel_id,
            status_code=exc.status_code,
            reason=str(exc.detail),
            context=context,
            event_id=event_id,
            details="postback_pixel_resolution_failed",
        )
        raise
    user_data = dict(fp)
    user_data.pop("tracker_pixel_id", None)
    user_data.pop("fbclid", None)

    context = {
        "clickid": clickid,
        "fbclid": fp.get("fbclid"),
        "fbp": fp.get("fbp"),
        "fbc": fp.get("fbc"),
        "event_source_url": None,
    }

    if not mark_event_for_send(pixel.public_id, "Purchase", event_id):
        log_duplicate_event(pixel, "Purchase", event_id, context)
        return {
            "ok": True,
            "accepted": False,
            "duplicate": True,
            "tracker_pixel_id": pixel.public_id,
            "event_id": event_id,
            "reason": "duplicate_event_id",
        }

    meta_event = {
        "event_name": "Purchase",
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": "website",
        "user_data": user_data,
        "custom_data": {
            **({"currency": cur} if cur else {}),
            **({"value": val} if val is not None else {}),
        },
    }
    meta_result = await send_to_meta(meta_event, pixel.meta_pixel_id, pixel.meta_access_token, pixel, context)
    return {
        "ok": True,
        "accepted": True,
        "tracker_pixel_id": pixel.public_id,
        "event_id": event_id,
        "meta": meta_result,
    }
