"""
explorer.py —— agentic 文件浏览工具集（沙箱化）

给语义层 LLM 一组只读文件工具，让它像 agent 一样自主探索题目目录
（workspace / solver_runs / gt 等），按需读取相关文件，而不是把提炼好的
片段一次性塞进 prompt。这样能处理任意大小的 workspace —— 模型只读它判断
相关的那部分，读一个文件、把结论留在对话历史里，再决定下一步读什么。

安全约束：
  - 所有路径都被限制在 task_root 之内（防目录穿越）。
  - 只读，不提供任何写/执行能力。
  - 单次读取有字符上限，目录列举有条目上限，grep 有命中上限，防止上下文爆炸。
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Callable

import config

# 单位上限（防上下文爆炸）—— 集中在 config.py，改那里即全局生效
_MAX_READ_CHARS = config.EXPLORER_MAX_READ_CHARS      # read_file 单次最多返回字符
_MAX_LIST_ENTRIES = config.EXPLORER_MAX_LIST_ENTRIES  # list_dir 单次最多列出条目
_MAX_GREP_HITS = config.EXPLORER_MAX_GREP_HITS        # grep 最多返回命中行
_MAX_FILE_BYTES = config.EXPLORER_MAX_FILE_BYTES      # 超过此大小的文件只读头部

# 跳过的噪音目录/文件
_SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "node_modules", ".pytest_cache"}
_BINARY_EXTS = {".xlsx", ".xls", ".pdf", ".pptx", ".docx", ".png", ".jpg", ".jpeg",
                ".gif", ".zip", ".gz", ".tar", ".db", ".sqlite", ".pkl", ".parquet",
                ".ico", ".so", ".pyc", ".woff", ".woff2", ".ttf", ".mp4", ".mp3"}


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI 风格工具定义
# ─────────────────────────────────────────────────────────────────────────────

EXPLORER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录下的文件和子目录（相对题目根目录的路径）。"
                           "返回每个条目的名称、类型(file/dir)、文件大小。用它先了解结构再决定读什么。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "相对题目根目录的目录路径，根目录传空串或 '.'。"},
                    "recursive": {"type": "boolean",
                                  "description": "是否递归列出所有子孙（默认 false，只列一层）。"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件内容。大文件可用 offset/limit 分段读。"
                           "二进制文件（xlsx/pdf/图片等）无法读取，会返回提示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对题目根目录的文件路径。"},
                    "offset": {"type": "integer",
                               "description": "起始字符位置（默认 0）。"},
                    "limit": {"type": "integer",
                              "description": f"最多读取字符数（默认且上限 {_MAX_READ_CHARS}）。"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "在题目目录下按正则搜索文本内容，返回匹配的 文件:行号:行内容。"
                           "用于快速定位某个关键词/数值/字段出现在哪些文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python 正则表达式。"},
                    "path": {"type": "string",
                             "description": "限定搜索的子目录（相对根，默认全目录）。"},
                    "ignore_case": {"type": "boolean", "description": "是否忽略大小写（默认 true）。"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# 沙箱路径解析
# ─────────────────────────────────────────────────────────────────────────────

def _safe_resolve(root: Path, rel: str) -> Path:
    """把相对路径安全解析到 root 之内；越界则抛 ValueError。"""
    rel = (rel or "").strip().lstrip("/")
    if rel in ("", "."):
        return root
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise ValueError(f"路径越界（不在题目目录内）：{rel}")
    return target


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现
# ─────────────────────────────────────────────────────────────────────────────

def _do_list_dir(root: Path, path: str, recursive: bool = False) -> str:
    try:
        base = _safe_resolve(root, path)
    except ValueError as e:
        return str(e)
    if not base.is_dir():
        return f"不是目录或不存在：{path}"

    lines: list[str] = []
    count = 0
    if recursive:
        for cur, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for d in sorted(dirs):
                rel = os.path.relpath(os.path.join(cur, d), root)
                lines.append(f"[dir]  {rel}/")
                count += 1
                if count >= _MAX_LIST_ENTRIES:
                    break
            for f in sorted(files):
                fp = os.path.join(cur, f)
                rel = os.path.relpath(fp, root)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    sz = 0
                lines.append(f"[file] {rel}  ({sz}B)")
                count += 1
                if count >= _MAX_LIST_ENTRIES:
                    break
            if count >= _MAX_LIST_ENTRIES:
                break
    else:
        for entry in sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if entry.name in _SKIP_DIRS:
                continue
            if entry.is_dir():
                lines.append(f"[dir]  {entry.name}/")
            else:
                try:
                    sz = entry.stat().st_size
                except OSError:
                    sz = 0
                lines.append(f"[file] {entry.name}  ({sz}B)")
            count += 1
            if count >= _MAX_LIST_ENTRIES:
                break

    if not lines:
        return f"（空目录）{path}"
    header = f"目录 {path or '.'} 内容（{count} 项{'，已截断' if count >= _MAX_LIST_ENTRIES else ''}）：\n"
    return header + "\n".join(lines)


def _do_read_file(root: Path, path: str, offset: int = 0, limit: int = _MAX_READ_CHARS) -> str:
    try:
        fp = _safe_resolve(root, path)
    except ValueError as e:
        return str(e)
    if not fp.is_file():
        return f"不是文件或不存在：{path}"
    if fp.suffix.lower() in _BINARY_EXTS:
        return f"[二进制文件，无法以文本读取]：{path}（{fp.stat().st_size}B）"

    limit = max(1, min(int(limit or _MAX_READ_CHARS), _MAX_READ_CHARS))
    offset = max(0, int(offset or 0))
    try:
        size = fp.stat().st_size
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            if size > _MAX_FILE_BYTES and offset == 0:
                head = f.read(limit)
                return (f"[文件较大 {size}B，仅显示前 {limit} 字符；用 offset 继续读]\n\n{head}")
            f.seek(0)
            content = f.read()
    except Exception as e:  # noqa: BLE001
        return f"[读取失败] {path}: {e}"

    chunk = content[offset: offset + limit]
    total = len(content)
    suffix = ""
    if offset + limit < total:
        suffix = f"\n\n[未读完：已显示 {offset}-{offset+limit}/{total} 字符，用 offset={offset+limit} 继续]"
    elif offset > 0:
        suffix = f"\n\n[文件结束，共 {total} 字符]"
    return f"=== {path} ===\n{chunk}{suffix}"


def _do_grep(root: Path, pattern: str, path: str = "", ignore_case: bool = True) -> str:
    try:
        base = _safe_resolve(root, path)
    except ValueError as e:
        return str(e)
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return f"[正则错误] {e}"

    hits: list[str] = []
    search_root = base if base.is_dir() else base.parent
    for cur, dirs, files in os.walk(search_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in sorted(files):
            fp = Path(cur) / f
            if fp.suffix.lower() in _BINARY_EXTS:
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    for ln, line in enumerate(fh, 1):
                        if rx.search(line):
                            rel = os.path.relpath(fp, root)
                            hits.append(f"{rel}:{ln}: {line.strip()[:200]}")
                            if len(hits) >= _MAX_GREP_HITS:
                                break
            except Exception:  # noqa: BLE001
                continue
            if len(hits) >= _MAX_GREP_HITS:
                break
        if len(hits) >= _MAX_GREP_HITS:
            break

    if not hits:
        return f"未找到匹配 /{pattern}/ 的内容（范围：{path or '.'}）"
    header = f"匹配 /{pattern}/（{len(hits)} 行{'，已截断' if len(hits) >= _MAX_GREP_HITS else ''}）：\n"
    return header + "\n".join(hits)


# ─────────────────────────────────────────────────────────────────────────────
# 工厂：绑定 task_root，返回 tool_executor
# ─────────────────────────────────────────────────────────────────────────────

def make_executor(task_root: str | Path) -> Callable[[str, dict], str]:
    """返回绑定到 task_root 的 tool_executor(name, args) -> str。"""
    root = Path(task_root).resolve()

    def executor(name: str, args: dict) -> str:
        if name == "list_dir":
            return _do_list_dir(root, args.get("path", ""),
                                bool(args.get("recursive", False)))
        if name == "read_file":
            return _do_read_file(root, args.get("path", ""),
                                 args.get("offset", 0), args.get("limit", _MAX_READ_CHARS))
        if name == "grep":
            return _do_grep(root, args.get("pattern", ""),
                            args.get("path", ""), bool(args.get("ignore_case", True)))
        return f"[未知工具] {name}"

    return executor


# ─────────────────────────────────────────────────────────────────────────────
# workspace 文件普查 + 类型分桶（探索充分性闸门用，与 executor 同口径）
# ─────────────────────────────────────────────────────────────────────────────

# 桶分类（与 _BINARY_EXTS 互斥；扩展名小写，含点）
_BUCKET_EXTS = {
    "data": {".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".ndjson"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rb",
             ".sh", ".sql", ".rs", ".c", ".cpp", ".h", ".php", ".scala"},
    "doc":  {".md", ".txt", ".html", ".htm", ".rst", ".ini", ".cfg", ".toml",
             ".properties", ".log"},
}


def _bucket_of(suffix: str) -> str:
    s = suffix.lower()
    for b, exts in _BUCKET_EXTS.items():
        if s in exts:
            return b
    return "doc"   # 其余可读文本兜底归 doc


def census_workspace(ws_dir: str | Path) -> dict:
    """普查 workspace 下模型可读的文本文件，返回数量 + 相对路径清单 + 类型分桶。

    口径与 explorer 工具一致：跳过 _SKIP_DIRS 噪音目录与 _BINARY_EXTS 二进制文件。
    返回 {"root": str, "count": W, "text_files": [rel...], "buckets": {data/code/doc: [rel...]}}。
    """
    root = Path(ws_dir).resolve()
    text_files: list[str] = []
    buckets: dict[str, list[str]] = {"data": [], "code": [], "doc": []}
    if root.is_dir():
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for f in sorted(files):
                fp = Path(cur) / f
                if fp.suffix.lower() in _BINARY_EXTS:
                    continue
                rel = os.path.relpath(fp, root)
                text_files.append(rel)
                buckets[_bucket_of(fp.suffix)].append(rel)
    return {
        "root": str(root),
        "count": len(text_files),
        "text_files": text_files,
        "buckets": {b: v for b, v in buckets.items() if v},   # 只保留非空桶
    }


def required_read_count(w: int) -> int:
    """最少读取文件数：min(W, max(FLOOR, ceil(W*RATIO)))，再封顶 CAP。参数见 config.py。"""
    if w <= 0:
        return 0
    target = max(config.GATE_MIN_READS_FLOOR, math.ceil(w * config.GATE_MIN_READS_RATIO))
    return min(w, target, config.GATE_MIN_READS_CAP)


# ─────────────────────────────────────────────────────────────────────────────
# 探索充分性闸门工厂（preannotate / qc_wild 共用）
# ─────────────────────────────────────────────────────────────────────────────

def make_exploration_gate(census: dict, *,
                          require_recursive_list: bool = True,
                          min_reads: int | None = None,
                          require_buckets: bool = True,
                          extra_required: list[tuple[str, Callable[[list], bool]]] | None = None):
    """构造 finish_gate(history) -> Optional[str]。

    history: [(tool_name, args_dict), ...]。满足全部条件返回 None（放行），
    否则返回中文「还差这些」提示拦截。

    检查项（按 census 自适应）：
      1. 递归全貌：做过 list_dir(recursive=True) 或 list ≥2 次。
      2. 最少读取数：去重 read_file 数 ≥ min_reads（缺省按 required_read_count(W)）。
      3. 关键类型覆盖：census 里存在的每个桶，至少有一个文件被 read/grep 碰过。
      4. extra_required: [(描述, 判定(history)->bool)]，业务侧附加（如 qc_wild 必须读过 task.md）。
    """
    count = census.get("count", 0)
    buckets = census.get("buckets", {})
    need_reads = required_read_count(count) if min_reads is None else min_reads

    def _norm(p) -> str:
        return str(p or "").replace("\\", "/")

    def _basename_set(paths) -> set:
        return {os.path.basename(p) for p in paths}

    def gate(history):
        list_calls = [(n, a) for n, a in history if n == "list_dir"]
        recursive_list = any(bool(a.get("recursive")) for _, a in list_calls)
        read_paths = [_norm(a.get("path")) for n, a in history if n == "read_file"]
        touched = [_norm(a.get("path")) for n, a in history if n in ("read_file", "grep")]
        read_uniq = {p for p in read_paths if p}

        missing: list[str] = []

        # 1. 递归全貌（或多次 list）
        if require_recursive_list and not recursive_list and len(list_calls) < 2:
            missing.append("先 list_dir(path=workspace, recursive=true) 拿到完整文件树全貌"
                           "（或多次 list 不同子目录），按文件名判断价值后再读")

        # 2. 最少读取数
        if need_reads and len(read_uniq) < need_reads:
            missing.append(f"用 read_file 实际读取的文件数不足：已读 {len(read_uniq)} 个，"
                           f"至少需读 {need_reads} 个（workspace 共 {count} 个可读文件）")

        # 3. 关键类型覆盖
        if require_buckets and buckets:
            touched_base = _basename_set(p for p in touched if p)
            for b, files in buckets.items():
                bucket_base = _basename_set(files)
                if not (touched_base & bucket_base):
                    label = {"data": "数据文件(csv/json…)",
                             "code": "代码文件(py/js…)",
                             "doc": "文档文件(md/txt/html…)"}.get(b, b)
                    eg = ", ".join(files[:3])
                    missing.append(f"尚未碰过任何{label}，至少 read_file/grep 一个，例如：{eg}")

        # 4. 业务附加项
        for desc, predicate in (extra_required or []):
            try:
                ok = predicate(history)
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                missing.append(desc)

        if not missing:
            return None
        return ("请勿过早结束——你还未完成必要的探索（这是保证标注/质检质量的硬要求）：\n"
                + "\n".join(f"  - {m}" for m in missing)
                + "\n请用文件工具继续探索；读完一批后可再 list_dir 回看，重新判断下一步该读什么。")

    return gate
