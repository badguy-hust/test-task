"""
static_checks.py —— 确定性静态检查（零 LLM，可复现）

依据：WildClawBench 引擎契约（task_parser / grading）+ 标注规范 §9 自检单。
覆盖：
  L0  结构：task.md 无法解析 / Workspace Path 缺失 → UNRESOLVABLE
  L1  格式契约：frontmatter 五字段、八个 ## section、Prompt 路径声明与无 ##、
              Env 声明、Skills 留空、id 与文件名一致
  L2  grade 代码：def grade(**kwargs)、返回含 overall_score、run-loop 兜底、
              产物路径、check 函数体雷同、子串误匹配、flaky 源
  L3  judge：真调模型（OpenAI+OPENROUTER+JUDGE_MODEL）、字数桩残留、key 缺失降级
  L4  prompt ↔ workspace：workspace 目录存在 + 空 results/、产物文件名被 grade 引用

只做"规则能确定判定"的项；需要语义理解的留给 semantic.py。
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Defect, Layer, Severity, ResolverStatus, WildTaskBundle
from .parsing import TaskMd, GradeModule, parse_grade_module


# frontmatter 必填五字段（缺 id/timeout 直接影响引擎；name/category/modality 缺则信息丢失）
REQUIRED_FRONTMATTER = ["id", "name", "category", "timeout_seconds", "modality"]
ENGINE_CRITICAL_FM = {"id", "timeout_seconds"}  # 引擎真正取用的两项

# 本批约定应出现的八个 section（顺序不强制；缺关键的报错，缺建议的提示）
REQUIRED_SECTIONS = ["Prompt", "Automated Checks", "Workspace Path"]
EXPECTED_SECTIONS = ["Prompt", "Expected Behavior", "Grading Criteria", "Automated Checks",
                     "Workspace Path", "Skills", "Env", "Warmup"]

# judge 用的环境变量（凡含 judge 的题必须在 ## Env 声明）
JUDGE_ENV_KEYS = ["OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "JUDGE_MODEL"]

# 子串误匹配危险写法（沿用旧框架）
SUBSTRING_TRAP_RE = re.compile(r'["\']\s*(or|and|in|on|at)\s*["\']\s+in\s+\w')
# flaky 源（沿用旧框架）
FLAKY_SOURCE_RE = re.compile(
    r'\b(?:time\.(?:time|monotonic)\b|datetime\.now\b|datetime\.utcnow\b|'
    r'random\.\w+\b|os\.getpid\b)'
)
# 写死的非 /tmp_workspace 绝对路径（产物应落 /tmp_workspace/results/）
SUSPECT_ABS_PATH_RE = re.compile(r'["\'](/(?!tmp_workspace)[A-Za-z][\w./-]+)["\']')
# Prompt 里点名的产物文件
DELIVERABLE_RE = re.compile(r'`?(results/[\w./-]+\.\w+|[\w-]+\.(?:xlsx|docx|json|csv|md|pdf|txt))`?')


class _Counter:
    def __init__(self):
        self.n: dict[str, int] = {}

    def next(self, prefix: str) -> str:
        self.n[prefix] = self.n.get(prefix, 0) + 1
        return f"{prefix}-{self.n[prefix]}"


# ─────────────────────────────────────────────────────────────────────────────
# L1 格式契约
# ─────────────────────────────────────────────────────────────────────────────

def check_format_contract(bundle: WildTaskBundle, tm: TaskMd,
                          c: _Counter) -> tuple[list[Defect], list[str]]:
    out: list[Defect] = []
    good: list[str] = []
    md_path = bundle.task_md_path

    # 1. frontmatter 五字段
    fm = tm.frontmatter or {}
    missing_fm = [k for k in REQUIRED_FRONTMATTER if k not in fm or str(fm.get(k, "")).strip() == ""]
    if missing_fm:
        eng_missing = [k for k in missing_fm if k in ENGINE_CRITICAL_FM]
        sev = Severity.BLOCKER if eng_missing else Severity.MINOR
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=sev,
            summary=f"frontmatter 缺字段：{missing_fm}",
            detail="引擎 parse_task_md 取用 id/timeout_seconds；name/category/modality 缺失会丢失题目识别信息。"
                   + ("（含引擎必需字段）" if eng_missing else ""),
            principle="格式合规(frontmatter 五字段)",
            evidence=md_path,
        ))
    else:
        good.append("frontmatter 五字段齐全（id/name/category/timeout_seconds/modality）")
        # modality 本批应为 pure-text
        if str(fm.get("modality", "")).strip() != "pure-text":
            out.append(Defect(
                id=c.next("FMT"), layer=Layer.L1, category="format",
                severity=Severity.MINOR,
                summary=f"modality 非 pure-text（本批分析题约定 pure-text）：{fm.get('modality')}",
                evidence=md_path,
            ))

    # 2. id 与文件名一致（建议）
    stem = Path(md_path).stem
    if fm.get("id") and str(fm["id"]).strip() != stem:
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.MINOR,
            summary=f"frontmatter id 与文件名不一致：id={fm['id']} vs 文件名={stem}",
            principle="id 建议与文件名一致",
            evidence=md_path,
        ))

    # 3. 必需 section
    missing_sec = [s for s in REQUIRED_SECTIONS if s not in tm.sections]
    if missing_sec:
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.BLOCKER,
            summary=f"缺少必需 ## section：{missing_sec}",
            detail="Prompt / Automated Checks / Workspace Path 缺任一，题目无法被引擎正常评测。",
            principle="格式合规(必需 section)",
            evidence=md_path,
        ))
    # 建议 section（仅提示）
    missing_exp = [s for s in EXPECTED_SECTIONS if s not in tm.sections]
    advisory = [s for s in missing_exp if s not in REQUIRED_SECTIONS]
    if advisory:
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.MINOR,
            summary=f"缺少建议 ## section：{advisory}",
            detail="Env/Warmup 缺失可能导致 judge 拿不到 key 或容器缺依赖；Expected Behavior/Grading Criteria 便于审阅。",
            evidence=md_path,
        ))

    # 4. Prompt 正文不得含 ##（引擎按 ## 切区块，会截断 Prompt）
    #    注：解析已经按 ## 切过，所以 tm.prompt 内不会再有 ##；这里检查"被截断"的迹象——
    #    即 Prompt section 之后紧跟的、本不该独立的 section（如正文小标题被误当 section）。
    #    更直接：扫描 Prompt 原文里是否有以 ## 起头的行（解析前的 raw 片段）。
    prompt_raw = tm.sections.get("Prompt", "")
    # tm.sections[Prompt] 已是切分后的内容，理论上不含 ##；但若作者用了 ### 是允许的。
    # 真正风险是正文里出现 "## X" 被切走 → 表现为出现了非预期的 section 名。
    unexpected = [s for s in tm.sections if s not in EXPECTED_SECTIONS]
    if unexpected:
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.MAJOR,
            summary=f"出现非预期 ## section（疑似 Prompt 正文内的 ## 被引擎误切为区块）：{unexpected}",
            detail="引擎按 `## 标题` 行切分；Prompt 正文若出现 `##` 会被截断成独立区块。"
                   "需把正文小标题降级为 ### 或更低。",
            principle="Prompt 正文禁含 ##",
            evidence=", ".join(unexpected),
        ))

    # 5. Prompt 路径声明：/tmp_workspace/ 与 results/
    p = tm.prompt or ""
    if "/tmp_workspace/" not in p:
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.MAJOR,
            summary="Prompt 未声明输入路径 `/tmp_workspace/`（模型据此得知 workspace 位置）",
            detail="框架把 workspace 挂到 /tmp_workspace；Prompt 不写明，agent 不知去哪读文件。",
            principle="Prompt 须写明 /tmp_workspace 路径",
            evidence=bundle.task_md_path,
        ))
    if "results/" not in p:
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.MAJOR,
            summary="Prompt 未声明产出路径 `results/`（agent 不知产物落点）",
            principle="Prompt 须写明 results/ 产出路径",
            evidence=bundle.task_md_path,
        ))
    if "/tmp_workspace/" in p and "results/" in p:
        good.append("Prompt 已声明 /tmp_workspace 输入与 results/ 产出路径")

    # 6. Skills 本批应留空
    if tm.skills_raw.strip():
        out.append(Defect(
            id=c.next("FMT"), layer=Layer.L1, category="format",
            severity=Severity.MINOR,
            summary="## Skills 非空（本批纯本地分析题约定留空）",
            detail=f"内容：{tm.skills_raw[:120]}",
            evidence=bundle.task_md_path,
        ))

    return out, good


# ─────────────────────────────────────────────────────────────────────────────
# L2 grade 代码
# ─────────────────────────────────────────────────────────────────────────────

def check_grade_code(gm: GradeModule, c: _Counter) -> tuple[list[Defect], list[str]]:
    out: list[Defect] = []
    good: list[str] = []

    if gm.parse_error:
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.BLOCKER,
            summary=f"## Automated Checks 代码无法解析：{gm.parse_error}",
            detail="引擎会把这段代码拼进 runner 执行，语法错误将导致 grade 直接崩溃、全题 0 分。",
            evidence="## Automated Checks",
        ))
        return out, good

    # 1. def grade 存在且签名 def grade(**kwargs)
    if gm.grade_func is None:
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.BLOCKER,
            summary="未找到 def grade（引擎调用 grade(transcript=..., workspace_path=...) 会失败）",
            evidence="## Automated Checks",
        ))
        return out, good
    if not gm.grade_signature_ok:
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.BLOCKER,
            summary="grade 签名不含 **kwargs（引擎以关键字传 transcript/workspace_path，会 TypeError）",
            detail="引擎固定调用：grade(transcript=_transcript, workspace_path=\"/tmp_workspace\")。",
            principle="grade 契约：def grade(**kwargs)",
            evidence="## Automated Checks",
        ))
    else:
        good.append("grade 签名正确（def grade(**kwargs)）")

    # 2. 返回 dict 且赋值 overall_score
    if not gm.returns_something:
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.BLOCKER,
            summary="grade 没有 return 语句（引擎取不到分数 dict）",
            evidence="## Automated Checks",
        ))
    if not gm.sets_overall_score:
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.BLOCKER,
            summary="grade 未设置 scores[\"overall_score\"]（框架取该键当总分）",
            detail="标注规范硬契约：返回 dict 必须含 overall_score 键。",
            principle="grade 契约：必含 overall_score",
            evidence="## Automated Checks",
        ))
    else:
        good.append("grade 设置了 overall_score 总分键")

    # 3. run-loop 兜底：grade 体内应有 try/except（逐项 check 失败不拖垮整体）
    if not gm.grade_body_has_try:
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.MAJOR,
            summary="grade 体内未见 try/except 兜底（单个 check 读文件失败会让整个 grade 崩溃）",
            detail="标注规范要求每个 check 读不到文件/解析失败一律返回 0.0，不让异常冒出拖垮 grade。",
            principle="每个 check 须 try/except 兜底",
            evidence="## Automated Checks",
            verifiable_by_dynamic=True,
        ))
    else:
        good.append("grade 体内有 try/except 兜底（check 失败不拖垮整体）")

    # 4. 产物路径：出现写死的非 /tmp_workspace 绝对路径
    for m in SUSPECT_ABS_PATH_RE.finditer(gm.source):
        path = m.group(1)
        # 排除常见无害绝对路径（如 judge 的 base_url 不会匹配到这里，因为带 http）
        if path.startswith("/root") or "openclaw" in path:
            continue  # Warmup 风格路径，非产物
        out.append(Defect(
            id=c.next("GRADE"), layer=Layer.L2, category="grade",
            severity=Severity.MAJOR,
            summary=f"grade 出现写死的非 /tmp_workspace 绝对路径：`{path}`",
            detail="产物路径应基于 workspace_path(=/tmp_workspace) 拼 results/；写死别的绝对路径在容器内读不到。",
            principle="产物路径用 /tmp_workspace/results/",
            evidence="## Automated Checks",
        ))
        break  # 报一次即可

    # 5. check 函数体逐字雷同（多条 rubric 永远同生同死，权重虚高）
    check_funcs = gm.check_functions()
    by_body: dict[str, list[str]] = {}
    for name, fi in check_funcs.items():
        key = fi.body_normalized
        if len(key) < 15:
            continue
        by_body.setdefault(key, []).append(name)
    for body, names in by_body.items():
        if len(names) > 1:
            out.append(Defect(
                id=c.next("GRADE"), layer=Layer.L2, category="grade",
                severity=Severity.MAJOR,
                summary=f"以下 check 函数体逐字相同，无法区分各自条目：{names}",
                detail="实现完全一致 → 对应多个评分项永远同时通过/失败，区分度为零、权重虚高。",
                principle="原子性/非冗余",
                evidence=", ".join(f"{n}()" for n in names),
            ))

    # 6. 子串误匹配 & flaky 源（逐函数）
    for name, fi in check_funcs.items():
        sm = SUBSTRING_TRAP_RE.search(fi.source)
        if sm:
            out.append(Defect(
                id=c.next("GRADE"), layer=Layer.L2, category="grade",
                severity=Severity.MAJOR,
                summary=f"{name}() 疑似子串误匹配：{sm.group(0).strip()}",
                detail="如 `\"or\" in text` 会被 'word'/'for' 命中而恒为真，使该检查失效。",
                principle="关键词匹配脆弱性",
                evidence=f"Automated Checks:{fi.lineno}",
                verifiable_by_dynamic=True,
            ))
        fm_ = FLAKY_SOURCE_RE.search(fi.source)
        if fm_:
            out.append(Defect(
                id=c.next("GRADE"), layer=Layer.L2, category="grade",
                severity=Severity.MAJOR,
                summary=f"{name}() 使用非确定性来源 `{fm_.group(0).strip()}`，评分可能不可复现",
                detail="datetime.now()/random/time 等会使同一产物在不同时刻得到不同分数。",
                principle="测试脆弱性",
                evidence=f"Automated Checks:{fi.lineno}",
            ))

    if check_funcs and not any(d.category == "grade" and "雷同" in d.summary for d in out):
        good.append(f"识别到 {len(check_funcs)} 个 check 函数，未见函数体雷同")

    return out, good


# ─────────────────────────────────────────────────────────────────────────────
# L3 judge
# ─────────────────────────────────────────────────────────────────────────────

def check_judge(gm: GradeModule, tm: TaskMd, c: _Counter) -> tuple[list[Defect], list[str]]:
    out: list[Defect] = []
    good: list[str] = []

    if not gm.has_judge:
        good.append("本题无 judge（纯 programmatic）")
        return out, good

    # 1. judge 必须真调模型（OpenAI 兼容 + OPENROUTER + JUDGE_MODEL）
    if not gm.judge_is_real_model:
        missing = []
        if not gm.mentions_openai:
            missing.append("OpenAI/chat.completions.create 调用")
        if not gm.mentions_openrouter:
            missing.append("OPENROUTER_* 环境变量")
        if not gm.mentions_judge_model:
            missing.append("JUDGE_MODEL")
        out.append(Defect(
            id=c.next("JUDGE"), layer=Layer.L3, category="judge",
            severity=Severity.BLOCKER,
            summary=f"judge 疑似未真调模型（缺：{missing}）",
            detail="标注规范 §3.2：字数桩型 judge 必须改写成真调 JUDGE_MODEL。"
                   "未真调会使语义项打分无意义。",
            principle="judge 须真调 JUDGE_MODEL(§3.2)",
            evidence="## Automated Checks",
            verifiable_by_dynamic=True,
        ))
    else:
        good.append("judge 真调模型（OpenAI 兼容 + OPENROUTER + JUDGE_MODEL）")

    # 2. judge 用的 env key 必须在 ## Env 声明（否则容器内拿不到 key）
    declared = set(tm.env_keys)
    needed = [k for k in JUDGE_ENV_KEYS if k in gm.source]
    missing_env = [k for k in needed if k not in declared]
    if missing_env:
        out.append(Defect(
            id=c.next("JUDGE"), layer=Layer.L3, category="judge",
            severity=Severity.BLOCKER,
            summary=f"judge 用到的环境变量未在 ## Env 声明：{missing_env}",
            detail="grading.py 只注入 ## Env 声明的 key；未声明 → 容器内 os.environ 取不到 → judge 全 0。",
            principle="judge 变量须在 ## Env 声明",
            evidence="## Env",
        ))
    elif needed:
        good.append(f"judge 环境变量已在 ## Env 声明：{needed}")

    return out, good


# ─────────────────────────────────────────────────────────────────────────────
# L4 prompt ↔ workspace 一致性
# ─────────────────────────────────────────────────────────────────────────────

def check_workspace(bundle: WildTaskBundle, tm: TaskMd, gm: GradeModule,
                    c: _Counter) -> tuple[list[Defect], list[str]]:
    out: list[Defect] = []
    good: list[str] = []

    # 1. workspace 目录存在
    if not bundle.workspace_dir:
        out.append(Defect(
            id=c.next("WS"), layer=Layer.L4, category="workspace",
            severity=Severity.BLOCKER,
            summary=f"Workspace Path 指向的目录不存在：{bundle.workspace_path_raw}",
            detail="; ".join(bundle.resolver_warnings),
            principle="workspace 必须存在",
            evidence=bundle.workspace_path_raw,
        ))
        return out, good

    ws_files = [f for f in Path(bundle.workspace_dir).rglob("*") if f.is_file()]
    good.append(f"workspace/ 存在，含 {len(ws_files)} 个文件")

    # 2. 空 results/ 目录
    if not bundle.results_dir:
        out.append(Defect(
            id=c.next("WS"), layer=Layer.L4, category="workspace",
            severity=Severity.MAJOR,
            summary="workspace 缺少 results/ 目录（约定的空产出目录）",
            detail="有的镜像不自动建目录，agent 写产物失败 → 全 0。规范要求随题带一个空 results/。",
            principle="workspace 须含空 results/",
            evidence=bundle.workspace_dir,
        ))
    else:
        n = len([x for x in Path(bundle.results_dir).iterdir()])
        if n == 0:
            good.append("workspace 含空 results/ 产出目录")
        else:
            out.append(Defect(
                id=c.next("WS"), layer=Layer.L4, category="workspace",
                severity=Severity.MINOR,
                summary=f"results/ 非空（含 {n} 项），应为干净空目录",
                detail="results/ 应只在 agent 做题时产出；预置内容可能污染评分或泄漏答案。",
                evidence=bundle.results_dir,
            ))

    # 3. Prompt 点名的产物文件是否在 grade 里被引用
    prompt = tm.prompt or ""
    deliverables = set()
    for m in DELIVERABLE_RE.finditer(prompt):
        fn = Path(m.group(1)).name
        if "." in fn:
            deliverables.add(fn)
    grade_src = gm.source
    not_referenced = sorted(d for d in deliverables if d not in grade_src)
    if not_referenced and deliverables:
        out.append(Defect(
            id=c.next("WS"), layer=Layer.L4, category="workspace",
            severity=Severity.MINOR,
            summary=f"Prompt 点名的产物未在 grade 中出现（路径/文件名可能不一致）：{not_referenced}",
            detail=f"Prompt 要求交付 {sorted(deliverables)}，但 grade 代码里未引用上列文件名。",
            principle="产物文件名一致",
            evidence=", ".join(not_referenced),
            verifiable_by_dynamic=True,
        ))
    elif deliverables:
        good.append(f"Prompt 点名的 {len(deliverables)} 个产物均在 grade 中被引用")

    return out, good


# ─────────────────────────────────────────────────────────────────────────────
# 顶层编排
# ─────────────────────────────────────────────────────────────────────────────

def run_static(bundle: WildTaskBundle) -> tuple[list[Defect], list[str]]:
    """运行全部确定性检查，返回 (缺陷列表, 良好项列表)。"""
    c = _Counter()
    defects: list[Defect] = []
    good: list[str] = []

    tm = TaskMd.load(bundle.task_md_path)

    # L0：无法解析直接返回（resolver 已标 UNRESOLVABLE）
    if bundle.resolver_status == ResolverStatus.UNRESOLVABLE:
        defects.append(Defect(
            id=c.next("STRUCT"), layer=Layer.L0, category="structure",
            severity=Severity.BLOCKER,
            summary="task.md 无法被引擎解析（缺 frontmatter 或 ## Workspace Path）",
            detail="; ".join(bundle.resolver_warnings),
            evidence=bundle.task_md_path,
        ))
        return defects, good

    gm = parse_grade_module(tm.automated_checks)

    d, g = check_format_contract(bundle, tm, c); defects += d; good += g
    d, g = check_grade_code(gm, c); defects += d; good += g
    d, g = check_judge(gm, tm, c); defects += d; good += g
    d, g = check_workspace(bundle, tm, gm, c); defects += d; good += g

    return defects, good
