"""
parsing.py —— 产物解析辅助

提供对 rubrics.json / grade.py / judge.py / task.md 的结构化读取，
供确定性检查与语义检查共用。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# rubrics.json
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Rubric:
    id: str
    raw: dict
    verification: str = ""          # programmatic / llm_judge
    criterion: str = ""
    dimension_code: str = ""
    polarity: str = ""              # positive / negative
    level: str = ""                 # must / nice
    check_type: str = ""
    params: dict = field(default_factory=dict)
    target_file: str = ""

    @property
    def is_programmatic(self) -> bool:
        return self.verification.lower() == "programmatic"

    @property
    def is_llm_judge(self) -> bool:
        return self.verification.lower() == "llm_judge"


def load_rubrics(path: str | Path) -> list[Rubric]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        # 兼容 {"rubrics": [...]} 形态
        data = data.get("rubrics", data.get("items", []))
    out: list[Rubric] = []
    for item in data:
        out.append(Rubric(
            id=str(item.get("id", "")),
            raw=item,
            verification=str(item.get("verification", "")),
            criterion=str(item.get("criterion", "")),
            dimension_code=str(item.get("dimension_code", item.get("dimension", ""))),
            polarity=str(item.get("polarity", "")),
            level=str(item.get("level", "")),
            check_type=str(item.get("check_type", "")),
            params=item.get("params", {}) if isinstance(item.get("params"), dict) else {},
            target_file=str(item.get("target_file", "")),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# grade.py / judge.py 的 AST 分析
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FuncInfo:
    name: str
    lineno: int
    end_lineno: int
    source: str
    body_normalized: str            # 去注释/空白后的函数体，用于雷同检测


@dataclass
class PyModuleInfo:
    path: str
    source: str
    funcs: dict[str, FuncInfo] = field(default_factory=dict)
    string_constants: list[str] = field(default_factory=list)
    parse_error: Optional[str] = None

    def check_functions(self) -> dict[str, FuncInfo]:
        """返回形似 rubric-check 的函数（check_XXX / 名字含 rubric id 风格）。"""
        out = {}
        for name, fi in self.funcs.items():
            if re.match(r"(check|grade|judge|verify|_check)", name, re.IGNORECASE):
                out[name] = fi
        return out


def extract_expected_kwargs(func_src: str) -> dict[str, object]:
    """从一个 check 函数体里抽取 `expected=...` / `expected_set=...` 关键字实参的字面量值。

    用于方向(2)：把 grade.py 硬编码的期望值（如 expected=2048）抽出来，
    与 rubric params.expected / verified_facts.verified_value 三方比对。
    只认字面量（数字/字符串/列表/字典/常量），表达式或变量返回时跳过该键。
    """
    out: dict[str, object] = {}
    try:
        tree = ast.parse(func_src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg not in ("expected", "expected_set", "expected_value"):
                continue
            try:
                out[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError):
                # 非字面量（如 min(2, len(...))），跳过
                continue
    return out



def _normalize_body(src: str) -> str:
    """规范化函数体：去掉 docstring/注释/空白，用于雷同检测。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # 退化：按行去注释
        lines = [l.split("#", 1)[0].strip() for l in src.splitlines()]
        return " ".join(l for l in lines if l)
    # 去掉首个 docstring
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
    try:
        return ast.unparse(tree)
    except Exception:  # noqa: BLE001
        lines = [l.split("#", 1)[0].strip() for l in src.splitlines()]
        return " ".join(l for l in lines if l)


def parse_py_module(path: str | Path) -> PyModuleInfo:
    p = Path(path)
    source = p.read_text(encoding="utf-8-sig")
    info = PyModuleInfo(path=str(p), source=source)
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        info.parse_error = f"{e.__class__.__name__}: {e}"
        return info

    src_lines = source.splitlines()

    def collect_func(node):
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        body_src = "\n".join(src_lines[start - 1 : end])
        info.funcs[node.name] = FuncInfo(
            name=node.name,
            lineno=start,
            end_lineno=end,
            source=body_src,
            body_normalized=_normalize_body(body_src),
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            collect_func(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) <= 200:
                info.string_constants.append(node.value)

    return info


# ─────────────────────────────────────────────────────────────────────────────
# task.md
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskMd:
    path: str
    text: str
    prompt: str = ""

    @staticmethod
    def load(path: str | Path) -> "TaskMd":
        text = Path(path).read_text(encoding="utf-8-sig")
        prompt = ""
        m = re.search(r"##\s*Prompt\s*\n(.+?)(?:\n---|\n##\s)", text, re.DOTALL)
        if m:
            prompt = m.group(1).strip()
        return TaskMd(path=str(path), text=text, prompt=prompt)
