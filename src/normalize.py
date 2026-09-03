"""stderr -> a stable error signature.

Nothing in Phase 0 reads these signatures. Write them anyway: they are the
load-bearing piece of the memory layer that indexes this corpus later, and a
signature that only starts being recorded in Phase 1 has no history to learn
from. Golden tests live in scripts/test_envbuild.py.
"""

from __future__ import annotations

import hashlib
import re

# Substitutions run in order; each kills one source of per-run variance.
_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\x1b\[[0-9;]*[A-Za-z]"), ""),                       # ANSI colour
    (re.compile(r"\b[0-9a-f]{12,64}\b"), "<HASH>"),                   # digests, blob ids
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<TS>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<ADDR>"),
    (re.compile(r"/tmp/[^\s:'\"]+"), "<TMP>"),
    (re.compile(r"(?:/[\w.+-]+){2,}"), "<PATH>"),                     # absolute paths
    (re.compile(r"\bline \d+\b"), "line <N>"),
    (re.compile(r"\b\d+\.\d+(?:\.\d+)+\b"), "<VER>"),                 # 1.26.4
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:[kKMG]i?B|ms|s)\b"), "<SIZE>"),
    (re.compile(r"\b\d{3,}\b"), "<NUM>"),                             # pids, byte counts
    (re.compile(r"[ \t]+"), " "),
]

# Lines worth keeping. Build logs are mostly progress noise; the diagnosis is in
# the handful of lines that say something went wrong.
_SIGNAL = re.compile(
    r"error|fatal|Traceback|Exception|not found|No such file|failed|cannot|"
    r"unable to|conflict|E: |undefined symbol|Permission denied",
    re.IGNORECASE,
)
_MAX_LINES = 20


def normalize_line(line: str) -> str:
    out = line.strip()
    for pat, rep in _SUBS:
        out = pat.sub(rep, out)
    return out.strip()


def salient_lines(stderr: str) -> list[str]:
    """The lines a human would read first: signal lines if any, else the tail."""
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    hits = [ln for ln in lines if _SIGNAL.search(ln)]
    picked = hits or lines[-_MAX_LINES:]
    return [normalize_line(ln) for ln in picked[-_MAX_LINES:]]


def signature(stderr: str) -> str:
    """Stable hash over the normalised salient lines."""
    body = "\n".join(salient_lines(stderr))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()
