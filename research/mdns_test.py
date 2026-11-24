"""zeroconf 實測：pub = 在本機通告一個服務；browse = 列出區網內看得到的服務與來源 IP。
用法：python mdns_test.py pub <秒>   /   python mdns_test.py browse <秒>
通告內容只有測試用的假資料，不含任何真實 MAC。"""
import socket, sys, time
from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

TYPE = "_automactest._tcp.local."
mode, secs = sys.argv[1], float(sys.argv[2])
zc = Zeroconf()


def my_ip():  # 取目前對外網卡的 IPv4（不實際送封包）
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("192.0.2.62", 9))
    ip = s.getsockname()[0]
    s.close()
    return ip


if mode == "pub":
    info = ServiceInfo(TYPE, f"probe-{socket.gethostname()}.{TYPE}", addresses=[socket.inet_aton(my_ip())],
                       port=47600, properties={"holder": "TEST-ONLY", "ts": str(int(time.time()))})
    t = time.perf_counter()
    zc.register_service(info)
    print(f"註冊完成，耗時 {time.perf_counter() - t:.2f} s（含 RFC 6762 的名稱衝突探測）")
    time.sleep(secs)
    zc.unregister_service(info)  # 會送出 TTL=0 的告別封包
else:
    seen = {}

    class L(ServiceListener):
        def add_service(self, z, t, n):
            if t.startswith("_services"):  # 元查詢：回來的是「別人有哪些服務類型」，不是實例，只記名稱
                seen[n] = ("(服務類型)", None, None)
                return
            i = z.get_service_info(t, n, timeout=2000)
            seen[n] = (t, [socket.inet_ntoa(a) for a in i.addresses] if i else None,
                       {k.decode(): v.decode() if v else v for k, v in (i.properties or {}).items()} if i else None)
        update_service = remove_service = lambda *a: None

    ServiceBrowser(zc, [TYPE, "_services._dns-sd._udp.local."], L())
    time.sleep(secs)
    for n, v in seen.items():
        print(n, v)
    print(f"共看到 {len(seen)} 筆")
zc.close()
