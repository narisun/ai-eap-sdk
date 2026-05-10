import json
import xml.etree.ElementTree as ET

from eap_core.eval.faithfulness import (
    ClaimResult,
    FaithfulnessResult,
    Verdict,
)
from eap_core.eval.reports import emit_html, emit_json, emit_junit
from eap_core.eval.runner import CaseResult, EvalReport
from eap_core.eval.trajectory import Trajectory


def _sample_report() -> EvalReport:
    traj = Trajectory(
        request_id="r1", final_answer="Paris is the capital.", retrieved_contexts=["Paris..."]
    )
    score = FaithfulnessResult(
        request_id="r1",
        score=0.5,
        per_claim=[ClaimResult(claim="Paris is the capital.", verdict=Verdict.NOT_FOUND)],
    )
    return EvalReport(
        cases=[
            CaseResult(case_id="c1", trajectory=traj, scores={"faithfulness": score}, passed=False)
        ],
        aggregate={"faithfulness": 0.5},
        threshold=0.7,
        passed_count=0,
        failed_count=1,
    )


def test_emit_json_round_trips_through_loads():
    report = _sample_report()
    out = emit_json(report)
    parsed = json.loads(out)
    assert parsed["aggregate"]["faithfulness"] == 0.5
    assert parsed["cases"][0]["case_id"] == "c1"


def test_emit_html_contains_score_and_threshold():
    report = _sample_report()
    out = emit_html(report)
    assert "<html" in out.lower()
    assert "0.5" in out
    assert "c1" in out
    assert "threshold" in out.lower()


def test_emit_junit_is_valid_xml_with_one_failure():
    report = _sample_report()
    out = emit_junit(report)
    root = ET.fromstring(out)
    assert root.tag == "testsuite"
    cases = root.findall("testcase")
    assert len(cases) == 1
    assert cases[0].attrib["name"] == "c1"
    assert cases[0].find("failure") is not None
