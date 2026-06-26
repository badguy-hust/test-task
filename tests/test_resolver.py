"""test_resolver.py —— 两个框架的 resolver + 续跑判断 has_successful_output（离线）。"""

from __future__ import annotations

import json

from qc_wild.resolver import resolve_wild_task
from qc_wild.models import ResolverStatus
from qc_wild.runpaths import has_successful_output, make_task_run_dir, safe_task_id
from preannotate.resolver import resolve_raw_task


def test_qcwild_resolver_ok(converted_task_6):
    b = resolve_wild_task(converted_task_6)
    assert b.resolver_status != ResolverStatus.UNRESOLVABLE
    assert b.workspace_dir is not None
    assert b.results_dir is not None


def test_qcwild_resolver_unresolvable(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("没有 frontmatter 也没有 Workspace Path", encoding="utf-8")
    b = resolve_wild_task(bad)
    assert b.resolver_status == ResolverStatus.UNRESOLVABLE


def test_preannotate_resolver_bare_prompt(tmp_path):
    # 裸 prompt 正文（无 frontmatter）+ workspace/
    d = tmp_path / "task_x"
    (d / "workspace").mkdir(parents=True)
    (d / "task.md").write_text("You are an analyst. Do the thing.", encoding="utf-8")
    t = resolve_raw_task(d)
    assert t.prompt.startswith("You are an analyst")
    assert t.workspace_dir is not None


def test_preannotate_resolver_with_frontmatter(tmp_path):
    d = tmp_path / "task_y"
    (d / "workspace").mkdir(parents=True)
    (d / "task.md").write_text(
        "---\nid: foo\n---\n\n## Prompt\n\nDo Y.\n", encoding="utf-8")
    t = resolve_raw_task(d)
    assert t.task_id == "foo"
    assert "Do Y." in t.prompt


# ── 断点续跑判断 ──

def test_has_successful_output_qcwild(tmp_path):
    out = tmp_path / "out"
    rd = make_task_run_dir(out, "task_z")
    # 没产物 → False
    assert has_successful_output(out, "task_z", "result.json") is False
    # 写 result.json → True
    (rd / "result.json").write_text("{}", encoding="utf-8")
    assert has_successful_output(out, "task_z", "result.json") is True


def test_has_successful_output_preannotate_nonempty(tmp_path):
    out = tmp_path / "out"
    rd = make_task_run_dir(out, "task_p")
    # 空 rubrics → 不算成功
    (rd / "rubric_draft.json").write_text(json.dumps({"rubrics": []}), encoding="utf-8")
    assert has_successful_output(out, "task_p", "rubric_draft.json",
                                 nonempty_json_key="rubrics") is False
    # 非空 rubrics → 成功
    (rd / "rubric_draft.json").write_text(
        json.dumps({"rubrics": [{"id": "DC-1"}]}), encoding="utf-8")
    assert has_successful_output(out, "task_p", "rubric_draft.json",
                                 nonempty_json_key="rubrics") is True


def test_safe_task_id():
    assert safe_task_id("a b/c") == "a_b_c"
