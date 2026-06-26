"""
resolver.py —— 单文件题定位器

输入一个 task.md（tasks/<Category>/<id>.md），产出 WildTaskBundle：
- 解析 frontmatter 拿 id/category；
- 把 ## Workspace Path 的相对路径解析到真实 workspace 目录；
- 定位 workspace 下的 results/；
- 判定 resolver 状态（task.md 无法解析 / Workspace Path 缺失 → UNRESOLVABLE）。

约定的目录形态（与转换产物一致）：
    <repo_root>/
    ├── tasks/<Category>/<id>.md
    └── workspace/<Category>/<short_name>/   ← Workspace Path 指向这里（相对 repo_root）

repo_root 通过 task.md 的位置回溯：tasks/<Category>/<id>.md → 上溯三层。
explorer 沙箱根 = repo_root（模型据此既能读 task.md，也能读 workspace/）。
"""

from __future__ import annotations

from pathlib import Path

from .models import WildTaskBundle, ResolverStatus
from .parsing import TaskMd


def _infer_repo_root(task_md: Path) -> Path:
    """从 tasks/<Category>/<id>.md 回溯仓库根。
    若结构不符（例如直接传一个孤立 md），退化为 task.md 所在目录。"""
    parts = task_md.resolve().parts
    # 找到名为 'tasks' 的那一层，repo_root = 它的父目录
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "tasks":
            return Path(*parts[:i])
    # 退化：上溯两层（<root>/<Category>/<id>.md 也兜得住）
    return task_md.resolve().parent.parent


def resolve_wild_task(task_md_path: str | Path) -> WildTaskBundle:
    md = Path(task_md_path).resolve()
    warnings: list[str] = []

    tm = TaskMd.load(md)

    repo_root = _infer_repo_root(md)
    category = md.parent.name

    # task_id：优先 frontmatter id，否则文件名 stem
    task_id = ""
    if tm.frontmatter:
        task_id = str(tm.frontmatter.get("id", "") or "").strip()
    if not task_id:
        task_id = md.stem
    fm_category = str((tm.frontmatter or {}).get("category", "") or "").strip()
    if fm_category:
        category = fm_category

    bundle = WildTaskBundle(
        task_id=task_id,
        task_md_path=str(md),
        task_root=str(repo_root),
        category=category,
        repo_root=str(repo_root),
        workspace_path_raw=tm.workspace_path_raw,
    )

    # task.md 致命解析错误 → UNRESOLVABLE（引擎 parse_task_md 也会报错）
    if tm.parse_error:
        warnings.append(f"task.md 无法解析：{tm.parse_error}")
        bundle.resolver_status = ResolverStatus.UNRESOLVABLE
        bundle.resolver_warnings = warnings
        return bundle

    if not tm.workspace_path_raw:
        warnings.append("缺少 ## Workspace Path（引擎 parse_task_md 会抛 ValueError）")
        bundle.resolver_status = ResolverStatus.UNRESOLVABLE
        bundle.resolver_warnings = warnings
        return bundle

    # 解析 workspace 目录：Workspace Path 相对 repo_root（与引擎 ROOT_DIR 一致）
    wp = Path(tm.workspace_path_raw)
    ws_dir = wp if wp.is_absolute() else (repo_root / wp)
    ws_dir = ws_dir.resolve()
    if ws_dir.is_dir():
        bundle.workspace_dir = str(ws_dir)
        results = ws_dir / "results"
        if results.is_dir():
            bundle.results_dir = str(results)
    else:
        warnings.append(
            f"Workspace Path 指向的目录不存在：{tm.workspace_path_raw} "
            f"（解析为 {ws_dir}）"
        )

    bundle.resolver_status = ResolverStatus.WARN if warnings else ResolverStatus.OK
    bundle.resolver_warnings = warnings
    return bundle
