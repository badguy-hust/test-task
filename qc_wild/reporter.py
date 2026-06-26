"""
reporter.py —— qc_wild 报告生成

复用旧 qc.reporter 的渲染原子（_render_defect_section / _sev_rank / build_summary），
只重排章节以贴合 WildClaw 单文件格式：
  ⭐ 一句话结论 / 0 题面摘录 / 1 概览 / 2 格式契约 / 3 grade(Automated Checks) /
  4 judge / 5 workspace 一致性 / 6 待人工复核 / 7 良好项 / 8 问题汇总表

verdict 三态（合格/需返修/无法解析）+ 文件名前缀沿用旧框架习惯。
"""

from __future__ import annotations

import json
from pathlib import Path

from qc.models import TaskReport, Defect, Severity
from qc.reporter import _render_defect_section, _sev_rank, build_summary, SEV_ICON
from .models import WildTaskBundle
from .parsing import TaskMd


def _defects_by_category(report: TaskReport, cats: list[str],
                         exclude_ids: set | None = None) -> list[Defect]:
    exclude_ids = exclude_ids or set()
    return [d for d in report.defects
            if d.category in cats and id(d) not in exclude_ids]


def _prompt_excerpt(bundle: WildTaskBundle) -> str:
    try:
        tm = TaskMd.load(bundle.task_md_path)
        p = tm.prompt or tm.raw[:800]
        return p.strip()[:1200]
    except Exception:  # noqa: BLE001
        return "_(task.md 读取失败)_"


def _rel(path: str | None, bundle: WildTaskBundle) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).relative_to(bundle.repo_root or bundle.task_root))
    except ValueError:
        return Path(path).name


def render_markdown(report: TaskReport, bundle: WildTaskBundle,
                    semantic_ran: bool, dry_run_ran: bool) -> str:
    L: list[str] = []
    review_defects = report.needs_review_defects()
    review_ids = set(id(d) for d in review_defects)

    L.append(f"# {report.task_id} 质检报告\n")
    L.append("> 质检对象：转换后的 WildClaw 单文件 task.md（frontmatter + Prompt + Automated Checks + Workspace/Env/Warmup）")
    L.append("> 质检依据：WildClawBench 引擎契约（task_parser / grading）+ 转换标注规范")
    L.append(f"> 语义层(LLM)：{'已运行' if semantic_ran else '未运行（AI 未配置或 --no-semantic）'}")
    L.append(f"> grade 干跑：{'已运行' if dry_run_ran else '未运行（--no-dry-run）'}\n")
    L.append("---\n")

    L.append("## ⭐ 一句话结论\n")
    L.append(f"> {build_summary(report, use_llm=semantic_ran)}\n")
    L.append("---\n")

    L.append("## 0. 题面摘录（## Prompt）\n")
    L.append("> " + _prompt_excerpt(bundle).replace("\n", "\n> ") + "\n")
    L.append("---\n")

    c = report.counts()
    L.append("## 1. 题目概览\n")
    L.append("| 项 | 内容 |")
    L.append("|---|---|")
    L.append(f"| Resolver 状态 | {report.resolver_status.value} |")
    L.append(f"| task.md | {_rel(bundle.task_md_path, bundle)} |")
    L.append(f"| 分类 | {bundle.category} |")
    L.append(f"| workspace | {_rel(bundle.workspace_dir, bundle) or '（未解析到）'} |")
    L.append(f"| 缺陷统计 | 🔴Blocker {c['blocker']} · 🟠Major {c['major']} · 🟡Minor {c['minor']} |")
    if review_defects:
        L.append(f"| 待人工复核 | 🔍 {len(review_defects)} 条低置信度高危结论（不计入自动判定） |")
    L.append(f"| **总体结论** | **{report.verdict}** |")
    if bundle.resolver_warnings:
        L.append(f"| Resolver 警告 | {'; '.join(bundle.resolver_warnings)} |")
    L.append("")
    L.append("---\n")

    L.append("## 2. 格式契约（frontmatter / ## section / Prompt 路径 / Env）\n")
    _render_defect_section(L, _defects_by_category(report, ["format", "structure"], review_ids))
    L.append("")

    L.append("## 3. grade（## Automated Checks）质检\n")
    _render_defect_section(L, _defects_by_category(report, ["grade"], review_ids))
    L.append("")

    L.append("## 4. judge 质检\n")
    _render_defect_section(L, _defects_by_category(report, ["judge"], review_ids))
    L.append("")

    L.append("## 5. workspace 一致性\n")
    _render_defect_section(L, _defects_by_category(report, ["workspace"], review_ids))
    L.append("")

    L.append("## 6. 语义层发现\n")
    sem = _defects_by_category(report, ["semantic"], review_ids)
    if not semantic_ran:
        L.append("_语义层未运行。_\n")
    else:
        _render_defect_section(L, sem)
    L.append("")

    L.append("## 6.5 待人工复核（低置信度高危结论）\n")
    if review_defects:
        L.append("> 以下结论 LLM 标为低置信度（缺乏可追溯硬证据），**不计入自动需返修判定**，"
                 "请人工确认后再决定。\n")
        _render_defect_section(L, review_defects)
    else:
        L.append("_无低置信度待复核结论。_\n")
    L.append("")

    if report.good_points:
        L.append("## 7. 良好项\n")
        for g in report.good_points:
            L.append(f"- ✅ {g}")
        L.append("")

    L.append("## 8. 问题汇总表\n")
    if report.defects:
        L.append("| 编号 | 层 | 类别 | 级别 | 置信度 | 一句话 | 对应原则 |")
        L.append("|---|---|---|---|---|---|---|")
        for d in sorted(report.defects, key=lambda x: _sev_rank(x.severity)):
            icon = SEV_ICON.get(d.severity.value, "")
            dyn = " 〔可动态证实〕" if d.verifiable_by_dynamic else ""
            conf = getattr(d, "confidence", "high")
            conf_cell = "🔍待复核" if (conf == "low" and d.severity.value in ("blocker", "major")) else conf
            L.append(f"| {d.id} | {d.layer.value} | {d.category} | {icon}{d.severity.value} | "
                     f"{conf_cell} | {d.summary}{dyn} | {d.principle} |")
    else:
        L.append("_无缺陷。_")
    L.append("")
    L.append(f"**统计：Blocker {c['blocker']} · Major {c['major']} · Minor {c['minor']}**")

    return "\n".join(L)


def write_reports(report: TaskReport, bundle: WildTaskBundle, task_run_dir: str | Path,
                  semantic_ran: bool, dry_run_ran: bool) -> tuple[Path, Path]:
    """写入【单题本次运行目录】（<framework>/output/<task_id>/<YYYYMMDD>_<n>/）。
    文件名用固定名（结论已体现在内容里）：质检报告.md + result.json。"""
    out = Path(task_run_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"{report.verdict}_质检报告.md"
    json_path = out / "result.json"
    md_path.write_text(
        render_markdown(report, bundle, semantic_ran, dry_run_ran), encoding="utf-8"
    )
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return md_path, json_path
