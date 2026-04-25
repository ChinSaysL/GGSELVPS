# Gift-Card VPS Middleware (G-Engine <-> GGSEL/Digiseller)

Backend service for automatic gift-card resale with safe cost-based pricing, balance-aware stock control, and idempotent order handling.

This repository currently includes Stage 1-5 foundations:
- FastAPI skeleton
- Config and DB setup
- SQLAlchemy models
- Structured logging
- Pricing engine (no competitor data)
- Stock rule engine
- API client layer with confirmed endpoint wiring where available, and explicit TODO `NotImplementedError` for unresolved operations
- Product sync service with supplier product upsert, marketplace offer create/update flow, and mapping auto-linking
- Tests for pricing, stock rules, and idempotency decisions
- Docker + docker-compose deployment baseline

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

Compatibility aliases are also supported:
- `GEngineToken`
- `GGSELToken`
- `DigisellerToken`

If `DEFAULT_CURRENCY=RUB`, set `MANUAL_USD_RUB_RATE` before running price sync logic.

For Digiseller auto-listing:
- Set `DIGISELLER_CATEGORY_ID` (required for product creation).
- Optional: set `DIGISELLER_CATEGORY_OWNER` (default `3` for GGSEL marketplace category).
- Optional: set `DIGISELLER_CONTENT_TYPE` (default `Text`).

Bilingual listing templates are configurable:
- `LISTING_TITLE_TEMPLATE_EN`
- `LISTING_TITLE_TEMPLATE_RU`
- `LISTING_DESCRIPTION_TEMPLATE_EN`
- `LISTING_DESCRIPTION_TEMPLATE_RU`

Supported template placeholders:
- `{supplier_title}`
- `{denomination}`
- `{currency}`
- `{region}`
- `{category}`
- `{delivery_type}`
- `{delivery_type_ru}`

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
python -m pytest
python -m ruff check .
python -m mypy app
```

5. Run API:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
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
6. Set `MANUAL_USD_RUB_RATE` if using RUB listings.
7. Start service:

```bash
docker compose up -d
```

8. Check logs:

```bash
docker compose logs -f
```

9. Add VPS public IP to G-Engine production whitelist.
10. Trigger manual sync endpoints after API clients are implemented.

## Safe first-run procedure

1. Run service with API clients in dry-run mode if implemented.
2. Sync G-Engine products.
3. Check calculated prices.
4. Create only 1-3 test offers first.
5. Test one cheap product purchase manually.
6. Confirm delivery works.
7. Enable stock sync.
8. Enable order sync.
9. Gradually enable more products.

## Current admin endpoints

- `GET /health`
- `GET /balance`
- `GET /products`
- `GET /offers`
- `POST /sync/products`
- `POST /sync/prices`
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
- No browser automation is used.
- Digiseller create/update listing flows are implemented for base product sync.
- GGSEL listing creation/update endpoints remain unresolved in public docs and are still explicit TODOs.
- Automatic thumbnail transfer from G-Engine is not guaranteed by current public API contracts; set preview images manually when needed.
