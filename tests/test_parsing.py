"""test_parsing.py —— qc_wild 的 TaskMd 切分 + grade 代码 AST 分析（离线）。"""

from __future__ import annotations

from qc_wild.parsing import TaskMd, parse_grade_module


def test_taskmd_load_converted(converted_task_6):
    tm = TaskMd.load(converted_task_6)
    assert tm.parse_error is None
    # frontmatter 五字段
    assert tm.frontmatter.get("id") == "Investigative_Analysis_task_6_stripe_subscription_audit"
    assert tm.frontmatter.get("category") == "Investigative_Analysis"
    # 区块
    for sec in ("Prompt", "Automated Checks", "Workspace Path", "Env"):
        assert sec in tm.sections
    # env_keys（# 注释/空行跳过）
    assert "JUDGE_MODEL" in tm.env_keys
    assert "OPENROUTER_API_KEY" in tm.env_keys
    # prompt 提取且含路径声明
    assert "/tmp_workspace/" in tm.prompt


def test_taskmd_missing_frontmatter(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("## Prompt\n没有 frontmatter\n", encoding="utf-8")
    tm = TaskMd.load(p)
    assert tm.parse_error is not None


def test_parse_grade_module_converted(converted_task_6):
    tm = TaskMd.load(converted_task_6)
    gm = parse_grade_module(tm.automated_checks)
    assert gm.parse_error is None
    assert gm.grade_func is not None
    assert gm.grade_signature_ok is True          # def grade(**kwargs)
    assert gm.sets_overall_score is True
    assert gm.returns_something is True
    assert gm.grade_body_has_try is True          # run-loop 兜底
    assert gm.has_judge is True
    assert gm.judge_is_real_model is True         # OpenAI + OPENROUTER + JUDGE_MODEL
    assert len(gm.check_functions()) > 0


def test_parse_grade_module_syntax_error():
    gm = parse_grade_module("def grade(**kwargs):\n    return {{{ broken")
    assert gm.parse_error is not None


def test_parse_grade_module_empty():
    gm = parse_grade_module("")
    assert gm.parse_error is not None
