import httpx

def test_payload(payload):
    client = httpx.Client(verify=False)
    resp = client.post(
        "https://seller.ggsel.com/api_sellers/api/product/edit/prices?token=invalid_token",
        json=payload
    )
    print(f"Status: {resp.status_code}")
    if resp.status_code == 500:
        print(resp.text[:200])
        print("-------------")
    else:
        print(resp.text)
        print("-------------")

print("No variants")
test_payload([{"product_id": 102260467, "price": 1.50}])

print("With variants Digiseller style")
test_payload([{"product_id": 102260467, "price": 1.50, "variants": [{"variant_id": 19224613, "rate": 0.5, "type": 2}]}])

print("With variants GGSEL style")
test_payload([{"product_id": 102260467, "price": 1.50, "variants": [{"value": 19224613, "modify_type": "priceplus", "modify_value": "0.5"}]}])

