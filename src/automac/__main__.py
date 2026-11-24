"""讓 `python -m automac ...` 可用。"""
import sys

from .cli import main

sys.exit(main())
