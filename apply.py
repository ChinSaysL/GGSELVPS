import re

content = open("tests/test_clients_stage4.py", "r", encoding="utf-8").read()

content = content.replace('''    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            captured["price_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"status": "ok"})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})''', '''    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
            captured["price_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"status": "ok"})
        if request.url.path.endswith("/api_sellers/api/products/5850465/data"):
            return httpx.Response(status_code=200, json={"product": {"id": 5850465, "options": [{"id": 777, "variants": [{"variant_id": 19472020}]}]}})
        if request.url.path.endswith("/api_sellers/api/products/options/777/variants/19472020"):
            captured["variant_json"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"retval": 0})
        return httpx.Response(status_code=404, json={"error": "unexpected path"})''')

content = content.replace('''            if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
                captured["price_query"] = dict(request.url.params)
                captured["price_json"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(status_code=200, json={"status": "ok"})
            return httpx.Response(status_code=404, json={"error": "unexpected path"})''', '''            if request.url.path.endswith("/api_sellers/api/product/edit/prices"):
                captured["price_query"] = dict(request.url.params)
                captured["price_json"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(status_code=200, json={"status": "ok"})
            if request.url.path.endswith("/api_sellers/api/products/102249445/data"):
                return httpx.Response(status_code=200, json={"product": {"id": 102249445, "options": [{"id": 888, "variants": [{"variant_id": 18944509}]}]}})
            if request.url.path.endswith("/api_sellers/api/products/options/888/variants/18944509"):
                captured["variant_json"] = json.loads(request.content.decode("utf-8"))
                return httpx.Response(status_code=200, json={"retval": 0})
            return httpx.Response(status_code=404, json={"error": "unexpected path"})''')

content = re.sub(r'assert result == \{"status": "ok"\}\s+assert captured\["price_json"\] == \[\s+\{\s+"product_id": 5850465,\s+"price": 7\.15,\s+"variants": \[\s+\{\s+"variant_id": 19472020,\s+"value": 19472020,\s+"type": 2,\s+"rate": 6\.6,\s+"modify": 1,\s+"modify_type": "priceplus",\s+"modify_value": "6\.6",\s+\}\s+\],\s+\}\s+\]', 
    'assert result.get("status") == "ok"\n    assert captured["price_json"] == [{"product_id": 5850465, "price": 7.15}]\n    assert captured["variant_json"]["rate"] == 6.6\n    assert captured["variant_json"]["modify_value"] == "6.6"', content)

content = re.sub(r'assert captured\["price_json"\] == \[\s+\{\s+"product_id": 102249445,\s+"price": 610\.55,\s+"variants": \[\s+\{\s+"variant_id": 18944509,\s+"value": 18944509,\s+"type": 2,\s+"rate": 0\.0,\s+"modify": 1,\s+"modify_type": "priceplus",\s+"modify_value": "0\.0",\s+\}\s+\],\s+\}\s+\]',
    'assert captured["price_json"] == [{"product_id": 102249445, "price": 610.55}]\n    assert captured["variant_json"]["rate"] == 0.0\n    assert captured["variant_json"]["modify_value"] == "0.0"', content)

open("tests/test_clients_stage4.py", "w", encoding="utf-8").write(content)
