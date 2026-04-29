from app.config import get_settings
from app.clients.ggsel import GGSELClient
import logging
logging.basicConfig(level=logging.DEBUG)

settings = get_settings()
client = GGSELClient(base_url="https://seller.ggsel.com", api_key=settings.ggsel_api_key, seller_id=settings.ggsel_seller_id)

print("Base price...")
try:
    res = client.set_offer_price("102260467", 10.0, "USD", variants=None)
    print("Base OK:", res)
except Exception as e:
    import traceback
    traceback.print_exc()

