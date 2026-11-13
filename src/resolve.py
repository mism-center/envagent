"""Resolve before building.

An image build is the most expensive way to learn that a pin does not exist,
that two requirements cannot be met together, or that a package ships no wheel
for the interpreter. The index already knows all three. `check()` asks a real
resolver (`uv pip compile`) for the answer in seconds, before a Kaniko pod is
ever created.

Two limits are deliberate, and both keep the answer TRUE rather than complete:

* `--no-build`, always. Resolving an sdist means running its build backend,
  i.e. repo-controlled code, and this runs in the agent pod, which holds the
  LLM key and a cluster token. Untrusted code runs in build and verification
  pods only. So a package with no wheel is reported (`needs_build`), not built.
* Index requirements only. A `name @ git+...` line cannot be resolved without
  building it, so it is left out. Dropping a requirement can only make a set
  easier to satisfy: "no solution" for the subset is still "no solution".

So `ok: False` is a proof, `ok: True` is not, and `ok: None` is no opinion
(uv missing, offline, anything unexpected). Callers refuse only on False.

ponytail: pip is still what installs; uv and pip differ at the edges
(pre-releases, multiple indexes). Install with uv too if a refusal here ever
contradicts a build.
"""
from __future__ import annotations

import re
import shutil
import subprocess

_NEEDS_WHEEL = re.compile(r"Wheels are required for `([^`]+)`")
_NAME = re.compile(r"\s*([A-Za-z0-9][\w.\-]*)")


def _key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def index_only(specs: list[str]) -> list[str]:
    """Requirements the index alone can answer: no URLs, VCS refs, paths or flags."""
    return [s for s in specs
            if s.strip() and "@" not in s and "://" not in s and not s.lstrip().startswith((".", "/", "-"))]


def _uv(reqs: list[str], python: str) -> tuple[int, str]:
    if not shutil.which("uv"):
        return -1, "uv not installed"
    try:
        p = subprocess.run(
            ["uv", "pip", "compile", "-", "--no-build", "--python-version", python,
             "--python-platform", "linux", "--no-header", "--no-annotate", "-q"],
            input="\n".join(reqs), capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return -1, str(exc)
    return p.returncode, p.stderr


def check(specs: list[str], python: str, run=None) -> dict:
    """{"ok": True|False|None, "needs_build": [names], "problem": str|None}."""
    reqs, needs_build = index_only(specs), []
    for _ in range(8):  # one round per wheel-less package; 8 is far past anything seen
        if not reqs:
            return {"ok": True, "needs_build": needs_build, "problem": None}
        rc, err = (run or _uv)(reqs, python)  # looked up per call so tests can stub _uv
        if rc == 0:
            return {"ok": True, "needs_build": needs_build, "problem": None}
        if "No solution found" not in err:
            break
        names = {_key(n) for n in _NEEDS_WHEEL.findall(err)}
        if not names:
            text = re.sub(r"[ \t]*[×╰─▶│]+[ \t]*", " ", err)
            return {"ok": False, "needs_build": needs_build,
                    "problem": re.sub(r"\s+", " ", text).strip()[:900]}
        # No wheel is not a conflict: note it, drop it, and keep checking the rest.
        needs_build += sorted(names - set(needs_build))
        reqs = [r for r in reqs if _key(_NAME.match(r).group(1)) not in names]
    return {"ok": None, "needs_build": needs_build, "problem": None}


def best_python(specs: list[str], candidates: list[str], run=None) -> tuple[str | None, dict]:
    """First candidate the requirements resolve on, preferring one that needs
    no source build. (None, first result) when none is proven to resolve."""
    results = [(py, check(specs, py, run)) for py in candidates]
    for want_clean in (True, False):
        for py, r in results:
            if r["ok"] and (not want_clean or not r["needs_build"]):
                return py, r
    return None, (results[0][1] if results else {"ok": None, "needs_build": [], "problem": None})
