"""
conftest.py —— pytest 共用 fixture / 路径

把仓库根加入 sys.path（让 config / qc / qc_wild / preannotate 可被 import），
并提供指向真实样例的路径常量供各测试复用。这些测试全部离线、不调 LLM。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 真实样例（已存在于仓库，离线可读）
CONVERTED_TASK_6 = (ROOT / "wildclaw_converted" / "tasks" / "Investigative_Analysis"
                    / "Investigative_Analysis_task_6_stripe_subscription_audit.md")
INBOX_DIR = ROOT / "preannotate_inbox"
INBOX_TASK_1_WS = INBOX_DIR / "task_1_sales_csv" / "workspace"


@pytest.fixture
def converted_task_6() -> Path:
    return CONVERTED_TASK_6


@pytest.fixture
def inbox_task_1_ws() -> Path:
    return INBOX_TASK_1_WS


@pytest.fixture
def repo_root() -> Path:
    return ROOT
