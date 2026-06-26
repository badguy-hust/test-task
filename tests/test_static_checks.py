"""test_static_checks.py —— 确定性质检层：真实题 0 Blocker，坏题触发对应缺陷（离线）。"""

from __future__ import annotations

from qc_wild.resolver import resolve_wild_task
from qc_wild.static_checks import run_static


def _blockers(defects):
    return [d for d in defects if d.severity.value == "blocker"]


def test_real_task_no_blocker(converted_task_6):
    bundle = resolve_wild_task(converted_task_6)
    defects, good = run_static(bundle)
    assert len(_blockers(defects)) == 0
    assert len(good) > 0


def test_missing_workspace_path(tmp_path, converted_task_6):
    # 删掉 ## Workspace Path 区块 → resolver 应判 UNRESOLVABLE
    import re
    text = converted_task_6.read_text(encoding="utf-8-sig")
    text = re.sub(r"## Workspace Path\n.*?(?=\n## Skills)", "", text, flags=re.DOTALL)
    bad = tmp_path / "no_ws.md"
    bad.write_text(text, encoding="utf-8")
    bundle = resolve_wild_task(bad)
    defects, _ = run_static(bundle)
    assert len(_blockers(defects)) >= 1


def test_no_overall_score(tmp_path, converted_task_6):
    # 删掉 overall_score 赋值行 → grade 代码层应报 Blocker
    import re
    text = converted_task_6.read_text(encoding="utf-8-sig")
    text = re.sub(r'.*scores\["overall_score"\].*\n', "", text)
    bad = tmp_path / "no_overall.md"
    bad.write_text(text, encoding="utf-8")
    bundle = resolve_wild_task(bad)
    defects, _ = run_static(bundle)
    summaries = " ".join(d.summary for d in _blockers(defects))
    assert "overall_score" in summaries


def test_prompt_hash_injection(tmp_path, converted_task_6):
    # 在 Prompt 正文插入 ## 标题 → 被引擎误切为区块，应报「非预期 section」
    text = converted_task_6.read_text(encoding="utf-8-sig")
    text = text.replace("All input files are located",
                        "## Injected\n\nAll input files are located", 1)
    bad = tmp_path / "hash.md"
    bad.write_text(text, encoding="utf-8")
    bundle = resolve_wild_task(bad)
    defects, _ = run_static(bundle)
    summaries = " ".join(d.summary for d in defects)
    assert "非预期" in summaries or "Injected" in summaries
