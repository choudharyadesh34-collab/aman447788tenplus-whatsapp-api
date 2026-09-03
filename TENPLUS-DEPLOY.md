# TenPlus WhatsApp API deployment

This is a branded OpenWA deployment. It is **not Meta's official Cloud API**. Use a dedicated
secondary number that the business can afford to lose, and message only recipients who opted in.

## Server requirements

- Ubuntu 24.04 VPS, 2 vCPU, 4 GB RAM and 40 GB storage minimum
- Docker Engine with the Compose plugin
- A domain/subdomain such as `wa-api.yourdomain.com`
- HTTPS reverse proxy (Caddy, Nginx or Cloudflare Tunnel)

## Configure

```bash
cp .env.tenplus.example .env
openssl rand -hex 32
openssl rand -hex 32
```

Put different generated values in `API_MASTER_KEY` and `API_KEY_PEPPER`, replace all `example.com`
URLs with the real domain, and keep `.env` private. Changing the pepper later invalidates existing
API keys. Never commit session data, API keys, or webhook secrets.

## Build and start

```bash
docker compose build openwa-api
docker compose up -d openwa-api docker-proxy
docker compose ps
curl http://127.0.0.1:2785/api/health
```

Expose the service only through HTTPS. After opening the dashboard, sign in with `API_MASTER_KEY`, create
one session using the `whatsapp-web.js` engine, and scan its QR code from the dedicated phone.

## API smoke test

```bash
curl -H "X-API-Key: YOUR_API_KEY" https://wa-api.yourdomain.com/api/sessions
```

Interactive endpoint documentation is available at `/api/docs` when Swagger is enabled.

## Operations

```bash
docker compose logs --tail=100 openwa-api
docker compose restart openwa-api
docker compose down
```

Back up the Docker volume `openwa_openwa-data`. The upstream `LICENSE` and attribution must remain
in redistributed copies.
