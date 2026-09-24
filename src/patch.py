"""Apply one typed remediation action to an EnvSpec.

One action per attempt, always. Multi-action repairs make attribution
impossible, and attribution is the whole point of the attempt record.

Every action returns a *new* spec (the previous one stays intact for
best-so-far rollback) plus a flag saying whether the base digest must be
re-resolved -- changing the base image invalidates the pin.
"""

from __future__ import annotations

import re
import shlex
from typing import NamedTuple

from envspec import EnvSpec, MountContract

ACTIONS = (
    "ADD_APT_PKG",
    "ADD_PKG",
    "PIN_PKG",
    "UNPIN_PKG",
    "CHANGE_INTERPRETER_VERSION",
    "CHANGE_BASE_IMAGE",
    "SWITCH_INSTALLER",
    "SWITCH_INSTALL_MODE",
    "SET_ENV_VAR",
    "ADD_PRE_INSTALL_CMD",
    "FIX_MOUNT_CONTRACT",
    # Correct the annotation's entry point to one the repo demonstrably ships
    # (driver.py checks grounding against evidence.examples and caps it at two
    # per job). Recorded on the verdict as a proposal against the annotation.
    "SET_ENTRYPOINT",
    # The example was killed at the L3 deadline while running. Extend it once
    # or twice (driver bounds it by l3_timeout_max_s); a passing run then
    # proposes `expected_runtime_s` to the annotation.
    "SET_L3_TIMEOUT",
    "ESCALATE",
)

_MOUNT_FIELDS = ("code_path", "input_path", "output_path", "workdir", "extra_path", "writable_copy")
_INSTALLERS = ("pip", "conda", "mamba", "renv", "pkg")


class PatchResult(NamedTuple):
    spec: EnvSpec
    needs_digest: bool     # base image changed -> re-resolve the digest
    note: str              # human-readable description of what changed


class PatchError(ValueError):
    """The action or its argument is not applicable to this spec."""


def req_name(spec_str: str) -> str:
    """Requirement string -> normalised distribution name (PEP 503-ish).

    `numpy<2` -> `numpy`, `scikit_learn==1.0` -> `scikit-learn`. Used so PIN/UNPIN
    replace the existing entry instead of appending a conflicting duplicate.
    """
    head = re.split(r"[\s=<>!~;\[]", spec_str.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", head).lower()


def _replace_req(specs: list[str], new: str) -> list[str]:
    """Substitute in place if the distribution is already listed, else append."""
    name = req_name(new)
    out, hit = [], False
    for s in specs:
        if req_name(s) == name:
            if not hit:
                out.append(new)
                hit = True
        else:
            out.append(s)
    if not hit:
        out.append(new)
    return out


def _kv(arg: str, action: str) -> tuple[str, str]:
    if "=" not in arg:
        raise PatchError(f"{action} needs KEY=VALUE, got {arg!r}")
    k, v = arg.split("=", 1)
    return k.strip(), v.strip()


# ADD_PRE_INSTALL_CMD is free shell, and the first corpus run showed what free
# shell gets used for when a probe is wrong: shim modules written into
# site-packages (`opencv_python/__init__.py` containing `from cv2 import *`,
# `ipython.py` containing `from IPython import *`) so the L1 import check would
# pass, and an /etc/hosts entry for pypi.org to route around a DNS outage. Each
# made a rung report success for an image that had not earned it. The patterns
# below name those shapes; the message tells the agent which typed action owns
# the real fix.
_PRE_INSTALL_FORBIDDEN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"site-packages|dist-packages|getsitepackages|getusersitepackages|"
                r"sysconfig\.get_path|\.libPaths\(|/usr/lib/R/(?:site-)?library|/usr/local/lib/R"),
     "writes into the interpreter's package directory -- a package belongs in "
     "pkg_specs (ADD_PKG / PIN_PKG), and an import-name mismatch is a probe bug, "
     "not an image bug"),
    (re.compile(r"/etc/hosts|/etc/resolv\.conf|resolv\.conf"),
     "rewrites name resolution -- a DNS/registry outage is INFRA_UNAVAILABLE and "
     "is retried at no cost, never repaired in the spec"),
    (re.compile(r"\bpip\s+(?:install|download)\b|\binstall\.packages\s*\(|\bmicromamba\s+install\b|"
                r"\bconda\s+install\b|\bR\s+CMD\s+INSTALL\b"),
     "installs a package through the shell -- use ADD_PKG / PIN_PKG so the "
     "dependency is in pkg_specs where the lockfile, the L1 probe and the "
     "failure attribution can see it"),
    (re.compile(r"\bln\s+-s\S*\s+\S*python|/usr/local/bin/python\b"),
     "rewires the interpreter -- pick a base with CHANGE_INTERPRETER_VERSION / "
     "CHANGE_BASE_IMAGE"),
    (re.compile(r"ENVBUILD_|::envbuild::"),
     "touches envbuild's own probe/marker machinery"),
]


def forbidden_pre_install(cmd: str) -> str | None:
    """Why this ADD_PRE_INSTALL_CMD argument is refused, or None if it is fine."""
    for pat, why in _PRE_INSTALL_FORBIDDEN:
        if pat.search(cmd or ""):
            return why
    return None


def apply(spec: EnvSpec, action: str, arg: str = "") -> PatchResult:
    """Apply `action` with `arg`. Raises PatchError on an unusable argument."""
    if action not in ACTIONS:
        raise PatchError(f"unknown action {action!r}")
    new = spec.copy()
    arg = (arg or "").strip()

    if action == "ESCALATE":
        # No spec change -- the driver turns this into a verdict.
        return PatchResult(new, False, "escalated; spec unchanged")

    if action == "SET_ENTRYPOINT":
        # The spec's `entrypoint` is informational in mounted mode (nothing is
        # baked); the driver updates the job's L2/L3 command alongside.
        if not arg:
            raise PatchError("SET_ENTRYPOINT requires a command")
        try:
            new.entrypoint = shlex.split(arg)
        except ValueError as exc:
            raise PatchError(f"SET_ENTRYPOINT: unparseable command: {exc}") from exc
        return PatchResult(new, False, f"entrypoint -> {arg}")

    if not arg:
        raise PatchError(f"{action} requires an argument")

    if action == "ADD_APT_PKG":
        # One action may name a family (`libgl1 libglib2.0-0 libxcb1`): still one
        # attributable repair, but the packages are stored one per entry so a
        # later single-package repair dedupes against them.
        pkgs = [p for p in arg.split() if p]
        if not pkgs or not all(re.fullmatch(r"[a-z0-9][a-z0-9+.\-]*", p) for p in pkgs):
            raise PatchError(f"not a Debian package list: {arg!r}")
        fresh = [p for p in pkgs if p not in new.apt_packages]
        if not fresh:
            raise PatchError(f"apt package {arg!r} already present")
        new.apt_packages.extend(fresh)
        return PatchResult(new, False, f"apt += {' '.join(fresh)}")

    if action == "ADD_PKG":
        if any(req_name(s) == req_name(arg) for s in new.pkg_specs):
            raise PatchError(f"package {arg!r} already present")
        new.pkg_specs.append(arg)
        return PatchResult(new, False, f"pkg += {arg}")

    if action == "PIN_PKG":
        if not re.search(r"[=<>~!]", arg):
            raise PatchError("PIN_PKG needs a version constraint, e.g. numpy==1.26.4")
        new.pkg_specs = _replace_req(new.pkg_specs, arg)
        return PatchResult(new, False, f"pin {arg}")

    if action == "UNPIN_PKG":
        name = req_name(arg)
        if not any(req_name(s) == name for s in new.pkg_specs):
            raise PatchError(f"{arg!r} is not in pkg_specs")
        new.pkg_specs = [req_name(s) if req_name(s) == name else s for s in new.pkg_specs]
        return PatchResult(new, False, f"unpin {name}")

    if action == "CHANGE_INTERPRETER_VERSION":
        repo = new.base_image.split(":", 1)[0]
        tag = new.base_image.split(":", 1)[1] if ":" in new.base_image else ""
        # Keep the tag's flavour suffix ("3.11-slim" -> "3.10-slim").
        suffix = "-" + tag.split("-", 1)[1] if "-" in tag else ""
        new.base_image = f"{repo}:{arg}{suffix}"
        new.base_digest = ""
        return PatchResult(new, True, f"base -> {new.base_image}")

    if action == "CHANGE_BASE_IMAGE":
        if ":" not in arg:
            raise PatchError("CHANGE_BASE_IMAGE needs repo:tag")
        new.base_image = arg
        new.base_digest = ""
        return PatchResult(new, True, f"base -> {arg}")

    if action == "SWITCH_INSTALLER":
        if arg not in _INSTALLERS:
            raise PatchError(f"installer must be one of {_INSTALLERS}")
        if arg == new.pkg_manager:
            raise PatchError(f"already using {arg}")
        new.pkg_manager = arg
        return PatchResult(new, False, f"installer -> {arg}")

    if action == "SWITCH_INSTALL_MODE":
        if arg not in ("mounted", "installed"):
            raise PatchError("install mode must be 'mounted' or 'installed'")
        if arg == new.install_mode:
            raise PatchError(f"already in {arg} mode")
        new.install_mode = arg
        return PatchResult(new, False, f"install_mode -> {arg}")

    if action == "SET_ENV_VAR":
        k, v = _kv(arg, action)
        new.env_vars[k] = v
        return PatchResult(new, False, f"env {k}={v}")

    if action == "ADD_PRE_INSTALL_CMD":
        if arg in new.pre_install:
            raise PatchError("command already present")
        bad = forbidden_pre_install(arg)
        if bad:
            raise PatchError(f"ADD_PRE_INSTALL_CMD refused: {bad}")
        new.pre_install.append(arg)
        return PatchResult(new, False, f"pre_install += {arg}")

    if action == "SET_L3_TIMEOUT":
        if not re.fullmatch(r"\d{2,5}", arg) or int(arg) < 60:
            raise PatchError("SET_L3_TIMEOUT needs a whole number of seconds >= 60, e.g. 1200")
        secs = int(arg)
        if new.l3_timeout_s is not None and secs <= new.l3_timeout_s:
            raise PatchError(f"l3_timeout_s is already {new.l3_timeout_s}; an extension must be longer")
        new.l3_timeout_s = secs
        return PatchResult(new, False, f"l3_timeout_s -> {secs}")

    # FIX_MOUNT_CONTRACT -- "field=value"; extra_path appends, the rest replace.
    k, v = _kv(arg, action)
    if k not in _MOUNT_FIELDS:
        raise PatchError(f"mount field must be one of {_MOUNT_FIELDS}")
    m = new.mount
    if k == "extra_path":
        paths = tuple(p for p in v.split(":") if p)
        if set(paths) <= set(m.extra_path):
            raise PatchError("extra_path entries already present")
        new.mount = MountContract(**{**m.to_dict(), "extra_path": tuple(dict.fromkeys(m.extra_path + paths))})
    else:
        if k == "writable_copy":
            if v.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise PatchError("writable_copy must be true or false")
            v = v.lower() in ("true", "1", "yes")
        if getattr(m, k) == v:
            raise PatchError(f"mount.{k} is already {v!r}")
        new.mount = MountContract(**{**m.to_dict(), k: v, "extra_path": m.extra_path})
    return PatchResult(new, False, f"mount.{k} -> {v}")
