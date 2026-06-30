# API Pixel

FastAPI service for Meta Conversion API with multi-pixel routing.

The service receives browser events from landing pages, resolves an internal `tracker_pixel_id` to a real Meta Pixel ID/access token, sends events to Meta CAPI, stores attribution fingerprints in Redis, and handles Lemonad purchase postbacks.

## Production

Current production setup:

- public host: `https://api.naturalgoods.info`
- app dir: `/opt/api-pixel`
- systemd service: `api-pixel.service`
- app bind: `127.0.0.1:8080`
- reverse proxy: Nginx, `api-pixel.conf`
- database: SQLite, `DB_PATH=/opt/api-pixel/api-pixel.db`
- cache/logs/fingerprints: Redis
- Python: `3.11`
- process user: `api-pixel`

Useful server commands:

```bash
systemctl status api-pixel --no-pager
journalctl -u api-pixel -n 120 --no-pager
systemctl restart api-pixel
```

## Repository Layout

```text
main.py                         FastAPI app, DB model, API, admin UI routes
static/capi-tracker.js           Browser tracker script for landing pages
templates/                       Jinja admin pages
scripts/smoke_test.py            Offline smoke test with fake Redis/Meta
deploy/api-pixel.service         Current systemd unit template
deploy/nginx-api-pixel.conf.example
requirements.txt                 Direct development dependencies
requirements.lock.txt            Production dependency lock from server
.env.example                     Safe env template
```

Never commit:

- `.env`
- `.venv`, `.venv-local`
- `api-pixel.db`
- `__pycache__`
- access tokens, API keys, passwords, raw phone/email dumps

## Event Flow

Browser landing page:

1. Loads `static/capi-tracker.js`.
2. Sends `PageView` to `POST /api/pixel/track`.
3. On form submit sends `Lead` to `POST /api/pixel/track`.
4. Payload includes internal `tracker_pixel_id`, not real Meta credentials.

Server:

1. Checks `x-api-key`.
2. Finds pixel config by `tracker_pixel_id`.
3. Optionally checks `event_source_url` against `allowed_domains`.
4. Hashes phone/email.
5. Stores fingerprint in Redis by `clickid`.
6. Sends CAPI event to the resolved Meta Pixel.
7. Stores Meta response summary in Redis log list.

Lemonad:

1. Calls `GET /postback/lemonad?clickid=...&status=sale&currency=...&value=...`.
2. Server loads fingerprint by `clickid`.
3. Resolves pixel from query `tracker_pixel_id` or Redis fingerprint.
4. Sends `Purchase` to the same pixel.

Whale -> TikTok:

1. Calls `POST /postbacks/whale/tiktok?secret=...` with JSON.
2. Server resolves TikTok config by Whale `pixel_id` alias first, then by `flow_id`.
3. Only allowed statuses are sent, default: `Approved,Paid`.
4. Server builds TikTok Events API payload and adds the server-side Events API token.
5. Duplicate `dataset_id + event + event_id` events are skipped in Redis.
6. TikTok responses are stored in separate Redis logs and visible in `/admin/tiktok/logs`.

## Admin UI

Admin pages are protected by HTTP Basic Auth:

- FB pixels: `/admin/pixels`
- TikTok pixels: `/admin/tiktok/pixels`
- new pixel: `/admin/pixels/new`
- logs: `/admin/logs`
- TikTok logs: `/admin/tiktok/logs`
- users: `/admin/users`

There are two user roles:

- `admin` - can manage pixels, users, and view all logs.
- `buyer` - can view logs only for their own `buyer_name`.

On first startup, if the `admin_users` table is empty, the service creates a bootstrap admin from:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...
```

After that, create real users in `/admin/users`. Passwords are stored as PBKDF2 hashes, not plaintext.

For buyer users, set `buyer_name` to the same value used in pixel configs and logs, for example `Andrey`. This is what drives default log filtering.

Pixel fields:

- `public_id` - generated internal public ID, for example `px_kxzo3Jo83KA`
- `name` - pixel/campaign label
- `buyer_name` - buyer name used in logs and filters
- `meta_pixel_id` - real Meta Pixel ID
- `meta_access_token` - real Meta CAPI token, server-only
- `allowed_domains` - optional list of allowed landing domains
- `is_active` - disabled pixels reject events

If `allowed_domains` is empty, any domain is allowed. If it is set and `STRICT_DOMAIN_CHECK=true`, non-matching domains return `403 domain_not_allowed`. With `STRICT_DOMAIN_CHECK=false`, the service logs a warning but still sends the event.

TikTok pixel fields:

- `public_id` - Whale `pixel_id` alias, for example `D75QFE3C77UDH74CJM70` or internal `tt_xxxxx`
- `name` - pixel/campaign label
- `buyer_name` - buyer name used in logs and filters
- `dataset_id` - real TikTok Dataset/Pixel ID
- `access_token` - real TikTok Events API token, server-only
- `event_name` - TikTok event sent to Events API, usually `Purchase`; Whale may still send `CompletePayment` in its own payload
- `currency` - default currency, usually `USD`
- `allowed_statuses` - comma/newline list, default `Approved,Paid`
- `flow_ids` - optional Whale flow aliases used as fallback mapping
- `test_event_code` - optional TikTok Test Events code
- `send_without_ttclid` - if disabled, Whale conversions without valid `ttclid` are ignored
- `is_active` - disabled TikTok pixels reject postbacks

TikTok tokens are never returned to Whale and should not be copied into flow URLs.

## Browser Tracker

Buyers receive only internal config:

```js
trackerPixelId: "px_xxxxx"
```

They must not receive:

- real `meta_pixel_id`
- real `meta_access_token`

The tracker sends:

- `tracker_pixel_id`
- `event_name`
- `clickid` from `clickid`, `click_id`, `subid`, or `sub_id`
- `fbclid`
- `_fbp`
- `_fbc`
- `event_source_url`
- hashed user data is produced server-side

The script has guards against duplicate initialization and duplicate submit, but the server also has Redis-based deduplication.

## Logs

Open:

```text
https://api.naturalgoods.info/admin/logs
```

Important columns:

- `event_id` - deduplication ID sent to Meta
- `clickid` - tracker/sub id used for Redis fingerprint and postback matching
- `fbclid` - Facebook click id from URL
- `fbc` - Meta click cookie format derived from `fbclid`
- `fbtrace` - Meta response trace id; useful for debugging Meta API responses, not a click id
- `dup` - duplicate event skipped by server
- `rejected before Meta` - request was received but rejected before Meta send, for example bad pixel id, disabled pixel, strict domain block, or missing postback fingerprint
- `Meta send failed` - request was accepted by the service but the outbound Meta API call failed

Log time is displayed and date-filtered in the configured application timezone, `APP_TIMEZONE=Europe/Moscow` by default.

Filtering behavior:

- `limit=300&event=Lead` returns up to 300 Lead rows after filtering.
- event and clickid filters read dedicated Redis indexes, so Leads are not lost from search just because recent PageView rows filled the main log stream.
- for `buyer` users, logs are always restricted to their own `buyer_name`.
- for `admin` users, `buyer=mine` filters by the admin user's `buyer_name`.
- for `admin` users, `buyer=__all__` shows all buyers.
- filters are available for status, event, tracker pixel, buyer, clickid, fbclid, and domain.
- date filters use Moscow dates by default and support exact day/range filtering.

## Deduplication

Redis key:

```text
dedup:meta:<sha256(tracker_pixel_id|event_name|event_id)>
```

Default TTL:

```env
DEDUP_TTL_SECONDS=600
APP_TIMEZONE=Europe/Moscow
META_LOG_MAX=10000
META_EVENT_LOG_MAX=5000
META_CLICK_LOG_MAX=200
ADMIN_LOG_LIMIT_MAX=1000
```

If the same event repeats during this window, the service returns:

```json
{
  "ok": true,
  "accepted": false,
  "duplicate": true,
  "reason": "duplicate_event_id"
}
```

The duplicate is logged in `/admin/logs`, but it is not sent to Meta.

TikTok deduplication uses:

```text
dedup:tiktok:<sha256(dataset_id|event_name|event_id)>
```

Whale duplicate postbacks return `200 OK` with `duplicate: true` and are not sent to TikTok again.

## Whale TikTok Postback

Endpoint:

```text
POST /postbacks/whale/tiktok?secret=<WHALE_TIKTOK_SECRET>
```

For manual diagnostics only, the TikTok event can be overridden in the URL:

```text
POST /postbacks/whale/tiktok?secret=<WHALE_TIKTOK_SECRET>&tiktok_event=SubmitForm
```

In production, keep the desired event in `/admin/tiktok/pixels`. Common values are `Purchase`, `SubmitForm`, `CompleteRegistration`, `Contact`, `InitiateCheckout`, `AddToCart`, and `ViewContent`.

The secret can also be sent as:

```text
Authorization: Bearer <WHALE_TIKTOK_SECRET>
```

Expected JSON shape:

```json
{
  "source": "whale",
  "event": "CompletePayment",
  "event_id": "conversion_id",
  "status": "Approved",
  "payout": "14.50",
  "offer_id": "offer_id",
  "flow_id": "flow_id",
  "source_id": "source_id",
  "click_uuid": "uuid",
  "ip": "127.0.0.1",
  "ttclid": "ttclid_value",
  "pixel_id": "D75QFE3C77UDH74CJM70",
  "campaign_id": "campaign_id",
  "campaign_name": "campaign_name",
  "adgroup_id": "adgroup_id",
  "adgroup_name": "adgroup_name",
  "creative_id": "creative_id",
  "creative_name": "creative_name",
  "created_at": "2026-06-30T10:00:00Z",
  "updated_at": "2026-06-30T10:01:00Z"
}
```

`pixel_id` is the alias configured in `/admin/tiktok/pixels`. For the first Whale flow, if the URL contains:

```text
aff_pixel_id=D75QFE3C77UDH74CJM70
```

then create a TikTok pixel with `public_id=D75QFE3C77UDH74CJM70` and set the real `dataset_id` and `access_token` on the server.

TikTok accepts events without email or phone, but matching quality is weaker. The service sends the available `ttclid`, IP, user agent, value, currency, `content_id`, and campaign metadata. Email/phone can only be sent if the landing/order source provides them to the server.

Ignored statuses return:

```json
{"ok": true, "ignored": true, "reason": "status_not_allowed"}
```

Unknown TikTok pixel aliases return `400 unknown_tiktok_pixel_id` and are logged in `/admin/tiktok/logs`.

## Environment

Use `.env.example` as a template.

Required production variables:

```env
API_PUBLIC_KEY=...
REDIS_URL=redis://localhost:6379/0
DB_PATH=/opt/api-pixel/api-pixel.db
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...
STRICT_DOMAIN_CHECK=false
CORS_ALLOW_ORIGINS=*
DEDUP_TTL_SECONDS=600
WHALE_TIKTOK_SECRET=...
TIKTOK_ALLOWED_STATUSES=Approved,Paid
TIKTOK_SEND_WITHOUT_TTCLID=true
TIKTOK_EVENTS_API_URL=https://business-api.tiktok.com/open_api/v1.3/event/track/
TIKTOK_TIMEOUT_SECONDS=10
TIKTOK_LOG_MAX=10000
TIKTOK_EVENT_LOG_MAX=5000
TIKTOK_CLICK_LOG_MAX=200
```

`ADMIN_USERNAME` and `ADMIN_PASSWORD` are used for bootstrap only when the DB has no users yet. Keep them valid as an emergency fallback, but manage normal access through `/admin/users`.

Legacy variables can stay for compatibility, but new multi-pixel routing should use DB pixel configs:

```env
META_PIXEL_ID=
META_ACCESS_TOKEN=
META_TEST_EVENT_CODE=
```

## Local Development

Windows PowerShell:

```powershell
py -3.11 -m venv .venv-local
.\.venv-local\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
$env:DB_PATH="$PWD\api-pixel.local.db"
$env:ADMIN_PASSWORD="admin"
$env:REDIS_URL="redis://localhost:6379/0"
.\.venv-local\Scripts\python.exe -m uvicorn main:app --reload
```

Linux/macOS:

```bash
python3.11 -m venv .venv-local
.venv-local/bin/pip install -r requirements.lock.txt
cp .env.example .env
DB_PATH=./api-pixel.local.db ADMIN_PASSWORD=admin REDIS_URL=redis://localhost:6379/0 .venv-local/bin/uvicorn main:app --reload
```

If Redis is not running locally, the app import still works, but real API calls using Redis will fail. The smoke test uses fake Redis.

## Tests

Run:

```bash
python scripts/smoke_test.py
```

The smoke test checks:

- `/healthz`
- `/admin/pixels`
- `POST /api/pixel/track`
- duplicate Lead handling

It does not send real requests to Meta.

## Manual Deploy

Manual server deploy:

```bash
cd /opt/api-pixel
.venv/bin/pip install -r requirements.lock.txt
systemctl restart api-pixel
systemctl status api-pixel --no-pager
```

When copying files manually, copy source files only. Do not overwrite:

- `/opt/api-pixel/.env`
- `/opt/api-pixel/.venv`
- `/opt/api-pixel/api-pixel.db`

## GitHub Actions

Workflows:

- `.github/workflows/ci.yml` - runs compile and smoke test on push/PR.
- `.github/workflows/deploy.yml` - deploys automatically on push to `main` and can also be started manually.

GitHub repository secrets for deploy:

```text
DEPLOY_HOST=45.11.229.77
DEPLOY_PORT=22
DEPLOY_USER=root
DEPLOY_SSH_KEY=<private SSH key>
```

The deploy workflow:

1. Checks out the repo.
2. Installs `requirements.lock.txt`.
3. Runs compile and smoke test.
4. Builds an archive excluding `.env`, `.venv`, DB files, and git metadata.
5. Uploads it to the server.
6. Extracts into `/opt/api-pixel`.
7. Installs locked dependencies.
8. Restarts `api-pixel`.

The deploy workflow is expected to run after every push to `main`; check GitHub Actions after pushing.

## Server Notes

From the last production inspection:

- systemd starts `/opt/api-pixel/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 --workers 2`
- Nginx proxies `api.naturalgoods.info` to `http://127.0.0.1:8080`
- `sqlite3` CLI was not installed on the server
- Redis is required for fingerprints, logs, and deduplication
- user accounts are stored in SQLite table `admin_users`

Useful diagnostics:

```bash
journalctl -u api-pixel -n 120 --no-pager
redis-cli keys 'log:*'
redis-cli keys 'click:*'
redis-cli keys 'dedup:*'
```

## Known Follow-ups

- Add real buyer accounts instead of only one Basic Auth pair.
- Add DB migrations instead of `Base.metadata.create_all`.
- Add structured request rejection logs for `400` before Meta send.
- Add pagination for `/admin/logs` if Redis log volume grows.
- Move from SQLite to Postgres if write volume increases.
- After deploy workflow is proven, enable deploy on push to `main`.
