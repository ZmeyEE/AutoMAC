"""產生開發文件用的資料圖表（matplotlib，輸出 docs/charts/*.png）。

用途：把文件裡原本只用表格呈現的數據畫成圖（使用者 2026-11-24 要求：很多數據都可以做成圖表，例如網路採樣）。
資料來源：
    C1 網路採樣時序   samples/probe_20261123-163212.json（健康）與 probe_20261123-205916.json（被限速），
                      都是 samples/probe.py 在實驗室真實網路錄的網卡每 0.5 秒速度（nic 欄位）。
    C2 健康與限速比較 實測資料與分析一章的「表 4」數字（手動輸入，來源同上）。
    C3 探針大小對讀值 「表 5」數字（手動輸入）。
    C4 切換耗時       2026-11-24 實機量測：使用者回報的切換紀錄與 tools/bench_switch.py、
                      switch_roundtrip 的事件紀錄（見 docs/開發日誌.md P11、P12）。
字型：中文標楷體（DFKai-SB）、英文與數字 Times New Roman（使用者的文件字體規則）；axes.unicode_minus=False。
圖表是 PNG，要改數字就改本檔再重跑（python tools/make_charts.py），不是 Word 內建圖表。
shortcut: 沒有用 Word 原生圖表（python-docx 不支援建立，要手寫 chart XML 與內嵌活頁簿）；要讓使用者在 Word 裡直接改數字時再做。
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "charts"
SAMPLES = ROOT / "samples"

plt.rcParams["font.family"] = ["Times New Roman", "DFKai-SB"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["font.size"] = 12
# 色票沿用視窗的粉彩色票（霧藍、粉、薰衣草、綠），飽和度一致
BLUE, PINK, LAV, GREEN, GRAY = "#5F7DB1", "#D98E98", "#8E98C6", "#5CAD85", "#8A8896"


def load_nic(name: str):
    """讀樣本的網卡取樣：回傳 (相對秒數, 階段, 下載 Mbps, 上傳 Mbps) 的串列。"""
    d = json.loads((SAMPLES / name).read_text(encoding="utf-8"))
    rows = d["nic"]
    t0 = rows[0][0]
    return [(r[0] - t0, r[1], r[2], r[3]) for r in rows], d


def c1_sampling() -> Path:
    """C1：網路採樣時序。上圖健康、下圖被限速，同一條 0.5 秒取樣的下載速度；階段用底色分段，虛線是偵測門檻。"""
    fig, axes = plt.subplots(2, 1, figsize=(9.5, 7.2), sharex=False)
    for ax, name, title, color in ((axes[0], "probe_20261123-163212.json", "健康狀態（未被限速）", BLUE),
                                   (axes[1], "probe_20261123-205916.json", "被限速狀態（約 1 Mbps 平線）", PINK)):
        rows, _ = load_nic(name)
        t = [r[0] for r in rows]
        rx = [r[2] for r in rows]
        ax.plot(t, rx, color=color, linewidth=1.8, label="下載速度")
        # 階段底色：把連續相同的階段併成一段
        seg_start, cur = rows[0][0], rows[0][1]
        palette = {"idle": "#F2EEF4", "load1": "#EAF1F8", "load4": "#F8EDEF"}
        for i in range(1, len(rows) + 1):
            if i == len(rows) or rows[i][1] != cur:
                end = rows[i - 1][0] if i == len(rows) else rows[i][0]
                ax.axvspan(seg_start, end, color=palette.get(cur, "#F3F3F3"), zorder=0)
                ax.text((seg_start + end) / 2, ax.get_ylim()[1] if False else max(rx) * 1.02, {"idle": "閒置", "load1": "單串流下載", "load4": "四串流下載", "cool1": "冷卻", "cool2": "冷卻"}.get(cur, cur),
                        ha="center", va="bottom", fontsize=10, color=GRAY)
                if i < len(rows):
                    seg_start, cur = rows[i][0], rows[i][1]
        ax.axhline(3.0, color=GRAY, linestyle="--", linewidth=1)
        ax.text(t[-1], 3.0, "安靜門檻 3 Mbps", ha="right", va="bottom", fontsize=10, color=GRAY)
        if color == PINK:                                  # 被限速那張：1 Mbps 平線在這個尺度下看不清楚，標出來
            ax.annotate("限速平線約 1 Mbps", xy=(31, 1.1), xytext=(24, 22), fontsize=11, color=PINK,
                        arrowprops={"arrowstyle": "->", "color": PINK})
            ax.annotate("連線初期約 1 秒的突發", xy=(43.9, 64), xytext=(30, 55), fontsize=11, color=GRAY,
                        arrowprops={"arrowstyle": "->", "color": GRAY})
        ax.set_title(title, fontsize=13)
        ax.set_xlabel("時間（秒）")
        ax.set_ylabel("下載速度（Mbps）")
        ax.set_ylim(0, max(rx) * 1.18)
        ax.grid(alpha=0.25)
    fig.tight_layout()
    p = OUT / "c1_網路採樣時序.png"
    fig.savefig(p, dpi=170)
    plt.close(fig)
    return p


def c2_healthy_vs_throttled() -> Path:
    """C2：表 4 的數字畫成長條：健康與被限速時的下載速度（單串流、多串流）。"""
    labels = ["單串流下載", "多串流下載"]
    healthy = [88, 95]        # 單串流約 88（最大 92.5）；多串流 94 到 96，取中間
    throttled = [1.0, 1.0]
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    x = range(len(labels))
    w = 0.34
    b1 = ax.bar([i - w / 2 for i in x], healthy, w, color=BLUE, label="未被限速")
    b2 = ax.bar([i + w / 2 for i in x], throttled, w, color=PINK, label="被限速")
    for b, v in list(zip(b1, healthy)) + list(zip(b2, throttled)):
        ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:g}", ha="center", fontsize=11)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("下載速度（Mbps）")
    ax.set_ylim(0, 110)
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    p = OUT / "c2_健康與限速比較.png"
    fig.savefig(p, dpi=170)
    plt.close(fig)
    return p


def c3_probe_size() -> Path:
    """C3：表 5 的探針大小對讀值。256 KB 健康讀值是 7 到 29 Mbps，畫成區間；被限速時只有 0.9 Mbps，差 8 倍以上。"""
    sizes = ["128 KB", "256 KB", "1 MB", "10 MB"]
    mid = [3.8, 18, 23.5, 68.6]
    err = [[0, 11, 0, 0], [0, 11, 0, 0]]          # 256 KB：7 到 29，中點 18、上下各 11
    colors = [LAV, BLUE, LAV, LAV]
    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    bars = ax.bar(sizes, mid, color=colors, yerr=err, capsize=6, ecolor=GRAY)
    ax.axhline(0.9, color=PINK, linewidth=2)
    ax.text(-0.4, 79, "粉紅線：被限速時 256 KB 的讀值，約 0.9 Mbps", ha="left", color=PINK, fontsize=11)
    ax.text(-0.4, 74, "256 KB 的長條取區間中點，細線是 7 到 29 Mbps 的範圍", ha="left", color=GRAY, fontsize=10)
    for b, label in zip(bars, ["3.8", "7 到 29", "約 23.5", "約 68.6"]):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + (13 if label == "7 到 29" else 2), label, ha="center", fontsize=11)
    ax.set_ylabel("健康時的讀值（Mbps）")
    ax.set_xlabel("探針大小")
    ax.set_ylim(0, 85)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    p = OUT / "c3_探針大小對讀值.png"
    fig.savefig(p, dpi=170)
    plt.close(fig)
    return p


def c4_switch_time() -> Path:
    """C4：切換耗時。左圖是「按下到完成」每次改善後的總秒數；右圖是最後一版的階段分解與兩種做法的比較。"""
    fig, (a, b) = plt.subplots(1, 2, figsize=(11, 4.9), gridspec_kw={"width_ratios": [1.25, 1]})
    steps = ["改善前\n（使用者回報）", "修正 IPv6\n逾時等待", "直接寫登錄\n（第一次，\n含查識別）", "預先快取識別、\n宣告改背景"]
    vals = [41.7, 8.6, 7.75, 4.4]
    bars = a.bar(steps, vals, color=[PINK, LAV, LAV, GREEN])
    for r, v in zip(bars, vals):
        a.text(r.get_x() + r.get_width() / 2, v + 0.8, f"{v:g} 秒", ha="center", fontsize=11)
    a.set_ylabel("按下到完成（秒）")
    a.set_ylim(0, 48)
    a.grid(axis="y", alpha=0.25)
    a.set_title("切換耗時的改善過程", fontsize=13)
    # 右：兩種做法的階段分解（量測：tools/bench_switch.py）
    kinds = ["PowerShell\nSet-NetAdapter", "寫登錄＋\npnputil"]
    cmd = [1.73, 0.19]
    link = [5.76 - 1.73, 4.20 - 0.19]
    b.bar(kinds, cmd, color=BLUE, label="下指令（含程序啟動）")
    b.bar(kinds, link, bottom=cmd, color=LAV, label="網卡重啟與連線協商")
    for i, (c, l) in enumerate(zip(cmd, link)):
        b.text(i, c + l + 0.12, f"{c + l:.2f} 秒", ha="center", fontsize=11)
    b.set_ylabel("秒")
    b.set_ylim(0, 7)
    b.legend(frameon=False, loc="upper right", fontsize=10)
    b.grid(axis="y", alpha=0.25)
    b.set_title("兩種換 MAC 做法的階段分解", fontsize=13)
    fig.tight_layout()
    p = OUT / "c4_切換耗時.png"
    fig.savefig(p, dpi=170)
    plt.close(fig)
    return p


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in (c1_sampling, c2_healthy_vs_throttled, c3_probe_size, c4_switch_time):
        print("已輸出", f())


if __name__ == "__main__":
    main()
