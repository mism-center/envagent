"""Builder/verifier log bytes -> text a classifier can actually read.

This sits between the pods and everything that consumes their output
(classify, normalize, the attempt record, the agent's `stderr_tail`). It exists
because of what the first corpus run recorded: `error_raw` was the last 8 KB of
a build log, and for most L0 failures that tail was structured progress output
-- BuildKit's `--progress=rawjson` stream, whose actual pip/apt/CRAN messages
sit base64-encoded inside `"data":"..."` fields -- or ANSI-coloured Kaniko INFO
lines. The rule table regexes plain text, so 111 of 176 attempts classified as
UNKNOWN and 51 network outages were repaired as if they were spec bugs. The
normaliser hashed the *encoded* blob, so the same outage produced dozens of
"distinct" signatures.

`decode()` is deterministic and idempotent: applying it to already-decoded text
returns the same text. Keep it that way -- the offline reclassifier
(scripts/reclassify.py) runs it over historical rows.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Kaniko: `INFO[0014] RUN pip install ...`. Keep the payload, drop the prefix --
# the instruction echo is what builder.match_kaniko_step needs, the timestamp
# is per-run noise the normaliser would otherwise have to strip.
_KANIKO_PREFIX = re.compile(r"^(?:INFO|WARN|ERROR|DEBUG|TRACE)\[\d+\]\s+")
# A BuildKit rawjson frame starts with one of these keys. `logs` frames carry
# stream bytes; `vertexes`/`statuses` frames are pure progress.
_RAWJSON_HEAD = re.compile(r'^\s*\{"(?:vertexes|statuses|logs|warnings)"')
# Fallback for a truncated frame `json.loads` cannot parse: pull the payloads
# out with a regex. Base64 alphabet only, so a hex digest never matches.
_B64_DATA = re.compile(r'"data"\s*:\s*"([A-Za-z0-9+/]{8,}={0,2})"')
_PLAIN = re.compile(r'\x1b|"data"\s*:\s*"|^(?:INFO|WARN|ERROR|DEBUG|TRACE)\[\d+\]', re.M)


def _b64(payload: str) -> str:
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _rawjson_frame(line: str) -> list[str]:
    """One rawjson frame -> the stream text it carried (possibly none)."""
    out: list[str] = []
    try:
        frame = json.loads(line)
    except json.JSONDecodeError:
        # Truncated at the 8 KB boundary, typically. Salvage what is intact.
        for m in _B64_DATA.finditer(line):
            out.append(_b64(m.group(1)))
        return out
    for entry in frame.get("logs") or []:
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, str):
            out.append(_b64(data))
    return out


def decode(text: str | None) -> str:
    """Strip ANSI, unwrap progress frames, drop the Kaniko log prefix.

    Order of operations matters: a rawjson frame is recognised on the raw line
    (before ANSI stripping would never touch it anyway), and its decoded payload
    is then treated as ordinary log text. Consecutive duplicate lines collapse
    -- pip prints the same retry warning five times, and five copies tell the
    classifier nothing the first did not.
    """
    if not text:
        return ""
    if not _PLAIN.search(text):
        return text                      # already plain -- idempotence, cheaply
    out: list[str] = []
    for raw in text.splitlines():
        if _RAWJSON_HEAD.match(raw):
            for chunk in _rawjson_frame(raw):
                out.extend(chunk.splitlines())
            continue
        line = _KANIKO_PREFIX.sub("", _ANSI.sub("", raw))
        out.append(line)
    cleaned: list[str] = []
    for line in out:
        line = line.rstrip()
        if not line.strip():
            continue
        if cleaned and cleaned[-1] == line:
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def tail(text: str | None, n: int) -> str:
    """Last `n` bytes of the *decoded* text. Decode first, then cut -- cutting
    an encoded stream first is exactly how the real error got lost."""
    return decode(text)[-n:]
