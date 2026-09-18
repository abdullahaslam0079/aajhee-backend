# Aajhee infra cutover checklist

Complete these dashboard steps after the code rename lands. Product is not live — prefer a clean cutover.

## 1. Firebase (new project `aajhee`)

1. Create project named `aajhee` at https://console.firebase.google.com/
2. Enable Auth providers used today (Phone, Google, Apple as needed).
3. Add apps:
   - Android package: `com.aajhee.app`
   - iOS bundle: `com.aajhee.app`
   - Web app for `aajhee-web`
4. Download and place:
   - `google-services.json` → consumer Flutter `android/app/`
   - `GoogleService-Info.plist` → consumer Flutter `ios/Runner/`
   - Web config → `aajhee-web` `.env.local` as `NEXT_PUBLIC_FIREBASE_*`
   - Service account JSON → Render env `FIREBASE_CREDENTIALS_JSON` (minified one line) and local `secrets/firebase-adminsdk.json`
5. Add Android SHA-1 / SHA-256 for debug/release keystores.
6. Restrict Google Maps API keys to `com.aajhee.app` / `com.aajhee.business` / `com.aajhee.admin` and `aajhee.com`.

## 2. Render

1. Deploy from renamed repo using updated `render.yaml` (`aajhee-backend` / `aajhee-db`).
2. Set env:
   - `ALLOWED_HOSTS=api.aajhee.com,aajhee-backend.onrender.com`
   - `CORS_ALLOWED_ORIGINS=https://aajhee.com,https://www.aajhee.com,https://admin.aajhee.com`
   - `FIREBASE_CREDENTIALS_JSON=...`
   - `AWS_*` (R2/S3)
   - `DATABASE_URL` (from new DB)
3. Custom domain: `api.aajhee.com`
4. After green checks, delete old `goluto-backend` service + `goluto-db`.

## 3. GoDaddy DNS (`aajhee.com`)

| Type | Name | Value |
|------|------|--------|
| A/ALIAS/CNAME | `@` | web host (Vercel/Render static) — remove Parked |
| CNAME | `www` | web host |
| CNAME | `api` | Render `aajhee-backend` target |
| CNAME | `admin` | admin web host (Vercel) |

Add any TXT/CNAME verification records Render/Vercel/Firebase request.

## 4. GitHub

Rename remotes:

- `goluto` → `aajhee`
- `GoLuto-backend` → `aajhee-backend`
- `goluto-web` → `aajhee-web`
- `goluto_business` → `aajhee_business`
- create/rename `aajhee-admin` for admin web, `aajhee_admin` for admin Flutter if needed

## 5. Retire old Firebase

After auth works on `aajhee`, stop using project `goluto-c5020`.

## 6. Smoke test (after infra is live)

1. `https://api.aajhee.com/api/docs/` opens
2. Consumer app phone/social login (new Firebase) → API JWT works
3. `aajhee-web` login + CORS
4. `admin.aajhee.com` / admin web login
5. Offer QR uses `AAJHEE:` prefix; scanner still accepts legacy `GOLUTO:`
6. Image upload to R2
7. Maps load with updated key restrictions
8. Delete old Render `goluto-backend` and stop using Firebase `goluto-c5020`
