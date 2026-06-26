"""
reporter.py —— 报告生成

产出：
  - 每题一份 Markdown 质检报告（沿用已验证的章节模板）
  - 每题一份机器可读 JSON
  - 跨题 rubrics.json 问题纵览表（Markdown）
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import TaskReport, Defect, Severity, Layer, TaskBundle
from .parsing import TaskMd

SEV_ICON = {"blocker": "🔴", "major": "🟠", "minor": "🟡", "info": "ℹ️"}


# ─────────────────────────────────────────────────────────────────────────────
# 方向(3)：一段话摘要（电梯结论）
# ─────────────────────────────────────────────────────────────────────────────

def _template_summary(report: TaskReport) -> str:
    """离线降级版：用确定的事实拼一段简明结论，不需要 AI。"""
    c = report.counts()
    if report.resolver_status.value == "UNRESOLVABLE":
        return "本题缺少 rubrics.json / grade.py 等必需评分产物，无法执行质检。"
    if not report.defects:
        return "本题评分产物未发现 Blocker / Major / Minor 级问题，可投入评测。"

    blockers = [d for d in report.defects if d.severity == Severity.BLOCKER]
    majors = [d for d in report.defects if d.severity == Severity.MAJOR]
    verdict_word = "需返修" if report.verdict == "需返修" else "合格"

    parts = [f"本题结论：**{verdict_word}**（🔴{c['blocker']} 🟠{c['major']} 🟡{c['minor']}）。"]
    if blockers:
        top = blockers[0]
        parts.append(f"最严重问题为 {top.id}：{top.summary}。")
        if len(blockers) > 1:
            parts.append(f"另有 {len(blockers)-1} 个 Blocker。")
    elif majors:
        top = majors[0]
        parts.append(f"主要问题为 {top.id}：{top.summary}。")
    # 问题主要集中在哪类
    cats: dict[str, int] = {}
    for d in report.defects:
        if d.severity in (Severity.BLOCKER, Severity.MAJOR):
            cats[d.category] = cats.get(d.category, 0) + 1
    if cats:
        focus = max(cats, key=cats.get)
        parts.append(f"问题主要集中在 {focus}。建议优先修复 Blocker 后重新质检。")
    return " ".join(parts)


def _llm_summary(report: TaskReport) -> str | None:
    """有 AI 时生成更像人话的根因总结；失败/未配置返回 None。"""
    try:
        from . import ai_api
        if not ai_api.is_ai_available():
            return None
    except Exception:  # noqa: BLE001
        return None

    defects_brief = [
        {"id": d.id, "severity": d.severity.value, "category": d.category,
         "summary": d.summary, "detail": d.detail[:300]}
        for d in sorted(report.defects, key=lambda x: _sev_rank(x.severity))[:20]
    ]
    if not defects_brief:
        return None
    prompt = (
        f"# 质检对象\n题目 {report.task_id}，结论：{report.verdict}\n\n"
        f"# 发现的缺陷（按严重度排序）\n"
        f"{json.dumps(defects_brief, ensure_ascii=False, indent=2)}\n\n"
        "请用 2-4 句中文写一段'电梯结论'，让审阅者不看下面的详细表格也能抓住要点：\n"
        "1. 这题最核心的问题是什么、根因在哪（不要罗列所有缺陷，抓主要矛盾）；\n"
        "2. 几个问题之间有没有关联（比如同一个根因导致多条）；\n"
        "3. 给出优先修复建议。\n"
        "直接输出这段话，不要加标题、不要 markdown 列表。"
    )
    try:
        from . import ai_api
        return ai_api.call_ai(
            prompt,
            system="你是 HiClaw 质检报告撰写助手，擅长把一堆缺陷条目浓缩成抓住主要矛盾的一段话。",
            max_tokens=512,
        ).strip()
    except Exception:  # noqa: BLE001
        return None


def build_summary(report: TaskReport, use_llm: bool = True) -> str:
    """返回报告顶部的一段话摘要：优先 LLM，失败降级模板。"""
    if use_llm:
        s = _llm_summary(report)
        if s:
            return s
    return _template_summary(report)


def _prompt_excerpt(bundle: TaskBundle) -> str:
    if not bundle.files.task_md:
        return "_(本题无 task.md)_"
    try:
        tm = TaskMd.load(bundle.files.task_md)
        p = tm.prompt or tm.text[:800]
        return p.strip()
    except Exception:  # noqa: BLE001
        return "_(task.md 读取失败)_"


def _defects_by_category(report: TaskReport, cats: list[str],
                         exclude_ids: set | None = None) -> list[Defect]:
    exclude_ids = exclude_ids or set()
    return [d for d in report.defects
            if d.category in cats and id(d) not in exclude_ids]


def render_markdown(report: TaskReport, bundle: TaskBundle, semantic_ran: bool) -> str:
    L = []
    # 待人工复核的低置信度高危项：从常规分类章节中剔除，单独成节，避免重复展示
    review_defects = report.needs_review_defects()
    review_ids = set(id(d) for d in review_defects)
    L.append(f"# {report.task_id} 质检报告\n")
    L.append("> 质检对象：rubrics.json / grade.py / judge.py（+task.md 对照）")
    L.append("> 质检依据：HiClaw 质检规范（JSON 版 rubric 只查内容语义）")
    L.append("> 评分权威：gt/rubrics.json + grade.py")
    L.append(f"> 语义层(LLM)：{'已运行' if semantic_ran else '未运行（AI 接口未配置，仅确定性层结果）'}\n")
    L.append("---\n")

    # ⭐ 一段话摘要（电梯结论）—— 方向(3)
    L.append("## ⭐ 一句话结论\n")
    L.append(f"> {build_summary(report, use_llm=semantic_ran)}\n")
    L.append("---\n")

    # 0. 题目原文
    L.append("## 0. 题目原文（task.md Prompt 摘录）\n")
    L.append("> " + _prompt_excerpt(bundle).replace("\n", "\n> ") + "\n")
    L.append("---\n")

    # 1. 概览
    c = report.counts()
    L.append("## 1. 题目概览\n")
    L.append("| 项 | 内容 |")
    L.append("|---|---|")
    L.append(f"| Resolver 状态 | {report.resolver_status.value} |")
    L.append(f"| rubrics.json | {_rel(bundle.files.rubrics_json, bundle)} |")
    L.append(f"| grade.py | {_rel(bundle.files.grade_py, bundle)} |")
    L.append(f"| judge.py | {_rel(bundle.files.judge_py, bundle) or '（无）'} |")
    L.append(f"| 缺陷统计 | 🔴Blocker {c['blocker']} · 🟠Major {c['major']} · 🟡Minor {c['minor']} |")
    review = report.needs_review_defects()
    if review:
        L.append(f"| 待人工复核 | 🔍 {len(review)} 条低置信度高危结论（不计入自动判定） |")
    L.append(f"| **总体结论** | **{report.verdict}** |")
    if bundle.resolver_warnings:
        L.append(f"| Resolver 警告 | {'; '.join(bundle.resolver_warnings)} |")
    L.append("")
    L.append("---\n")

    # 2. rubrics.json
    L.append("## 2. rubrics.json 内容质检\n")
    _render_defect_section(L, _defects_by_category(report, ["rubrics.json"], review_ids))
    L.append("")

    # 3. grade.py
    L.append("## 3. grade.py 质检\n")
    _render_defect_section(L, _defects_by_category(report, ["grade.py"], review_ids))
    L.append("")

    # 4. judge.py
    L.append("## 4. judge.py 质检\n")
    jd = _defects_by_category(report, ["judge.py"], review_ids)
    if not bundle.files.judge_py:
        L.append("_本题无 judge.py（纯 programmatic）。_\n")
    _render_defect_section(L, jd)
    L.append("")

    # 5. task.md 一致性 + 结构
    L.append("## 5. task.md 一致性 / 结构\n")
    _render_defect_section(L, _defects_by_category(report, ["task.md", "structure"], review_ids))
    L.append("")

    # 6. 陷阱覆盖矩阵
    L.append("## 6. 陷阱覆盖矩阵\n")
    trap_defs = _defects_by_category(report, ["trap"], review_ids)
    if report.trap_matrix:
        L.append("| 陷阱 | 类型 | 覆盖 rubric | 有牙齿 |")
        L.append("|---|---|---|---|")
        for t in report.trap_matrix:
            L.append(f"| {t.get('description','')[:40]} | {t.get('trap_type','')} | "
                     f"{', '.join(t.get('covering_rubric',[])) or '—'} | {t.get('has_teeth','')} |")
        L.append("")
    elif not semantic_ran:
        L.append("_语义层未运行，陷阱矩阵不可用。_\n")
    else:
        L.append("_未识别到需覆盖的陷阱，或无环境数据可分析。_\n")
    _render_defect_section(L, trap_defs)
    L.append("")

    # 6.5 待人工复核（低置信度高危结论）—— 方向(3)
    L.append("## 6.5 待人工复核（低置信度高危结论）\n")
    if review_defects:
        L.append("> 以下结论 LLM 标为低置信度（缺乏可追溯的硬证据），**不计入自动需返修判定**，"
                 "请人工确认后再决定是否返修。\n")
        _render_defect_section(L, review_defects)
    else:
        L.append("_无低置信度待复核结论。_\n")
    L.append("")

    # 7. 良好项
    if report.good_points:
        L.append("## 7. 良好项\n")
        for g in report.good_points:
            L.append(f"- ✅ {g}")
        L.append("")

    # 8. 汇总表
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


def _render_defect_section(L: list[str], defects: list[Defect]) -> None:
    if not defects:
        L.append("_无问题。_\n")
        return
    for d in sorted(defects, key=lambda x: _sev_rank(x.severity)):
        icon = SEV_ICON.get(d.severity.value, "")
        dyn = " 〔可动态证实〕" if d.verifiable_by_dynamic else ""
        conf = f" 〔置信度:{d.confidence}〕" if getattr(d, "confidence", "high") != "high" else ""
        L.append(f"#### {d.id} {icon}{d.severity.value}：{d.summary}{dyn}{conf}")
        if d.detail:
            L.append(f"- **现象/影响**：{d.detail}")
        if d.rubric_ref:
            L.append(f"- **关联 rubric**：{', '.join(d.rubric_ref)}")
        if d.evidence:
            L.append(f"- **证据**：{d.evidence}")
        if d.principle:
            L.append(f"- **违反原则**：{d.principle}")
        if d.suggestion:
            L.append(f"- **建议**：{d.suggestion}")
        L.append("")


def _sev_rank(sev: Severity) -> int:
    return {"blocker": 0, "major": 1, "minor": 2, "info": 3}.get(sev.value, 9)


def _rel(path: str | None, bundle: TaskBundle) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).relative_to(bundle.task_root))
    except ValueError:
        return Path(path).name


# ─────────────────────────────────────────────────────────────────────────────
# 纵览表（只关注 rubrics.json 问题）
# ─────────────────────────────────────────────────────────────────────────────

def render_overview(reports: list[TaskReport]) -> str:
    L = []
    L.append("# rubrics.json 问题纵览表\n")
    L.append("> 跨题汇总，仅收录 rubrics.json 相关问题（含陷阱覆盖问题）。\n")
    L.append("| 题目 | 编号 | 级别 | 问题 | 关联 rubric | 原则 |")
    L.append("|---|---|---|---|---|---|")
    total = {"blocker": 0, "major": 0, "minor": 0}
    for r in reports:
        rub_defs = [d for d in r.defects if d.category in ("rubrics.json", "trap")]
        for d in sorted(rub_defs, key=lambda x: _sev_rank(x.severity)):
            icon = SEV_ICON.get(d.severity.value, "")
            if d.severity.value in total:
                total[d.severity.value] += 1
            L.append(f"| {r.task_id} | {d.id} | {icon}{d.severity.value} | {d.summary} | "
                     f"{', '.join(d.rubric_ref) or '—'} | {d.principle} |")
    L.append("")
    L.append(f"**合计 rubrics.json 类问题：Blocker {total['blocker']} · "
             f"Major {total['major']} · Minor {total['minor']}**")
    return "\n".join(L)


def write_reports(report: TaskReport, bundle: TaskBundle, out_dir: str | Path,
                  semantic_ran: bool) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 文件名格式：<结论>_<series>_<task_id>_质检报告.md
    # 例：需返修_batch_2_task_1_github_AutoLLM_ArxivDigest_质检报告.md
    verdict_tag = {"合格": "合格", "需返修": "需返修", "无法解析": "无法解析"}.get(
        report.verdict, report.verdict
    )
    series_tag = bundle.series.value.replace("/", "-")  # TRJ / batch_2
    safe_id = report.task_id.replace(" ", "_")
    stem = f"{verdict_tag}_{series_tag}_{safe_id}"
    md_path   = out / f"{stem}_质检报告.md"
    json_path = out / f"{stem}_result.json"
    md_path.write_text(render_markdown(report, bundle, semantic_ran), encoding="utf-8")
    json_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path
