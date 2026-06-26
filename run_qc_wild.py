#!/usr/bin/env python3
"""
run_qc_wild.py —— WildClaw 转换产物质检入口

质检对象：转换后的单文件 task.md（tasks/<分类>/<id>.md）+ 配套 workspace/。

两种启动方式：

1. 直接模式：
   python3 run_qc_wild.py <路径>  [--out DIR] [--no-semantic] [--no-dry-run]

   <路径> 可以是：
     - 单个 task.md 文件
     - 一个分类目录（tasks/Investigative_Analysis/）
     - tasks/ 根目录（递归找所有 *.md）

2. 扫描模式（交互式选择）：
   python3 run_qc_wild.py -s <路径>  [...]

质检流程：static（确定性）→ dry_run（grade 冒烟）→ semantic（LLM，可选）。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from qc.models import TaskReport, ResolverStatus, Severity
from qc_wild.resolver import resolve_wild_task
from qc_wild.static_checks import run_static
from qc_wild.dry_run import dry_run_grade
from qc_wild.semantic import run_semantic
from qc_wild.reporter import write_reports
from qc_wild.runpaths import make_task_run_dir, has_successful_output
from qc_wild.models import WildTaskBundle


# ─────────────────────────────────────────────────────────────────────────────
# 核心质检逻辑
# ─────────────────────────────────────────────────────────────────────────────

def _verdict(defects, status: ResolverStatus) -> str:
    if status == ResolverStatus.UNRESOLVABLE:
        return "无法解析"
    # 高/中置信度的 Blocker/Major 才自动判需返修；低置信度高危转人工复核。
    actionable = [d for d in defects
                  if d.severity in (Severity.BLOCKER, Severity.MAJOR)
                  and getattr(d, "confidence", "high") != "low"]
    return "需返修" if actionable else "合格"


def inspect_one(bundle: WildTaskBundle, use_semantic: bool,
                use_dry_run: bool) -> tuple[TaskReport, bool, bool]:
    defects, good = [], []
    semantic_ran = dry_run_ran = False

    if bundle.resolver_status != ResolverStatus.UNRESOLVABLE:
        d, g = run_static(bundle); defects += d; good += g

        if use_dry_run:
            dd, dg = dry_run_grade(bundle); defects += dd; good += dg
            dry_run_ran = True

        if use_semantic:
            sd, sg, ran = run_semantic(bundle)
            defects += sd; good += sg; semantic_ran = ran
    else:
        d, g = run_static(bundle); defects += d; good += g

    report = TaskReport(
        task_id=bundle.task_id,
        resolver_status=bundle.resolver_status,
        verdict=_verdict(defects, bundle.resolver_status),
        defects=defects, good_points=good,
        bundle=None,
    )
    return report, semantic_ran, dry_run_ran


# ─────────────────────────────────────────────────────────────────────────────
# 运行目录 + 摘要
# ─────────────────────────────────────────────────────────────────────────────

def _make_run_dir(base_out: Path, n: int) -> Path:
    # 保留函数名兼容；现在仅确保 output 根存在，逐题目录由 make_task_run_dir 生成。
    base_out.mkdir(parents=True, exist_ok=True)
    return base_out


def _write_run_summary(base_out: Path, bundles: list[WildTaskBundle],
                       reports: list[TaskReport], run_dirs: list[Path],
                       semantic_ran: bool, dry_run_ran: bool,
                       failures: list[tuple[str, str]] | None = None,
                       skipped: list[str] | None = None) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# WildClaw 转换产物质检 — 最近一次运行摘要", "",
        f"- 运行时间：{ts}",
        f"- 题目总数：{len(bundles)}",
        f"- 语义层：{'已运行' if semantic_ran else '未运行'}",
        f"- grade 干跑：{'已运行' if dry_run_ran else '未运行'}", "",
        "## 各题结论", "",
        "| # | 题目 | 结论 | Blocker | Major | Minor | 报告目录 |",
        "|---|---|---|---|---|---|---|",
    ]
    totals = {"合格": 0, "需返修": 0, "无法解析": 0}
    for i, (b, r, rd) in enumerate(zip(bundles, reports, run_dirs), 1):
        cc = r.counts()
        totals[r.verdict] = totals.get(r.verdict, 0) + 1
        icon = "✅" if r.verdict == "合格" else ("❌" if r.verdict == "需返修" else "⚠️")
        rel = rd.relative_to(base_out)
        lines.append(f"| {i} | {b.task_id} | {icon} {r.verdict} "
                     f"| {cc['blocker']} | {cc['major']} | {cc['minor']} | {rel} |")
    lines += [
        "", "## 汇总", "",
        f"- ✅ 合格：{totals.get('合格', 0)}",
        f"- ❌ 需返修：{totals.get('需返修', 0)}",
        f"- ⚠️  无法解析：{totals.get('无法解析', 0)}",
    ]
    if skipped:
        lines += ["", "## ⏭️ 跳过（--resume，已有产物）", ""]
        lines += [f"- {t}" for t in skipped]
    if failures:
        lines += ["", "## ❌ 失败（运行时异常，未中断整批）", "",
                  "| 题目 | 错误 |", "|---|---|"]
        lines += [f"| {tid} | {err} |" for tid, err in failures]
    p = base_out / "_last_run_summary.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _run_bundles(bundles: list[WildTaskBundle], base_out: Path,
                 use_semantic: bool, use_dry_run: bool,
                 resume: bool = False) -> list[TaskReport]:
    base_out.mkdir(parents=True, exist_ok=True)
    reports: list[TaskReport] = []
    run_dirs: list[Path] = []
    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    any_semantic = any_dry = False

    for b in bundles:
        if resume and has_successful_output(base_out, b.task_id, "result.json"):
            print(f"  ⏭️ 跳过(已完成)：{b.task_id}")
            skipped.append(b.task_id)
            continue
        print(f"  正在质检：{b.task_id} ...", end=" ", flush=True)
        try:
            report, semantic_ran, dry_ran = inspect_one(b, use_semantic, use_dry_run)
            any_semantic = any_semantic or semantic_ran
            any_dry = any_dry or dry_ran
            run_dir = make_task_run_dir(base_out, b.task_id)
            write_reports(report, b, run_dir, semantic_ran, dry_ran)
            run_dirs.append(run_dir)
            cc = report.counts()
            icon = "✅" if report.verdict == "合格" else ("⚠️" if report.verdict == "无法解析" else "❌")
            print(f"{icon} [{report.verdict}] B{cc['blocker']} M{cc['major']} m{cc['minor']}"
                  f"  → {run_dir.relative_to(base_out)}/")
            reports.append(report)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            print(f"❌ 失败（已跳过，不中断整批）：{err}")
            failures.append((b.task_id, err))
            continue

    _write_run_summary(base_out, bundles, reports, run_dirs,
                       any_semantic, any_dry, failures, skipped)
    print(f"\n📁 输出根目录：{base_out}")
    print(f"   ├─ _last_run_summary.md       （最近一次运行总览）")
    print(f"   └─ <task_id>/<YYYYMMDD>[_n]/   （每题每次运行：<结论>_质检报告.md + result.json）")
    if failures:
        print(f"\n⚠️  {len(failures)} 题失败（见摘要「失败」区）；{len(skipped)} 题跳过。")
    return reports


# ─────────────────────────────────────────────────────────────────────────────
# 目录识别
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_task_md(p: Path) -> bool:
    """是否是一个候选 task.md：.md 文件且含 YAML frontmatter。"""
    if p.suffix.lower() != ".md" or not p.is_file():
        return False
    if p.name.startswith("_") or p.name in ("README.md",) or "规范" in p.name or "说明" in p.name or "总结" in p.name:
        return False
    try:
        head = p.read_text(encoding="utf-8-sig")[:200]
    except Exception:  # noqa: BLE001
        return False
    return head.lstrip().startswith("---")


def _collect_task_mds(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if _looks_like_task_md(target) else []
    found: list[Path] = []
    for p in sorted(target.rglob("*.md")):
        if "__pycache__" in str(p) or "/.git/" in str(p):
            continue
        if _looks_like_task_md(p):
            found.append(p)
    return found


# ─────────────────────────────────────────────────────────────────────────────
# 扫描模式
# ─────────────────────────────────────────────────────────────────────────────

def _scan_mode(target: Path, out_dir: Path, use_semantic: bool, use_dry_run: bool,
               resume: bool = False) -> None:
    print(f"\n🔍 正在扫描：{target}\n")
    mds = _collect_task_mds(target)
    if not mds:
        print("❌ 未找到任何含 YAML frontmatter 的 task.md。")
        sys.exit(1)

    valid: list[tuple[int, WildTaskBundle]] = []
    invalid: list[tuple[Path, str]] = []
    for md in mds:
        b = resolve_wild_task(md)
        if b.resolver_status == ResolverStatus.UNRESOLVABLE:
            invalid.append((md, "; ".join(b.resolver_warnings)))
        else:
            valid.append((len(valid) + 1, b))

    print(f"找到 {len(valid)} 个可质检题目（{len(invalid)} 个无法解析）\n")
    if invalid:
        print("── 无法解析（仍可质检并报 Blocker）─────────────────")
        for p, reason in invalid:
            print(f"  ✗  {p.name[:55]:55}  {reason[:60]}")
        print()

    # 无法解析的也纳入（它们会得到"无法解析"报告）；这里只让用户从可解析里选，
    # 但若全是无法解析，也允许直接跑全部。
    all_bundles = [b for _, b in valid] + [resolve_wild_task(p) for p, _ in invalid]

    if not valid and invalid:
        print("全部题目无法解析，将直接为它们生成「无法解析」报告。")
        out_dir.mkdir(parents=True, exist_ok=True)
        _run_bundles(all_bundles, out_dir, use_semantic, use_dry_run, resume)
        return

    print("── 可质检列表 ──────────────────────────────────")
    for idx, b in valid:
        flag = " ⚠" if b.resolver_status == ResolverStatus.WARN else "  "
        print(f"  {idx:>3}.{flag} {b.task_id}")
    print()
    print(f"  a.   全部质检（共 {len(valid)} 题）")
    print()

    selected = _prompt_selection(valid)
    if not selected:
        print("已取消。")
        sys.exit(0)

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n输出基础目录：{out_dir}\n")
    print("── 开始质检 ─────────────────────────────────────")
    _run_bundles(selected, out_dir, use_semantic, use_dry_run, resume)


def _prompt_selection(valid: list[tuple[int, WildTaskBundle]]) -> list[WildTaskBundle]:
    max_idx = valid[-1][0]
    idx_map = {idx: b for idx, b in valid}
    while True:
        try:
            raw = input("请输入要质检的题目编号（多个用空格/逗号分隔，输入 a 全跑，q 退出）：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if not raw:
            continue
        if raw.lower() == "q":
            return []
        if raw.lower() == "a":
            return [b for _, b in valid]
        tokens = re.split(r"[\s,，]+", raw)
        chosen, bad = [], []
        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            try:
                n = int(tok)
                if n in idx_map:
                    if idx_map[n] not in chosen:
                        chosen.append(idx_map[n])
                else:
                    bad.append(tok)
            except ValueError:
                bad.append(tok)
        if bad:
            print(f"  ⚠ 无效编号：{bad}，有效范围 1-{max_idx}，请重新输入。")
            continue
        if chosen:
            print(f"\n已选择 {len(chosen)} 题：{', '.join(b.task_id for b in chosen)}\n")
            return chosen


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="WildClaw 转换产物质检框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 质检单个 task.md
  python3 run_qc_wild.py wildclaw_converted/tasks/Investigative_Analysis/xxx.md

  # 质检 tasks/ 下所有题（离线，仅确定性 + 干跑）
  python3 run_qc_wild.py wildclaw_converted/tasks --no-semantic

  # 扫描模式
  python3 run_qc_wild.py -s wildclaw_converted/tasks
        """,
    )
    ap.add_argument("target", help="task.md 文件、分类目录或 tasks/ 根目录")
    ap.add_argument("-s", "--scan", action="store_true",
                    help="扫描模式：列出所有题目，交互选择后执行")
    ap.add_argument("--out", default=None, help="报告输出根目录（默认 qc_wild/output）")
    ap.add_argument("--no-semantic", action="store_true",
                    help="跳过 LLM 语义层，只跑确定性 + 干跑")
    ap.add_argument("--no-dry-run", action="store_true",
                    help="跳过 grade() 子进程冒烟测试")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：跳过输出目录里已有成功产物（result.json）的题")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not target.exists():
        print(f"❌ 路径不存在：{target}")
        sys.exit(1)

    out_dir = Path(args.out) if args.out else Path(__file__).parent / "qc_wild" / "output"
    use_semantic = not args.no_semantic
    use_dry_run = not args.no_dry_run

    if args.scan:
        _scan_mode(target, out_dir, use_semantic, use_dry_run, args.resume)
        return

    mds = _collect_task_mds(target)
    if not mds:
        print(f"❌ 未找到可质检的 task.md：{target}")
        print("   （需要 .md 文件且以 YAML frontmatter `---` 开头）")
        sys.exit(1)

    bundles = [resolve_wild_task(md) for md in mds]
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"共 {len(bundles)} 题，输出基础目录：{out_dir}\n")
    _run_bundles(bundles, out_dir, use_semantic, use_dry_run, args.resume)


if __name__ == "__main__":
    sys.exit(main())
