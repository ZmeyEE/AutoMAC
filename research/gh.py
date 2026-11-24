"""GitHub 搜尋（未登入 API，每分鐘 10 次）：列出星數、授權、最近更新，供「先查再寫」評估用。"""
import json, subprocess, sys, os, urllib.parse, time
M = os.path.expandvars(r"%LOCALAPPDATA%\Moli\bin\moli.exe")
for q in sys.argv[1:]:
    u = "https://api.github.com/search/repositories?per_page=8&sort=stars&q=" + urllib.parse.quote_plus(q)
    out = subprocess.run([M, "fetch", "--dump", "markdown", "--wait-until", "done", u], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    s = out[out.find("{"): out.rfind("}") + 1]
    print("##", q)
    try:
        for r in json.loads(s)["items"]:
            print(f'- {r["full_name"]} | ★{r["stargazers_count"]} | {(r.get("license") or {}).get("spdx_id")} | 更新 {r["pushed_at"][:10]} | {(r.get("description") or "")[:80]}')
    except Exception as e:
        print("ERR", e, out[:150])
    time.sleep(7)
