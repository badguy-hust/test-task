"""
runpaths.py —— 统一的输出目录命名（两个框架共用约定）

结构：<framework>/output/<task_id>/<YYYYMMDD>[_<n>]/
  - 按【天】分目录，不带时分秒。
  - 同一任务同一天重复跑：第 1 次是 <YYYYMMDD>，之后 <YYYYMMDD>_1 / _2 / ...
    （n 从 1 开始递增；首跑无后缀，等价于第 0 次）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def safe_task_id(task_id: str) -> str:
    """把 task_id 规整为合法目录名。"""
    s = task_id.replace(" ", "_").replace("/", "_")
    return re.sub(r"[^\w.\-]", "_", s)


def make_task_run_dir(framework_out: str | Path, task_id: str,
                      day: str | None = None) -> Path:
    """为某题创建本次运行目录：<framework_out>/<task_id>/<YYYYMMDD>[_<n>]/。

    首跑用纯日期目录；该日期目录已存在时追加 _1/_2/... 取首个不存在的。
    """
    day = day or datetime.now().strftime("%Y%m%d")
    base = Path(framework_out) / safe_task_id(task_id)
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / day
    if not candidate.exists():
        candidate.mkdir(parents=True)
        return candidate
    n = 1
    while (base / f"{day}_{n}").exists():
        n += 1
    run_dir = base / f"{day}_{n}"
    run_dir.mkdir(parents=True)
    return run_dir


def has_successful_output(framework_out: str | Path, task_id: str,
                          marker: str, nonempty_json_key: str | None = None) -> bool:
    """断点续跑判断：某题在输出根下是否已有成功产物。

    marker：标志产物文件名（qc_wild→result.json；preannotate→rubric_draft.json）。
    nonempty_json_key：若给定，还要求该 JSON 里这个键对应的列表非空（如 preannotate 的 criteria/rubrics），
                       避免把『跑了但啥也没生成』的空壳当成功。
    """
    import json
    base = Path(framework_out) / safe_task_id(task_id)
    if not base.is_dir():
        return False
    for run_dir in base.iterdir():
        f = run_dir / marker
        if not f.is_file():
            continue
        if nonempty_json_key is None:
            return True
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get(nonempty_json_key):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False
