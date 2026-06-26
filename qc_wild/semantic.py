"""
semantic.py —— 语义层质检（LLM agentic 探索）

复用旧框架的 qc.ai_api（call_ai_agentic 工具循环）+ qc.explorer（只读文件沙箱）。
沙箱根 = repo_root（wildclaw_converted），模型据此既能读 tasks/<Cat>/<id>.md，
也能读 workspace/<Cat>/<short>/ 的原始素材。

核查（确定性层判不了、需要语义理解的）：
  - 覆盖性：grade 的 check 是否覆盖 Prompt 要求的全部交付物与要点。
  - 事实正确性：grade 里写死的字面量（"14.00"/"2022-08-01" 等）是否与 workspace
    原始文件一致——grep workspace 求证（workspace 是唯一事实源，verified_facts 已不存在）。
  - 翻译保真 / 关键词脆弱 / 捷径作弊：check 是否只认一种写法、能否被硬编码关键词绕过。
  - judge 忠实：criterion/anchor 是否说清分档、是否真调模型、是否 0–1 归一。
  - overall_score 合成：是否等权平均（或块均值）覆盖全部键（标注规范 §3.3）。

AI 未配置时整体跳过（返回 [], [], False），不影响确定性层。
"""

from __future__ import annotations

from pathlib import Path

import config
from qc import ai_api, explorer
from .models import Defect, Layer, Severity, WildTaskBundle
from .parsing import TaskMd


# 复用旧框架的结构化缺陷 schema（severity/summary/evidence/confidence）
_DEFECT_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "defects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "summary": {"type": "string"},
                    "detail": {"type": "string"},
                    "principle": {"type": "string"},
                    "evidence": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "confidence": {
                        "type": "string", "enum": ["high", "medium", "low"],
                        "description": "high=有明确文件证据；medium=证据较强但有解读空间；"
                                       "low=主观判断、缺可追溯硬证据（转人工复核，不自动定责）。",
                    },
                },
                "required": ["severity", "summary", "evidence"],
            },
        },
        "good_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["defects"],
}

_EXPLORE_SYSTEM = (
    "你是 WildClawBench 转换题质检员。审查对象是一道【转换后的单文件 task.md】"
    "（frontmatter + ## Prompt + ## Automated Checks 内联的 def grade + "
    "## Workspace Path/Env/Warmup）及其配套 workspace/ 数据目录。\n\n"
    "你拥有只读文件工具（list_dir / read_file / grep），沙箱根是仓库根，其下有 "
    "tasks/<分类>/<id>.md（题面+评分逻辑全在这一个文件里）和 "
    "workspace/<分类>/<短名>/（被分析的真实素材：代码库/数据集/文档，是质检的第一手事实来源）。\n\n"
    "工作方式：先读 task.md 看懂 Prompt 要交付什么、grade 怎么判；再 list_dir(workspace) "
    "看清真实素材；遇到 grade 里写死的数值/字段/路径（如某个价格、API 版本、列名），"
    "回 workspace 原始文件 grep 求证。不要臆测，所有结论落到你实际读到的文件证据。\n\n"
    "只质检【内容与语义】是否正确、穷举、能区分好坏答案；不查 markdown 语法风格。\n"
    "宁缺毋滥：没把握、找不到文件证据的不要报。每条缺陷给 confidence：读到明确证据标 high；"
    "证据较强但有解读空间标 medium；推断/主观标 low（low 转人工复核，不自动判返修）。"
)


def _emit(raw: dict, counter: dict) -> tuple[list[Defect], list[str]]:
    defects: list[Defect] = []
    for item in raw.get("defects", []):
        counter["n"] = counter.get("n", 0) + 1
        sev = item.get("severity", "minor")
        conf = item.get("confidence", "high")
        if conf not in ("high", "medium", "low"):
            conf = "high"
        defects.append(Defect(
            id=f"SEM-{counter['n']}",
            layer=Layer.L5, category="semantic",
            severity=Severity(sev) if sev in ("blocker", "major", "minor") else Severity.MINOR,
            summary=item.get("summary", ""),
            detail=item.get("detail", ""),
            principle=item.get("principle", ""),
            evidence=item.get("evidence", ""),
            suggestion=item.get("suggestion", ""),
            confidence=conf,
        ))
    return defects, raw.get("good_points", []) or []


def run_semantic(bundle: WildTaskBundle) -> tuple[list[Defect], list[str], bool]:
    """运行语义层。返回 (缺陷, 良好项, 是否实际运行)。AI 未配置时 ([], [], False)。"""
    if not ai_api.is_ai_available():
        return [], [], False

    tm = TaskMd.load(bundle.task_md_path)

    # task.md 相对沙箱根的路径（让模型知道去哪读题）
    try:
        md_rel = str(Path(bundle.task_md_path).resolve().relative_to(Path(bundle.task_root).resolve()))
    except ValueError:
        md_rel = bundle.task_md_path
    ws_rel = ""
    if bundle.workspace_dir:
        try:
            ws_rel = str(Path(bundle.workspace_dir).resolve().relative_to(Path(bundle.task_root).resolve()))
        except ValueError:
            ws_rel = bundle.workspace_dir

    census = explorer.census_workspace(bundle.workspace_dir) if bundle.workspace_dir else {"count": 0, "buckets": {}}
    need_reads = explorer.required_read_count(census.get("count", 0))
    bucket_summary = ", ".join(f"{k}:{len(v)}" for k, v in census.get("buckets", {}).items()) or "（空）"

    user_msg = (
        "# 待质检的转换题\n"
        f"题面+评分逻辑单文件：`{md_rel}`\n"
        f"配套数据目录：`{ws_rel or '（未解析到）'}`（共 {census.get('count', 0)} 个可读文件，类型 {bucket_summary}）\n\n"
        "# task.md 全文\n"
        f"{tm.raw[:14000]}\n\n"
        "请按【三阶段工作流】探索后再核查：\n"
        "■ 阶段A：先 read_file 完整读 task.md（尤其 ## Automated Checks 的 grade），再 "
        "list_dir(path 指向 workspace, recursive=true) 拿到 workspace 全貌。\n"
        "■ 阶段B：对 workspace 候选文件先 read_file(小 limit) 读头部或 grep 分诊，搞清各文件作用。\n"
        f"■ 阶段C：深读与 grade 写死值相关的文件求证。本题 workspace 共 {census.get('count', 0)} 个文件，"
        f"**至少实际 read_file {need_reads} 个**，数据/代码/文档（存在的类型）每类至少碰一个；"
        "读完一批可再 list 回看。\n\n"
        "重点核查：\n"
        "1. 覆盖性：对照 ## Prompt 要求交付的产物与要点，grade 的 check 是否都覆盖到？漏判报 major/blocker。\n"
        "2. 事实正确性：grade 写死的数值/字段/路径以 workspace 原始文件为准，grep/read 求证；不一致报 blocker。\n"
        "3. 翻译保真 & 排斥合理解法 & 捷径作弊：check 是否忠实、是否只认一种写法、能否被关键词绕过？报 major。\n"
        "4. judge 忠实：criterion/anchor 是否说清 0–1 分档、是否真调模型、是否归一？报 major。\n"
        "5. overall_score 合成：是否覆盖全部键等权平均/块均值？偏离报 major。\n"
        "每条缺陷的 evidence 必须写明你读到的 文件:行号 或 section 名。\n\n"
        "未完成必要探索（读过 task.md / 递归全貌 / 最少读取数 / 类型覆盖）时不要急着下结论——闸门会拦你回去。"
    )

    def _on_step(rnd, name, args):
        loc = args.get("path") or args.get("pattern") or ""
        rec = " recursive" if args.get("recursive") else ""
        print(f"[探索 r{rnd+1}] {name}({loc}{rec})", flush=True)

    # task.md 必读（业务附加项）：read_file 碰过 task.md
    def _read_taskmd(history):
        return any(n == "read_file" and (".md" in str(a.get("path", "")))
                   for n, a in history)

    extra = [(f"用 read_file 完整读取 task.md（{md_rel}）", _read_taskmd)]
    if bundle.workspace_dir:
        _finish_gate = explorer.make_exploration_gate(census, extra_required=extra)
    else:
        # 无 workspace：仍要求读过 task.md
        def _finish_gate(history):
            return None if _read_taskmd(history) else (
                f"请先用 read_file 完整读取 task.md（{md_rel}），再下结论。")

    executor = explorer.make_executor(bundle.task_root)
    try:
        raw = ai_api.call_ai_agentic(
            user_msg, system=_EXPLORE_SYSTEM,
            tools=explorer.EXPLORER_TOOLS, tool_executor=executor,
            schema=_DEFECT_LIST_SCHEMA, max_rounds=config.EXPLORE_MAX_ROUNDS, max_tokens=config.QCWILD_SEMANTIC_MAX_TOKENS,
            on_step=_on_step, finish_gate=_finish_gate, max_gate_nudges=config.GATE_MAX_NUDGES,
        )
    except ai_api.AINotConfiguredError:
        return [], [], False
    except Exception as e:  # noqa: BLE001
        return [], [f"语义层探索未完成（{type(e).__name__}: {e}）"], True

    counter: dict = {}
    defects, good = _emit(raw, counter)
    return defects, good, True
