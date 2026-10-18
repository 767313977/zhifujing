"""让 `pytest` 能直接 ``from app...``（仓库根目录运行）。"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
