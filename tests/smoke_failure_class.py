"""Preserve failure evidence without equating a failed path with product blame."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

ENVIRONMENT_UNAVAILABLE = "environment-unavailable"

FAILURE_CLASS_LABELS = {
    ENVIRONMENT_UNAVAILABLE: "Environment unavailable",
}

# Workflow step output and report-job input carrying the classification.
FAILURE_CLASS_OUTPUT = "failure_class"
FAILURE_CLASS_ENV = "CCH_REPORT_FAILURE_CLASS"


@contextmanager
def failure_phase(name: str):
    """Annotate the original exception with its innermost failed operation."""

    try:
        yield
    except BaseException as error:
        # pytest.fail also inherits directly from BaseException. Never replace
        # the error or suppress interrupts; only preserve context for reporting.
        if not getattr(error, "smoke_phase", None):
            error.smoke_phase = name
        error.smoke_phases = [*getattr(error, "smoke_phases", []), name]
        raise


def emit_failure_diagnostic(diagnostic: dict) -> None:
    """Emit a single JSON output; diagnostics must never mask the test failure."""

    encoded = json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":"))
    print(f"SMOKE_FAILURE_DIAGNOSTIC={encoded}", flush=True)
    github_output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if github_output:
        try:
            with Path(github_output).open("a", encoding="utf-8") as output:
                output.write(f"failure_diagnostic={encoded}\n")
        except OSError as error:
            print(f"Could not publish failure diagnostic: {error}", flush=True)


class EnvironmentUnavailable(AssertionError):
    """The test environment cannot run this scenario.

    Subclasses AssertionError so a live test that does not classify still fails
    the run normally.
    """


def _error_chain(error: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify_failure(error: BaseException) -> str | None:
    """Return the failure class for an error, or None for an unattributed fault."""

    for candidate in _error_chain(error):
        if isinstance(candidate, EnvironmentUnavailable):
            return ENVIRONMENT_UNAVAILABLE
    return None


def emit_failure_class(error: BaseException) -> str | None:
    """Publish the classification as a GitHub Actions step output, if any."""

    failure_class = classify_failure(error)
    if failure_class is None:
        return None
    github_output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"{FAILURE_CLASS_OUTPUT}={failure_class}\n")
    return failure_class


def describe_environment_failure(failure_class: str) -> str:
    """One-line explanation for reports, or an empty string when not environmental."""

    if failure_class != ENVIRONMENT_UNAVAILABLE:
        return ""
    return (
        "Test environment unavailable during relay preflight. "
        "The relay payment path was not verified; check the failure diagnosis."
    )
