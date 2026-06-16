# API Pixel

FastAPI service for Meta Conversion API with multiple internal tracker pixels.

## Runtime

Production layout:

- app dir: `/opt/api-pixel`
- service: `api-pixel.service`
- bind: `127.0.0.1:8080`
- public host: `https://api.naturalgoods.info`
- database: SQLite at `DB_PATH`
- runtime cache/logs: Redis

Do not commit `.env`, `.venv`, `api-pixel.db`, or `__pycache__`.

## Local Run

```bash
python -m venv .venv-local
.venv-local/bin/pip install -r requirements.txt
cp .env.example .env
DB_PATH=./api-pixel.local.db ADMIN_PASSWORD=admin REDIS_URL=redis://localhost:6379/0 .venv-local/bin/uvicorn main:app --reload
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv-local
.\.venv-local\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:DB_PATH="$PWD\api-pixel.local.db"
$env:ADMIN_PASSWORD="admin"
.\.venv-local\Scripts\python.exe -m uvicorn main:app --reload
```

## Admin

- Pixels: `/admin/pixels`
- Meta logs: `/admin/logs`

Admin auth uses `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

## Deploy Manually

Copy changed source files to `/opt/api-pixel`, install dependencies, then restart:

```bash
cd /opt/api-pixel
.venv/bin/pip install -r requirements.lock.txt
systemctl restart api-pixel
systemctl status api-pixel --no-pager
```

## GitHub Actions Deploy

The repository contains two workflows:

- `.github/workflows/ci.yml` runs compile and smoke tests on push/PR.
- `.github/workflows/deploy.yml` runs the same checks and deploys manually via `workflow_dispatch`.

Required repository secrets:

- `DEPLOY_HOST` - server host, for example `45.11.229.77`
- `DEPLOY_PORT` - SSH port, usually `22`
- `DEPLOY_USER` - SSH user with permissions to update `/opt/api-pixel` and restart `api-pixel`
- `DEPLOY_SSH_KEY` - private SSH key for that user

The deploy workflow preserves production-only files because the archive excludes `.env`, `.venv`, SQLite database files, and git metadata.

## Smoke Test

```bash
python scripts/smoke_test.py
```
