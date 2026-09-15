"""EnvSpec: the structured build spec, and its deterministic Dockerfile renderer.

The agent never emits Dockerfile text -- it emits an EnvSpec. Every typed repair
action is then a trivial mutation of a field (see patch.py) and rendering is
plain code with unit tests. Two consequences the loop depends on:

  * failure attribution -- the renderer records which EnvSpec field produced each
    Dockerfile instruction, so a failing build step maps back to `apt_packages`
    vs `pkg_specs` for free (see `RenderedDockerfile.steps`).
  * stable hashing -- attempts are compared by spec hash, not by text, so
    whitespace churn does not look like a new attempt.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

SCHEMA_VERSION = "envspec/1"

InstallMode = Literal["mounted", "installed"]
PkgManager = Literal["pip", "conda", "mamba", "renv", "pkg"]

# Package-manager knobs. Kept as plain tables rather than a strategy class:
# five managers, three facts each, no behaviour to override.
#
# A cache mount must only ever hold *downloads*. Its contents are not committed
# into the image, so pointing one at an install prefix silently produces an image
# with nothing installed in it.
#   pip   -- wheel cache; the install itself goes to site-packages. Safe.
#   R     -- `destdir` (below) sends downloads here; the library is untouched. Safe.
#   conda -- NOT cached: the env prefix hardlinks into pkgs/, so caching pkgs/
#            breaks every one of those links when the layer is committed.
# ponytail: no conda download cache at all. Add one via a separate
# `--download-only` directory if the corpus shows conda solves dominating wall clock.
_R_DOWNLOAD_DIR = "/root/.cache/R"
_PKG_CACHE = {
    "pip": "/root/.cache/pip",
    "conda": None,
    "mamba": None,
    "renv": _R_DOWNLOAD_DIR,
    "pkg": _R_DOWNLOAD_DIR,
}
_BOOTSTRAP = {
    "pip": "python -m pip install --upgrade pip setuptools wheel",
    "conda": None,   # base image ships conda
    "mamba": None,   # micromamba image ships mamba
    "renv": None,    # rocker images ship R
    "pkg": None,
}
# Which env var carries MountContract.extra_path for this manager.
_PATH_VAR = {
    "pip": "PYTHONPATH",
    "conda": "PYTHONPATH",
    "mamba": "PYTHONPATH",
    "renv": "R_LIBS",
    "pkg": "R_LIBS",
}


@dataclass(frozen=True)
class MountContract:
    """Where the model's code, inputs and outputs appear at *run* time.

    Frozen: a repair mutates the spec by building a new contract, never by
    reaching into a shared one. `extra_path` is a tuple, not a list, so the
    frozen dataclass stays hashable and has no mutable default.
    """

    code_path: str = "/model"
    input_path: str = "/inputs"
    output_path: str = "/outputs"
    workdir: str = "/model"
    extra_path: tuple[str, ...] = ()      # PYTHONPATH / R_LIBS additions

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["extra_path"] = list(self.extra_path)
        return d

    @staticmethod
    def from_dict(d: dict) -> "MountContract":
        d = dict(d or {})
        d["extra_path"] = tuple(d.get("extra_path") or ())
        return MountContract(**d)


@dataclass
class EnvSpec:
    """The whole build, as data. Rendered by `render()`, mutated by patch.py."""

    base_image: str                                   # "python:3.11-slim"
    base_digest: str                                  # "sha256:..." resolved once
    pkg_manager: PkgManager = "pip"
    install_mode: InstallMode = "mounted"
    schema_version: str = SCHEMA_VERSION
    apt_packages: list[str] = field(default_factory=list)
    pkg_specs: list[str] = field(default_factory=list)
    pre_install: list[str] = field(default_factory=list)
    post_install: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    mount: MountContract = field(default_factory=MountContract)
    entrypoint: list[str] = field(default_factory=list)  # recorded; not baked when mounted
    builder_version: str = "unknown"

    # ---- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["mount"] = self.mount.to_dict()
        return d

    @staticmethod
    def from_dict(d: dict) -> "EnvSpec":
        d = dict(d)
        d["mount"] = MountContract.from_dict(d.get("mount"))
        known = {f.name for f in dataclasses.fields(EnvSpec)}
        spec = EnvSpec(**{k: v for k, v in d.items() if k in known})
        # base_digest is agent-supplied on every patch, and a mistyped one is
        # only caught by the builder -- after a whole attempt has been paid for.
        # Seen twice in the AKS corpus run as a 65-character digest: Kaniko
        # reports `could not parse reference`, which classifies as UNKNOWN,
        # which spends a repair on a typo. Fail here instead -- it is a trust
        # boundary and the check is one regex.
        if spec.base_digest and not _DIGEST_RE.fullmatch(spec.base_digest):
            raise ValueError(
                f"base_digest is not a sha256 digest: {spec.base_digest!r} "
                f"(expected sha256: plus exactly 64 hex characters, got "
                f"{len(spec.base_digest.split(':')[-1])}). Leave base_digest empty "
                f"to have it resolved from base_image instead of typing it out.")
        return spec

    def copy(self) -> "EnvSpec":
        """Deep copy. patch.py mutates the copy so the previous attempt's spec
        stays intact for best-so-far rollback."""
        return EnvSpec.from_dict(copy.deepcopy(self.to_dict()))

    def hash(self) -> str:
        """Canonical hash of the spec -- the attempt identity."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()

    @property
    def image_ref_base(self) -> str:
        """`python:3.11-slim` -> `python-3.11-slim`, safe as a registry path."""
        return self.base_image.replace(":", "-").replace("/", "-")


class Step(NamedTuple):
    """One rendered Dockerfile instruction, tagged with its originating field.

    `index` is 0-based over the rendered instructions; builder.py maps a failing
    BuildKit vertex back onto it, which is what makes `failed_step_kind` free.
    """

    index: int
    # from | env | apt | bootstrap | pkg | cmd | mkdir | workdir | copy |
    # install | entrypoint
    kind: str
    field: str         # the EnvSpec field this came from ("" for structural lines)
    instruction: str   # the rendered text, used for vertex matching


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


class RenderedDockerfile(NamedTuple):
    text: str
    steps: list[Step]


def _run_script(lines: list[str]) -> str:
    r"""Multi-command RUN, joined with `&&` across continuation lines.

    NOT a heredoc (`RUN <<'ENVBUILD'`), which is what this used to emit. Heredoc
    RUN is BuildKit-only syntax: Kaniko does not implement it and runs the
    *delimiter* as the command --

        INFO RUN <<'ENVBUILD'
        INFO Args: [-c <<'ENVBUILD']
        INFO No files were changed, appending empty layer to config.

    -- so the step silently succeeds having done nothing. pre_install and
    post_install are both rendered here, which meant the whole
    ADD_PRE_INSTALL_CMD / ADD_POST_INSTALL_CMD repair vocabulary was a no-op
    that reported success on that backend, and the repair loop then spent its
    budget re-diagnosing a failure its own (correct) repair had never been
    allowed to fix. `&&` runs everywhere.

    Embedded newlines are split into separate commands rather than passed
    through: the heredoc form tolerated them, a single RUN line does not.
    """
    cmds = [ln.strip() for raw in lines for ln in raw.splitlines() if ln.strip()]
    body = " \\\n && ".join(["set -eux"] + cmds)
    return f"RUN {body}"


def _apt_run(packages: list[str]) -> str:
    pkgs = " \\\n      ".join(packages)
    return (
        "RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \\\n"
        "    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \\\n"
        # apt's docker-clean hook deletes the very cache we just mounted.
        "    rm -f /etc/apt/apt.conf.d/docker-clean \\\n"
        " && apt-get update \\\n"
        " && apt-get install -y --no-install-recommends \\\n"
        f"      {pkgs}"
    )


# Ships with every R distribution and cannot be installed: `install.packages("stats")`
# fails with "package 'stats' is a base package, and should not be updated".
# DESCRIPTION lists them under Imports, so they land in pkg_specs honestly and get
# dropped here rather than failing every R build on line one.
R_BASE_PACKAGES = frozenset({
    "base", "compiler", "datasets", "grDevices", "graphics", "grid", "methods",
    "parallel", "splines", "stats", "stats4", "tcltk", "tools", "utils",
})


def installable_specs(manager: str, specs: list[str]) -> list[str]:
    """Drop entries the manager cannot install. R base packages only, so far."""
    if manager in ("renv", "pkg"):
        return [s for s in specs if s.split("(")[0].strip() not in R_BASE_PACKAGES]
    return list(specs)


def _install_cmd(manager: str, specs: list[str]) -> str:
    quoted = " ".join(shlex.quote(s) for s in specs)
    if manager == "pip":
        return f"pip install {quoted}"
    if manager == "conda":
        return f"conda install -y {quoted}"
    if manager == "mamba":
        return f"micromamba install -y -n base {quoted}"
    # ponytail: R installs go through install.packages with no renv.lock restore.
    # Add renv::restore (needs the lockfile in the context) when R models actually
    # show up in the corpus -- no point guessing the shape before then.
    vec = ", ".join(f'"{s.replace(chr(34), "")}"' for s in specs)
    return (f"Rscript -e 'install.packages(c({vec}), "
            f'repos="https://cloud.r-project.org", destdir="{_R_DOWNLOAD_DIR}")\'')


def _run_with_cache(cache: str | None, command: str) -> str:
    r"""`RUN <cmd>`, or `RUN --mount=... \` + indented cmd when there is a cache.

    The `mkdir -p` is not redundant. BuildKit creates the cache mount's target
    directory; Kaniko ignores the `--mount` flag entirely and creates nothing,
    so a command handed that same path as a destination fails on a backend that
    reported no error of its own -- e.g. R:

        Error in download.packages(...) : 'destdir' is not a directory

    Under BuildKit the mkdir is a no-op against the mounted directory, so one
    rendering is correct on both backends and nothing has to branch on which
    builder is in use.
    """
    if not cache:
        return f"RUN {command}"
    return (f"RUN --mount=type=cache,target={cache} \\\n"
            f"    mkdir -p {cache} \\\n"
            f" && {command}")


def _pkg_run(manager: str, specs: list[str]) -> str:
    return _run_with_cache(_PKG_CACHE.get(manager), _install_cmd(manager, specs))


def dedupe(items) -> list[str]:
    """Order-preserving dedupe. Declared order is meaningful for pkg_specs
    (constraint files, `--index-url` style entries), so never sort those."""
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def render(spec: EnvSpec) -> RenderedDockerfile:
    """EnvSpec -> Dockerfile, with a fixed layer order chosen for cache reuse.

    Order: FROM, ENV, apt, bootstrap, pre_install, pkgs, post_install,
    mkdir+WORKDIR, then (installed mode only) COPY + install.

    Deviation from the plan's list: `pre_install` renders *before* the package
    step rather than beside `post_install`. Rendering a hook named "pre" after
    the install it is meant to precede makes the field useless.
    """
    steps: list[Step] = []

    def emit(kind: str, src_field: str, instruction: str) -> None:
        steps.append(Step(len(steps), kind, src_field, instruction))

    emit("from", "base_image", f"FROM {spec.base_image}@{spec.base_digest}")

    # ENV: static vars plus extra_path folded into the manager's path variable.
    env = dict(spec.env_vars)
    if spec.mount.extra_path:
        var = _PATH_VAR.get(spec.pkg_manager, "PYTHONPATH")
        parts = list(spec.mount.extra_path)
        if env.get(var):
            parts.append(env[var])
        env[var] = ":".join(dedupe(parts))
    if env:
        body = " \\\n    ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env.items()))
        emit("env", "env_vars", f"ENV {body}")

    if spec.apt_packages:
        emit("apt", "apt_packages", _apt_run(sorted(dedupe(spec.apt_packages))))

    boot = _BOOTSTRAP.get(spec.pkg_manager)
    if boot:
        emit("bootstrap", "pkg_manager", f"RUN {boot}")

    if spec.pre_install:
        emit("cmd", "pre_install", _run_script(spec.pre_install))

    to_install = installable_specs(spec.pkg_manager, dedupe(spec.pkg_specs))
    if to_install:
        emit("pkg", "pkg_specs", _pkg_run(spec.pkg_manager, to_install))

    if spec.post_install:
        emit("cmd", "post_install", _run_script(spec.post_install))

    m = spec.mount
    mounts = dedupe([m.code_path, m.input_path, m.output_path, m.workdir])
    emit("mkdir", "mount", "RUN mkdir -p " + " ".join(mounts))
    emit("workdir", "mount", f"WORKDIR {m.workdir}")

    if spec.install_mode == "installed":
        # Code is baked in: the image is model-specific, so an ENTRYPOINT is safe.
        emit("copy", "install_mode", f"COPY . {m.code_path}")
        if spec.pkg_manager in ("renv", "pkg"):
            # No cache mount: R CMD INSTALL writes straight to the library and
            # downloads nothing.
            install, cache = f"R CMD INSTALL {m.code_path}", None
        else:
            install, cache = f"pip install {m.code_path}", _PKG_CACHE.get(spec.pkg_manager)
        emit("install", "install_mode", _run_with_cache(cache, install))
        if spec.entrypoint:
            emit("entrypoint", "entrypoint", "ENTRYPOINT " + json.dumps(spec.entrypoint))
    # Mounted mode bakes no ENTRYPOINT on purpose: the image is a dependency
    # stack, the entrypoint belongs to the model record and is supplied at run
    # time. That is what lets one image serve many models.

    header = "# syntax=docker/dockerfile:1\n# generated by envbuild -- do not edit\n"
    text = header + "\n".join(s.instruction for s in steps) + "\n"
    return RenderedDockerfile(text, steps)
