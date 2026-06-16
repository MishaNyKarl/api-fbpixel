import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime
from typing import Any, Dict, Optional, Set
from urllib.parse import parse_qs, urlparse

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
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
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

META_LOG_KEY = "log:meta"
META_LOG_MAX = 500
CLICK_TTL = 60 * 60 * 24 * 11


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
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def generate_public_id() -> str:
    return "px_" + secrets.token_urlsafe(8)


def generate_unique_public_id(db: Session) -> str:
    for _ in range(10):
        public_id = generate_public_id()
        if not db.query(Pixel).filter(Pixel.public_id == public_id).first():
            return public_id
    raise RuntimeError("failed_to_generate_public_id")


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


def log_meta_to_redis(entry: Dict[str, Any]):
    try:
        entry["_ts"] = int(time.time())
        rds.lpush(META_LOG_KEY, json.dumps(entry, ensure_ascii=False))
        rds.ltrim(META_LOG_KEY, 0, META_LOG_MAX - 1)
    except Exception as e:
        log.warning("Failed to write meta log to Redis: %s", e)


def load_meta_logs(limit: int = 100):
    rows = rds.lrange(META_LOG_KEY, 0, max(0, min(limit, META_LOG_MAX) - 1))
    result = []
    for row in rows:
        try:
            result.append(json.loads(row))
        except Exception:
            result.append({"raw": row})
    return result


def load_all_meta_logs():
    return load_meta_logs(META_LOG_MAX)


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
        "when": datetime.utcnow().isoformat() + "Z",
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


def mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


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

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)

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
        "when": datetime.utcnow().isoformat() + "Z",
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


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="admin_password_not_configured")
    username_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin_auth_required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def row_matches_filter(row: Dict[str, Any], key: str, expected: Optional[str]) -> bool:
    if not expected:
        return True
    return str(row.get(key) or "").lower() == expected.lower()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/admin/pixels", response_class=HTMLResponse)
async def admin_pixels(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    pixels = db.query(Pixel).order_by(Pixel.created_at.desc()).all()
    return templates.TemplateResponse(
        name="pixels_list.html",
        request=request,
	context={"pixels": pixels, "mask_token": mask_token},
    )


@app.get("/admin/pixels/new", response_class=HTMLResponse)
async def admin_pixel_new(request: Request, _: str = Depends(require_admin)):
    return templates.TemplateResponse(
        name="pixel_form.html",
        request=request,
	context={"pixel": None, "mode": "new", "masked_token": ""},
    )


@app.post("/admin/pixels/new")
async def admin_pixel_create(request: Request, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    form = await parse_form(request)
    pixel = Pixel(
        public_id=generate_unique_public_id(db),
        name=form.get("name", "").strip(),
        buyer_name=form.get("buyer_name", "").strip() or None,
        meta_pixel_id=form.get("meta_pixel_id", "").strip(),
        meta_access_token=form.get("meta_access_token", "").strip(),
        allowed_domains=form.get("allowed_domains", "").strip() or None,
        is_active=form.get("is_active") == "on",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
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
    _: str = Depends(require_admin),
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
        },
    )


@app.post("/admin/pixels/{public_id}/edit")
async def admin_pixel_update(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
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
    pixel.updated_at = datetime.utcnow()
    if not pixel.name or not pixel.meta_pixel_id or not pixel.meta_access_token:
        raise HTTPException(status_code=400, detail="name_pixel_id_and_token_required")
    db.commit()
    return RedirectResponse(f"/admin/pixels/{pixel.public_id}/edit?saved=1", status_code=303)


@app.post("/admin/pixels/{public_id}/disable")
async def admin_pixel_disable(public_id: str, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    pixel.is_active = False
    pixel.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/admin/pixels", status_code=303)


@app.post("/admin/pixels/{public_id}/enable")
async def admin_pixel_enable(public_id: str, db: Session = Depends(get_db), _: str = Depends(require_admin)):
    pixel = db.query(Pixel).filter(Pixel.public_id == public_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="pixel_not_found")
    pixel.is_active = True
    pixel.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/admin/pixels", status_code=303)


@app.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=META_LOG_MAX),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    event: Optional[str] = Query(default=None),
    tracker_pixel_id: Optional[str] = Query(default=None),
    buyer: Optional[str] = Query(default="mine"),
    clickid: Optional[str] = Query(default=None),
    fbclid: Optional[str] = Query(default=None),
    domain: Optional[str] = Query(default=None),
    user: str = Depends(require_admin),
):
    logs = load_all_meta_logs()
    selected_buyer = buyer or "mine"
    if selected_buyer == "mine":
        selected_buyer = user

    if status_filter:
        logs = [row for row in logs if str(row.get("status_code", "")) == status_filter]
    if event:
        logs = [row for row in logs if str(row.get("event_name", "")).lower() == event.lower()]
    if tracker_pixel_id:
        logs = [row for row in logs if row.get("tracker_pixel_id") == tracker_pixel_id]
    if selected_buyer != "__all__":
        logs = [row for row in logs if row_matches_filter(row, "buyer_name", selected_buyer)]
    if clickid:
        logs = [row for row in logs if row_matches_filter(row, "clickid", clickid)]
    if fbclid:
        logs = [row for row in logs if row_matches_filter(row, "fbclid", fbclid)]
    if domain:
        logs = [row for row in logs if row_matches_filter(row, "source_domain", normalize_domain(domain))]
    total_filtered = len(logs)
    logs = logs[:limit]

    buyers = sorted({row.get("buyer_name") for row in load_all_meta_logs() if row.get("buyer_name")})
    return templates.TemplateResponse(
        name="logs.html",
        request=request,
	context=
	{
            "logs": logs,
            "limit": limit,
            "total_filtered": total_filtered,
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


@app.post("/api/pixel/track")
async def track(
    ev: TrackEvent,
    request: Request,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
):
    if API_PUBLIC_KEY and x_api_key != API_PUBLIC_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")

    pixel = get_pixel_by_public_id(ev.tracker_pixel_id, db)
    ensure_domain_allowed(pixel, ev.event_source_url)

    now = int(time.time())
    event_time = ev.event_time or now
    ua = ev.ua or request.headers.get("user-agent", "")
    ip = get_client_ip(request, ev.ip)
    fbc = ev.fbc or make_fbc_from_fbclid(ev.fbclid)

    em_hash = ph_hash = None
    if ev.user_data_raw:
        if isinstance(ev.user_data_raw.get("email"), str) and ev.user_data_raw["email"].strip():
            em_hash = sha256_norm_email(ev.user_data_raw["email"])
        if isinstance(ev.user_data_raw.get("phone"), str) and ev.user_data_raw["phone"].strip():
            ph_hash = sha256_norm_phone(ev.user_data_raw["phone"])

    cache_fp(ev.clickid, pixel.public_id, ev.fbclid, ev.fbp, fbc, ip, ua, em_hash, ph_hash)
    event_id = build_event_id(ev.clickid, ev.event_name, ev.order_id)
    context = {
        "clickid": ev.clickid,
        "fbclid": ev.fbclid,
        "fbp": ev.fbp,
        "fbc": fbc,
        "event_source_url": ev.event_source_url,
    }

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

    resolved_tracker_pixel_id = tracker_pixel_id or fp.get("tracker_pixel_id")
    if not resolved_tracker_pixel_id:
        return {"ok": False, "accepted": False, "reason": "tracker_pixel_id_not_found"}

    pixel = get_pixel_by_public_id(resolved_tracker_pixel_id, db)
    user_data = dict(fp)
    user_data.pop("tracker_pixel_id", None)
    user_data.pop("fbclid", None)

    event_id = build_event_id(clickid, "Purchase", None)
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
