"""Evidence -> base image, package manager, install mode, and a resolved digest.

A rule table, not a classifier. At this stage a table you can read beats a model
you cannot debug, and every row is a thing you can point at when a base choice
turns out wrong.

The digest resolution is the load-bearing half: `EnvSpec.base_image` must never
reach the rendered Dockerfile as a bare tag, or a model that verified in March
silently stops verifying in June and "verified" becomes a lie.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

# Rule table, first match wins. Order matters: conda beats plain python because
# an environment.yml means the author already fought the solver and won.
BASE_TABLE = [
    ("conda_env",  "mambaorg/micromamba:1.5", "mamba"),
    ("r",          "rocker/r-ver:4.4.1",      "renv"),
    ("julia",      "julia:1.10",              "pkg"),
    ("python",     "python:3.11-slim",        "pip"),
    ("unknown",    "ubuntu:24.04",            "pip"),
]
_PY_TAG = re.compile(r"(\d+\.\d+)")


@dataclass
class BaseChoice:
    base_image: str
    pkg_manager: str
    install_mode: str
    reason: str
    evidence_keys: list[str]


def _python_tag(evidence: dict, annotation: dict) -> str:
    """Pick the interpreter minor version, preferring proven sources.

    CI is first on purpose: a green workflow is a *verified* version on a known
    OS, which beats a `requires-python` floor that nobody ever tested against.
    """
    for ci in evidence.get("ci") or []:
        for v in ci.get("python_versions") or []:
            m = _PY_TAG.search(str(v))
            if m:
                return m.group(1)
    for src in (annotation.get("language_version"),
                evidence.get("python", {}).get("requires_python")):
        if not src:
            continue
        m = _PY_TAG.search(str(src))
        # ">=3.9" is a floor, not a target -- only trust an exact-looking pin.
        if m and not _is_floor(src):
            return m.group(1)
    return "3.11"


def _is_floor(constraint) -> bool:
    """`>= 4.0` is a floor nobody tested against; `4.3.1` is a target.

    Searches the whole string, not just its start: DESCRIPTION writes the
    constraint as `R (>= 4.0)`, where the operator is four characters in.
    """
    return bool(re.search(r"[><^~*]", str(constraint or "")))


def _r_tag(evidence: dict, annotation: dict) -> str:
    """rocker tag, same priority order as the Python interpreter pick.

    A green CI first, then an explicit pin, then the default. `Depends: R (>= 4.0)`
    must NOT become `rocker/r-ver:4.0` -- that tag exists, so the mistake builds
    green on a five-year-old R and only surfaces when a current package refuses
    to install.
    """
    for ci in evidence.get("ci") or []:
        for image in ci.get("containers") or []:
            m = re.search(r"rocker/r-ver:([\d.]+)", str(image))
            if m:
                return m.group(1)
    r = evidence.get("r") or {}
    for raw, num in ((annotation.get("language_version"), annotation.get("language_version")),
                     (r.get("r_version_raw"), r.get("r_version"))):
        if num and not _is_floor(raw):
            m = _PY_TAG.search(str(num))
            if m:
                return str(num).strip()
    return "4.4.1"


def guess_install_mode(evidence: dict) -> tuple[str, str]:
    """`mounted` unless the repo entangles installation with its own source.

    Forcing a compiled-extension repo into mounted mode produces weird failures
    in the tail, so this is a declared field with SWITCH_INSTALL_MODE as the
    escape hatch when the guess is wrong.
    """
    # An R package is installable by construction: `library(mbmm)` only works if
    # the package was installed, and mounting R/ at /model does not install it.
    # Mounted mode would fail at L2 every time and burn an attempt on
    # SWITCH_INSTALL_MODE to learn what DESCRIPTION already said.
    if (evidence.get("r") or {}).get("is_package") and "NAMESPACE" in (evidence.get("markers") or {}):
        return "installed", "R package (DESCRIPTION + NAMESPACE): library() needs it installed"

    c = evidence.get("compiled") or {}
    reasons = []
    if c.get("ext_modules"):
        reasons.append("setup.py declares ext_modules")
    if c.get("pyx"):
        reasons.append(f"{len(c['pyx'])} .pyx sources")
    if c.get("build_requires_compiler"):
        reasons.append("build-system requires a compiler toolchain")
    if c.get("makefile") or c.get("cmake"):
        reasons.append("Makefile/CMakeLists present")
    if c.get("editable_hint"):
        reasons.append("docs call for `pip install -e`")
    if reasons:
        return "installed", "; ".join(reasons)
    return "mounted", "no compiled extensions or build step found"


def select(evidence: dict, annotation: dict | None = None) -> BaseChoice:
    """Apply the table. `annotation` is the model record's declared language."""
    annotation = annotation or {}
    langs = evidence.get("languages") or {}
    declared = (annotation.get("language") or "").lower()
    keys = []

    if evidence.get("conda"):
        kind, keys = "conda_env", [evidence["conda"].get("file", "environment.yml")]
    elif declared.startswith("r") or evidence.get("r") or langs.get("r"):
        kind, keys = "r", ["DESCRIPTION" if evidence.get("r") else "*.R"]
    elif declared == "julia" or evidence.get("julia") or langs.get("julia"):
        kind, keys = "julia", ["Project.toml"]
    elif declared.startswith("python") or langs.get("python"):
        kind, keys = "python", sorted(evidence.get("python", {}).get("sources") or ["*.py"])
    else:
        kind, keys = "unknown", []

    image, manager = next((img, mgr) for k, img, mgr in BASE_TABLE if k == kind)
    if kind == "python":
        image = f"python:{_python_tag(evidence, annotation)}-slim"
    if kind == "r":
        image = f"rocker/r-ver:{_r_tag(evidence, annotation)}"

    mode, mode_reason = guess_install_mode(evidence)
    return BaseChoice(image, manager, mode, f"{kind}: {mode_reason}", keys)


def resolve_digest(image: str, timeout_s: int = 120) -> str:
    """tag -> `sha256:...`, once, at synthesis time.

    Uses `docker buildx imagetools inspect` (talks to the registry, no pull) and
    falls back to `docker manifest inspect`. Raises rather than returning a bare
    tag: an unpinned base is worse than a failed job.
    """
    attempts = [
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{.Manifest.Digest}}"],
        ["docker", "manifest", "inspect", "--verbose", image],
    ]
    errors = []
    for cmd in attempts:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{cmd[1]}: {exc}")
            continue
        if out.returncode != 0:
            errors.append(f"{cmd[1]}: {out.stderr.strip()[:200]}")
            continue
        text = out.stdout.strip()
        m = re.search(r"sha256:[0-9a-f]{64}", text)
        if m:
            return m.group(0)
        try:                                    # manifest inspect --verbose JSON
            data = json.loads(text)
            data = data[0] if isinstance(data, list) else data
            d = data.get("Descriptor", {}).get("digest")
            if d:
                return d
        except (json.JSONDecodeError, AttributeError, IndexError):
            pass
        errors.append(f"{cmd[1]}: no digest in output")
    raise RuntimeError(f"could not resolve digest for {image}: {'; '.join(errors)}")
