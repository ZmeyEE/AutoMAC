# AutoMAC：校園網 MAC 限速偵測與自動切換

AutoMAC 是針對實驗室有線網路環境開發的 MAC 管理工具。

實驗室有多台電腦時，多組 MAC 的分配與使用狀態不容易管理。本工具透過中央伺服器管理 MAC 清單、分配及使用狀態，並由組員端監測網路流量。

主要功能：

1. **限速偵測**：監測網卡流量，判斷目前網路是否受到限速。使用者下載時不進行探測，避免干擾正常流量。
2. **MAC 切換**：符合切換條件時，從 MAC 池中選擇可用項目。
3. **使用狀態同步**：透過 ARP 鄰居掃描、mDNS 宣告及伺服器租約管理，降低 MAC 重複使用的情況。

> 本工具針對特定校園網路環境設計：有線乙太網路、同一台不可管理交換機、MAC 綁定 IP。
>
> 使用前請確認有權使用相關 MAC，並遵守校方網路規定。

## 1. 環境需求

- Windows 10 / 11
- Python 3.11 以上
- MAC 修改需要系統管理員權限，單純監測不需要
- Python 依賴：`psutil`、`zeroconf`
- `openssl`：Hub 初始化時產生憑證，需要 Git for Windows 或其他提供 OpenSSL 的環境

## 2. 快速開始

### 安裝依賴

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install psutil zeroconf
```

在專案根目錄設定模組路徑：

```powershell
$env:PYTHONPATH = "src"
```

也可以使用 `pip install -e .`。

### 基本指令

```powershell
# 新增 MAC
python -m automac pool add 00-00-5E-00-53-01 "電腦 A"

# 查看網卡、目前 MAC 與池內狀態
python -m automac status

# 試運行：只監測與回報，不切換 MAC
python -m automac run --dry-run

# 開啟 GUI
python -m automac gui

# 匯入舊版設定
python -m automac pool import 舊的mac_settings.json
```

## 3. 限速偵測

### 實測結果

2026-11-23 實測：

- 正常網路下載速度可接近 95 Mbps。
- 受到限速時，下載速度約為 1 Mbps。
- 使用 1 條或 4 條連線時，總下載速度相同，限速以 MAC 為單位。

### 判斷規則

| 網卡流量狀態 | 動作 |
|---|---|
| 最近 10 秒中位數 ≥ 3 Mbps | 不進行探測 |
| 最近 10 秒流量較低，但最近 3 秒仍有流量 | 暫不探測 |
| 最近 10 秒及最近 3 秒都安靜 | 發送 256 KB 探針 |
| 探針期間出現其他流量 | 本次測量無效 |
| 第一次有效測量低於 2 Mbps | 至少間隔 5 秒後再次確認；60 秒內沒有確認則作廢 |
| 連續兩次有效測量低於 2 Mbps | 判定限速 |

高流量時不發送探針，避免干擾使用者下載。低流量時還需要確認最近 3 秒沒有流量，才會開始探測。

第一次測量偏慢時，不會立即判定限速，而是間隔至少 5 秒後再次確認；若 60 秒內沒有完成確認，第一次的結果作廢。探針期間若出現其他流量，該次結果不納入判斷。

網路斷線不視為限速。若短時間內切換次數過多，系統會停止輪替並通知使用者，避免在整體網路壅塞或 MAC 池都無法正常使用時持續切換。

## 4. MAC 管理與系統角色

AutoMAC 提供三種角色：

| 角色 | 用途 | MAC 清單來源 |
|---|---|---|
| `standalone` | 單機使用，自行管理 MAC | 本機 `config.json` |
| `hub` | 伺服器，管理清單及分配 MAC | 本機 `config.json` |
| `client` | 組員端，同步清單及申請 MAC | 伺服器同步，離線時使用快取 |

### Standalone

單機模式，由使用者自行管理 MAC 清單。

### Hub

實驗室中央管理端，是唯一能夠新增、刪除、修改標籤及設定固定占用的角色。

主要負責：

- 管理 MAC 清單。
- 記錄每台電腦租用的 MAC。
- 分配可用 MAC。
- 接收組員回報的限速狀態。
- 將限速狀態同步給其他組員。

伺服器統一處理分配，避免兩台電腦同時取得相同的 MAC。

### Client

組員端不能直接修改中央 MAC 清單，透過伺服器取得清單及申請 MAC。

伺服器無法連線時，使用上次同步的快取清單，並透過 mDNS 與鄰居掃描確認設備使用狀態。

### 初始化 Hub

用 Python 執行時，在伺服器執行：

```powershell
# 初始化伺服器
python -m automac hub init

# 匯出組員設定檔
python -m automac hub client-file automac-client.json
```

使用打包後的 `automac.exe` 部署時，指令見「7. 部署」。

`automac-client.json` 包含 Token，只能提供給授權的實驗室成員，不可公開上傳。

### 加入 Hub

將 `automac-client.json` 放在 EXE 旁邊，第一次執行時會自動套用設定。

Python 版本則從目前工作資料夾尋找設定檔。

也可以手動執行：

```powershell
python -m automac join automac-client.json
python -m automac gui
```

組員端 GUI 提供「自動挑選並切換」及「切換到所選」功能，MAC 清單由伺服器同步。

### 陌生電腦自動入網

若組員電腦目前使用的 MAC 不在池內，Client 同步清單後會自動向 Hub 申請可用 MAC，再執行切換。

- `auto_join` 預設開啟。
- 需要系統管理員權限。
- 失敗後每 60 秒重試一次。

此功能有一項前提：使用未申請的 MAC 時，電腦仍須能連線至伺服器。目前尚未完成這種情境的實機驗證，詳見「已知限制」。

## 5. MAC 使用狀態與標籤

切換前會透過鄰居掃描及 mDNS 確認目標 MAC 的使用狀態。

伺服器會記錄各電腦目前租用的 MAC，並接收組員端回報的限速狀態，供其他組員分配時參考。

電腦標籤與設備綁定，不會因為切換 MAC 而改變。

修改設備標籤：

```powershell
python -m automac device label <電腦名稱> <標籤>
```

修改 MAC 標籤：

```powershell
python -m automac pool label <MAC> <標籤>
```

也可以在伺服器視窗中雙擊 MAC 列修改標籤。

## 6. Windows 組員端

### 背景服務

組員第一次雙擊 `automac.exe` 時，Windows 會跳出系統管理員權限確認。確認後，程式會自動安裝 Windows 排程工作並開啟視窗。

背景服務負責：

- 流量監測與限速偵測。
- MAC 切換。
- 伺服器同步。
- 用量統計。

服務設定：

| 項目 | 設定 |
|---|---|
| 啟動方式 | Windows 開機啟動 |
| 執行身分 | SYSTEM |
| 當機處理 | 自動重新啟動 |
| GUI | 僅作為檢視與操作介面 |

關閉視窗不會停止背景服務。新版 EXE 再次執行時會自動升級服務。

實測服務在 60 秒內約使用 0.16% 單核 CPU、41 MB 記憶體，並設定為低優先權。

### 本機通訊

GUI 與背景服務透過 `127.0.0.1` 及 Token 檔進行通訊。

流量監測設有看門狗：

- 10 秒內讀不到網卡計數器時發出警告。
- 重新偵測網卡。
- MAC 切換前先結算用量。

### 系統匣

組員登入 Windows 後，右下角會出現 AutoMAC 圓形圖示。

由排程工作 `AutoMAC Tray` 啟動：

```powershell
automac.exe tray
```

圖示狀態：

| 顏色 | 狀態 |
|---|---|
| 綠色 | 正常 |
| 黃色 | 切換中或輪替已停止 |
| 紅色 | 背景服務沒有回應 |

操作方式：

- 左鍵：開啟視窗。
- 右鍵：開啟功能選單。
- 關閉視窗：縮回系統匣。

右鍵選單提供：

- 自動挑選並切換。
- 被限速時自動切換。
- 解除安裝。
- 關閉圖示。

若背景服務沒有回應，系統匣會透過排程工作重新啟動服務。

### 解除安裝

可從 GUI 右下角或系統匣右鍵選單解除安裝。

也可以執行：

```powershell
automac uninstall-service --purge
```

### MAC 切換時間

實機測試約 4.4 秒。

切換時會寫入登錄設定並透過 `pnputil` 重新啟動網卡，失敗時改用 `Set-NetAdapter`。

若切換後無法取得 IP，會嘗試還原原本 MAC。

## 7. 部署

### Windows 伺服器

伺服器部署腳本預設使用 `C:\AutoMAC` 作為安裝目錄，也可以透過環境變數 `AUTOMAC_HOME` 指定其他位置。

部署前先將 `automac.exe` 放入安裝目錄，並在該目錄中初始化 Hub：

```powershell
cd C:\AutoMAC

.\automac.exe hub init

.\automac.exe hub client-file automac-client.json
```

`automac.exe` 沒有主控台視窗，指令執行時不會印出訊息。完成後安裝目錄會多出 `config.json`、`hub-cert.pem`、`hub-key.pem` 與匯出的 `automac-client.json`。

接著以系統管理員身分開啟 PowerShell，執行防火牆設定：

```powershell
powershell -ExecutionPolicy Bypass -File server\open-firewall.ps1
```

安裝伺服器背景服務：

```powershell
powershell -ExecutionPolicy Bypass -File server\install-service.ps1
```

防火牆僅開放本機子網路：

| 通訊埠 | 協定 | 用途 |
|---|---|---|
| 47601 | TCP | 伺服器 API |
| 5353 | UDP | mDNS |

伺服器服務預設為試運行模式，確認日誌正常後再啟用：

```powershell
powershell -ExecutionPolicy Bypass -File server\install-service.ps1 -Enable
```

開啟伺服器視窗：

```powershell
server\open-gui.bat
```

停止背景程序：

```powershell
powershell -ExecutionPolicy Bypass -File server\stop-automac.ps1
```

`open-gui.bat` 同樣使用上述安裝目錄。

### Windows 組員端

將以下兩個檔案放在同一資料夾：

```text
automac.exe
automac-client.json
```

雙擊 EXE 即可安裝背景服務並開啟視窗，不需要另外安裝 Python。

打包指令：

```powershell
pip install pyinstaller

powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
```

產生的執行檔位於：

```text
dist\automac.exe
```

### Linux 樹莓派監視器

使用以下腳本修復 NAS 掛載：

```bash
sudo bash pi/fix-nas-mount.sh <伺服器電腦名稱> [共用資料夾名稱]
```

第二個參數為共用資料夾名稱，預設為 `share`。

腳本需要 root 權限，且 Git 中沒有設定執行權限，因此使用 `sudo bash` 執行。

伺服器切換 MAC 後，IP 可能改變。此腳本會將 `/etc/fstab` 中的伺服器位址改為 mDNS 名稱，並安裝每 30 秒檢查一次的 systemd 計時器，需要時重新掛載。

還原設定時，`--undo` 必須是第一個參數：

```bash
sudo bash pi/fix-nas-mount.sh --undo
```

## 8. 用量帳本

伺服器會記錄每個 MAC 每天的下載及上傳用量。

GUI 顯示今日用量及每日上限使用百分比。

查看最近 7 天的紀錄：

```powershell
automac usage --days 7
```

分配 MAC 時，優先選擇當日累計用量較少的項目；用量相同時維持原本的輪替順序。

用量由各組員電腦的網卡計數器統計，並歸入當時使用的 MAC。切換 MAC 時會分開計算前後用量。

目前統計有以下限制：

- 只統計有執行 AutoMAC 的電腦，Pi 等其他設備及未執行 AutoMAC 的電腦不會計入。
- 網卡計數器可能包含區網內部流量，例如 Pi 傳輸影片至伺服器，這些流量不一定會被校方計入。
- 因此統計值只是估計值，可能偏高，也可能偏低，不能視為校方實際計算值。
- 校方計算方式尚不確定，GUI 暫以下載量加上傳量估算。
- `daily_quota_gb` 預設為 10 GB，僅供用量顯示，不會用來排除 MAC。
- 每日恢復時間可透過 `quota_reset_hour` 調整。

每日 10 GB 上限及午夜恢復規則來自使用者提供的資訊，尚未經校方文件證實。

## 9. 通訊加密

Hub 與 Client 之間的通訊使用 TLS 1.3，透過 Python 標準函式庫 `ssl` 實作，不增加額外加密套件。

| 威脅 | 防護方式 |
|---|---|
| 同網段竊聽 | TLS 1.3、臨時 ECDHE 金鑰交換及 AEAD 加密 |
| 假冒伺服器或中間人攻擊 | SHA-256 憑證指紋釘選 |
| 冒充組員 | 256 位元隨機 Token |
| 暴力猜測 Token | 同一來源連續失敗 5 次，鎖定 60 秒 |
| 慢速連線 | 握手 10 秒逾時、請求大小限制 |

實測 TLS 協商使用 AES-256-GCM。

伺服器憑證使用自簽 ECDSA P-384，有效期限 10 年，由 `automac hub init` 使用 Git for Windows 內建的 OpenSSL 產生。

私鑰檔權限限制為本人、SYSTEM 與管理員。

Client 會在 TLS 握手後、傳送任何資料前，比對伺服器憑證的 SHA-256 指紋。若不符合，立即中止連線，Token 不會送出。

伺服器只允許 TLS 1.3，明文 HTTP 連線會直接失敗。

效能測試：

- 每次新連線包含 TLS 握手及憑證釘選。
- 相較明文連線，實測增加約 3 ms。
- Client 每 30 秒同步一次。

測試腳本：

```powershell
python tools/bench_tls.py
```

設計方案及文獻比較見 `research/參考文獻.md` 第五輪，包含自簽憑證加釘選，以及不採用雙向 TLS 與 Noise 的原因。

目前沒有憑證撤銷機制。若伺服器更換憑證或私鑰外洩，需要重新發送組員設定檔。組員數量增加後，可考慮改用自建 CA 簽發憑證。

## 10. 安全與隱私

- 自動切換預設關閉。
- `--dry-run` 只監測及回報，不執行切換。
- 切換前會檢查目標 MAC 是否有人使用。
- 伺服器 API 必須設定 Token 才會啟動。
- `hub_token`、`hub-key.pem`、`automac-client.json` 均為機密資料，不可公開上傳。
- 設定檔包含真實 MAC，請勿上傳。
- `hooks_before` 與 `hooks_after` 可執行 Shell 命令，預設不執行，必須設定 `allow_hooks: true` 才會啟用。
- Client 設定檔只接受連線相關欄位，夾帶的命令設定會被忽略。

設定檔位置：

| 執行方式 | 路徑 |
|---|---|
| Python 版本 | `%APPDATA%\AutoMAC\config.json` |
| 組員端 EXE | `C:\ProgramData\AutoMAC\config.json` |
| 伺服器 EXE | EXE 所在資料夾的 `config.json` |

範例設定見 `config.example.json`。

文件中的 MAC 使用保留範例位址 `00-00-5E-00-53-xx`，IP 使用 `192.0.2.0/24`。`samples/` 僅包含不含識別資訊的網卡速度取樣。

安全性說明及漏洞回報方式見 `SECURITY.md`。

公開前的隱私與網路安全檢查紀錄見 `docs/隱私與安全檢查.md`。

## 11. 程式架構

專案模組依照職責分層，上層依賴下層。

| 模組 | 職責 |
|---|---|
| `macaddr`、`stats`、`config` | MAC 格式、統計、設定與狀態檔 |
| `winshell`、`nic`、`traffic` | PowerShell、網卡偵測與切換、流量取樣 |
| `probe`、`scanner`、`presence` | 探針、鄰居掃描、mDNS 宣告及佔用協定 |
| `detector`、`policy` | 限速狀態機、MAC 選擇與切換頻率限制 |
| `hub` | MAC 池、租約、分配、限速共享、HTTP 介面及 Client |
| `switcher`、`service`、`events`、`hooks` | 切換流程、背景主迴圈、事件匯流排及切換前後命令 |
| `cli`、`gui/` | 命令列及圖形介面 |

其他模組：

`devices`、`ledger`、`localapi`、`svcinstall`、`launcher`、`tlsutil`、`takeover`、`tray`、`gui/widgets`。

## 12. 測試

執行測試：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -t .
```

目前共有 **242 項測試**，涵蓋：

- 正常網路、限速及使用者下載的真實樣本重播。
- 隨機忙碌流量持續兩小時，確認沒有誤觸探針。
- MAC 切換失敗及回滾流程。
- GUI 操作。

`tools/` 另外提供實機驗證腳本，包含探針、鄰居掃描、mDNS 及端到端切換。

CI 設定檔位於 `docs/ci/test.yml`。要啟用時複製到 `.github/workflows/test.yml`。

## 13. 文件與圖表

| 項目 | 路徑 |
|---|---|
| 開發文件（Word） | `docs/AutoMAC開發文件_合併版.docx` |
| 流程圖（Draw.io、PNG） | `docs/diagrams/` |
| 網路採樣、探針大小及切換耗時圖表 | `docs/charts/` |
| GUI 示範截圖 | `docs/gui_main.png` |
| 組員端示範截圖 | `docs/charts/組員端視窗.png` |
| 開發日誌 | `docs/開發日誌.md` |
| 文件撰寫準則 | `docs/文件撰寫準則.md` |
| 參考文獻 | `research/參考文獻.md` |

流程圖的 `.drawio` 檔可用 diagrams.net 編輯。圖表使用 `samples/` 中的實測樣本；示範截圖使用假資料。

## 14. 已知限制與待驗證項目

1. **每日流量上限尚未證實：** 每個 MAC 每天 10 GB、午夜恢復，是使用者提供的規則，尚未取得校方文件確認。
2. **限速探針尚未直接量測：** 限速時探針約 0.9 Mbps 的讀值是由下載曲線推算，尚未在實際限速狀態下直接量測。
3. **ARP 掃描範圍有限：** 只能看到自己的網段。跨網段依靠 mDNS 補強，但未執行 AutoMAC 的設備只能透過掃描發現。
4. **陌生電腦自動入網尚未驗證：** 若校園網路不會向未登記 MAC 發放 IP，電腦便無法連線至伺服器申請 MAC，需要先手動設定池內的 MAC。
5. **憑證沒有撤銷機制：** 更換伺服器憑證或私鑰外洩時，需要重新發送組員設定檔。
6. **組員設定檔外流：** 設定檔包含 Token 及憑證指紋，外流可能讓他人冒充組員。若懷疑外流，使用以下指令重新產生 Token：

```powershell
python -m automac hub init --new-token
```

7. **伺服器離線：** Client 只能使用快取清單及本機備援，無法取得伺服器最新租約。
8. **鄰居表殘留紀錄：** 非管理員無法清空鄰居表，可能將已離線設備視為仍在使用，因此分配結果會偏保守。
9. **登入後系統匣圖示：** 重開機後圖示是否能正常自動出現，尚未完成實機驗證。
10. **解除安裝：** GUI 解除安裝按鈕的完整實機操作流程，尚未完成驗證。
11. **多帳號環境：** 同一台電腦有多個 Windows 使用者帳號時，背景服務與系統匣的行為尚未完成驗證。

## 15. 授權

本專案採用 MIT License，詳見 `LICENSE`。

依賴套件 `zeroconf` 採用 LGPL-2.1 以上授權，透過 pip 動態安裝及使用。
