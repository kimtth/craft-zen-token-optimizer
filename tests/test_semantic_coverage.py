"""Offline coverage-audit mechanics, not evidence of model audit accuracy."""

from copy import deepcopy
from pathlib import Path

import pytest
from test_semantic import CANDIDATE, SOURCE, _audit, _cases, _json, _make

from zen.optimization.semantic_coverage import report_lines, validate_audit


def _fixture():
    return {
        "requirements": [{
            "id": "r1", "requirement": "Do not disclose secrets.",
            "source_quote": "Never disclose secrets.", "text_verifiable": True,
            "rationale": "An answer can reveal supplied private information.",
            "links": [{"phase": "validation", "case_id": "validation-1",
                       "rationale": "The boundary request exercises the disclosure prohibition."}],
        }, {
            "id": "r2", "requirement": "Report clearly.",
            "source_quote": "Report the result clearly.", "text_verifiable": True,
            "rationale": "Clarity can be assessed in answer text.", "links": [],
        }],
        "unsupported_criteria": [{
            "phase": "holdout", "case_id": "holdout-0", "field": "required_meaning",
            "index": 0, "rationale": "Scripted unsupported criterion warning.",
        }],
    }


def test_coverage_is_derived_per_phase_and_raw_evidence_is_retained():
    raw = _fixture()
    original = deepcopy(raw)
    result = validate_audit(raw, SOURCE, _cases())
    assert raw == original
    assert result["requirements"][0]["coverage"] == {
        "validation": "TESTED", "holdout": "UNTESTED",
    }
    assert result["requirements"][1]["coverage"] == {
        "validation": "UNTESTED", "holdout": "UNTESTED",
    }
    assert result["unsupported_criteria"][0]["criterion"] == "Explain the supplied result"
    report = "\n".join(report_lines(result))
    assert "r1: UNTESTED" in report and "r2: UNTESTED" in report
    assert "Never disclose secrets." in report
    assert "Scripted unsupported criterion warning." in report
    assert "not a passing answer" in report
    assert "inventory may be incomplete" in report


def test_unverifiable_execution_is_not_tested_or_untested():
    raw = _fixture()
    row = raw["requirements"][1]
    row.update(requirement="Run tests before responding.", source_quote="Run tests.",
               text_verifiable=False, rationale="Actual execution needs tool evidence.")
    result = validate_audit(raw, SOURCE + "Run tests.", _cases())
    assert set(result["requirements"][1]["coverage"].values()) == {"NOT_TEXT_VERIFIABLE"}


@pytest.mark.parametrize("mutation", [
    lambda a: a.update(requirements=[]),
    lambda a: a.update(unsupported_criteria=None),
    lambda a: a["requirements"].append(deepcopy(a["requirements"][0])),
    lambda a: a["requirements"][0].update(source_quote="fabricated quote"),
    lambda a: a["requirements"][0].update(source_quote=" "),
    lambda a: a["requirements"][0].update(text_verifiable="true"),
    lambda a: a["requirements"][0].update(text_verifiable=False),
    lambda a: a["requirements"][0].update(rationale=""),
    lambda a: a["requirements"][0].update(links=[None]),
    lambda a: a["requirements"][0]["links"][0].update(case_id="unknown"),
    lambda a: a["requirements"][0]["links"][0].update(phase="holdout"),
    lambda a: a["requirements"][0]["links"][0].update(phase=[]),
    lambda a: a["requirements"][0]["links"].append(
        deepcopy(a["requirements"][0]["links"][0])),
    lambda a: a["unsupported_criteria"][0].update(field="inquiry"),
    lambda a: a["unsupported_criteria"][0].update(field=[]),
    lambda a: a["unsupported_criteria"][0].update(index=True),
    lambda a: a["unsupported_criteria"][0].update(index=-1),
    lambda a: a["unsupported_criteria"][0].update(index=100),
    lambda a: a["unsupported_criteria"][0].update(phase="validation"),
    lambda a: a["unsupported_criteria"].append(deepcopy(a["unsupported_criteria"][0])),
])
def test_invalid_provenance_or_references_are_rejected(mutation):
    raw = _fixture()
    mutation(raw)
    with pytest.raises((ValueError, TypeError)):
        validate_audit(raw, SOURCE, _cases())


def test_audit_is_frozen_before_answers_and_does_not_leak_to_target(tmp_path):
    def before(stage, _system, _user):
        if stage == "TASK":
            saved = _json(harness.optimizer.root / "case-audit.json")
            assert saved["status"] == "COMPLETE"
            assert saved == harness.optimizer.details["case_audit"]

    def audit(system, user):
        import json

        payload = json.loads(user)
        assert set(payload) == {"source", "cases"}
        assert payload["source"] == SOURCE
        assert payload["cases"] == _cases()
        assert CANDIDATE not in user
        result = _audit(system, user)
        result["requirements"][0]["requirement"] = "PRIVATE_AUDIT_SENTINEL"
        return result

    harness = _make(tmp_path, responses={"AUDIT": [audit]}, before=before)
    result = harness.run()
    assert result.decision == "VERIFIED"
    stages = [stage for stage, _, _ in harness.script.calls]
    assert stages.index("CASES") < stages.index("AUDIT") < stages.index("TASK")
    assert stages.count("AUDIT") == 1
    assert result.calls == 16
    assert all("PRIVATE_AUDIT_SENTINEL" not in system + user
               for stage, system, user in harness.script.calls if stage != "AUDIT")
    assert _json(Path(result.run_directory) / "cases.json") == _cases()


def test_coverage_warnings_are_visible_without_changing_acceptance(tmp_path):
    harness = _make(tmp_path, responses={"AUDIT": [_fixture()]})
    result = harness.run()
    assert result.decision == "VERIFIED"
    assert result.details["case_audit"]["requirements"][0]["coverage"]["holdout"] == "UNTESTED"
    report = (Path(result.run_directory) / "report.md").read_text(encoding="utf-8")
    assert "r1: UNTESTED" in report
    assert "Scripted unsupported criterion warning." in report
    assert harness.script.counts["REPAIR"] == 0
    assert _json(Path(result.run_directory) / "summary.json")["details"]["case_audit"] == (
        result.details["case_audit"]
    )


@pytest.mark.parametrize("budget", [3, 4])
def test_audit_budget_or_schema_failure_retains_draft_and_stops_before_answers(tmp_path, budget):
    harness = _make(tmp_path, budget=budget, responses={"AUDIT": ["invalid", "invalid"]})
    result = harness.run()
    assert result.decision == "REVIEW_REQUIRED"
    assert result.calls <= budget
    assert result.details["case_audit"]["status"] == "ERROR"
    assert result.details["errors"]
    assert harness.script.counts["TASK"] == 0
    assert harness.script.counts["FINAL"] == 0
    assert Path(result.details["artifacts"]["draft"]).is_file()
    assert result.details["artifacts"]["optimized"] is None


def test_failed_case_generation_does_not_run_audit(tmp_path):
    harness = _make(tmp_path, responses={"CASES": ["bad", "bad"]})
    result = harness.run()
    assert result.details["case_audit"]["status"] == "NOT_RUN"
    assert harness.script.counts["AUDIT"] == 0