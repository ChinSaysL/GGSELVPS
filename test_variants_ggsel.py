from app.config import get_settings
from app.clients.ggsel import GGSELClient

settings = get_settings()
client = GGSELClient(base_url="https://seller.ggsel.com", api_key=settings.ggsel_api_key, seller_id=settings.ggsel_seller_id)

print("Base price...")
try:
    res = client.set_offer_price("102260467", 10.0, "USD", variants=None)
    print("Base OK:", res)
except Exception as e:
    print("Base fail:", e)

print("With variant...")
try:
    res = client.set_offer_price("102260467", 10.0, "USD", variants=[{"variant_id": 19224615, "type": 2, "rate": 5}])
    print("Variant OK:", res)
except Exception as e:
    print("Variant fail:", e)

