"""test_explorer.py —— census / required_read_count / 探索闸门（纯逻辑，离线）。"""

from __future__ import annotations

from qc import explorer
import config


# ── required_read_count：min(W, max(FLOOR, ceil(W*RATIO))) 封顶 CAP ──

def test_required_read_count_small():
    # W 小 → 被 min(W,..) 收敛到 W
    assert explorer.required_read_count(5) == 5
    assert explorer.required_read_count(1) == 1


def test_required_read_count_mid():
    # W=16 → max(6, ceil(6.4)=7)=7
    assert explorer.required_read_count(16) == 7
    # W=30 → max(6, 12)=12
    assert explorer.required_read_count(30) == 12


def test_required_read_count_cap():
    # 超大 → 封顶 CAP(25)
    assert explorer.required_read_count(2485) == config.GATE_MIN_READS_CAP
    assert explorer.required_read_count(100) == 25


def test_required_read_count_zero():
    assert explorer.required_read_count(0) == 0


# ── census_workspace：统计 + 分桶 + 排除二进制 ──

def test_census_inbox_task1(inbox_task_1_ws):
    c = explorer.census_workspace(inbox_task_1_ws)
    # task_1：sales.csv(data) + 两个 .py(code)；.xlsx/.pptx 二进制被排除
    assert c["count"] == 3
    assert "sales.csv" in c["buckets"].get("data", [])
    codes = c["buckets"].get("code", [])
    assert any(p.endswith(".py") for p in codes)
    # 二进制不计入
    assert all(not p.endswith((".xlsx", ".pptx")) for p in c["text_files"])


def test_census_missing_dir(tmp_path):
    c = explorer.census_workspace(tmp_path / "nope")
    assert c["count"] == 0
    assert c["buckets"] == {}


# ── make_exploration_gate：五场景拦截 ──

def _build_census(n_data=2, n_code=2, n_doc=2):
    files = ([f"d{i}.csv" for i in range(n_data)]
             + [f"c{i}.py" for i in range(n_code)]
             + [f"m{i}.md" for i in range(n_doc)])
    buckets = {}
    if n_data: buckets["data"] = [f"d{i}.csv" for i in range(n_data)]
    if n_code: buckets["code"] = [f"c{i}.py" for i in range(n_code)]
    if n_doc: buckets["doc"] = [f"m{i}.md" for i in range(n_doc)]
    return {"count": len(files), "text_files": files, "buckets": buckets}


def test_gate_empty_history_blocks():
    gate = explorer.make_exploration_gate(_build_census())
    msg = gate([])
    assert msg is not None and "list" in msg.lower() or "递归" in (msg or "")


def test_gate_only_flat_list_blocks_recursive():
    gate = explorer.make_exploration_gate(_build_census())
    msg = gate([("list_dir", {"path": "workspace"})])
    assert msg is not None  # 没递归、只 list 一层 → 拦


def test_gate_not_enough_reads():
    c = _build_census(n_data=10, n_code=10, n_doc=10)  # W=30 need=12
    gate = explorer.make_exploration_gate(c)
    hist = [("list_dir", {"path": "workspace", "recursive": True}),
            ("read_file", {"path": "d0.csv"})]
    msg = gate(hist)
    assert msg is not None and ("至少需读" in msg or "不足" in msg)


def test_gate_missing_bucket():
    c = _build_census(n_data=10, n_code=10, n_doc=10)  # 三桶都有
    gate = explorer.make_exploration_gate(c)
    # 读够 12 个但全是 code → 漏 data/doc 桶
    reads = [("read_file", {"path": f"c{i}.py"}) for i in range(10)]
    reads += [("read_file", {"path": "c0.py"}), ("read_file", {"path": "c1.py"})]
    hist = [("list_dir", {"path": "workspace", "recursive": True})] + reads
    # 补到 12 个唯一其实不够（只有10个code），改用足量唯一 code 名
    c2 = _build_census(n_data=5, n_code=20, n_doc=5)
    gate2 = explorer.make_exploration_gate(c2)  # W=30 need=12
    reads2 = [("read_file", {"path": f"c{i}.py"}) for i in range(12)]
    hist2 = [("list_dir", {"path": "workspace", "recursive": True})] + reads2
    msg = gate2(hist2)
    assert msg is not None and ("数据" in msg or "文档" in msg)


def test_gate_pass_all_satisfied():
    c = _build_census(n_data=5, n_code=20, n_doc=5)  # W=30 need=12
    gate = explorer.make_exploration_gate(c)
    reads = ([("read_file", {"path": f"c{i}.py"}) for i in range(10)]
             + [("read_file", {"path": "d0.csv"}), ("read_file", {"path": "m0.md"})])
    hist = [("list_dir", {"path": "workspace", "recursive": True})] + reads
    assert gate(hist) is None  # 递归+12个+三桶 → 放行


def test_gate_extra_required():
    c = _build_census(n_data=5, n_code=20, n_doc=5)
    seen = {"md": False}

    def _read_md(history):
        return any("task.md" in str(a.get("path", "")) for _, a in history)

    gate = explorer.make_exploration_gate(c, extra_required=[("读 task.md", _read_md)])
    reads = ([("read_file", {"path": f"c{i}.py"}) for i in range(10)]
             + [("read_file", {"path": "d0.csv"}), ("read_file", {"path": "m0.md"})])
    base = [("list_dir", {"path": "workspace", "recursive": True})] + reads
    # 没读 task.md → 拦
    assert gate(base) is not None
    # 读了 task.md → 放行
    assert gate(base + [("read_file", {"path": "tasks/x/task.md"})]) is None
