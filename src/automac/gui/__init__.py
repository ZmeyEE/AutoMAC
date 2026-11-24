"""視窗進入點。"""
from __future__ import annotations


def main(config_path: str | None = None) -> int:
    from .app import build_app
    build_app(config_path).mainloop()
    return 0
