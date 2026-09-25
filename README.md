# aajhee-backend

Django REST API for categories, offers, map businesses, JWT auth, and user preferences.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # optional: set SECRET_KEY / DEBUG
python manage.py migrate
python manage.py runserver
```

## Commerce platform (Product / Order)

Aajhee is evolving from offer/QR redemption to a **product catalog + order** marketplace.

**Preferred APIs (use these for new clients):**
- `GET /api/feeds/home` — top picks, offers/discounts, trending products
- `GET /api/products`, `GET /api/products/<id>`
- `GET /api/stores/branch/<id>/catalog` — discounted-first + categories + contacts
- `GET/POST /api/cart`, checkout (`/api/checkout/preview`, `/api/checkout/place`)
- `GET /api/orders`, payment proof, cancel
- Business: `/api/business/products`, `/api/business/orders`, branch contacts & fulfillment
- Admin: `/api/admin/products`, `/api/admin/orders`, `/api/admin/categories/tree`

**Deprecated (kept for transition):** QR scan/redeem and primary offer discovery flows.
Prefer Product + Order for new UI. Offer endpoints remain until clients fully cut over.

## API docs

- **Production Swagger:** https://api.aajhee.com/api/docs/
- **Production OpenAPI:** https://api.aajhee.com/api/schema/
- Local OpenAPI: `http://127.0.0.1:8000/api/schema/`
- Local Swagger: `http://127.0.0.1:8000/api/docs/`

## Postman

Share these files with anyone testing the API:

| File | Purpose |
|------|---------|
| [`postman/Aajhee-API.postman_collection.json`](postman/Aajhee-API.postman_collection.json) | All endpoints, sample bodies, auto-save JWT on login |
| [`postman/Aajhee-Production.postman_environment.json`](postman/Aajhee-Production.postman_environment.json) | `base_url` → production |
| [`postman/Aajhee-Local.postman_environment.json`](postman/Aajhee-Local.postman_environment.json) | `base_url` → local dev server |

**Import in Postman**

1. **Import** → drag the collection + environment JSON files (or **Link** → `https://api.aajhee.com/api/schema/` to import from OpenAPI only).
2. Select **Aajhee — Production** (top-right environment dropdown).
3. Run **Auth — Consumer → Login** or **Auth — Business → Login** — the access token is saved automatically.
4. Call endpoints in **Public**, **Consumer**, or **Business** folders.

Regenerate the collection after endpoint changes: `python3 postman/generate_collection.py`

## Auth (JWT)

Consumer apps use **Firebase Auth** (phone OTP, Google, or Apple). The app signs in with Firebase, then exchanges the Firebase ID token for Aajhee JWTs:

- `POST /api/auth/firebase` — `{ "id_token": "<firebase_id_token>" }` → `access`, `refresh`, `user`, `addresses`
- `POST /api/auth/phone` — same as `/api/auth/firebase` (backwards-compatible alias)
- `POST /api/auth/token/refresh` — `refresh`
- Protected routes: `Authorization: Bearer <access>`

Requires the same Firebase Admin credentials used for FCM (`FIREBASE_CREDENTIALS_JSON` or `FIREBASE_CREDENTIALS_PATH`).

Email/password endpoints remain available for business/admin and legacy tooling (`/api/auth/register`, `/api/auth/token`, password reset).

### Firebase Admin credentials

Without these, `POST /api/auth/firebase` returns **401** (`Firebase authentication is unavailable…` or `Invalid or expired Firebase ID token.`).

**Local:** in `.env` set either:

```bash
FIREBASE_CREDENTIALS_PATH=/absolute/path/to/firebase-adminsdk.json
# or
FIREBASE_CREDENTIALS_JSON={"type":"service_account",...}
```

Download the key from [Firebase Console](https://console.firebase.google.com/) → Project settings → Service accounts → Generate new private key. The `project_id` must match the mobile apps (`aajhee`).

**Render (production):** Web Service → Environment → add `FIREBASE_CREDENTIALS_JSON` with the **entire** service-account JSON as one line (minified). Do not use `FIREBASE_CREDENTIALS_PATH` on Render unless you also upload a [Secret File](https://render.com/docs/configure-environment-variables#secret-files). Redeploy after setting it.

Quick minify for pasting into Render:

```bash
python3 -c 'import json,sys; print(json.dumps(json.load(open("secrets/firebase-adminsdk.json")), separators=(",",":")))'
```

## Deploy on Render

1. Push this repo to GitHub (see below).
2. In [Render](https://dashboard.render.com): **New** → **Blueprint** → connect the repo, or **Web Service** + **PostgreSQL** manually.
3. If you use the included `render.yaml`, Render creates a **PostgreSQL** database and sets `DATABASE_URL`; the web service runs migrations then Gunicorn on each start (free tier does not support `preDeployCommand`, so migrate is in `startCommand`).
4. In the web service, set environment variables if not using Blueprint:
   - `DEBUG=False`
   - `SECRET_KEY` (long random string)
   - `DATABASE_URL` (from Render Postgres **Internal Database URL**)
   - `FIREBASE_CREDENTIALS_JSON` — full Firebase service-account JSON (required for consumer Google/phone/Apple login)
   - `ALLOWED_HOSTS` optional — if unset on Render, `RENDER_EXTERNAL_HOSTNAME` is used when `RENDER` is set (see `config/settings.py`).
   - **Media storage (required):** Render’s disk is wiped on restart/redeploy, so logos and offer images must live in object storage. Set the `AWS_*` vars below (Cloudflare R2 is free-tier friendly).

### Persistent media (Cloudflare R2 / S3)

Without these, uploaded logos work briefly then return **404 Not Found** after the service sleeps or redeploys.

1. Create a [Cloudflare R2](https://dash.cloudflare.com/?to=/:account/r2) bucket (or AWS S3).
2. Enable **public access** (R2: allow public bucket / R2.dev subdomain, or attach a custom domain).
3. Create an R2 API token with Object Read & Write.
4. Set on the Render web service:

| Variable | Example (R2) |
|----------|----------------|
| `AWS_ACCESS_KEY_ID` | R2 access key id |
| `AWS_SECRET_ACCESS_KEY` | R2 secret access key |
| `AWS_STORAGE_BUCKET_NAME` | your-bucket-name |
| `AWS_S3_REGION_NAME` | `auto` |
| `AWS_S3_ENDPOINT_URL` | `https://<accountid>.r2.cloudflarestorage.com` |
| `AWS_S3_CUSTOM_DOMAIN` | `pub-xxxx.r2.dev` (no `https://`) |
| `AWS_QUERYSTRING_AUTH` | `False` |

Redeploy after setting them. Re-upload any logos that already 404’d (those files were lost with the old local disk).

**Build command:** `pip install --retries 15 --timeout 120 -r requirements.txt && python manage.py collectstatic --noinput`  
**Start command:** `python manage.py migrate --noinput && python manage.py ensure_superuser && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`

### Django admin without Shell (Render free tier)

Render’s **Shell** often requires a paid plan. To create an admin user, set these on the **Web Service** → **Environment** (not in git):

| Variable | Example |
|----------|---------|
| `ADMIN_EMAIL` | `you@example.com` |
| `ADMIN_PASSWORD` | A strong one-time password |

Redeploy or restart. Then open `https://<your-service>.onrender.com/admin/` and sign in with that email and password (type the password in the form—it is not filled from env automatically). **Remove `ADMIN_PASSWORD` from the environment afterward** (or change the password in admin) so it is not stored in the dashboard long term.

If that email was already used for a normal signup via the API, the command **promotes** that account to superuser instead of skipping.

Locally you can always run: `python manage.py createsuperuser`

### Optional AI enrichment (admin product URL import)

`POST /api/admin/offers/import-from-url` scrapes product pages for free (JSON-LD / Open Graph). To fill missing title/description/price and suggest category + discount copy, set:

| Variable | Example |
|----------|---------|
| `GEMINI_API_KEY` | API key from [Google AI Studio](https://aistudio.google.com/apikey) (free tier) |
| `GEMINI_MODEL` | `gemini-2.0-flash` (optional; this is the default) |

Without `GEMINI_API_KEY`, the endpoint behaves exactly as before (scrape-only draft). AI never overwrites high-confidence scraped fields and never creates an offer.

### Brand listing / affiliate offer sync

Attach a **deal source** to a business (sale page URL or affiliate CSV/XML feed). For AWIN, use **Toolbox → Create-a-Feed** and paste the gzip CSV feed URL (not a Link Builder deep link). Sync imports new products into a review queue (`is_enabled=false`). After you approve, later syncs refresh prices and **disable** offers that disappeared, 404, or are out of stock. Manual offers are never changed.

Admin: `POST /api/admin/deal-sources/<id>/sync` (Sync now in the admin web app).

Scheduled (optional, no Celery):

```bash
python manage.py sync_deal_sources
python manage.py sync_deal_sources --source-id 12
```

Hook that command to a Render Cron job or GitHub Action when you want nightly refresh. Bulk sync skips Gemini to keep runs cheap.

**Render:** In the web service **Settings**, check **Start Command**. If it was set manually, it overrides `render.yaml` and must be:

`python manage.py migrate --noinput && python manage.py ensure_superuser && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`

Do **not** rely on `seed_test_data` for data — it is a deprecated no-op kept only so older start commands do not fail deploys. Prefer updating Start Command to remove it entirely:

`python manage.py migrate --noinput && python manage.py ensure_superuser && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`

If deploy fails with `Unknown command: 'seed_test_data'`, push the latest code (includes the no-op command) or update Start Command in the Render dashboard, then redeploy.

After deploy, **Logs** should mention `Created superuser`, `Promoted`, or `Synced password`. If you only see Gunicorn lines, the command above is not running.

## GitHub

```bash
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/abdullahaslam0079/aajhee-backend.git
git push -u origin main
```

Use a [Personal Access Token](https://github.com/settings/tokens) or SSH if HTTPS asks for credentials.
