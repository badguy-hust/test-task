"""test_dry_run.py —— smoke_run_grade 各退出码分支（离线、子进程、秒级）。"""

from __future__ import annotations

from qc_wild.dry_run import smoke_run_grade

_GOOD = '''
import os, json
def grade(**kwargs) -> dict:
    ws = kwargs.get("workspace_path", "/tmp_workspace")
    scores = {}
    def check_DC_1():
        try:
            return os.path.exists(os.path.join(ws, "results", "x.json"))
        except Exception:
            return False
    scores["DC-1"] = 1.0 if check_DC_1() else 0.0
    scores["overall_score"] = round(sum(scores.values()) / 1, 4)
    return scores
'''

_NO_OVERALL = '''
def grade(**kwargs) -> dict:
    return {"DC-1": 0.0}
'''

_RAISES = '''
def grade(**kwargs) -> dict:
    raise RuntimeError("boom")
'''

_NOT_DICT = '''
def grade(**kwargs):
    return [1, 2, 3]
'''

_SYNTAX = '''
def grade(**kwargs):
    return {{{ not valid
'''


def test_smoke_good():
    r = smoke_run_grade(_GOOD)
    assert r.ok is True
    assert r.returncode == 0
    assert r.scores is not None and "overall_score" in r.scores


def test_smoke_no_overall_score():
    r = smoke_run_grade(_NO_OVERALL)
    assert r.ok is False
    assert r.returncode == 5


def test_smoke_raises():
    r = smoke_run_grade(_RAISES)
    assert r.ok is False
    assert r.returncode == 3
    assert "boom" in r.error_detail


def test_smoke_not_dict():
    r = smoke_run_grade(_NOT_DICT)
    assert r.ok is False
    assert r.returncode == 4


def test_smoke_syntax_error():
    r = smoke_run_grade(_SYNTAX)
    assert r.ok is False
    assert r.returncode != 0


def test_smoke_empty():
    r = smoke_run_grade("")
    assert r.ok is False
