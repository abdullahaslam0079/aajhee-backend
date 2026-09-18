# Aajhee marketing site

Static landing page for **https://aajhee.com**.

Preview locally:

```bash
cd website
python3 -m http.server 4173
```

Then open http://127.0.0.1:4173

## Deploy on Render (Static Site)

1. Push this repo.
2. Render → **New** → **Static Site** → same GitHub repo (`Aajhee-backend`).
3. Settings:
   - **Root Directory:** `website`
   - **Build Command:** *(leave empty)*
   - **Publish Directory:** `.`
4. After deploy, **Settings → Custom Domains** → add `aajhee.com`.
5. At GoDaddy DNS add:

| Type | Name | Value |
|------|------|--------|
| A or ALIAS | `@` | follow Render’s root-domain instructions |
| CNAME | `www` | optional; or forward `www` to `aajhee.com` in GoDaddy |

Hobby includes 2 custom domains. `api.aajhee.com` already uses one, so prefer attaching only `aajhee.com` here and forwarding `www` at GoDaddy.

Fill in your name and address on `impressum.html` before going public (required in Germany).
