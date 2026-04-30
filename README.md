# Gift-Card VPS Middleware (G-Engine <-> GGSEL/Digiseller)

Backend service for gift-card resale automation with safe cost-based pricing, balance-aware stock control, and idempotent order handling.

This repository currently includes Stage 1-5 foundations:
- FastAPI skeleton
- Config and DB setup
- SQLAlchemy models
- Structured logging
- Pricing engine (no competitor data)
- Stock rule engine
- API client layer with confirmed endpoint wiring where available
- Product sync service that imports/upserts G-Engine supplier products only
- Manual marketplace offer linking via `/offers/manual-link` and `/mappings/import`
- Cost-based price preview/sync for already-linked offers
- Stock sync with balance-aware decisions; Digiseller and GGSEL variant-level visibility updates for linked option mappings
- Paid-order sync that buys once from G-Engine and delivers through Digiseller/GGSEL order chat
- Digiseller unique-code verification endpoint for delayed buyer-confirmed delivery
- Tests for pricing, stock rules, product import, client behavior, order delivery, and idempotency decisions
- Docker + docker-compose deployment baseline

Important: this app does not auto-create marketplace listings. Create GGSEL/Digiseller offers manually in the marketplace, then link them to imported supplier products inside this app.

API integrations continue to evolve as endpoint mappings are confirmed from official docs. Listing creation stays disabled; fulfillment works only for manually linked offers.

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
- Digiseller async task polling can be tuned with:
  - `DIGISELLER_TASK_POLL_TIMEOUT_SECONDS` (default `120`)
  - `DIGISELLER_TASK_POLL_INTERVAL_SECONDS` (default `1.0`)

For RUB pricing, the app resolves USD/RUB using this priority:
- `MANUAL_USD_RUB_RATE` when explicitly set
- automatic daily fetch (cached) when `AUTO_USD_RUB_RATE=true`

Default automatic source is `USD_RUB_RATE_URL=https://www.cbr-xml-daily.ru/daily_json.js`.
Cache path is controlled by `USD_RUB_RATE_CACHE_PATH`.

There are intentionally no listing template, category, or publish-switch settings. Marketplace listings are created manually outside this app.

For automatic price updates, set `SCHEDULER_ENABLED=true` and `PRICE_SYNC_INTERVAL_MINUTES=5`.
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
python manage.py sync-stock
python manage.py sync-orders
python manage.py manual-retries
python manage.py offer-halts
python manage.py digiseller-code
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
- manual fallback for unmatched option values so one marketplace post can map each radio/drop-down value to different G-Engine rows
- entering/confirming marketplace price and currency (prefilled when fetched)
- entering marketplace commission percent (manual override per mapping)
- for GGSEL: entering payment-system commission percent (defaults are configurable in `.env`)
- auto-detecting required buyer text fields (such as Steam friend code), with optional manual override

`python manage.py auto-link` walks through:
- choosing marketplace (`digiseller` or `ggsel`)
- selecting one G-Engine product link/id (and importing rows if needed)
- loading marketplace offers (by explicit IDs or by auto-discovery keyword)
- matching denominations/signatures (for example `250 TRY`, `500 TRY`)
- creating/updating local links in one batch

For marketplace pages with option values like `250 TRY` and `500 TRY`, keep those option texts aligned with G-Engine denominations and `manual-link` can map the whole page in one run.

For Digiseller recharge offers, selected radio/drop-down option values (for example currency or amount) are passed into G-Engine recharge params when keys match (such as `Currency`, `Amount`, `Quantity`).

After `python manage.py manual-link`, run the offered live stock sync prompt if the listing is already public. Price sync only updates prices; stock sync is what hides/disables denominations when G-Engine balance is too low.

`python manage.py price-preview` refreshes linked G-Engine costs when possible and shows the recommended cost-based price. `python manage.py sync-prices` applies those prices to linked marketplaces.
- Digiseller and GGSEL use the same simplified formula:
  - `base = supplier_cost` (USD) or `supplier_cost * usd_rub_rate` (RUB)
  - `price = base + (base * markup_percent) + (base * marketplace_commission_percent)`
  - resulting price is rounded to 3 decimals
- Multi-denomination offers with linked variant IDs are updated in one bulk price call using variant modifiers.
- If G-Engine is unavailable during supplier cost refresh, price sync skips those affected offers instead of writing prices from stale refreshed data.

For GGSEL live writes, set `GGSEL_API_KEY` to the seller API key/token accepted by GGSEL write endpoints. If you have the long-lived seller API key plus seller ID, also set `GGSEL_SELLER_ID` so the app can call `/api_sellers/api/apilogin` and refresh the short-lived token automatically. A browser/session JWT may work for public item lookup but can still return `401` for price updates.

`POST /sync/stock` now:
- fetches G-Engine balance and writes a `balance_snapshots` row
- evaluates each linked mapping with stock rules (supplier availability, supplier stock, usable balance)
- for Digiseller mappings with `marketplace_variant_id`, toggles each variant visibility individually, moves the default selection to the first fulfillable denomination, and disables the parent product when no linked denomination can be fulfilled
- for GGSEL mappings with `marketplace_variant_id`, uses the same option-variant visibility flow so one unavailable denomination does not disable the whole product
- enforces whole-offer enable/disable on every stock sync instead of trusting the local cached `is_enabled` flag
- whole-product GGSEL enable/disable remains disabled until GGSEL exposes or confirms a stock/status endpoint; stock sync skips that parent toggle after variant updates

Use `python manage.py offer-halts` for products you no longer want to sell. This sets a local
`manually_disabled` flag that stock sync treats as an absolute stop, so the app will keep the
marketplace product disabled instead of re-enabling it when G-Engine balance/stock looks healthy.

Supported G-Engine link shapes include:
- `https://b2b.g-engine.net/en/shop/5/playstation-store-wallet-turkey`
- `https://b2b.g-engine.net/ru/recharge/1/steam`
- `https://b2b.g-engine.net/en/gifts/4451080/vituber-melody`

Recharge links are imported from the G-Engine `/recharge/services` catalogue. Fixed recharge
denominations are imported as separate local rows. Variable/unfixed recharge services, such as
Steam Wallet top-ups, can also be linked in two modes during `manual-link`:
- fixed package mode: every order sends the same G-Engine `Quantity`
- marketplace-quantity mode: the buyer's marketplace quantity is sent as G-Engine `Quantity`

Gift links are imported from `/gifts/apps/{app_id}` as one local row per package and country.
The local supplier ID is shaped like `gift:{app_id}:{package_id}:{region}` and the row
denomination is the country code, so marketplace options such as `RU`, `US`, or `CIS` can be
linked to the matching G-Engine gift country.

For unit listings like "1 Steam RUB unit", use marketplace-quantity mode with `G-Engine Quantity
per 1 marketplace unit = 1` and supplier cost per 1 marketplace unit. Recharge orders use
`/recharge/orders/{service_id}`, pay verified orders through `/recharge/orders/{order_id}/pay`, and
treat `shipped` as fulfilled without a gift-card code.

## Delivery flow

`POST /sync/orders` polls configured marketplaces for paid sales. For each linked paid order it:
- matches the marketplace product and option/variant to a local `ProductMapping`
- polls incrementally from the last successful order-sync run, with a small overlap, instead of re-scanning all historical marketplace sales every time
- creates one G-Engine order for the mapped supplier row, including buyer input fields when the marketplace order provides them
- immediately pays G-Engine shop orders through `/shop/orders/{order_id}/pay`; if the pay response is still processing, later order sync runs poll `/shop/orders/{order_id}` until codes are available
- creates G-Engine gift orders through `/gifts/orders` using the linked country/region row and the buyer's Steam quick-invite URL, then polls `/gifts/orders/{order_id}` until delivery is complete
- stores the supplier order ID before delivery so retries do not buy the same marketplace order twice
- delivers returned codes through the marketplace order correspondence and stores only masked codes locally

If G-Engine is temporarily unavailable while polling an already-created supplier order, order sync
sends one buyer message saying the provider is currently down and the order will be delivered when
it is active again. The local order stays `supplier_order_created`, so later order-sync runs continue
delivery without creating a duplicate purchase. If G-Engine fails while the supplier purchase is
being created, the order moves to `needs_manual_review` because the app cannot prove whether
G-Engine accepted the purchase.

Digiseller paid sales are detected during `POST /sync/orders`, but the service does not buy from
G-Engine until the buyer's Digiseller unique code is verified. Do not point Digiseller buyers at
the VPS as an automatic payment or delivery redirect; the service only needs seller API polling
and the order chat. When a paid Digiseller sale has no code yet, order sync parks the local order
as pending and asks the buyer to paste the unique code in chat. Later sync runs read recent buyer
chat messages, verify any code-like token with Digiseller, then create the G-Engine order and
deliver it. `python manage.py digiseller-code` is still available as a manual rescue command.

Supplier terminal states such as `canceled`, `cancelled`, or `expired` stay in manual review to
avoid duplicate purchases. Use `python manage.py manual-retries` to list those orders, queue a
specific local order ID for retry, and optionally run live order sync after the explicit
confirmation.

Order polling scale knobs:
- `ORDER_SYNC_INITIAL_LOOKBACK_HOURS` controls the first/recovery polling window when no successful order-sync checkpoint exists.
- `ORDER_SYNC_OVERLAP_MINUTES` adds overlap after the last successful checkpoint so late marketplace writes are not missed.
- `ORDER_SYNC_FUTURE_WINDOW_MINUTES` extends the poll end time to tolerate marketplace/server clock skew.
- `DIGISELLER_SALES_PAGE_SIZE` and `DIGISELLER_SALES_MAX_PAGES` cap Digiseller sales pagination per run.

GGSEL delivery happens during `POST /sync/orders` as soon as a linked paid sale is detected.
There is no unique-code gate for GGSEL.

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
12. Trigger manual sync endpoints after API keys and linked offers are configured.

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
- `POST /offers/{id}/halt`
- `POST /offers/{id}/resume`
- `POST /mappings/import`
- `POST /mappings/{id}/enable`
- `POST /mappings/{id}/disable`

## Notes

- No competitor pricing data is used.
- No competitor scraping is implemented.
- Price sync uses a simplified markup model from supplier cost:
  - `base = supplier_cost` (USD) or `supplier_cost * usd_rub_rate` (RUB)
  - `price = base + (base * markup_percent) + (base * marketplace_commission_percent)`
  - final listing price is rounded to 3 decimals
- No browser automation is used.
- No automatic marketplace listing creation is implemented.
- Automatic listing creation is disabled in client code.
- GGSEL/Digiseller offers must be created manually, then linked to supplier products.
- Automatic thumbnail transfer from G-Engine is not guaranteed by current public API contracts; set preview images manually when needed.
