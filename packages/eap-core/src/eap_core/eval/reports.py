"""Eval report emitters — JSON, HTML, JUnit XML."""
from __future__ import annotations

import html
from xml.etree import ElementTree as ET

from eap_core.eval.runner import EvalReport


def emit_json(report: EvalReport) -> str:
    return report.model_dump_json(indent=2)


def emit_html(report: EvalReport) -> str:
    rows: list[str] = []
    for case in report.cases:
        score_cells = " ".join(
            f"<td>{html.escape(name)}: {res.score:.2f}</td>"
            for name, res in case.scores.items()
        )
        status = "PASS" if case.passed else "FAIL"
        rows.append(
            f"<tr class='{status.lower()}'><td>{html.escape(case.case_id)}</td>"
            f"{score_cells}<td>{status}</td></tr>"
        )
    aggregate_lines = "".join(
        f"<li>{html.escape(name)}: <strong>{value:.3f}</strong></li>"
        for name, value in report.aggregate.items()
    )
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>EAP-Core eval report</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; }}
  table {{ border-collapse: collapse; }}
  td, th {{ padding: .5rem 1rem; border-bottom: 1px solid #ddd; }}
  tr.pass {{ background: #f1faf1; }}
  tr.fail {{ background: #fbeaea; }}
</style></head>
<body>
<h1>EAP-Core eval report</h1>
<p>Threshold: <strong>{report.threshold}</strong> &mdash;
Passed: {report.passed_count} / Failed: {report.failed_count}</p>
<h2>Aggregate</h2><ul>{aggregate_lines}</ul>
<h2>Cases</h2>
<table><tr><th>Case</th><th>Scores</th><th>Status</th></tr>
{''.join(rows)}
</table>
</body></html>
"""


def emit_junit(report: EvalReport) -> str:
    suite = ET.Element(
        "testsuite",
        attrib={
            "name": "eap-core-eval",
            "tests": str(len(report.cases)),
            "failures": str(report.failed_count),
        },
    )
    for case in report.cases:
        scores_str = ", ".join(f"{n}={r.score:.3f}" for n, r in case.scores.items())
        tc = ET.SubElement(
            suite,
            "testcase",
            attrib={"name": case.case_id, "classname": "eap_core.eval"},
        )
        if not case.passed:
            failure = ET.SubElement(
                tc,
                "failure",
                attrib={"message": f"Below threshold {report.threshold}: {scores_str}"},
            )
            failure.text = scores_str
    return ET.tostring(suite, encoding="unicode")
