from app.config import get_settings
from app.clients.ggsel import GGSELClient

settings = get_settings()
client = GGSELClient(base_url="https://seller.ggsel.com", api_key=settings.ggsel_api_key, seller_id=settings.ggsel_seller_id)

try:
    print("Testing string price...")
    token = client._get_access_token()
    res = client._request_json("POST", "/api_sellers/api/product/edit/prices", params={"token": token}, json_body=[{"product_id": 102260467, "price": "10.00"}])
    print("String OK:", res)
except Exception as e:
    import traceback
    traceback.print_exc()

try:
    print("Testing wrapper object...")
    res = client._request_json("POST", "/api_sellers/api/product/edit/prices", params={"token": token}, json_body={"products": [{"product_id": 102260467, "price": 10.0}]})
    print("Wrapper OK:", res)
except Exception as e:
    import traceback
    traceback.print_exc()

