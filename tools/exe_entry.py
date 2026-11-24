"""打包成 exe 的進入點（PyInstaller 用）。不帶參數 = 組員（client）：安裝／啟動背景服務並開視窗檢視；伺服器：接手背景服務並開視窗；單機：開內嵌視窗；帶參數 = 照常當命令列。
視窗版 exe 沒有主控台，啟動失敗時使用者什麼都看不到，所以把例外寫進 crash.log（設定檔旁）並跳出訊息框。"""
import sys
import traceback


def _crash(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(exc))
    try:
        from automac import config
        d = config.default_config_path().parent
        d.mkdir(parents=True, exist_ok=True)
        (d / "crash.log").write_text(text, encoding="utf-8")
        where = f"\n\n詳細內容已寫入：{d / 'crash.log'}"
    except Exception:
        where = ""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("AutoMAC 無法啟動", f"{type(exc).__name__}: {exc}{where}")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        from automac import config
        from automac.cli import main
        argv = sys.argv[1:]
        if not argv:                                   # 雙擊：開視窗；伺服器（role=hub）要先接手背景服務
            role = config.load().role
            argv = ["gui", "--takeover"] if role == "hub" else (["launch"] if role == "client" else ["gui"])
        sys.exit(main(argv))
    except SystemExit:
        raise
    except BaseException as e:      # noqa: BLE001
        _crash(e)
        sys.exit(1)
