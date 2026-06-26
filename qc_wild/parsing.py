"""
parsing.py —— WildClaw 单文件 task.md 解析

两个核心结构：
- TaskMd：复刻 WildClawBench `src/utils/task_parser.parse_task_md` 的切分逻辑
  （frontmatter + 按 `^##\s+` 切 section），保证「本框架判定能解析」== 「引擎能解析」。
- GradeModule：把 ## Automated Checks 的代码块抽出来做 AST 分析，定位 def grade、
  内嵌 check_* 函数、judge 相关结构、overall_score 赋值、import 位置等。

仅做解析；判定逻辑在 static_checks.py。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# 复用旧 qc.parsing 的函数体规范化（雷同检测用）
from qc.parsing import _normalize_body


# ─────────────────────────────────────────────────────────────────────────────
# task.md（引擎同款切分）
# ─────────────────────────────────────────────────────────────────────────────

# 与 task_parser.parse_task_md 完全一致的两条正则
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
_HEADER_RE = re.compile(r"^##\s+(.+)$")


def _strip_codeblock(raw: str) -> str:
    """与引擎 strip_codeblock 一致：剥掉首尾 ``` 围栏。"""
    s = re.sub(r"^```[^\n]*\n?", "", raw.strip())
    s = re.sub(r"\n?```$", "", s).strip()
    return s


@dataclass
class TaskMd:
    path: str
    raw: str
    parse_error: Optional[str] = None            # frontmatter 缺失等致命解析错误
    frontmatter: dict = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)  # section 名 -> 去首尾空白的原文
    # 便捷字段（已剥代码块）
    prompt: str = ""
    automated_checks: str = ""
    workspace_path_raw: str = ""
    env_raw: str = ""
    skills_raw: str = ""
    warmup_raw: str = ""

    @property
    def env_keys(self) -> list[str]:
        """## Env 里声明的环境变量名（#开头/空行跳过），与 grading.py 注入逻辑一致。"""
        out = []
        for line in self.env_raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
        return out

    @staticmethod
    def load(path: str | Path) -> "TaskMd":
        p = Path(path)
        # 引擎用 encoding="utf-8"；这里用 utf-8-sig 容忍 BOM 但不改变切分语义
        content = p.read_text(encoding="utf-8-sig")
        tm = TaskMd(path=str(p), raw=content)

        m = _FM_RE.match(content)
        if not m:
            tm.parse_error = "YAML frontmatter not found（引擎 parse_task_md 会直接报错）"
            return tm

        try:
            fm = yaml.safe_load(m.group(1))
            tm.frontmatter = fm if isinstance(fm, dict) else {}
        except Exception as e:  # noqa: BLE001
            tm.parse_error = f"frontmatter YAML 解析失败：{e}"
            return tm

        body = m.group(2)
        sections: dict[str, str] = {}
        current: Optional[str] = None
        lines: list[str] = []
        for line in body.split("\n"):
            h = _HEADER_RE.match(line)
            if h:
                if current is not None:
                    sections[current] = "\n".join(lines).strip()
                current = h.group(1)
                lines = []
            else:
                lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip()
        tm.sections = sections

        tm.prompt = sections.get("Prompt", "").strip()
        tm.automated_checks = _strip_codeblock(sections.get("Automated Checks", ""))
        tm.workspace_path_raw = _strip_codeblock(sections.get("Workspace Path", "").strip())
        tm.env_raw = _strip_codeblock(sections.get("Env", ""))
        tm.skills_raw = _strip_codeblock(sections.get("Skills", ""))
        tm.warmup_raw = _strip_codeblock(sections.get("Warmup", ""))
        return tm


# ─────────────────────────────────────────────────────────────────────────────
# ## Automated Checks 的 grade 代码 AST 分析
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FuncInfo:
    name: str
    lineno: int                  # 相对 Automated Checks 代码块的行号
    end_lineno: int
    source: str
    body_normalized: str
    has_try: bool                # 函数体里是否出现 try/except


@dataclass
class GradeModule:
    """对 ## Automated Checks 代码块的结构化分析。

    注意：引擎把整个代码块当一个模块 exec，因此辅助函数 / check 函数 / import 既可写在
    grade 体内、也可写在代码块顶层（与 grade 平级）——两种都能正常运行。本批已跑通的
    参考转换（task_1 / task_8）即用顶层写法。所以「顶层 import」「顶层 check 函数」都不是缺陷。
    """

    source: str                                  # 代码块原文
    parse_error: Optional[str] = None
    grade_func: Optional[FuncInfo] = None        # def grade
    grade_signature_ok: bool = False             # 是否 def grade(**kwargs)
    all_funcs: dict[str, FuncInfo] = field(default_factory=dict)     # 代码块内所有函数（任意层级）
    imports: list[str] = field(default_factory=list)                # 所有 import（任意层级）
    grade_body_has_try: bool = False             # grade 体内是否出现 try/except（多为 run-loop 兜底）
    sets_overall_score: bool = False             # 是否给 scores["overall_score"] 赋值
    returns_something: bool = False              # grade 是否有 return <value>
    # judge 相关迹象
    mentions_openai: bool = False                # 出现 OpenAI / chat.completions.create
    mentions_judge_model: bool = False           # 出现 JUDGE_MODEL
    mentions_openrouter: bool = False            # 出现 OPENROUTER_*
    has_judge_criteria: bool = False             # 出现 JUDGE_CRITERIA / judge 项
    string_literals: list[str] = field(default_factory=list)  # 代码里的字符串字面量（事实核对用）

    def check_functions(self) -> dict[str, FuncInfo]:
        """形似评分 check 的函数（check_* / verify_* 风格，任意层级）。"""
        return {
            n: fi for n, fi in self.all_funcs.items()
            if re.match(r"(check|verify)_", n, re.IGNORECASE)
        }

    @property
    def has_judge(self) -> bool:
        """这道题是否含 judge（语义打分）部分。"""
        return self.has_judge_criteria or self.mentions_judge_model

    @property
    def judge_is_real_model(self) -> bool:
        """judge 是否真调模型（而非字数桩等假实现）。"""
        return self.has_judge and self.mentions_openai and self.mentions_openrouter


def parse_grade_module(automated_checks_src: str) -> GradeModule:
    gm = GradeModule(source=automated_checks_src)
    src = automated_checks_src
    if not src.strip():
        gm.parse_error = "## Automated Checks 为空"
        return gm

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        gm.parse_error = f"{e.__class__.__name__}: {e}"
        return gm

    src_lines = src.splitlines()

    def _mk_funcinfo(node) -> FuncInfo:
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        body_src = "\n".join(src_lines[start - 1: end])
        has_try = any(isinstance(n, ast.Try) for n in ast.walk(node))
        return FuncInfo(
            name=node.name, lineno=start, end_lineno=end,
            source=body_src, body_normalized=_normalize_body(body_src),
            has_try=has_try,
        )

    # 所有函数（任意层级）+ 所有 import + 定位 def grade
    grade_node = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            gm.all_funcs[node.name] = _mk_funcinfo(node)
            if node.name == "grade":
                grade_node = node
                gm.grade_func = gm.all_funcs[node.name]
                gm.grade_signature_ok = node.args.kwarg is not None
        elif isinstance(node, ast.Import):
            gm.imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            gm.imports.append(node.module or "")

    # grade 函数体内部细节
    if grade_node is not None:
        for n in ast.walk(grade_node):
            if isinstance(n, ast.Try):
                gm.grade_body_has_try = True
            if isinstance(n, ast.Return) and n.value is not None:
                gm.returns_something = True
            if isinstance(n, ast.Assign):
                for tgt in n.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "overall_score"):
                        gm.sets_overall_score = True

    # 字符串字面量（事实核对用）
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) <= 200:
                gm.string_literals.append(node.value)

    text = src
    gm.mentions_openai = bool(
        re.search(r"\bOpenAI\b", text) or "chat.completions.create" in text
    )
    gm.mentions_judge_model = "JUDGE_MODEL" in text
    gm.mentions_openrouter = "OPENROUTER" in text
    gm.has_judge_criteria = "JUDGE_CRITERIA" in text
    return gm
