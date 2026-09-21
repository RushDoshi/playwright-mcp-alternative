"""JUnit / Surefire XML parsing (Layer A domain).

Playwright's JUnit reporter and Maven Surefire both emit the same ``<testsuite>`` XML, so
one parser serves both. Produces per-suite pass/fail/skip plus the individual failures
that ``failure_triage`` will classify.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass
class TestCaseResult:
    __test__ = False  # not a pytest test class
    name: str
    classname: str
    status: str            # "passed" | "failed" | "skipped"
    time: float = 0.0
    message: str = ""      # failure/error message
    trace: str = ""        # stack trace / details

    @property
    def is_failure(self) -> bool:
        return self.status == "failed"


@dataclass
class SuiteResult:
    name: str
    cases: list[TestCaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.cases if c.status == "skipped")

    def failures(self) -> list[TestCaseResult]:
        return [c for c in self.cases if c.is_failure]


def _parse_testsuite(node: ET.Element) -> SuiteResult:
    suite = SuiteResult(name=node.get("name", "suite"))
    for tc in node.findall("testcase"):
        name = tc.get("name", "")
        classname = tc.get("classname", "")
        time = float(tc.get("time", "0") or 0)
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None or error is not None:
            el = failure if failure is not None else error
            suite.cases.append(
                TestCaseResult(name, classname, "failed", time,
                               message=el.get("message", ""), trace=(el.text or "").strip())
            )
        elif skipped is not None:
            suite.cases.append(TestCaseResult(name, classname, "skipped", time))
        else:
            suite.cases.append(TestCaseResult(name, classname, "passed", time))
    return suite


def parse_xml_text(xml: str) -> list[SuiteResult]:
    """Parse a JUnit XML string. Handles both a single <testsuite> root and a
    <testsuites> wrapper."""
    root = ET.fromstring(xml)
    if root.tag == "testsuite":
        return [_parse_testsuite(root)]
    return [_parse_testsuite(ts) for ts in root.findall("testsuite")]


def parse_files(paths: list[str | Path]) -> list[SuiteResult]:
    suites: list[SuiteResult] = []
    for p in paths:
        suites.extend(parse_xml_text(Path(p).read_text(encoding="utf-8")))
    return suites


def summarize(suites: list[SuiteResult]) -> dict[str, dict[str, int]]:
    """Per-suite scorecard: {suite_name: {total, passed, failed, skipped}}."""
    return {
        s.name: {"total": s.total, "passed": s.passed, "failed": s.failed, "skipped": s.skipped}
        for s in suites
    }
