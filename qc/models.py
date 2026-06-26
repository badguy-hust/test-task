"""
models.py —— 框架共享数据契约

定义两个核心结构：
- TaskBundle: Resolver 把杂乱题目目录标准化后的统一输入契约。
- Defect:     质检引擎产出的单条缺陷。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# 枚举
# ─────────────────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"        # 良好项 / 提示，不计入缺陷统计


class Layer(str, Enum):
    L0 = "L0"            # 结构 / Resolver
    L1 = "L1"            # rubrics.json 内容
    L2 = "L2"            # grade.py
    L3 = "L3"            # judge.py
    L4 = "L4"            # task.md 一致性
    L5 = "L5"            # 陷阱覆盖 / 综合


class ResolverStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"                # 能定位但有歧义（多套 rubric / 多命中）
    UNRESOLVABLE = "UNRESOLVABLE"  # 缺少 rubrics.json 或 grade.py


class SeriesType(str, Enum):
    TRJ = "TRJ"          # TRJ 系列：操作执行类，有 mock 服务
    BATCH2 = "batch_2"   # batch_2：分析研究类，真实代码库


# ─────────────────────────────────────────────────────────────────────────────
# TaskBundle —— 质检引擎的唯一输入
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskFiles:
    """标准化后的关键文件路径（绝对路径，缺失为 None）。"""
    task_md: Optional[str] = None
    rubrics_json: Optional[str] = None      # 必需
    grade_py: Optional[str] = None          # 必需
    judge_py: Optional[str] = None          # 可选
    # TRJ 系列专属
    tools_md: Optional[str] = None
    gold_actions: Optional[str] = None
    oracle_result: Optional[str] = None
    mock_data_dir: Optional[str] = None
    mock_fastapi_dir: Optional[str] = None
    # 两套共有
    workspace_dir: Optional[str] = None
    # batch_2 专属
    verified_facts: Optional[str] = None    # gt/verified_facts.json
    workspace_context: Optional[str] = None  # gt/workspace_context.json
    solution_plan: Optional[str] = None     # solution_plan.md
    meta_json: Optional[str] = None         # meta.json
    gold_output_dirs: list[str] = field(default_factory=list)


@dataclass
class TaskBundle:
    task_id: str
    task_root: str
    files: TaskFiles = field(default_factory=TaskFiles)
    resolver_status: ResolverStatus = ResolverStatus.OK
    resolver_warnings: list[str] = field(default_factory=list)
    series: SeriesType = SeriesType.TRJ     # 题目系列
    # batch_2 专属元数据
    scene_id: str = ""       # scene_43_Complex_AI_Workflow_Architecture
    category: str = ""       # AI_Machine_Learning
    solver_runs: list[str] = field(default_factory=list)  # solver_runs 子目录列表
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["resolver_status"] = self.resolver_status.value
        d["series"] = self.series.value
        return d

    def save_manifest(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Defect —— 单条缺陷
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Defect:
    id: str                       # 报告内编号，如 "GRADE-1" / "RUB-3"
    layer: Layer
    category: str                 # "rubrics.json" / "grade.py" / "judge.py" / "task.md" / "trap" / "structure"
    severity: Severity
    summary: str                  # 一句话描述
    detail: str = ""              # 现象 / 影响 / 建议 展开
    rubric_ref: list[str] = field(default_factory=list)  # 关联的 rubric id
    principle: str = ""           # 对应质检原则
    evidence: str = ""            # 文件:行号 或 rubric id 等可追溯证据
    suggestion: str = ""
    verifiable_by_dynamic: bool = False  # v2 动态层可证实/推翻的"疑似"结论
    confidence: str = "high"      # high / medium / low —— LLM 主张的把握程度。
                                  # low = 无硬文件证据的主观判断，转入"待人工复核"，不自动定责。

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layer"] = self.layer.value
        d["severity"] = self.severity.value
        return d


# ─────────────────────────────────────────────────────────────────────────────
# 单题质检结果
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TaskReport:
    task_id: str
    resolver_status: ResolverStatus
    verdict: str                       # PASS / FAIL / UNRESOLVABLE
    defects: list[Defect] = field(default_factory=list)
    good_points: list[str] = field(default_factory=list)
    trap_matrix: list[dict] = field(default_factory=list)
    bundle: Optional[TaskBundle] = None

    def counts(self) -> dict[str, int]:
        c = {"blocker": 0, "major": 0, "minor": 0}
        for d in self.defects:
            if d.severity in (Severity.BLOCKER, Severity.MAJOR, Severity.MINOR):
                c[d.severity.value] += 1
        return c

    def needs_review_defects(self) -> list[Defect]:
        """低置信度的 blocker/major：转人工复核，不自动判需返修。"""
        return [d for d in self.defects
                if d.confidence == "low"
                and d.severity in (Severity.BLOCKER, Severity.MAJOR)]

    def actionable_defects(self) -> list[Defect]:
        """足以触发需返修判定的缺陷：高/中置信度的 blocker/major。
        低置信度的高危缺陷被剔除（它们进人工复核区，不直接定责）。"""
        review = set(id(d) for d in self.needs_review_defects())
        return [d for d in self.defects
                if d.severity in (Severity.BLOCKER, Severity.MAJOR)
                and id(d) not in review]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "resolver_status": self.resolver_status.value,
            "verdict": self.verdict,
            "counts": self.counts(),
            "defects": [d.to_dict() for d in self.defects],
            "good_points": self.good_points,
            "trap_matrix": self.trap_matrix,
        }
