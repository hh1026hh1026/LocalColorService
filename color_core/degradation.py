"""Telling a bug apart from an expected fallback.

A feature that degrades gracefully is good; a feature that degrades because of a
programming error and says so only in a WARNING line is not. The difference
matters and the code was not making it.

Concretely: V0.6.2 changed shot LUTs to a chained list, and the face-selective
renderer still did ``Path(full_lut)`` on what was now a list. The resulting
``TypeError`` was caught by the same ``except Exception`` that handles a missing
encoder, so four consecutive renders quietly fell back to the plain timeline and
face protection never ran. It was found by reading logs, days later.

An environmental failure (no encoder, unreadable file, GPU busy) is a legitimate
reason to fall back quietly. A ``TypeError`` never is - it means the two sides of
an interface disagree, and it will not fix itself.
"""

from __future__ import annotations

import traceback
from typing import Any

# Exceptions that indicate the code is wrong rather than the environment.
# These should never be treated as an expected fallback condition.
PROGRAMMING_ERRORS = (
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    NameError,
    UnboundLocalError,
    AssertionError,
)


def is_programming_error(error: BaseException) -> bool:
    return isinstance(error, PROGRAMMING_ERRORS)


def describe_degradation(feature: str, error: BaseException) -> dict[str, Any]:
    """Structured record of a feature falling back, tagged by cause."""
    bug = is_programming_error(error)
    return {
        "feature": feature,
        "error_type": type(error).__name__,
        "error": str(error),
        "cause": "defect" if bug else "environment",
        # A defect is worth the traceback; an expected fallback is not.
        "traceback": traceback.format_exc() if bug else "",
        "severity": "error" if bug else "warning",
    }


def report_degradation(logger, feature: str, error: BaseException) -> dict[str, Any]:
    """Log a fallback at the severity its cause deserves, and return the record."""
    record = describe_degradation(feature, error)
    if record["cause"] == "defect":
        logger.error(
            f"{feature} fell back because of a DEFECT ({record['error_type']}: "
            f"{record['error']}). This is an interface mismatch, not an environment "
            f"limitation, and the feature is silently not running.\n{record['traceback']}"
        )
    else:
        logger.warning(f"{feature} unavailable, falling back: {record['error']}")
    return record
