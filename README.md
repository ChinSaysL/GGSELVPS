# Gift-Card VPS Middleware (G-Engine <-> GGSEL/Digiseller)

Backend service for gift-card resale automation with safe cost-based pricing, balance-aware stock control, and idempotent order handling.

This repository currently includes Stage 1-5 foundations:
- FastAPI skeleton
- Config and DB setup
- SQLAlchemy models
- Structured logging
- Pricing engine (no competitor data)
- Stock rule engine
- API client layer with confirmed endpoint wiring where available, and explicit TODO `NotImplementedError` for unresolved operations
- Product sync service that imports/upserts G-Engine supplier products only
- Manual marketplace offer linking via `/offers/manual-link` and `/mappings/import`
- Cost-based price preview/sync for already-linked offers
- Tests for pricing, stock rules, product import, client behavior, and idempotency decisions
- Docker + docker-compose deployment baseline

Important: this app does not auto-create marketplace listings. Create GGSEL/Digiseller offers manually in the marketplace, then link them to imported supplier products inside this app.

API integrations are partially implemented and continue to evolve as endpoint mappings are confirmed from official docs.

## Tech

- Python 3.12
- FastAPI
- SQLAlchemy
- SQLite (v1)
- httpx
- pydantic-settings
- APScheduler
- pytest
- Docker

## Environment variables

Copy `.env.example` to `.env` and fill values.

Canonical API key variables:
- `GENGINE_API_KEY`
- `GGSEL_API_KEY`
- `DIGISELLER_API_KEY`
- `DIGISELLER_SELLER_ID` (recommended for Digiseller live edits)

Compatibility aliases are also supported:
- `GEngineToken`
- `GGSELToken`
- `DigisellerToken`
- `DigisellerSellerId`

Digiseller auth note:
- If `DIGISELLER_SELLER_ID` is set, the app auto-fetches/refreshes short-lived access
tokens via `POST /api/apilogin` using `DIGISELLER_API_KEY` (seller API key).
- If `DIGISELLER_SELLER_ID` is empty, `DIGISELLER_API_KEY` is treated as a direct token.
Direct tokens can expire and then price sync may return `401`.

For RUB pricing, the app resolves USD/RUB using this priority:
- `MANUAL_USD_RUB_RATE` when explicitly set
- automatic daily fetch (cached) when `AUTO_USD_RUB_RATE=true`

Default automatic source is `USD_RUB_RATE_URL=https://www.cbr-xml-daily.ru/daily_json.js`.
Cache path is controlled by `USD_RUB_RATE_CACHE_PATH`.

There are intentionally no listing template, category, or publish-switch settings. Marketplace listings are created manually outside this app.

For automatic price updates, set `SCHEDULER_ENABLED=true` and `PRICE_SYNC_INTERVAL_MINUTES=60`.
`python manage.py sync-prices` is a manual immediate trigger, while the scheduler handles recurring runs.

## Management menu

The repo includes a small helper menu:

```bash
python manage.py
```

Useful direct commands:

```bash
python manage.py check-config
python manage.py init-db
python manage.py check
python manage.py run-api
python manage.py sync-products
python manage.py auto-link
python manage.py price-preview
python manage.py sync-prices
python manage.py list-products
python manage.py manual-link
python manage.py manual-link-example
```

`python manage.py manual-link` walks through:
- choosing `digiseller` or `ggsel`
- pasting the G-Engine product link or product ID
- entering the marketplace offer/product ID and attempting to fetch marketplace metadata
- auto-mapping marketplace option values (for example `250 TRY`, `500 TRY`) to imported G-Engine rows by exact denomination/signature
- showing unmatched/ambiguous option values that were skipped
- entering/confirming marketplace price and currency (prefilled when fetched)
- auto-detecting required buyer text fields (such as Steam friend code), with optional manual override

`python manage.py auto-link` walks through:
- choosing marketplace (`digiseller` or `ggsel`)
- selecting one G-Engine product link/id (and importing rows if needed)
- loading marketplace offers (by explicit IDs or by auto-discovery keyword)
- matching denominations/signatures (for example `250 TRY`, `500 TRY`)
- creating/updating local links in one batch

For marketplace pages with option values like `250 TRY` and `500 TRY`, keep those option texts aligned with G-Engine denominations and `manual-link` can map the whole page in one run.

`python manage.py price-preview` refreshes linked G-Engine costs when possible and shows the recommended cost-based price. `python manage.py sync-prices` applies those prices to supported linked marketplaces. Digiseller price updates support USD/RUB product prices and, when variant IDs are linked, per-variant price modifiers. GGSEL price updates remain disabled until the exact update payload schema is confirmed.

Supported G-Engine link shapes include:
- `https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey`
- `https://b2b.g-engine.net/ru/recharge/1/steam`
- `https://b2b.g-engine.net/en/gifts/4451080/vituber-melody`

## Local setup (test PC)

1. Install Python 3.12.
2. Create virtual environment and install dependencies:

```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

3. Create env file:

```bash
cp .env.example .env
```

4. Run checks:

```bash
python manage.py check
```

5. Run API:

```bash
python manage.py run-api
```

6. Or run the local admin menu:

```bash
python manage.py
```

## Docker run

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

## Ubuntu VPS deployment

1. Create Ubuntu VPS (22.04+ recommended).
2. Install Docker Engine + Compose plugin:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

3. Clone repository on VPS.
4. Create `.env` from `.env.example`.
5. Add API keys.
6. For RUB prices, either set `MANUAL_USD_RUB_RATE` or keep automatic FX enabled (`AUTO_USD_RUB_RATE=true`).
7. Run validation before starting the service:

```bash
python manage.py check-config
python manage.py init-db
python manage.py check
```

8. Start service:

```bash
docker compose up -d
```

9. Check logs:

```bash
docker compose logs -f
```

10. Add VPS public IP to G-Engine production whitelist.
11. Run `python manage.py price-preview` before any real price update.
12. Trigger manual sync endpoints after API clients are implemented.

## Safe first-run procedure

1. Sync G-Engine products.
2. Check imported products and calculated prices with `python manage.py price-preview`.
3. Create a small number of marketplace offers manually in GGSEL/Digiseller.
4. Link those offers with `/offers/manual-link`.
5. Test one cheap product purchase manually.
6. Confirm delivery works.
7. Enable stock sync.
8. Enable order sync.
9. Gradually link more products.

## Current admin endpoints

- `GET /health`
- `GET /balance`
- `GET /products`
- `GET /offers`
- `POST /offers/manual-link`
- `POST /sync/products`
- `POST /sync/prices`
- `POST /sync/prices?dry_run=true`
- `POST /sync/stock`
- `POST /sync/orders`
- `GET /orders`
- `POST /orders/{id}/retry`
- `POST /mappings/import`
- `POST /mappings/{id}/enable`
- `POST /mappings/{id}/disable`

## Notes

- No competitor pricing data is used.
- No competitor scraping is implemented.
- Price sync uses supplier cost, fee/buffer settings, markup tiers, and minimum profit only.
- No browser automation is used.
- No automatic marketplace listing creation is implemented.
- Base listing edits are intentionally disabled in client code.
- GGSEL/Digiseller offers must be created manually, then linked to supplier products.
- Automatic thumbnail transfer from G-Engine is not guaranteed by current public API contracts; set preview images manually when needed.
