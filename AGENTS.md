# AGENTS.md

## Project

This repository is a VPS middleware for automatic gift-card resale between G-Engine and GGSEL/Digiseller.

## Main goals

- Sync supplier products from G-Engine.
- Create/update GGSEL/Digiseller offers.
- Calculate safe selling prices from internal cost-based rules.
- Disable products that cannot be fulfilled from current G-Engine balance.
- Detect paid marketplace orders.
- Buy codes from G-Engine.
- Deliver codes to buyers.
- Prevent duplicate purchases and duplicate deliveries.

## Tech stack

- Python 3.12
- FastAPI
- SQLAlchemy or SQLModel
- SQLite for v1
- httpx
- pydantic-settings
- pytest
- Docker

## Commands

Use these commands when available:

python -m pytest
python -m ruff check .
python -m mypy app
docker compose build

If a command is not configured yet, add the missing config or explain what is missing.

## Security rules

- Never hardcode API keys.
- Never print API keys in logs.
- Never expose API keys to frontend code.
- Mask gift-card codes in logs.
- Store full delivered codes only where required for order recovery.
- Use idempotency for every paid marketplace order.
- Never buy twice for the same marketplace order ID.

## Business rules

- G-Engine balance is the fulfillment limit.
- Products must be disabled if the current usable balance cannot cover the supplier cost plus safety buffer.
- Default safety balance is 15%.
- Default platform fee assumption is 6%, configurable by env var.
- Default FX buffer is 3%.
- Default risk buffer is 2%.
- Minimum profit must be enforced.
- Expensive products require extra balance safety.

## Pricing rule for v1

Do not use competitor prices in v1.

Prices must be calculated only from:
- supplier cost
- platform fee
- FX buffer
- risk buffer
- markup tier
- minimum profit

The system should not scrape competitors, undercut competitors, or adjust prices based on marketplace listings.

The goal is safe, reasonable markup, not lowest price.

## Done means

A task is complete only when:

- Code is implemented.
- Tests are added or updated.
- Existing tests pass.
- README or .env.example is updated if config changed.
- Errors are handled clearly.
- Logs do not leak secrets.
