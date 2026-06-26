"""
dry_run.py —— grade() 结构冒烟测试（确定性，高价值）

对应标注规范 §9「用一份样例产物干跑 grade 不报错」。在隔离子进程里：
- 把 ## Automated Checks 代码拼进 runner，调用
  grade(transcript={}, workspace_path=<临时空目录>)，print(json.dumps(result))。
- 断言：能 import、能调用、返回 dict、含 overall_score、不抛异常。

注意（如实说明，不误判）：
- grade 多写死 /tmp_workspace，本地无此目录 → 读不到产物是预期的。我们验的是
  「结构健壮、能跑完返回合法 dict」，不是「打分正确」。results/ 空 → 所有 check 走
  try/except 返回 0.0、judge 无 key 返回 0.0 → overall_score≈0.0，这恰好证明兜底生效。
- openpyxl/openai 等容器内依赖本地缺失时，若 grade 把 import 写在函数体内并 try/except
  兜底（本批写法），不影响 dry-run；若顶层 import 缺依赖会导致 NameError/ImportError，
  这本身就是值得报告的健壮性问题。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import config
from .models import Defect, Layer, Severity, WildTaskBundle, ResolverStatus
from .parsing import TaskMd


_RUNNER_TEMPLATE = """\
import json, sys
{automated_checks}

try:
    _result = grade(transcript={{}}, workspace_path={workspace_path!r})
except Exception:
    import traceback
    sys.stderr.write("GRADE_RAISED\\n")
    traceback.print_exc()
    sys.exit(3)

if not isinstance(_result, dict):
    sys.stderr.write("NOT_DICT:" + type(_result).__name__ + "\\n")
    sys.exit(4)
if "overall_score" not in _result:
    sys.stderr.write("NO_OVERALL_SCORE\\n")
    sys.exit(5)
print(json.dumps(_result))
"""


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数：对一段 ## Automated Checks 源码做子进程冒烟测试
#   供 dry_run_grade(bundle)（质检）与 preannotate.grade_builder（生成自修）共用。
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SmokeResult:
    ok: bool                       # 是否跑通（返回 dict 且含 overall_score）
    returncode: int                # 子进程退出码（0 成功；3 抛异常；4 非 dict；5 缺 overall_score；124 超时）
    scores: dict | None            # 成功时解析出的分数 dict
    error_detail: str              # 失败时的 stderr 尾部 / 超时说明（成功为空）
    summary: str                   # 一句话结论


# 退出码 → 失败原因（与 _RUNNER_TEMPLATE 的 sys.exit 对应）
_CODE_SUMMARY = {
    3: "grade() 执行时抛出异常（未被 try/except 兜底）",
    4: "grade() 返回值不是 dict（引擎取不到分数）",
    5: "grade() 返回 dict 缺少 overall_score 键（框架取不到总分）",
    124: "grade() 干跑超时",
}


def smoke_run_grade(automated_checks_src: str, timeout: int | None = None) -> SmokeResult:
    """把 grade 代码拼进 runner，在空 workspace 下子进程冒烟跑。纯函数、无副作用。

    空 workspace（含空 results/）→ 读不到产物，所有 check 走 try/except 返回 0.0、
    judge 无 key 返回 0.0 → overall_score≈0.0。验的是「结构合法、能跑完返回合法 dict」。
    """
    if timeout is None:
        timeout = config.DRY_RUN_TIMEOUT_SEC
    if not automated_checks_src.strip():
        return SmokeResult(ok=False, returncode=-1, scores=None,
                           error_detail="空的 ## Automated Checks 代码",
                           summary="无 grade 代码可干跑")

    with tempfile.TemporaryDirectory(prefix="qc_wild_ws_") as empty_ws:
        (Path(empty_ws) / "results").mkdir(exist_ok=True)
        runner = _RUNNER_TEMPLATE.format(
            automated_checks=automated_checks_src,
            workspace_path=empty_ws,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(runner)
            runner_path = f.name
        try:
            r = subprocess.run(
                [sys.executable, runner_path],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return SmokeResult(
                ok=False, returncode=124, scores=None,
                error_detail=f"grade() 在空 workspace 下 {timeout}s 未返回"
                             "（judge 无 key 应直接 0.0；超时多因 judge 无降级或死循环/网络阻塞）。",
                summary=_CODE_SUMMARY[124],
            )
        finally:
            Path(runner_path).unlink(missing_ok=True)

    if r.returncode == 0:
        scores = None
        try:
            scores = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            pass
        n_keys = 0
        if isinstance(scores, dict):
            n_keys = len([k for k, v in scores.items()
                          if isinstance(v, (int, float)) and k != "overall_score"])
        ov = scores.get("overall_score") if isinstance(scores, dict) else None
        return SmokeResult(
            ok=True, returncode=0, scores=scores, error_detail="",
            summary=f"干跑通过：返回合法 dict、含 overall_score={ov}、{n_keys} 个评分项",
        )

    tail = "\n".join((r.stderr or "").strip().splitlines()[-25:])
    return SmokeResult(
        ok=False, returncode=r.returncode, scores=None,
        error_detail=tail or "无 stderr 输出。",
        summary=_CODE_SUMMARY.get(r.returncode, f"grade() 干跑失败（退出码 {r.returncode}）"),
    )


def dry_run_grade(bundle: WildTaskBundle, timeout: int | None = None) -> tuple[list[Defect], list[str]]:
    """子进程冒烟测试 grade()。返回 (缺陷, 良好项)。"""
    out: list[Defect] = []
    good: list[str] = []

    if bundle.resolver_status == ResolverStatus.UNRESOLVABLE:
        return out, good  # 解析都不过，干跑无意义

    tm = TaskMd.load(bundle.task_md_path)
    checks_src = tm.automated_checks
    if not checks_src.strip():
        return out, good  # 已由 static 层报缺 grade

    res = smoke_run_grade(checks_src, timeout=timeout)
    if res.ok:
        good.append(
            f"grade() {res.summary}（空产物下应≈0，证明 try/except 兜底生效）"
        )
        return out, good

    detail = res.error_detail
    if res.returncode == 124:
        detail += " 引擎容器内限时 120s。"
    else:
        detail = f"子进程 stderr（尾部）：\n{detail}"
    out.append(Defect(
        id="DRY-1", layer=Layer.L2, category="grade",
        severity=Severity.BLOCKER,
        summary=res.summary,
        detail=detail,
        principle="grade 须能健壮运行并返回含 overall_score 的 dict",
        evidence="## Automated Checks",
    ))
    return out, good
