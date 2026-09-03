"""Apply one typed remediation action to an EnvSpec.

One action per attempt, always. Multi-action repairs make attribution
impossible, and attribution is the whole point of the attempt record.

Every action returns a *new* spec (the previous one stays intact for
best-so-far rollback) plus a flag saying whether the base digest must be
re-resolved -- changing the base image invalidates the pin.
"""

from __future__ import annotations

import re
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
    "ESCALATE",
)

_MOUNT_FIELDS = ("code_path", "input_path", "output_path", "workdir", "extra_path")
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


def apply(spec: EnvSpec, action: str, arg: str = "") -> PatchResult:
    """Apply `action` with `arg`. Raises PatchError on an unusable argument."""
    if action not in ACTIONS:
        raise PatchError(f"unknown action {action!r}")
    new = spec.copy()
    arg = (arg or "").strip()

    if action == "ESCALATE":
        # No spec change -- the driver turns this into a verdict.
        return PatchResult(new, False, "escalated; spec unchanged")

    if not arg:
        raise PatchError(f"{action} requires an argument")

    if action == "ADD_APT_PKG":
        if arg in new.apt_packages:
            raise PatchError(f"apt package {arg!r} already present")
        new.apt_packages.append(arg)
        return PatchResult(new, False, f"apt += {arg}")

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
        new.pre_install.append(arg)
        return PatchResult(new, False, f"pre_install += {arg}")

    # FIX_MOUNT_CONTRACT -- "field=value"; extra_path appends, the rest replace.
    k, v = _kv(arg, action)
    if k not in _MOUNT_FIELDS:
        raise PatchError(f"mount field must be one of {_MOUNT_FIELDS}")
    m = new.mount
    if k == "extra_path":
        paths = tuple(p for p in v.split(":") if p)
        if set(paths) <= set(m.extra_path):
            raise PatchError("extra_path entries already present")
        new.mount = MountContract(
            m.code_path, m.input_path, m.output_path, m.workdir,
            tuple(dict.fromkeys(m.extra_path + paths)),
        )
    else:
        if getattr(m, k) == v:
            raise PatchError(f"mount.{k} is already {v!r}")
        new.mount = MountContract(**{**m.to_dict(), k: v, "extra_path": m.extra_path})
    return PatchResult(new, False, f"mount.{k} -> {v}")
