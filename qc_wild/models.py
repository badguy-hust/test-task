"""
models.py —— qc_wild 数据契约（WildClaw 单文件 task.md 格式）

与旧 qc/ 的 models.py 区别：
- 旧框架解析的是 HiClaw 多文件题（gt/rubrics.json + grade.py + judge.py + ...）。
- 本框架解析的是转换后的 WildClaw 单文件题：一个 task.md 内联了 frontmatter +
  ## Prompt + ## Automated Checks（一个 def grade(**kwargs)）+ ## Workspace Path/Env/...，
  配一个 workspace/ 数据目录（含空 results/）。

复用旧 qc.models 的 Defect / Severity / Layer / TaskReport —— 它们是格式无关的通用契约。
本模块只新增 WildTaskBundle（单文件题的标准化输入）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# 复用旧框架的通用枚举与缺陷/报告契约（不重复定义）
from qc.models import (  # noqa: F401  (re-export 供 qc_wild 内部统一从此处取)
    Severity,
    Layer,
    ResolverStatus,
    Defect,
    TaskReport,
)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 在 WildClaw 单文件语义下的重映射（沿用旧枚举值，仅注释含义不同）
#   L0 结构 / Resolver（task.md 能否解析、workspace 是否齐全）
#   L1 格式契约（frontmatter / ## section / Prompt 路径声明 / Env 声明）
#   L2 grade 代码（## Automated Checks 里的 def grade：签名/返回/兜底/健壮性）
#   L3 judge（grade 内嵌的 judge 部分：真调模型 / 归一 / 降级）
#   L4 prompt ↔ workspace 一致性（产物路径、results/ 目录）
#   L5 语义（LLM agentic 探索：覆盖性 / 事实正确性 / 翻译保真）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class WildTaskBundle:
    """转换后单文件题的标准化输入契约。"""

    task_id: str
    task_md_path: str                       # tasks/<Category>/<id>.md 绝对路径
    task_root: str                          # 用于 explorer 沙箱的根（见下）
    category: str = ""                      # 分类目录名（Investigative_Analysis 等）
    workspace_path_raw: str = ""            # task.md 里 ## Workspace Path 的原始相对路径
    workspace_dir: Optional[str] = None     # 解析到的真实 workspace 目录（缺失为 None）
    results_dir: Optional[str] = None       # workspace 下的 results/（约定空目录）
    repo_root: str = ""                     # 仓库根（解析 Workspace Path 相对路径的基准）
    resolver_status: ResolverStatus = ResolverStatus.OK
    resolver_warnings: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resolver_status"] = self.resolver_status.value
        return d

    def save_manifest(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
