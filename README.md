# AutoMAC：校園網 MAC 限速偵測與自動切換

校園網路對每個登記的 MAC 設有流量上限，超過後速度會被壓到約 1 Mbps。實驗室裡好幾個人共用一池已登記的 MAC，
本工具負責三件事：

1. **偵測**：判斷目前這個 MAC 是否被限速，而且在你正在下載時不會誤判、也不會讓網路變卡。
2. **切換**：被限速時（或你按一下）換到池內沒人用的 MAC，換完驗證網路真的通了才算成功，失敗會換回原 MAC。
3. **防重疊**：切換前用「區網鄰居掃描」加「mDNS 互相宣告」確認沒有別人正在用同一個 MAC。

> 這是為特定校園網環境（有線乙太網路、同一台不可管理交換機、MAC 綁 IP）寫的工具。
> 換 MAC 之前請確認你有權使用該 MAC，並遵守校方網路規定。

## 需求

- Windows 10 / 11，Python 3.11 以上
- 換 MAC 需要系統管理員權限（只監看不需要）
- 依賴：`psutil`、`zeroconf`

## 快速開始

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install psutil zeroconf
set PYTHONPATH=src                                  # 或 pip install -e .
python -m automac pool add 00-00-5E-00-53-01 "電腦 A"
python -m automac status                            # 看網卡、MAC、池內各 MAC 狀態
python -m automac run --dry-run                     # 試運行：只監看與報告，不換 MAC
python -m automac gui                               # 視窗（新增／刪除 MAC、切換、自動開關）
```

從舊版匯入：`python -m automac pool import 舊的mac_settings.json`。

## 偵測原理（為什麼這樣不誤判）

實測（2026-11-23）：健康時下載可貼近約 95 Mbps；被限速時被壓在約 1 Mbps 的平線，且 1 條或 4 條連線總和相同
（以 MAC 為單位）。規則：

| 網卡目前流量 | 動作 |
|---|---|
| 大（10 秒中位數 ≥ 3 Mbps） | 一定不是限速，**不探測、不打擾** |
| 小 | 發 256 KB 探針；探針期間若有別的流量，這次讀值無效 |
| 連續兩次有效且低於 2 Mbps | 判定限速 |

網路斷線（探針連不上）不算限速。連續切換太多次（預設一小時 4 次）會停止輪流並通知，因為多半是整池都慢或校園網壅塞。

## 三種角色：單機、伺服器、組員端

| 角色 | 用途 | 清單怎麼來 |
|---|---|---|
| `standalone`（預設） | 一個人用，自己管 MAC 清單 | 本機 `config.json` |
| `hub` 伺服器 | 實驗室登記處：**唯一能新增、刪除、改標籤、設固定占用**，並替組員分配 MAC | 本機 `config.json` |
| `client` 組員端 | 組員電腦：不能改清單，向伺服器同步清單、向伺服器要 MAC | 從伺服器同步（離線時用上次快取） |

伺服器掃描區網並記錄每台電腦租了哪個 MAC，一次決定分配，所以兩台同時要不會拿到同一個；某台回報某 MAC 被限速，全員都避開。
連不上伺服器時，組員端退回本機快取的清單加 mDNS 與鄰居掃描，照常運作。

```powershell
# 伺服器上（一次）：啟用角色、產生 token、匯出給組員的設定檔
python -m automac hub init
python -m automac hub client-file automac-client.json     # 含 token，只給實驗室成員，不要上傳

# 組員電腦：把 automac-client.json 放在程式旁邊第一次執行就自動套用；或手動
python -m automac join automac-client.json
python -m automac gui                                      # 組員視窗只有「自動挑選並切換」「切換到所選」，清單自動同步
```

打包成 exe 給組員（不需裝 Python）：`pip install pyinstaller` 後執行 `powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1`，產生 `dist\automac.exe`（雙擊開視窗、自動要求系統管理員）。把 exe 與伺服器匯出的 `automac-client.json` 放同一資料夾，第一次執行自動加入伺服器。

伺服器 API 預設埠 47601，**TLS 1.3 加密**（見「通訊加密」）。改標籤：伺服器視窗雙擊該列，或 `automac pool label <MAC> <標籤>`。

## 陌生電腦自動入網

組員包可以放到任何一台同區網的電腦。若這台電腦目前的 MAC **不在池內**（沒申請過），組員端同步到清單後會自動向伺服器要一個沒人用的 MAC 並切換（需要系統管理員權限，`auto_join` 預設開，失敗每 60 秒再試）。
前提（未驗證，見「已知限制」）：這台電腦用未申請的 MAC 時，仍要能在區網內連到伺服器。

## 組員端：背景服務與視窗

組員雙擊 `automac.exe`：自動安裝背景服務（Windows 排程工作，開機啟動、SYSTEM 身分，當掉自動重啟）並開視窗。**偵測、換 MAC、同步、用量都由背景服務做，視窗只是檢視器，關掉視窗不影響任何功能**。
新版 exe 雙擊時自動升級服務；解除安裝在視窗右下角或右下角圖示的選單裡，不需要另外的批次檔。服務很輕：實測 60 秒內約 0.16% 單核 CPU、約 41 MB 記憶體，低優先權。
視窗與服務透過本機控制介面（127.0.0.1、token 檔）溝通；流量監測有看門狗：讀不到網卡計數器 10 秒就警告並重新偵測網卡，換 MAC 前先結算用量。
標籤跟著電腦走：電腦換到哪個 MAC，伺服器視窗與組員視窗的標籤就顯示在哪個 MAC（`automac device label <電腦名稱> <標籤>` 或在伺服器視窗改）。

## 系統匣圖示與解除安裝

組員端登入後在右下角有圓形圖示（`automac.exe tray`，排程工作「AutoMAC Tray」登入時啟動）：綠＝正常、黃＝切換中或輪流已停止、紅＝服務沒回應（會自動叫排程工作重啟）。點一下開視窗，右鍵有「自動挑選並切換」「被限速時自動切換」「解除安裝」「關閉圖示」。關視窗＝縮回右下角。
解除安裝在介面裡（視窗右下角、圖示右鍵），`automac uninstall-service --purge` 是同一件事；不再附批次檔。切換耗時實機 4.4 秒（寫登錄＋pnputil 重啟網卡，失敗退回 Set-NetAdapter）。

## 部署

| 目標 | 怎麼做 |
|---|---|
| Windows 伺服器（登記處） | 在伺服器以系統管理員執行 `server\open-firewall.ps1`（只開本機子網路的 TCP 47601 與 UDP 5353），再 `server\install-service.ps1` 安裝排程工作（開機啟動、SYSTEM；預設試運行，確認日誌沒問題再加 `-Enable`）。`server\open-gui.bat` 開伺服器視窗，`server\stop-automac.ps1` 確實停掉背景程序 |
| Windows 組員端 | 把打包好的 `automac.exe` 與伺服器匯出的 `automac-client.json` 放同一資料夾，雙擊；自動安裝背景服務與系統匣圖示 |
| Linux（樹莓派監視器） | `pi/fix-nas-mount.sh`：伺服器換 MAC 後 IP 會變，這支腳本把 fstab 的伺服器位址改成 mDNS 名稱，並裝一個每 30 秒檢查的 systemd 計時器自動重新掛載；`--undo` 還原 |

## 文件與圖表

- 開發文件（Word）：[docs/AutoMAC開發文件_合併版.docx](docs/AutoMAC開發文件_合併版.docx)
- 流程圖（可用 diagrams.net 編輯的 `.drawio` 與 PNG）：`docs/diagrams/`，由 `tools/make_diagrams.py` 產生
- 資料圖表（網路採樣時序、探針大小、切換耗時等）：`docs/charts/`，由 `tools/make_charts.py` 用 `samples/` 的實測樣本產生
- 示範截圖（假資料）：`docs/gui_main.png`、`docs/charts/組員端視窗.png`，由 `tools/gui_demo_screenshot.py` 產生
- 開發日誌與參考文獻：`docs/開發日誌.md`、`research/參考文獻.md`

## 用量帳本

伺服器記錄每個 MAC 每天的用量（下載、上傳分開記，視窗顯示「今日用量」與佔每日上限的百分比）；`automac usage --days 7` 列出歷史。
分配時在可用的 MAC 中優先選今天用得最少的（用量相同就維持輪流）。用量是各台跑本工具的電腦用網卡計數器量的，歸給當時用的 MAC，換 MAC 的邊界會切開。
限制：只看得到跑本工具的電腦（Pi 等不算），所以是下限而不是校方實際計算的值；不知道校方算下載、上傳或兩者之和，百分比以兩者之和估算；每日上限 `daily_quota_gb`（預設 10）是使用者描述的規則，未經校方證實，只用來顯示，不會排除 MAC。

## 通訊加密

組員端與伺服器之間的所有通訊都經 TLS 1.3（標準函式庫 `ssl`，不增加相依套件）：

| 威脅 | 對策 |
|---|---|
| 同網段竊聽 | TLS 1.3，臨時 ECDHE 金鑰交換（前向保密），資料用 AEAD（實測協商為 AES-256-GCM）；只允許 1.3，不退讓舊版；明文 HTTP 連線直接失敗 |
| 假冒伺服器、中間人 | 憑證釘選：組員設定檔記下伺服器憑證的 SHA-256 指紋，握手後、送出任何資料前先比對，不符就中止，token 不會送出 |
| 冒充組員 | 256 位元隨機 token，在 TLS 內傳送，常數時間比對 |
| 暴力猜 token、濫用 | 同一來源連續失敗 5 次鎖 60 秒；請求大小上限；握手在處理執行緒做（10 秒逾時），慢速連線卡不住服務 |

伺服器憑證是自簽的 ECDSA P-384（有效 10 年），由 `automac hub init` 用 openssl（Git for Windows 內建）產生；私鑰檔權限限制為本人、系統與管理員。
成本：每次新連線含握手與釘選，實測比明文多約 3 ms（`tools/bench_tls.py`）；組員端每 30 秒同步一次，影響可忽略。
方案選擇與文獻見 `research/參考文獻.md` 第五輪（自簽加釘選、不採用雙向 TLS 與 Noise 的理由）。

## 隱私與安全

- 安全性說明與回報方式：[SECURITY.md](SECURITY.md)
- 公開前的隱私與網路安全檢查紀錄（發現什麼、怎麼處理、還剩什麼風險）：[docs/隱私與安全檢查.md](docs/隱私與安全檢查.md)
- 文件與範例中的 MAC 一律是保留給文件的 `00-00-5E-00-53-xx`，IP 是 `192.0.2.0/24`；`samples/` 只有不含識別資訊的網卡速度取樣。

## 安全設計

- **自動切換預設關閉**；`--dry-run` 只報告；失敗（沒拿到 IP）自動換回原 MAC。
- 切換前先確認目標 MAC 沒人在用（掃描＋宣告），兩人同時搶同一個時以時間戳決勝，恰好一方勝出。
- 伺服器 API 必須帶 token，沒設就不開；`hub_token`、`hub-key.pem`、`automac-client.json` 屬機密，不要上傳。
- 設定檔含真實 MAC，存在 `%APPDATA%\AutoMAC\config.json`（組員端 exe 是 `C:\ProgramData\AutoMAC\config.json`），**不要上傳**（已在 `.gitignore`）。範例見 `config.example.json`。
- 設定檔的 `hooks_before` / `hooks_after` 會用 shell 執行命令，所以**預設不執行**，要自己設 `allow_hooks: true` 才生效；組員設定檔只接受連線欄位，夾帶的命令一律忽略。

## 專案結構（由下往上，上層只依賴下層）

| 模組 | 職責 |
|---|---|
| `macaddr` `stats` `config` | MAC 格式、統計、設定與狀態檔（純邏輯／檔案） |
| `winshell` `nic` `traffic` | PowerShell、網卡偵測與換 MAC、網卡流量取樣（系統邊界，可用假物件取代） |
| `probe` `scanner` `presence` | 探針與有效性、鄰居掃描、mDNS 宣告與佔用協定（網路邊界） |
| `detector` `policy` | 是否被限速的狀態機、選 MAC 與切換頻率限制（純邏輯，可用真實樣本重播） |
| `hub` | 伺服器登記處（池、租約、分配、限速共享）與 HTTP 介面、組員端用戶端 |
| `switcher` `service` `events` `hooks` | 切換流程、背景主迴圈、事件匯流排、切換前後外掛命令 |
| `cli` `gui/` | 命令列與視窗，只做呈現 |

## 測試

```powershell
set PYTHONPATH=src
python -m unittest discover -s tests -t .
```

涵蓋：真實樣本重播（健康、限速、使用者自己下載）、隨機忙碌流量兩小時零探針、各種失敗路徑的切換回滾、視窗操作。
`tools/` 內有實機驗證腳本（探針、掃描、mDNS、端到端切換）。

## 已知限制（誠實說明）

- 「每個 MAC 每天上限 10GB、午夜恢復」是使用者描述的規則，**未經校方文件證實**；恢復時間可用 `quota_reset_hour` 調整。
- 限速狀態下探針的實際讀值由下載曲線推算（約 0.9 Mbps），尚未在被限速時直接量測探針本身。
- ARP 掃描只看得到自己的網段；跨網段靠 mDNS 宣告補強，但不跑本工具的裝置只能靠掃描。
- 陌生電腦自動入網未在「未申請的 MAC」下實測：若校園網路對未登記的 MAC 不發 IP，那台電腦就連不到伺服器，無法自動配發，只能先手動設定一個池內的 MAC。
- 憑證沒有撤銷機制：伺服器換憑證（或私鑰外洩）後，要重新發組員設定檔給每個人；組員多到不好管理時，改成自建小型 CA 簽發。
- 組員設定檔含 token 與憑證指紋，外流等於把「冒充組員」的條件交出去（仍擋不了假冒伺服器）；懷疑外流就 `automac hub init --new-token` 換掉。
- 伺服器沒開時，組員端只能靠快取清單與備援，看不到伺服器登記的租約。
- 非管理員無法清空鄰居表，掃描結果會偏保守（可能把已離線的 MAC 當成使用中，只會少選，不會撞）。

## 授權

MIT（見 `LICENSE`）。依賴 `zeroconf` 為 LGPL-2.1 以上，以動態連結（pip 安裝）方式使用。
