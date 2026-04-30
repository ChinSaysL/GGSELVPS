
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

open("tests/test_clients_stage4.py", "w", encoding="utf-8").write(content)
