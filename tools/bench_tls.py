"""量測 TLS 1.3 加密的成本：同一個 Hub、同一組請求，比較「明文 HTTP」與「TLS 1.3＋憑證釘選」每次請求（每次新連線，含握手）的耗時。
用法：python tools/bench_tls.py [次數]    （需要 openssl 產生測試憑證）"""
import http.client
import json
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from automac import tlsutil  # noqa: E402
from automac.config import Settings, StateStore  # noqa: E402
from automac.hub import Hub, HubClient, HubServer  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
td = tempfile.TemporaryDirectory()
s = Settings()
for i in range(1, 10):
    s.add_mac(f"00005E0053{i:02X}")
hub = Hub(s, StateStore(Path(td.name) / "s.json"), scan_fn=lambda: [])
BODY = json.dumps(hub.view("c", "00005E005301"), ensure_ascii=False).encode()


class Plain(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


plain = HTTPServer(("127.0.0.1", 0), Plain)
threading.Thread(target=plain.serve_forever, daemon=True).start()
cert, key = Path(td.name) / "c.pem", Path(td.name) / "k.pem"
pin = tlsutil.generate_cert(cert, key, ["127.0.0.1"])
srv = HubServer(hub, "t" * 40, cert, key, port=0, host="127.0.0.1")
srv.start()
cli = HubClient(lambda: f"https://127.0.0.1:{srv.port}", "t" * 40, "c", pin)


def timeit(fn):
    fn()                                                            # 暖機
    xs = []
    for _ in range(N):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000)
    xs.sort()
    return statistics.mean(xs), xs[len(xs) // 2], xs[int(len(xs) * 0.95)]


def plain_req():
    c = http.client.HTTPConnection("127.0.0.1", plain.server_address[1])
    c.request("GET", "/")
    c.getresponse().read()
    c.close()


m1 = timeit(plain_req)
m2 = timeit(lambda: cli.fetch("00005E005301"))
print(f"回應大小 {len(BODY)} 位元組，各 {N} 次（每次新連線）")
print(f"明文 HTTP            平均 {m1[0]:.2f} ms  中位數 {m1[1]:.2f}  P95 {m1[2]:.2f}")
print(f"TLS 1.3＋釘選＋token 平均 {m2[0]:.2f} ms  中位數 {m2[1]:.2f}  P95 {m2[2]:.2f}")
print(f"額外成本約 {m2[0] - m1[0]:.2f} ms／次；組員端每 30 秒同步一次 → 每天約 {86400 / 30 * (m2[0] - m1[0]) / 1000:.1f} 秒的額外 CPU 時間")
