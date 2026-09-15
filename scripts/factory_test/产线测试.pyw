"""双击打开产线测试窗口。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.factory_test.gui import main

main()
