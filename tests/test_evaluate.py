"""Тесты оценочного прогона сценариев на фикстурах."""
import json
from datetime import date

import evaluate
from evaluate import SCENARIOS, run_scenarios


def test_all_scenarios_pass_expectations():
    results = run_scenarios()

    assert len(results) == len(SCENARIOS)
    for item in results:
        assert item.passed, (item.scenario.key, item.failed_checks)


def test_baseline_reduces_risk_and_keeps_remaining_issue():
    baseline = next(item for item in run_scenarios() if item.scenario.key == "baseline")
    correction = baseline.correction

    assert correction.initial_plan.risk_score == 21
    assert correction.plan.risk_score == 3
    assert correction.plan.request.start_date == date(2026, 11, 14)
    assert baseline.initial_codes == {
        "LATE_ARRIVAL",
        "PLACE_CLOSED",
        "DAY_OVERLOADED",
        "OPENING_HOURS_CONFLICT",
        "WEATHER_CONFLICT",
    }
    assert baseline.final_codes == {"WEATHER_CONFLICT"}
    assert correction.attempts <= baseline.max_attempts
    assert set(correction.resolved_codes) == baseline.initial_codes - baseline.final_codes


def test_impossible_improvement_is_reported_honestly():
    history = next(item for item in run_scenarios() if item.scenario.key == "history")
    correction = history.correction

    assert correction.attempts == 0
    assert correction.applied == []
    assert not correction.improved
    assert history.initial_codes == history.final_codes
    assert any("остались нерешённые проблемы" in note for note in correction.notes)


def test_json_output_is_machine_readable(capsys):
    exit_code = evaluate.main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["provider"] == "fixtures"
    assert payload["data_kind"] == "fixture"
    assert payload["summary"] == {
        "total": len(SCENARIOS),
        "passed": len(SCENARIOS),
        "failed": 0,
        "all_passed": True,
    }
    for scenario in payload["scenarios"]:
        assert scenario["initial"]["issues"]
        assert scenario["final"]["risk_score"] <= scenario["initial"]["risk_score"]
        assert scenario["checks"]["honest_notes"] is True


def test_scenario_filter(capsys):
    exit_code = evaluate.main(["--scenario", "baseline"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Базовый план" in output
    assert "Жёсткий бюджет" not in output
