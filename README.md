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

## Admin UI

Admin pages are protected by HTTP Basic Auth:

- pixels: `/admin/pixels`
- new pixel: `/admin/pixels/new`
- logs: `/admin/logs`

Credentials come from:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...
```

Pixel fields:

- `public_id` - generated internal public ID, for example `px_kxzo3Jo83KA`
- `name` - pixel/campaign label
- `buyer_name` - buyer name used in logs and filters
- `meta_pixel_id` - real Meta Pixel ID
- `meta_access_token` - real Meta CAPI token, server-only
- `allowed_domains` - optional list of allowed landing domains
- `is_active` - disabled pixels reject events

If `allowed_domains` is empty, any domain is allowed. If it is set and `STRICT_DOMAIN_CHECK=true`, non-matching domains return `403 domain_not_allowed`. With `STRICT_DOMAIN_CHECK=false`, the service logs a warning but still sends the event.

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

Filtering behavior:

- `limit=300&event=Lead` returns up to 300 Lead rows after filtering.
- `buyer=mine` filters by `buyer_name == ADMIN_USERNAME`.
- `buyer=__all__` shows all buyers.
- filters are available for status, event, tracker pixel, buyer, clickid, fbclid, and domain.

## Deduplication

Redis key:

```text
dedup:meta:<sha256(tracker_pixel_id|event_name|event_id)>
```

Default TTL:

```env
DEDUP_TTL_SECONDS=600
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
```

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
- `.github/workflows/deploy.yml` - manual deploy via GitHub Actions.

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

Current workflow is manual (`workflow_dispatch`) by design. After one or two successful manual deploys, it can be changed to deploy automatically on push to `main`.

## Server Notes

From the last production inspection:

- systemd starts `/opt/api-pixel/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8080 --workers 2`
- Nginx proxies `api.naturalgoods.info` to `http://127.0.0.1:8080`
- `sqlite3` CLI was not installed on the server
- Redis is required for fingerprints, logs, and deduplication

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
