"""
Verify that a recharge product's G-Engine param keys (account, region, etc.)
will be correctly resolved from a simulated buyer_inputs dict.

Usage (inside the container):
    python check_recharge_params.py <offer_id> [marketplace]

Examples:
    python check_recharge_params.py 12345678
    python check_recharge_params.py 12345678 ggsel

The script:
  1. Looks up the ProductMapping for the given marketplace offer ID.
  2. Reads the supplier product's raw_payload_json to find recharge_param_keys.
  3. Simulates buyer_inputs using the param keys as-is (what the marketplace sends).
  4. Runs _build_recharge_params to confirm every param resolves without error.
  5. Prints the final params that would be sent to G-Engine.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal

from sqlalchemy.orm import Session

from app.clients.gengine import GEngineClient
from app.db import SessionLocal
from app.models import MarketplaceOffer, ProductMapping, SupplierProduct
from app.services.order_sync import _normalize_input_key


def _get_session() -> Session:
    return SessionLocal()


def _find_mapping(session: Session, offer_id: str, marketplace: str) -> ProductMapping | None:
    return (
        session.query(ProductMapping)
        .join(ProductMapping.supplier_product)
        .join(ProductMapping.marketplace_offer)
        .filter(
            MarketplaceOffer.marketplace_offer_id == offer_id,
            MarketplaceOffer.marketplace == marketplace,
        )
        .first()
    )


def _check(offer_id: str, marketplace: str) -> None:
    session = _get_session()
    try:
        mapping = _find_mapping(session, offer_id, marketplace)
        if mapping is None:
            print(f"[ERROR] No ProductMapping found for {marketplace} offer {offer_id!r}.")
            print("        Make sure the offer is linked (manage.py -> auto-link or manual-link).")
            sys.exit(1)

        product: SupplierProduct = mapping.supplier_product
        print(f"Supplier product : {product.supplier_product_id}")
        print(f"Title            : {product.title}")

        if not str(product.supplier_product_id).startswith("recharge:"):
            print("\n[INFO] This is not a recharge product — no param mapping to verify.")
            sys.exit(0)

        raw_payload: dict = {}
        if product.raw_payload_json:
            try:
                raw_payload = json.loads(product.raw_payload_json)
            except json.JSONDecodeError:
                print("[WARN] raw_payload_json is not valid JSON.")

        param_keys = GEngineClient._payload_param_keys(raw_payload)
        print(f"\nG-Engine param keys in raw_payload: {param_keys}")

        # Simulate what _extract_buyer_inputs produces for each param key.
        # The marketplace sends labels like "Account (Integer)", "Region (Integer)".
        # We test the worst-case: the label IS the param key (label == param name).
        print("\n--- Mapping simulation ---")
        simulated_buyer_inputs: dict[str, str] = {}
        for i, key in enumerate(param_keys, start=1):
            normalized = _normalize_input_key(key, f"input_{i}")
            print(f"  Param key {key!r:30s} -> _normalize_input_key -> {normalized!r}")
            if normalized in ("account", "region"):
                simulated_buyer_inputs[normalized] = "TEST_VALUE"
            elif normalized == "quantity":
                simulated_buyer_inputs[normalized] = "1"

        # Also add quantity so _build_recharge_params can fill it from default_params.
        print("\n--- _build_recharge_params dry-run ---")
        print(f"  buyer_inputs : {simulated_buyer_inputs}")
        try:
            params = GEngineClient._build_recharge_params(
                product_payload=raw_payload,
                buyer_inputs=simulated_buyer_inputs,
                marketplace_quantity=1,
            )
            print("\n[OK] All params resolved successfully.")
            print("     Params that would be sent to G-Engine:")
            for p in params:
                print(f"       {p['param_key']:20s} = {p['param_value']}")
        except Exception as exc:
            print(f"\n[FAIL] _build_recharge_params raised: {exc}")
            print(
                "       This means a buyer order for this product would fail at delivery time."
            )
            sys.exit(1)

        # Check account key variant — simulate what a real Digiseller/GGSEL order sends.
        print("\n--- Real-label simulation (e.g. 'Account (Integer)', 'Region (Integer)') ---")
        real_label_inputs: dict[str, str] = {}
        for i, key in enumerate(param_keys, start=1):
            label = f"{key} (Integer)"
            normalized = _normalize_input_key(label, f"input_{i}")
            print(f"  Label {label!r:35s} -> {normalized!r}")
            real_label_inputs[normalized] = "TEST_VALUE"

        try:
            params2 = GEngineClient._build_recharge_params(
                product_payload=raw_payload,
                buyer_inputs=real_label_inputs,
                marketplace_quantity=1,
            )
            print("\n[OK] All params resolved with '(Integer)'-suffixed labels too.")
        except Exception as exc:
            print(f"\n[FAIL] Failed with real marketplace labels: {exc}")
            sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    offer_id = sys.argv[1]
    marketplace = sys.argv[2].lower() if len(sys.argv) >= 3 else "digiseller"
    _check(offer_id, marketplace)


if __name__ == "__main__":
    main()
