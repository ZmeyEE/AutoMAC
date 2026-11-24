import json, subprocess, sys, os, urllib.parse, time
M = os.path.expandvars(r"%LOCALAPPDATA%\Moli\bin\moli.exe")
for q in sys.argv[1:]:
    u = "https://api.crossref.org/works?rows=10&select=title,container-title,issued,is-referenced-by-count,DOI,type&query.bibliographic=" + urllib.parse.quote_plus(q)
    out = subprocess.run([M, "fetch", "--dump", "markdown", "--wait-until", "done", u], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    s = out[out.find("{"): out.rfind("}") + 1]
    print("##", q)
    try:
        for it in json.loads(s)["message"]["items"]:
            y = (it.get("issued", {}).get("date-parts") or [[None]])[0][0]
            print(f'- {y} | {(it.get("container-title") or ["?"])[0][:60]} | {it["title"][0][:90]} | cites {it.get("is-referenced-by-count")} | {it["DOI"]}')
    except Exception as e:
        print("ERR", e, out[:200])
    time.sleep(2)
