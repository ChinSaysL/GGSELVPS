import socket
import ssl
import re
context = ssl.create_default_context()
conn = socket.socket(socket.AF_INET)
conn.settimeout(10.0)
conn = context.wrap_socket(conn, server_hostname="seller.ggsel.com")
conn.connect(("seller.ggsel.com", 443))
conn.sendall(b"GET /docs/en/seller-api-v-1 HTTP/1.1\r\nHost: seller.ggsel.com\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n")
resp = b""
while True:
    data = conn.recv(8192)
    if not data: break
    resp += data
html = resp.decode("utf-8", "ignore")
prices = re.findall(r'.{0,300}prices?.{0,1000}', html, re.IGNORECASE | re.DOTALL)
with open("ggsel_docs_prices.txt", "w", encoding="utf-8") as f:
    for idx, match in enumerate(prices[:10]):
        f.write(f"--- MATCH {idx} ---\n{match}\n")
print(f"Found {len(prices)} matches.")
