# 用本機 Word 驗證產出的 docx：更新欄位（目錄、圖表標號）、回報頁數、標題的實際編號字串，並匯出 PDF 供檢查頁面。
# 用法：powershell -STA -ExecutionPolicy Bypass -File verify_docx.ps1 -Doc <docx> -Out <輸出資料夾>
# 速度：舊版逐段落走 COM 要跑一個多小時；這版只用 GetCrossReferenceItems 一次取得全部標題（含編號），
# 字體、全形空白、空段落等結構檢查改由 verify_docx_struct.py 直接讀 XML（秒級）。
param([string]$Doc, [string]$Out)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $Out | Out-Null
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
    $d = $word.Documents.Open($Doc, $false, $true)       # 唯讀開啟，不動原檔
    $d.Repaginate()
    foreach ($t in $d.TablesOfContents) { $t.Update() }
    $d.Fields.Update() | Out-Null
    $d.Repaginate()
    "頁數: " + $d.ComputeStatistics(2)
    "表格: " + $d.Tables.Count + "  圖(行內): " + $d.InlineShapes.Count + "  欄位: " + $d.Fields.Count
    "目錄項目數: " + $d.TablesOfContents.Item(1).Range.Paragraphs.Count
    "--- 標題（含 Word 實際編號，前 60 個）"
    $items = $d.GetCrossReferenceItems(1)                # 1 = wdRefTypeHeading
    "標題總數: " + $items.Count
    $items | Select-Object -First 60 | ForEach-Object { $_ }
    $d.ExportAsFixedFormat((Join-Path $Out 'preview.pdf'), 17)
    $d.Close($false)
} finally {
    $word.Quit()
}
