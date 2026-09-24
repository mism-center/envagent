#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Offline regression suite: renderer, every typed action, classifier rules,
normaliser goldens, evidence scan, record guards.

No Docker, no network, no framework -- plain asserts, run it directly:

    uv run scripts/test_envbuild.py

Milestones 2, 3 and 5 of the plan are entirely covered here, which is why they
can proceed while the compose stack is still being sorted.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import baseselect                                     # noqa: E402
import classify                                       # noqa: E402
import driver                                         # noqa: E402
import evidence                                       # noqa: E402
import ladder                                         # noqa: E402
import normalize                                      # noqa: E402
import patch                                          # noqa: E402
import record                                         # noqa: E402
import verifier                                      # noqa: E402
import builder                                       # noqa: E402
import k8s                                           # noqa: E402
import registry                                      # noqa: E402
from builder import K8sBuilder, match_step            # noqa: E402
from errors import InfraError                        # noqa: E402
from envspec import EnvSpec, MountContract, render     # noqa: E402
import envspec as envspec_mod                         # noqa: E402

FIXTURES = ROOT / "fixtures"
PASSED: list[str] = []


def check(label):
    """Decorator that runs the test immediately and records its label."""
    def wrap(fn):
        fn()
        PASSED.append(label)
        return fn
    return wrap


def spec(**kw) -> EnvSpec:
    base = {"base_image": "python:3.11-slim", "base_digest": "sha256:" + "a" * 64,
            "pkg_specs": ["numpy<2"], "apt_packages": ["libxml2-dev"]}
    base.update(kw)
    return EnvSpec(**base)


class FakeClient:                              # pylint: disable=unused-argument
    """A cluster that records what it was asked to create and answers canned.

    The whole Kubernetes surface is five calls, so faking it is five methods --
    which is itself the argument for the surface being that small.
    """

    def __init__(self, namespace="envbuild", allow=True, exit_code=0, logs=""):
        self.namespace = namespace
        self.allow = allow
        self.exit_code = exit_code
        self.logs = logs
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def can_i(self, verb, resource):
        return self.allow

    def create_pod(self, manifest):
        self.created.append(manifest)
        return manifest

    def delete_pod(self, name, grace_seconds=0):
        self.deleted.append(name)

    def pod_log(self, name, container, tail_bytes=200_000):
        return self.logs

    def wait_terminated(self, name, container, deadline, poll_s=2.0):
        return self.exit_code, ""


def mk_builder(job_id="job-abc", registry_push="registry:5000", client=None, **kw):
    return K8sBuilder(job_id, client or FakeClient(), registry_push,
                      work_pvc="envbuild-work", work_mount="/work", **kw)


def mk_verifier(job_id="job-abc", client=None, **kw):
    kw.setdefault("work_pvc", "envbuild-work")
    kw.setdefault("work_mount", "/work")
    kw.setdefault("models_pvc", "models")
    kw.setdefault("models_mount", "/models")
    kw.setdefault("code_ref", "/models/mbmm/1.0")
    return verifier.K8sVerifier(job_id, client or FakeClient(), **kw)


# ---------------------------------------------------------------- renderer
@check("renderer: fixed layer order")
def _render_order():
    s = spec(env_vars={"MPLBACKEND": "Agg"}, pre_install=["echo pre"], post_install=["echo post"])
    kinds = [st.kind for st in render(s).steps]
    # LABEL is last on purpose: changing a label must never invalidate a cached layer.
    assert kinds == ["from", "env", "apt", "bootstrap", "cmd", "pkg", "cmd", "mkdir", "workdir",
                     "label"], kinds


@check("renderer: base is always digest-pinned, never a bare tag")
def _render_pinned():
    text = render(spec()).text
    assert "FROM python:3.11-slim@sha256:" in text
    assert "\nFROM python:3.11-slim\n" not in text


@check("renderer: mounted mode bakes no ENTRYPOINT and copies no code")
def _render_mounted():
    text = render(spec(entrypoint=["python", "run.py"])).text
    assert "ENTRYPOINT" not in text and "COPY" not in text


@check("renderer: installed mode copies, installs, and may bake an entrypoint")
def _render_installed():
    text = render(spec(install_mode="installed", entrypoint=["python", "run.py"])).text
    assert "COPY . /model" in text
    assert "pip install /model" in text
    assert 'ENTRYPOINT ["python", "run.py"]' in text


@check("renderer: apt sorted+deduped, pkg order preserved")
def _render_dedupe():
    s = spec(apt_packages=["zlib1g-dev", "libxml2-dev", "zlib1g-dev"],
             pkg_specs=["scipy", "numpy<2", "scipy"])
    text = render(s).text
    assert text.index("libxml2-dev") < text.index("zlib1g-dev")
    assert text.count("zlib1g-dev") == 1
    assert text.index("scipy") < text.index("numpy<2")      # declared order kept
    assert text.count("scipy") == 1


@check("renderer: extra_path lands in PYTHONPATH (R_LIBS for R)")
def _render_extra_path():
    s = spec(mount=MountContract(extra_path=("/model/src",)))
    assert "PYTHONPATH=/model/src" in render(s).text
    r = spec(pkg_manager="renv", mount=MountContract(extra_path=("/model/R",)))
    assert "R_LIBS=/model/R" in render(r).text


@check("renderer: no cache mount ever targets an install prefix")
def _render_cache_safety():
    # A cache mount's contents are not committed into the image. Pointing one at
    # a library / site-packages / conda-pkgs path yields an image that builds
    # green and has nothing installed in it.
    prefixes = ("/usr/local/lib/R", "/usr/lib/R", "site-packages",
                "/opt/conda/pkgs", "/opt/conda/lib")
    for manager in ("pip", "conda", "mamba", "renv", "pkg"):
        for mode in ("mounted", "installed"):
            text = render(spec(pkg_manager=manager, install_mode=mode,
                               pkg_specs=["deSolve"])).text
            for instruction in text.splitlines():
                if "type=cache" not in instruction:
                    continue
                target = instruction.split("target=", 1)[1].split(",")[0].split()[0]
                assert not any(pre in target for pre in prefixes), \
                    f"{manager}/{mode}: cache mount targets an install prefix: {target}"


@check("renderer: R base packages are never handed to install.packages")
def _render_r_base():
    # DESCRIPTION lists stats/graphics/grDevices under Imports. install.packages()
    # errors on them, so an unfiltered spec fails every R build on its first line.
    s = spec(pkg_manager="renv", apt_packages=[],
             pkg_specs=["stats", "graphics", "grDevices", "deSolve"])
    text = render(s).text
    assert "deSolve" in text
    for base in ("stats", "graphics", "grDevices"):
        assert f'"{base}"' not in text, f"{base} reached install.packages"
    # All-base means there is nothing to install: emit no package step at all.
    bare = render(spec(pkg_manager="renv", apt_packages=[], pkg_specs=["stats"])).text
    assert "install.packages" not in bare
    # Python is untouched by the filter.
    assert "'numpy<2'" in render(spec(apt_packages=[])).text


@check("renderer: R downloads land in the cached dir, not the library")
def _render_r_destdir():
    text = render(spec(pkg_manager="renv", pkg_specs=["deSolve"])).text
    assert 'destdir="/root/.cache/R"' in text
    assert "mkdir -p /root/.cache/R" in text


@check("envspec: a malformed agent-supplied base_digest is rejected, not built")
def _spec_digest_guard():
    good = spec().to_dict()
    assert EnvSpec.from_dict(good).base_digest == good["base_digest"]
    for bad in ("sha256:" + "f" * 65, "sha256:" + "a" * 63, "sha256:nothex", "deadbeef"):
        try:
            EnvSpec.from_dict({**good, "base_digest": bad})
        except ValueError:
            continue
        raise AssertionError(f"accepted malformed digest {bad!r}")
    # empty stays legal: that is what makes the driver resolve it from base_image
    assert EnvSpec.from_dict({**good, "base_digest": ""}).base_digest == ""


@check("renderer: emits no BuildKit-only syntax Kaniko silently mis-executes")
def _render_kaniko_portable():
    # Both halves of the mbmm regression found on the AKS/Kaniko benchmark pass,
    # plus the two directives Kaniko reads as plain comments.
    # 1. Heredoc RUN: Kaniko runs the delimiter as the command and the step
    #    no-ops, so every pre/post_install repair silently did nothing.
    # 2. Cache-mount target: Kaniko ignores --mount and never creates the
    #    directory, so R's destdir= is handed a path that does not exist.
    text = render(spec(pkg_manager="renv", pkg_specs=["deSolve"],
                       apt_packages=["libxml2-dev"],
                       pre_install=["mkdir -p /root/.cache/R"],
                       post_install=["echo done"])).text
    assert "<<'ENVBUILD'" not in text and "ENVBUILD" not in text, text
    assert "--mount=" not in text, text          # BuildKit-only; Kaniko ignores it
    assert "# syntax=" not in text, text         # selects a BuildKit frontend
    # Every RUN opens with its step marker, then the script proper.
    assert "field=pre_install' \\\n && set -eux \\\n && mkdir -p /root/.cache/R" in text, text
    # the cache dir is created before anything is told to write into it
    run = next(r for r in text.split("\nRUN ") if "destdir=" in r)
    assert run.index("mkdir -p /root/.cache/R") < run.index("destdir="), run


@check("renderer: deterministic and hash is text-independent")
def _render_deterministic():
    a, b = spec(), spec()
    assert render(a).text == render(b).text
    assert a.hash() == b.hash()
    assert spec(pkg_specs=["numpy<2", "scipy"]).hash() != a.hash()


@check("envspec: JSON round-trip is lossless")
def _roundtrip():
    s = spec(mount=MountContract(extra_path=("/a", "/b")), env_vars={"X": "1"})
    assert EnvSpec.from_dict(json.loads(json.dumps(s.to_dict()))).hash() == s.hash()


# ---------------------------------------------------------------- actions
def rendered_diff(before: EnvSpec, action: str, arg: str = "") -> tuple[str, str]:
    after = patch.apply(before, action, arg).spec
    return render(before).text, render(after).text


@check("action ADD_APT_PKG: appends one apt package")
def _a_apt():
    b, a = rendered_diff(spec(), "ADD_APT_PKG", "libhdf5-dev")
    assert "libhdf5-dev" not in b and "libhdf5-dev" in a


@check("action ADD_PKG: appends one package spec")
def _a_pkg():
    b, a = rendered_diff(spec(), "ADD_PKG", "scipy>=1.10")
    assert "scipy" not in b and "'scipy>=1.10'" in a


@check("action PIN_PKG: replaces the existing entry, does not duplicate")
def _a_pin():
    after = patch.apply(spec(pkg_specs=["numpy<2"]), "PIN_PKG", "numpy==1.26.4").spec
    assert after.pkg_specs == ["numpy==1.26.4"]
    try:
        patch.apply(spec(), "PIN_PKG", "numpy")            # no constraint
        raise AssertionError("PIN_PKG accepted a bare name")
    except patch.PatchError:
        pass


@check("action UNPIN_PKG: strips the constraint, keeps the name")
def _a_unpin():
    after = patch.apply(spec(pkg_specs=["numpy<2", "scipy"]), "UNPIN_PKG", "numpy").spec
    assert after.pkg_specs == ["numpy", "scipy"]


@check("action CHANGE_INTERPRETER_VERSION: keeps the flavour, clears the digest")
def _a_interp():
    r = patch.apply(spec(), "CHANGE_INTERPRETER_VERSION", "3.10")
    assert r.spec.base_image == "python:3.10-slim" and r.needs_digest and not r.spec.base_digest


@check("action CHANGE_BASE_IMAGE: replaces the base, clears the digest")
def _a_base():
    r = patch.apply(spec(), "CHANGE_BASE_IMAGE", "ubuntu:24.04")
    assert r.spec.base_image == "ubuntu:24.04" and r.needs_digest


@check("action SWITCH_INSTALLER: changes the install command and cache mount")
def _a_installer():
    b, a = rendered_diff(spec(), "SWITCH_INSTALLER", "mamba")
    assert "pip install" in b and "micromamba install -y -n base" in a


@check("action SWITCH_INSTALL_MODE: adds COPY + install")
def _a_mode():
    b, a = rendered_diff(spec(), "SWITCH_INSTALL_MODE", "installed")
    assert "COPY" not in b and "COPY . /model" in a


@check("action SET_ENV_VAR: adds an ENV line")
def _a_env():
    b, a = rendered_diff(spec(), "SET_ENV_VAR", "MPLBACKEND=Agg")
    assert "MPLBACKEND" not in b and "ENV MPLBACKEND=Agg" in a


@check("action ADD_PRE_INSTALL_CMD: renders before the package step")
def _a_pre():
    a = render(patch.apply(spec(), "ADD_PRE_INSTALL_CMD", "echo hi").spec).text
    # Match the package step specifically -- the bootstrap layer also says "pip install".
    assert a.index("echo hi") < a.index("pip install 'numpy<2'"), \
        "pre_install must precede the package install"


@check("action FIX_MOUNT_CONTRACT: extra_path appends, other fields replace")
def _a_mount():
    s = patch.apply(spec(), "FIX_MOUNT_CONTRACT", "extra_path=/model/src").spec
    assert s.mount.extra_path == ("/model/src",)
    s2 = patch.apply(s, "FIX_MOUNT_CONTRACT", "extra_path=/model/lib").spec
    assert s2.mount.extra_path == ("/model/src", "/model/lib")
    s3 = patch.apply(spec(), "FIX_MOUNT_CONTRACT", "workdir=/model/sim").spec
    assert s3.mount.workdir == "/model/sim" and "WORKDIR /model/sim" in render(s3).text


@check("action ESCALATE: leaves the spec untouched")
def _a_escalate():
    s = spec()
    assert patch.apply(s, "ESCALATE").spec.hash() == s.hash()


@check("actions: patch never mutates the input spec (best-so-far rollback depends on it)")
def _a_immutable():
    s = spec()
    before = s.hash()
    patch.apply(s, "ADD_APT_PKG", "cmake")
    patch.apply(s, "FIX_MOUNT_CONTRACT", "extra_path=/x")
    assert s.hash() == before


@check("actions: every action in the enum is implemented")
def _a_coverage():
    args = {"ADD_APT_PKG": "cmake", "ADD_PKG": "scipy", "PIN_PKG": "numpy==1.0",
            "UNPIN_PKG": "numpy", "CHANGE_INTERPRETER_VERSION": "3.12",
            "CHANGE_BASE_IMAGE": "ubuntu:24.04", "SWITCH_INSTALLER": "conda",
            "SWITCH_INSTALL_MODE": "installed", "SET_ENV_VAR": "A=b",
            "ADD_PRE_INSTALL_CMD": "echo x", "FIX_MOUNT_CONTRACT": "workdir=/w",
            "SET_ENTRYPOINT": "python run.py --steps 10", "SET_L3_TIMEOUT": "1200", "ESCALATE": ""}
    assert set(args) == set(patch.ACTIONS)
    for action, arg in args.items():
        patch.apply(spec(), action, arg)


# ---------------------------------------------------------------- classify
@check("classify: header -> deterministic apt package")
def _c_header():
    c = classify.classify("fatal error: libxml/parser.h: No such file or directory")
    assert (c.failure_class, c.action, c.arg) == ("MISSING_SYSTEM_LIB", "ADD_APT_PKG", "libxml2-dev")
    c2 = classify.classify("fatal error: sundials/sundials_nvector.h: No such file")
    assert c2.arg == "libsundials-dev", c2.arg


@check("classify: rung disambiguates ModuleNotFoundError (the mounting split)")
def _c_rung():
    txt = "ModuleNotFoundError: No module named 'mypkg'"
    assert classify.classify(txt, rung="L1", local_modules={"mypkg"}).failure_class == "MISSING_DEPENDENCY"
    assert classify.classify(txt, rung="L2", local_modules={"mypkg"}).failure_class == "IMPORT_PATH_ERROR"
    assert classify.classify(txt, rung="L2", local_modules=set()).failure_class == "MISSING_DEPENDENCY"


@check("classify: import name mapped to its PyPI distribution")
def _c_alias():
    c = classify.classify("ModuleNotFoundError: No module named 'sklearn'", rung="L1")
    assert c.arg == "scikit-learn"


@check("classify: compiled submodule beats the generic module rule")
def _c_build_mode():
    c = classify.classify("ModuleNotFoundError: No module named 'mypkg._ext'",
                          rung="L2", local_modules={"mypkg"})
    assert (c.failure_class, c.action, c.arg) == ("BUILD_MODE_MISMATCH", "SWITCH_INSTALL_MODE", "installed")


@check("classify: dotted submodule miss is a version break, not a missing pkg")
def _c_dotted_submodule():
    # "pint.quantity" missing means pint itself imported fine -- the installed
    # version just lacks that internal module. Re-adding pint is a no-op and
    # gets the repair loop stuck (this is exactly what happened on the
    # vivarium-chemotaxis benchmark job).
    c = classify.classify("ModuleNotFoundError: No module named 'pint.quantity'", rung="L1")
    assert (c.failure_class, c.action, c.arg) == ("ABI_MISMATCH", "PIN_PKG", "pint")


@check("ladder: vivarium-core's import probe name is vivarium, not vivarium_core")
def _c_vivarium_probe():
    # Real bench failure: dist.replace("-", "_") guessed "vivarium_core", so L1
    # reported the installed distribution as a missing module forever.
    import ladder
    s = spec(pkg_specs=["vivarium-core==0.0.34"])
    assert ladder.import_names(s) == ["vivarium"]


@check("ladder: opencv-python and ipython probe as cv2 / IPython")
def _c_probe_aliases():
    # Real bench failures: dist.replace("-", "_") guessed "opencv_python" and
    # "ipython" (lowercase), both wrong -- L1 reported installed, working
    # distributions as permanently missing.
    import ladder
    assert ladder.import_names(spec(pkg_specs=["opencv-python==4.9.0"])) == ["cv2"]
    assert ladder.import_names(spec(pkg_specs=["ipython"])) == ["IPython"]


@check("classify: the rest of the table fires on representative stderr")
def _c_table():
    cases = {
        "ERROR: ResolutionImpossible: for help visit": "DEP_RESOLUTION_CONFLICT",
        "ERROR: No matching distribution found for numpy==999.999.999": "DEP_RESOLUTION_CONFLICT",
        "error: command '/usr/bin/gcc' failed with exit code 1": "COMPILE_ERROR",
        "unable to execute 'gcc': No such file or directory": "COMPILE_ERROR",
        "ImportError: /usr/lib/libx.so: undefined symbol: _ZN5boost": "ABI_MISMATCH",
        "Error in library(deSolve) : there is no package called 'deSolve'": "MISSING_DEPENDENCY",
        "PermissionError: [Errno 13] Permission denied: '/outputs/run.csv'": "MOUNT_CONTRACT_ERROR",
        "ENVBUILD_NO_OUTPUT: nothing written to /outputs": "MOUNT_CONTRACT_ERROR",
        "ENVBUILD_ENTRYPOINT_MISSING: /model/run.py": "ENTRYPOINT_UNKNOWN",
        "ENVBUILD_TIMEOUT: exceeded 600s": "TIMEOUT",
        "MATLAB Runtime is required": "UNSUPPORTED_TOOLCHAIN",
        "License file not found; set GUROBI_HOME": "LICENSE_REQUIRED",
    }
    for text, expected in cases.items():
        got = classify.classify(text, rung="L3").failure_class
        assert got == expected, f"{text!r}: expected {expected}, got {got}"


@check("classify: unmatched stderr falls back to the LLM, not to a wrong class")
def _c_unknown():
    c = classify.classify("something nobody has seen before", rung="L0")
    assert c.failure_class == "UNKNOWN" and c.classified_by == "llm" and c.routes_to == "llm"


@check("classify: every class has a routing entry")
def _c_routing():
    assert {"MISSING_SYSTEM_LIB", "IMPORT_PATH_ERROR", "UNKNOWN"} <= set(classify.ROUTING)
    # Every rule must produce a class the routing table knows about.
    for rule_name, _pat, _handler in classify.RULES:
        assert rule_name


# ---------------------------------------------------------------- normalize
@check("normalize: golden -- same failure from two runs shares a signature")
def _n_golden():
    a = ("#8 12.34 gcc -I/tmp/pip-build-abc123/src -o /tmp/x.o\n"
         "  fatal error: libxml/parser.h: No such file or directory, line 42\n"
         "  error: command '/usr/bin/gcc' failed with exit code 1 at 2026-08-27T10:00:01Z\n")
    b = ("#8 88.01 gcc -I/tmp/pip-build-zzz999/src -o /tmp/x.o\n"
         "  fatal error: libxml/parser.h: No such file or directory, line 7\n"
         "  error: command '/usr/bin/gcc' failed with exit code 1 at 2026-09-02T22:13:55Z\n")
    assert normalize.signature(a) == normalize.signature(b)


@check("normalize: different failures do not collide")
def _n_distinct():
    assert normalize.signature("fatal error: zlib.h: No such file") != \
           normalize.signature("fatal error: libxml/parser.h: No such file")


@check("normalize: strips paths, hashes, versions, timestamps, addresses")
def _n_subs():
    out = normalize.normalize_line(
        "at /usr/lib/python3.11/site-packages/x.py line 12: sha 9f2caa1b3d4e5f60718293, "
        "numpy 1.26.4 at 0xdeadbeef 2026-08-27T10:00:01Z")
    for leaked in ("site-packages", "9f2caa1b3d4e", "1.26.4", "0xdeadbeef", "2026-08-27"):
        assert leaked not in out, f"{leaked} survived: {out}"


@check("normalize: prefers signal lines over log tail")
def _n_signal():
    noisy = "\n".join([f"progress {i}" for i in range(200)] + ["fatal error: zlib.h missing"])
    assert normalize.salient_lines(noisy) == ["fatal error: zlib.h missing"]


# ---------------------------------------------------------------- evidence
@check("evidence: scans a fixture and finds its own modules")
def _e_scan():
    ev = evidence.scan(FIXTURES / "import_path_error")
    assert "mypkg" in ev["local_modules"], ev["local_modules"]
    assert ev["languages"].get("python", 0) >= 2


@check("evidence: local_module_paths knows src/ from flat layout")
def _e_local_module_paths():
    # import_path_error is a src/ layout fixture; installed_mode is flat
    # (the package sits directly at repo root). Same fact _local_modules always
    # computed to find the name -- now it survives instead of being thrown away,
    # which is what lets a mount-path repair be computed instead of guessed
    # (see the tumor-tcell / vivarium-chemotaxis IMPORT_PATH_ERROR jobs).
    src_ev = evidence.scan(FIXTURES / "import_path_error")
    assert src_ev["local_module_paths"]["mypkg"] == "src", src_ev["local_module_paths"]
    flat_ev = evidence.scan(FIXTURES / "installed_mode")
    assert flat_ev["local_module_paths"]["mypkg"] == "", flat_ev["local_module_paths"]


@check("draft_spec: mount.extra_path is seeded from local_module_paths, not guessed later")
def _d_draft_spec_mount():
    digest = "sha256:" + "a" * 64
    src_ev = evidence.scan(FIXTURES / "import_path_error")
    src_spec = driver.draft_spec(src_ev, {}, baseselect.select(src_ev), digest, "v0.24.0")
    assert src_spec.mount.extra_path == ("/model/src",), src_spec.mount.extra_path

    flat_ev = evidence.scan(FIXTURES / "installed_mode")
    flat_spec = driver.draft_spec(flat_ev, {}, baseselect.select(flat_ev), digest, "v0.24.0")
    assert flat_spec.mount.extra_path == ("/model",), flat_spec.mount.extra_path


@check("evidence: install_mode guessed from compiled-extension evidence")
def _e_mode():
    assert baseselect.guess_install_mode(evidence.scan(FIXTURES / "installed_mode"))[0] == "installed"
    assert baseselect.guess_install_mode(evidence.scan(FIXTURES / "import_path_error"))[0] == "mounted"


@check("evidence: grouped annotation dependencies flatten to requirements")
def _e_grouped_deps():
    # The real biomodel-annotator schema groups deps. Iterating that mapping as a
    # list yields the GROUP NAMES -- pkg_specs became ["runtime","optional","system"].
    ex = {"dependencies": {
              "runtime": [{"name": "deSolve", "version_constraint": None},
                          {"name": "jsonlite", "version_constraint": ">=1.8"}],
              "optional": [{"name": "testthat", "version_constraint": ">=3.0.0"}],
              "system": [{"name": "libxml2-dev", "version_constraint": None}]},
          "language": {"name": "R", "version_constraint": ">=4.0"}}
    runtime, system = evidence.annotation_deps(ex)
    assert runtime == ["deSolve", "jsonlite>=1.8"], runtime   # optional (Suggests) dropped
    assert system == ["libxml2-dev"], system
    assert evidence.language_version(ex["language"]) == ">=4.0"
    # A flat list of bare strings must still work.
    assert evidence.annotation_deps({"dependencies": ["numpy<2"]})[0] == ["numpy<2"]


@check("baseselect: an R version floor does not become the rocker tag")
def _b_r_floor():
    ev = {"languages": {"r": 3}, "compiled": {}, "ci": [], "markers": {},
          "r": {"deps": [], "r_version": "4.0", "r_version_raw": "R (>= 4.0)"}}
    assert baseselect.select(ev).base_image == "rocker/r-ver:4.4.1"
    ev["r"]["r_version_raw"] = "R (4.3.1)"          # an actual pin is honoured
    ev["r"]["r_version"] = "4.3.1"
    assert baseselect.select(ev).base_image == "rocker/r-ver:4.3.1"


@check("baseselect: an R package installs, it does not mount")
def _b_r_package():
    ev = {"languages": {"r": 3}, "compiled": {}, "ci": [],
          "markers": {"DESCRIPTION": "DESCRIPTION", "NAMESPACE": "NAMESPACE"},
          "r": {"deps": [], "is_package": True, "r_version_raw": "R (>= 4.0)"}}
    assert baseselect.select(ev).install_mode == "installed"


@check("evidence: annotation subset read from a plain YAML")
def _e_annotation():
    ann = evidence.read_annotation(FIXTURES / "import_path_error" / "annotation.yaml")
    assert ann["entry_points"][0]["command"] == "python run.py"
    assert ann["language"] == "python"


@check("baseselect: the table picks the documented base per language")
def _b_table():
    assert baseselect.select({"languages": {"python": 3}, "python": {}, "ci": [],
                              "compiled": {}}).base_image.startswith("python:")
    assert baseselect.select({"languages": {}, "r": {"deps": [], "r_version": "4.3.1"},
                              "ci": [], "compiled": {}}).base_image == "rocker/r-ver:4.3.1"
    assert baseselect.select({"languages": {}, "conda": {"file": "environment.yml"},
                              "ci": [], "compiled": {}}).pkg_manager == "mamba"
    assert baseselect.select({"languages": {}, "ci": [], "compiled": {}}).base_image == "ubuntu:24.04"


@check("baseselect: a green CI python-version outranks a requires-python floor")
def _b_ci():
    ev = {"languages": {"python": 1}, "compiled": {},
          "python": {"requires_python": ">=3.8", "sources": []},
          "ci": [{"file": "ci.yml", "python_versions": ["3.10"], "apt": [], "runs": []}]}
    assert baseselect.select(ev).base_image == "python:3.10-slim"


# ---------------------------------------------------------------- builder / ladder
@check("builder: a logged instruction maps back to the EnvSpec field that produced it")
def _bd_map():
    steps = render(spec()).steps
    # Kaniko echoes the rendered instruction flattened onto one line.
    apt = match_step("RUN apt-get update && apt-get install -y "
                     "--no-install-recommends libxml2-dev", steps)
    assert (apt.kind, apt.field) == ("apt", "apt_packages")
    pkg = match_step("RUN mkdir -p /root/.cache/pip && pip install 'numpy<2'", steps)
    assert (pkg.kind, pkg.field) == ("pkg", "pkg_specs")
    # Nothing close enough must not be forced onto some step anyway.
    assert match_step("LABEL nothing=here", steps) is None


@check("ladder: entry point derived from file, module and bare-script commands")
def _l_entry():
    assert ladder.entry_from_command("python run_sim.py --n 10") == {"kind": "file", "value": "run_sim.py"}
    assert ladder.entry_from_command("python -m pkg.main") == {"kind": "module", "value": "pkg.main"}
    assert ladder.entry_from_command("vivarium-run")["kind"] == "none"


@check("ladder: L1 probes the right import names")
def _l_imports():
    mods = ladder.import_names(spec(pkg_specs=["scikit-learn", "PyYAML", "numpy<2", "pip"]))
    assert mods == ["numpy", "sklearn", "yaml"], mods


@check("ladder: the file-output gate is opt-in from the annotation")
def _l_expect_outputs():
    # A console-only entry point must be able to reach L3. Demanding files makes
    # L3 unreachable and sends the loop after FIX_MOUNT_CONTRACT repairs that
    # cannot work -- MBMM's example script is pure cat/print.
    ann = evidence.read_annotation(FIXTURES / "bad_output_path" / "annotation.yaml")
    assert ann["entry_points"][0]["default_output_location"] == "outputs"
    plain = evidence.read_annotation(FIXTURES / "import_path_error" / "annotation.yaml")
    assert not plain["entry_points"][0]["default_output_location"]


@check("ladder: CRAN names keep their case, PyPI names normalise")
def _l_r_case():
    # Lowercasing turned library(deSolve) into library(desolve) and failed L1 for
    # every R model against an image that was actually correct.
    r = ladder.import_names(spec(pkg_manager="renv",
                                 pkg_specs=["deSolve", "Matrix", "MASS", "stats"]))
    assert r == ["MASS", "Matrix", "deSolve"], r      # base `stats` not probed
    # Python still normalises: PyPI is case-insensitive.
    assert ladder.import_names(spec(pkg_specs=["PyYAML", "scikit-learn"])) == \
        ["sklearn", "yaml"]


@check("ladder: verify_timeout_s reaches every rung, not just L3")
def _l_timeout_reaches_all_rungs():
    # L1 and L2 used to hardcode 180s, so raising the budget moved L3 alone and
    # an L1 timeout could not be configured away at all. Record every timeout
    # the ladder hands the verifier, and assert the budget is what arrives.
    seen = []

    class _Recorder:
        def run(self, _image, _code, _cmd, _mount, timeout_s, **_kw):
            seen.append(timeout_s)
            return verifier.RunResult(True, 0, "ok", "")

        def outputs_listing(self):
            return []

    ladder.climb(_Recorder(), "img@sha256:" + "a" * 64, "code-vol", spec(),
                 {"kind": "file", "value": "run.py"}, ["python", "run.py"], 451)
    assert seen == [451, 451, 451], seen


@check("ladder: rung ordering is what best-so-far rollback compares")
def _l_rungs():
    assert ladder.rung_index("L3") > ladder.rung_index("L1") > ladder.rung_index("L0")
    assert ladder.rung_index("") == -1


# ---------------------------------------------------------------- infra faults
@check("infra: a k8s error surfaces its Status reason, not a raw body")
def _i_status():
    import urllib.error
    body = json.dumps({"kind": "Status", "reason": "Forbidden",
                       "message": 'pods is forbidden: User "sa" cannot create '
                                  'resource "pods" in namespace "envbuild"'}).encode()
    exc = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(body))
    msg = k8s.Client._status_message("POST", "/api/v1/pods", exc)
    # The operator needs the missing rule named. "HTTP 403" alone does not.
    assert "Forbidden" in msg and "cannot create" in msg, msg


@check("infra: builder preflight raises InfraError instead of building blind")
def _i_builder():
    # The Role is missing the rule. Die here, not after a ten-minute build.
    b = mk_builder(client=FakeClient(allow=False))
    try:
        b.check_builder()
        raise AssertionError("a refused access review did not raise")
    except InfraError as exc:
        # Must name the file that actually fixes it -- deploy/rbac.yaml was a
        # plausible-looking path that has never existed.
        assert "deploy/envbuild.yaml" in str(exc), exc


@check("infra: no cluster credential at all is an InfraError naming every option")
def _i_no_cred():
    saved = k8s._SA_DIR
    k8s._SA_DIR = Path("/nonexistent/serviceaccount")
    try:
        k8s.Client.resolve(namespace="envbuild")
        raise AssertionError("missing credential did not raise")
    except InfraError as exc:
        for hint in ("ENVBUILD_K8S_TOKEN", "ServiceAccount", "kubeconfig"):
            assert hint in str(exc), exc
    finally:
        k8s._SA_DIR = saved


@check("infra: auth precedence is explicit token, then ServiceAccount, then kubeconfig")
def _i_auth_order():
    saved = k8s._SA_DIR
    tmp = Path(tempfile.mkdtemp())
    try:
        sa = tmp / "sa"
        sa.mkdir()
        (sa / "token").write_text("sa-token")
        (sa / "ca.crt").write_text("")
        (sa / "namespace").write_text("from-sa")
        k8s._SA_DIR = sa

        # An explicit token outranks a mounted ServiceAccount.
        c = k8s.Client.resolve(token="explicit", api_server="https://api.example")
        assert c.token == "explicit" and c.api_server == "https://api.example"

        # With no explicit token, the ServiceAccount wins -- and supplies the
        # namespace, which is why in-cluster needs no configuration at all.
        c = k8s.Client.resolve()
        assert c.token == "sa-token" and c.namespace == "from-sa"
        assert c.api_server == "https://kubernetes.default.svc"
    finally:
        k8s._SA_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


@check("infra: an exec-plugin kubeconfig is refused with a reason, not half-used")
def _i_exec_kubeconfig():
    saved = k8s._SA_DIR
    k8s._SA_DIR = Path("/nonexistent/serviceaccount")
    tmp = Path(tempfile.mkdtemp())
    try:
        kc = tmp / "kubeconfig"
        kc.write_text(json.dumps({          # JSON is valid YAML
            "current-context": "c",
            "contexts": [{"name": "c", "context": {"cluster": "k", "user": "u"}}],
            "clusters": [{"name": "k", "cluster": {"server": "https://api.example"}}],
            "users": [{"name": "u", "user": {"exec": {"command": "kubelogin"}}}],
        }))
        try:
            k8s.Client.resolve(kubeconfig=str(kc))
            raise AssertionError("exec-plugin kubeconfig was accepted")
        except InfraError as exc:
            # Running a helper binary is the shape being removed; say so.
            assert "credential plugin" in str(exc), exc
    finally:
        k8s._SA_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)


@check("builder: nested registry keeps the job id in the path, flat folds it into the tag")
def _k8s_naming():
    b = mk_builder()
    b.attempt = 3
    assert b._image_name() == "registry:5000/envbuild/job-abc:a3"

    b2 = mk_builder(registry_push="docker.io/mismplatform", flat_registry=True)
    b2.attempt = 3
    assert b2._image_name() == "docker.io/mismplatform/envbuild:job-abc-a3"


@check("k8s: an object name always ends on an alphanumeric, whatever it is built from")
def _k8s_object_name():
    # The real failure: a UUID model id truncated onto a trailing dash, which the
    # API server rejects with one error for the name and one for every label that
    # carries it. Anything the caller hands us gets folded down, not trusted.
    label = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    for job_id in ("825c3a4f-dec1-4085-a1ae-495d043b1d5a",   # the one that broke
                   "job-2026-09-15-a1b2",                     # the ordinary case
                   "mism:model/1a2b3c",                       # colon and slash
                   "UPPER_CASE_ID",                           # caps, underscore
                   "x" * 80,                                  # longer than the limit
                   "---",                                     # nothing usable at all
                   ""):
        name = k8s.object_name("envbuild", job_id, "a1")
        assert label.match(name), name
        assert len(name) <= 63, (len(name), name)
    # Still unique per call -- two attempts must not collide on one pod name.
    assert k8s.object_name("envbuild", "j", "a1") != k8s.object_name("envbuild", "j", "a1")


@check("builder: pod name is a valid, unique k8s object name")
def _k8s_pod_name():
    b = mk_builder(job_id="job-2026-09-14-a10a")
    b.attempt = 2
    name = b._pod_name()
    assert len(name) <= 63, name
    assert re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name), name
    assert name != b._pod_name()          # two calls, two different names


@check("builder: a Kaniko log line maps back to the EnvSpec field")
def _k8s_step_match():
    # Real captured Kaniko output shape (v1.23.2): ANSI-colored INFO line
    # echoing the rendered instruction verbatim, no "Step N/M" header.
    rendered = render(spec())
    pkg_step = next(s for s in rendered.steps if s.kind == "pkg")
    # Kaniko flattens a multi-line rendered instruction onto one INFO line,
    # same as the real captured output did.
    flattened = " ".join(pkg_step.instruction.splitlines())
    logs = "\n".join([
        "\x1b[36mINFO\x1b[0m[0001] Retrieving image manifest python:3.11-slim",
        f"\x1b[36mINFO\x1b[0m[0014] {flattened}",
        "ERROR: Could not find a version that satisfies the requirement",
        "error building image: error building stage: failed to execute command",
    ])
    step = builder.match_kaniko_step(logs, rendered.steps)
    assert step is not None and step.index == pkg_step.index, step


@check("config: an empty env override clears a config.ini value, not just unset")
def _cfg_empty_override():
    # deploy/agent-job.yaml sets ENVBUILD_K8S_TOKEN="" to mean "use the
    # ServiceAccount this pod already has". If empty did not beat config.ini,
    # an in-cluster run would try a stale token instead.
    import os
    import driver
    os.environ["ENVBUILD_K8S_TOKEN"] = ""
    try:
        cfg = driver.load_config(None)
        assert cfg.get("builder", "token") == ""
    finally:
        del os.environ["ENVBUILD_K8S_TOKEN"]


@check("infra: InfraError is not a classifiable failure")
def _i_not_classified():
    # Belt and braces: if one ever reaches the classifier, it must not be dressed
    # up as a repairable model failure with a confident action.
    c = classify.classify('pods is forbidden: User "system:serviceaccount:envbuild:'
                          'envbuild" cannot create resource "pods"', rung="L1")
    assert c.action is None, c


@check("builder: the build pod needs nothing that requires pods/exec")
def _k8s_build_manifest():
    # This is the security result of the whole design, so it is asserted rather
    # than described: one container, no init containers, no sidecar. Anything
    # that reappears here means bytes are moving through the API again, which
    # means the Role needs pods/exec back.
    b = mk_builder()
    b.attempt = 1
    pod = b._pod_manifest("p", "reg/envbuild/j:a1", "/workspace/context", 600)
    spec = pod["spec"]
    assert len(spec["containers"]) == 1, spec["containers"]
    assert "initContainers" not in spec, spec
    assert spec["automountServiceAccountToken"] is False
    assert spec["activeDeadlineSeconds"] == 600
    # Kaniko still writes the digest; the agent reads it off the shared volume.
    args = spec["containers"][0]["args"]
    assert any(a.startswith("--digest-file=") for a in args), args
    assert any(a == "--context=dir:///workspace/context" for a in args), args


@check("builder: the rendered Dockerfile reaches the pod on the work claim")
def _k8s_build_writes_dockerfile():
    tmp = Path(tempfile.mkdtemp())
    try:
        client = FakeClient(exit_code=1, logs="INFO[0003] RUN pip install 'numpy<2'\nERROR")
        b = K8sBuilder("job-abc", client, "registry:5000",
                       work_pvc="envbuild-work", work_mount=str(tmp))
        res = b.build(spec(), "", 600)
        written = (tmp / "job-abc" / "a1" / "Dockerfile").read_text()
        assert written == res.dockerfile and written.startswith("# generated by envbuild")
        # The pod mounts exactly that attempt's directory, so two attempts of the
        # same job cannot read each other's Dockerfile.
        mount = res and client.created[0]["spec"]["containers"][0]["volumeMounts"][0]
        assert mount["subPath"] == "job-abc/a1", mount
        assert client.deleted == [client.created[0]["metadata"]["name"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("verifier: the verify pod is unprivileged, tokenless and network-denied")
def _k8s_verify_manifest():
    tmp = Path(tempfile.mkdtemp())
    try:
        v = mk_verifier(work_mount=str(tmp))
        pod = v._pod_manifest("p", "reg@sha256:x", "/models/mbmm/1.0", ["python", "run.py"],
                              MountContract(), 300, network=False, env={"A": "1"})
        spec_, meta = pod["spec"], pod["metadata"]
        # Model code must not be handed a cluster credential.
        assert spec_["automountServiceAccountToken"] is False
        assert "serviceAccountName" not in spec_
        # `--network none`, as a label the deny-all NetworkPolicy selects.
        assert meta["labels"]["envbuild.io/network"] == "deny"
        sec = spec_["containers"][0]["securityContext"]
        assert sec["allowPrivilegeEscalation"] is False
        assert sec["capabilities"]["drop"] == ["ALL"]
        # Source is mounted read-only: a model cannot rewrite the corpus.
        code = next(m for m in spec_["containers"][0]["volumeMounts"]
                    if m["mountPath"] == MountContract().code_path)
        # Artifacts are laid out <model_id>/<version>/, so the subPath is both
        # segments -- mounting just "mbmm" would verify the wrong version.
        assert code["readOnly"] is True and code["subPath"] == "mbmm/1.0", code
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("verifier: network=True drops the deny label, and L1 mounts no code")
def _k8s_verify_network_and_l1():
    tmp = Path(tempfile.mkdtemp())
    try:
        v = mk_verifier(work_mount=str(tmp))
        on = v._pod_manifest("p", "img", "/models/mbmm/1.0", ["true"], MountContract(),
                             300, network=True, env=None)
        # A pod no policy selects gets traffic; that is what "network on" means.
        assert "envbuild.io/network" not in on["metadata"]["labels"]
        l1 = v._pod_manifest("p", "img", "", ["true"], MountContract(), 300,
                             network=False, env=None)
        paths = [m["mountPath"] for m in l1["spec"]["containers"][0]["volumeMounts"]]
        # L1 proves the image alone. Mounting code here would destroy the rung.
        assert MountContract().code_path not in paths, paths
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("verifier: a pod log is reported as stderr, because k8s merges the streams")
def _k8s_verify_run():
    tmp = Path(tempfile.mkdtemp())
    try:
        client = FakeClient(exit_code=1, logs="ModuleNotFoundError: No module named 'scipy'")
        v = mk_verifier(client=client, work_mount=str(tmp))
        res = v.run("img", "", ["python", "-c", "1"], MountContract(), 300)
        assert not res.ok and res.exit_code == 1
        # The classifier reads stderr; a merged stream in stdout would be invisible.
        assert "ModuleNotFoundError" in res.stderr and res.stdout == ""
        assert classify.classify(res.stderr, rung="L1").failure_class == "MISSING_DEPENDENCY"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("registry: a repository is not mistaken for a host")
def _reg_parse():
    # `mismplatform/envbuild` parsing as host `mismplatform` is how you spend an
    # afternoon, so the host rule is asserted in both directions.
    assert registry.parse_ref("python:3.11-slim") == (
        "registry-1.docker.io", "library/python", "3.11-slim")
    assert registry.parse_ref("mismplatform/envbuild:job-a1") == (
        "registry-1.docker.io", "mismplatform/envbuild", "job-a1")
    assert registry.parse_ref("registry:5000/envbuild/job/x:a2") == (
        "registry:5000", "envbuild/job/x", "a2")
    assert registry.parse_ref("ghcr.io/org/repo@sha256:" + "a" * 64) == (
        "ghcr.io", "org/repo", "sha256:" + "a" * 64)


@check("registry: image size sums the config and every layer")
def _reg_size():
    saved = registry.manifest
    registry.manifest = lambda image, timeout_s=120: (
        "sha256:" + "b" * 64,
        {"mediaType": "application/vnd.oci.image.manifest.v1+json",
         "config": {"size": 7}, "layers": [{"size": 100}, {"size": 2000}]})
    try:
        assert registry.image_size("x:1") == 2107
    finally:
        registry.manifest = saved


@check("registry: an unreadable manifest is None, never a crashed build")
def _reg_size_soft():
    saved = registry.manifest
    def boom(image, timeout_s=120):
        raise registry.RegistryError("401")
    registry.manifest = boom
    try:
        # A missing size must not fail a build that otherwise succeeded.
        assert registry.image_size("x:1") is None
    finally:
        registry.manifest = saved


# ---------------------------------------------------------------- record
@check("record: an incomplete row raises instead of corrupting the dataset")
def _r_guard():
    with tempfile.TemporaryDirectory() as d:
        try:
            record.append_attempt(d, {"job_id": "j", "attempt": 1})
            raise AssertionError("incomplete attempt row accepted")
        except record.RecordError:
            pass
        try:
            record.write_verdict(d, {"job_id": "j", "status": "maybe"})
            raise AssertionError("invalid status accepted")
        except record.RecordError:
            pass


@check("record: complete rows append and read back")
def _r_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        row = {k: None for k in record.ATTEMPT_REQUIRED}
        row.update(model_id="m", job_id="j", attempt=1)
        record.append_attempt(d, row)
        record.append_attempt(d, row)
        assert len(record.read_jsonl(Path(d) / "attempts.jsonl")) == 2
        v = {k: None for k in record.VERDICT_REQUIRED}
        v.update(model_id="m", job_id="j", status="verified")
        assert record.write_verdict(d, v)["status"] == "verified"


# ---------------------------------------------------------------- rev 3: logs / infra / probe / lock
# Every check below is a shape taken from outputs/attempts.jsonl of the first
# corpus run (7 models, 176 attempts). See the review that motivated them for
# the counts; the short version is that 111 rows were UNKNOWN and 51 of those
# were network outages the classifier never saw.
import base64                                          # noqa: E402
import logs                                            # noqa: E402


def _rawjson(*texts: str) -> str:
    """A BuildKit --progress=rawjson frame carrying `texts` as log payloads."""
    entries = [{"vertex": "sha256:" + "0" * 64, "stream": 2,
                "data": base64.b64encode(t.encode()).decode()} for t in texts]
    return json.dumps({"logs": entries})


@check("logs: base64 payloads inside BuildKit rawjson frames are unwrapped")
def _lg_rawjson():
    raw = "\n".join([
        json.dumps({"vertexes": [{"digest": "sha256:" + "1" * 64, "name": "[stage-0 3/7] RUN pip install"}]}),
        _rawjson("WARNING: Retrying (Retry(total=4)) after connection broken by "
                 "'NewConnectionError(...)': /simple/numpy/\n",
                 "ERROR: Could not find a version that satisfies the requirement numpy "
                 "(from versions: none)\n",
                 "ERROR: No matching distribution found for numpy\n"),
    ])
    text = logs.decode(raw)
    assert '"data"' not in text and "vertexes" not in text, text
    assert "No matching distribution found for numpy" in text, text
    assert logs.decode(text) == text, "decode must be idempotent"


@check("logs: a rawjson frame truncated mid-line still yields its intact payloads")
def _lg_truncated():
    frame = _rawjson("Temporary failure resolving 'deb.debian.org'\n", "second payload\n")
    cut = frame[: frame.rfind('"data"') + 40]           # chop inside the last payload
    text = logs.decode(cut)
    assert "Temporary failure resolving" in text, text


@check("logs: Kaniko ANSI + INFO[t] prefixes are stripped, duplicate lines collapse")
def _lg_kaniko():
    raw = ("\x1b[36mINFO\x1b[0m[0014] RUN pip install numpy\n"
           "\x1b[36mINFO\x1b[0m[0015] Taking snapshot of full filesystem...\n"
           "WARNING: retry\nWARNING: retry\nWARNING: retry\n"
           "error building image: exit status 1\n")
    text = logs.decode(raw)
    assert text.splitlines()[0] == "RUN pip install numpy", text
    assert text.count("WARNING: retry") == 1, text
    assert "\x1b" not in text


@check("logs: tail() decodes BEFORE cutting, so the real error survives an 8 KB window")
def _lg_tail():
    noise = json.dumps({"vertexes": [{"digest": "sha256:" + "2" * 64, "name": "x" * 200}]})
    raw = _rawjson("fatal error: libxml/parser.h: No such file or directory\n") + "\n" + \
        "\n".join([noise] * 60)
    assert "libxml/parser.h" not in raw[-8000:]          # the old behaviour lost it
    assert "libxml/parser.h" in logs.tail(raw, 8000)


@check("classify: network/registry outages are INFRA_UNAVAILABLE and route to 'infra'")
def _c_infra():
    cases = [
        # The five shapes the corpus actually contained.
        "WARNING: Retrying (Retry(total=0, connect=None, read=None, redirect=None, status=None)) "
        "after connection broken by 'NewConnectionError('<pip._vendor.urllib3.connection."
        "HTTPSConnection object at 0x7f>: Failed to establish a new connection: [Errno -3] "
        "Temporary failure in name resolution')': /simple/numpy/\n"
        "ERROR: Could not find a version that satisfies the requirement numpy (from versions: none)\n"
        "ERROR: No matching distribution found for numpy",
        "W: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease "
        "Temporary failure resolving 'deb.debian.org'\n"
        "W: Some index files failed to download. They have been ignored, or old ones used instead.\n"
        "E: Unable to locate package graphviz",
        "Warning: unable to access index for repository https://cloud.r-project.org/src/contrib:\n"
        "  cannot open URL 'https://cloud.r-project.org/src/contrib/PACKAGES'\n"
        "Warning message:\npackage 'deSolve' is not available for this version of R",
        "error building image: GET https://index.docker.io/v2/library/python/manifests/3.11-slim: "
        "TOOMANYREQUESTS: You have reached your pull rate limit; toomanyrequests",
        "Failed to pull image: ErrImagePull",
    ]
    for stderr in cases:
        c = classify.classify(stderr)
        assert c.failure_class == "INFRA_UNAVAILABLE", (c, stderr[:80])
        assert c.routes_to == "infra" and c.classified_by == "rule", c
        assert c.action is None, "infra failures have no spec repair"
    # ... and wrapped in a rawjson stream, exactly as the corpus recorded them.
    c = classify.classify(_rawjson(cases[0]))
    assert c.failure_class == "INFRA_UNAVAILABLE", c


@check("classify: 'from versions: none' is an unreachable index, not a resolution conflict")
def _c_none_versions():
    # UNPIN_PKG on this text was the single most damaging false repair in the
    # corpus: it stripped the author's exact `vivarium-core==1.6.0` to fix DNS.
    c = classify.classify("ERROR: Could not find a version that satisfies the requirement "
                          "vivarium-core==1.6.0 (from versions: none)\n"
                          "ERROR: No matching distribution found for vivarium-core==1.6.0")
    assert c.failure_class == "INFRA_UNAVAILABLE", c
    # A real conflict lists what the index *did* have.
    c = classify.classify("ERROR: Could not find a version that satisfies the requirement "
                          "numpy==999 (from versions: 1.26.4, 2.0.0)\n"
                          "ERROR: No matching distribution found for numpy==999")
    assert (c.failure_class, c.action) == ("DEP_RESOLUTION_CONFLICT", "UNPIN_PKG"), c


@check("classify: a missing shared library maps soname -> Debian runtime package")
def _c_soname():
    # 17 corpus rows, all UNKNOWN: opencv wheels link libGL/libxcb/libgthread
    # that -slim bases do not ship. The old `build_mode` regex wanted `.so:` and
    # would have routed to SWITCH_INSTALL_MODE -- wrong on both counts.
    for so, pkg in [("libxcb.so.1", "libxcb1"), ("libGL.so.1", "libgl1"),
                    ("libgthread-2.0.so.0", "libglib2.0-0")]:
        c = classify.classify(f"somemod: ImportError: {so}: cannot open shared object file: "
                              f"No such file or directory", rung="L1")
        assert (c.failure_class, c.action, c.arg) == ("MISSING_SYSTEM_LIB", "ADD_APT_PKG", pkg), c
    # cv2 is a known family: one action names all of it (rev 4.1).
    c = classify.classify("cv2: ImportError: libxcb.so.1: cannot open shared object file", rung="L1")
    assert c.arg == classify.SONAME_BUNDLE["cv2"], c
    # Unmapped soname: class only, so the agent picks the package.
    c = classify.classify("ImportError: libfoo.so.9: cannot open shared object file")
    assert (c.failure_class, c.action) == ("MISSING_SYSTEM_LIB", None), c
    # The model's own compiled extension failing under mounted mode is still a
    # build-mode problem, not an apt problem.
    c = classify.classify("ImportError: /model/mypkg/_ext.cpython-311-x86_64-linux-gnu.so: "
                          "cannot open shared object file", rung="L2")
    assert (c.failure_class, c.action) == ("BUILD_MODE_MISMATCH", "SWITCH_INSTALL_MODE"), c


@check("classify: numpy-2 removed attributes are an ABI_MISMATCH with a deterministic pin")
def _c_numpy2():
    c = classify.classify("pint: AttributeError: module 'numpy' has no attribute 'cumproduct'", rung="L1")
    assert (c.failure_class, c.action, c.arg) == ("ABI_MISMATCH", "PIN_PKG", "numpy<2"), c


@check("classify: R install failures split into not-available / build-failed / network")
def _c_r_install():
    c = classify.classify("Warning message:\npackage 'deSolve' is not available for this version of R")
    assert (c.failure_class, c.arg) == ("MISSING_DEPENDENCY", "deSolve"), c
    c = classify.classify("ERROR: compilation failed for package 'Rcpp'\n"
                          "installation of package 'Rcpp' had non-zero exit status")
    assert (c.failure_class, c.arg) == ("COMPILE_ERROR", "Rcpp"), c


@check("classify: R CMD INSTALL naming a missing Import is MISSING_DEPENDENCY -> ADD_PKG")
def _c_r_dependency():
    c = classify.classify("ERROR: dependency ‘deSolve’ is not available for package ‘mbmm’\n"
                          "* removing ‘/usr/local/lib/R/site-library/mbmm’", rung="L0")
    assert (c.failure_class, c.action, c.arg) == ("MISSING_DEPENDENCY", "ADD_PKG", "deSolve"), c


@check("classify: a base with no interpreter is BASE_IMAGE_MISMATCH -> CHANGE_BASE_IMAGE")
def _c_no_interpreter():
    # 8 corpus rows: ubuntu:24.04 chosen for a repo whose language the scan
    # missed, then the pip bootstrap ran `python` on an image without one.
    c = classify.classify("/bin/sh: 1: python: not found", rung="L0")
    assert (c.failure_class, c.action, c.arg) == ("BASE_IMAGE_MISMATCH", "CHANGE_BASE_IMAGE", "python:3.11-slim"), c
    c = classify.classify("/bin/sh: 1: Rscript: not found")
    assert c.arg == "rocker/r-ver:4.4.1", c


@check("classify: a malformed image reference is SPEC_INVALID, not UNKNOWN")
def _c_spec_invalid():
    c = classify.classify("error building image: could not parse reference: rocker/r-ver:4.4.1@sha256:f3ef")
    assert c.failure_class == "SPEC_INVALID" and c.routes_to == "retry", c


@check("classify: the L1 probe's 'distribution not installed' is MISSING_DEPENDENCY -> ADD_PKG")
def _c_dist_missing():
    c = classify.classify("scipy: PackageNotFoundError: distribution 'scipy' is not installed", rung="L1")
    assert (c.failure_class, c.action, c.arg) == ("MISSING_DEPENDENCY", "ADD_PKG", "scipy"), c


@check("classify: ROUTING and the taxonomy spec agree on every class")
def _c_routing_spec():
    spec_text = (ROOT / "specs" / "failure_taxonomy.md").read_text(encoding="utf-8")
    for cls in classify.ROUTING:
        assert f"`{cls}`" in spec_text, f"{cls} is in ROUTING but not in specs/failure_taxonomy.md"


@check("renderer: every RUN opens with a step marker naming index, kind and field")
def _r_markers():
    rendered = render(spec(pre_install=["echo pre"]))
    for st in rendered.steps:
        if st.instruction.startswith("RUN "):
            m = envspec_mod._STEP_MARKER_RE.search(st.instruction)
            assert m and (int(m.group(1)), m.group(2), m.group(3)) == (st.index, st.kind, st.field), st
        else:
            assert "::envbuild::" not in st.instruction, st


@check("builder: the last step marker in a log attributes the failure, whatever the executor printed")
def _bd_marker_attr():
    rendered = render(spec())
    pkg = next(s for s in rendered.steps if s.kind == "pkg")
    apt = next(s for s in rendered.steps if s.kind == "apt")
    # No instruction echo at all -- only the markers the commands themselves
    # printed, as an executor that swallows its own echo would leave behind.
    text = "\n".join([f"::envbuild::step={apt.index} kind=apt field=apt_packages",
                      "Get:1 http://deb.debian.org bookworm InRelease",
                      f"::envbuild::step={pkg.index} kind=pkg field=pkg_specs",
                      "ERROR: No matching distribution found for numpy<2"])
    st = builder.match_kaniko_step(text, rendered.steps)
    assert st is not None and (st.index, st.field) == (pkg.index, "pkg_specs"), st
    # And through a rawjson stream, where the first corpus run saw nothing.
    st = builder.match_kaniko_step(_rawjson(text), rendered.steps)
    assert st is not None and st.index == pkg.index, st


@check("renderer: bootstrap is pinned, pip caches are off, labels render last")
def _r_hygiene():
    text = render(spec(), labels={"io.envbuild.model_id": "bench:mbmm"}).text
    for pin in envspec_mod.BOOTSTRAP_PINS:
        assert pin in text, pin
    assert "pip install --upgrade pip setuptools wheel" not in text
    assert "PIP_NO_CACHE_DIR=1" in text and "PYTHONDONTWRITEBYTECODE=1" in text
    assert text.rstrip().splitlines()[-1].strip().startswith("io.envbuild.") or \
        "LABEL" in text.rstrip().splitlines()[-1], text
    assert "io.envbuild.model_id=bench:mbmm" in text
    # Labels never enter the spec hash: same spec, same layers, different job.
    assert spec().hash() == spec().hash()
    a = render(spec(), labels={"io.envbuild.job": "a"}).steps
    b = render(spec(), labels={"io.envbuild.job": "b"}).steps
    assert [s.instruction for s in a[:-1]] == [s.instruction for s in b[:-1]]


@check("renderer: the spec's own env_vars beat the hygiene defaults")
def _r_env_override():
    text = render(spec(env_vars={"PIP_NO_CACHE_DIR": "0"})).text
    assert "PIP_NO_CACHE_DIR=0" in text and "PIP_NO_CACHE_DIR=1" not in text


@check("ladder: L1 receives distribution names and resolves modules inside the image")
def _l_dists():
    s = spec(pkg_specs=["scikit-learn", "PyYAML", "numpy<2", "pip", "vivarium-core==0.0.34"])
    assert ladder.dist_names(s) == ["numpy", "pyyaml", "scikit-learn", "vivarium-core"]
    calls = []

    class Rec:
        def run(self, image_ref, code_ref, cmd, mount, timeout_s, env=None, **kw):
            calls.append(env)
            return verifier.RunResult(True, 0, "L1 ok")
    ladder.run_l1(Rec(), "img@sha256:x", s, 60)
    env = calls[0]
    assert env["ENVBUILD_DISTS"] == "numpy,pyyaml,scikit-learn,vivarium-core", env
    # The table's guess rides along, per dist, for metadata that names no module.
    assert "vivarium-core=vivarium" in env["ENVBUILD_MODS"] and "pyyaml=yaml" in env["ENVBUILD_MODS"]
    assert "packages_distributions" in env["ENVBUILD_PROBE"]
    assert "ENVBUILD_LOCK_BEGIN" in env["ENVBUILD_PROBE"]


@check("ladder: the L1 probe runs against this interpreter and emits a parseable lockfile")
def _l_probe_local():
    # Execute the probe payload in-process, the way the runner does in the pod,
    # against distributions this test environment certainly has.
    import io as _io
    import contextlib
    import os as _os
    env = {"ENVBUILD_DISTS": "pyyaml,definitely-not-a-real-dist", "ENVBUILD_MODS": "pyyaml=yaml"}
    out, err = _io.StringIO(), _io.StringIO()
    saved = dict(_os.environ)
    _os.environ.update(env)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(ladder._L1_PROBE, {"os": _os})
            except SystemExit as e:
                assert e.code == 1
    finally:
        _os.environ.clear()
        _os.environ.update(saved)
    assert "PackageNotFoundError: distribution 'definitely-not-a-real-dist' is not installed" in err.getvalue(), err.getvalue()
    assert "yaml" not in err.getvalue(), "an installed dist must resolve its module from metadata"
    # Now the passing case, which must print the lock block.
    _os.environ["ENVBUILD_DISTS"] = "pyyaml"
    out = _io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            exec(ladder._L1_PROBE, {"os": _os})
    finally:
        _os.environ.clear()
        _os.environ.update(saved)
    lock = ladder.parse_lockfile(out.getvalue())
    assert lock and lock["format"] == "pip" and lock["sha256"].startswith("sha256:"), lock
    assert any(e.startswith("pyyaml==") for e in lock["entries"]), lock["entries"][:5]


@check("ladder: climb records the lockfile from L1 stdout")
def _l_lock_in_notes():
    class V:
        code_ref = "/models/m/1.0"

        def run(self, image_ref, code_ref, cmd, mount, timeout_s, env=None, **kw):
            probe = (env or {}).get("ENVBUILD_PROBE", "")
            if "ENVBUILD_DISTS" in probe:       # L1
                return verifier.RunResult(True, 0, "L1 ok\nENVBUILD_LOCK_BEGIN pip\nnumpy==1.26.4\nENVBUILD_LOCK_END\n")
            return verifier.RunResult(False, 1, "", "ENVBUILD_ENTRYPOINT_MISSING: /model/run.py")

        def outputs_listing(self):
            return []
    res = ladder.climb(V(), "img", "/models/m/1.0", spec(), {"kind": "file", "value": "run.py"},
                       "python run.py", 60)
    assert res.reached == "L1" and res.notes["lockfile"]["entries"] == ["numpy==1.26.4"], res.notes


@check("patch: ADD_PRE_INSTALL_CMD refuses the shims and workarounds the corpus produced")
def _p_pre_install_guard():
    # Verbatim (trimmed) from attempts.jsonl action_taken.arg.
    refused = [
        "python -c 'import site; open(site.getsitepackages()[0] + \"/ipython.py\", \"w\")"
        ".write(\"from IPython import *\\n\")'",
        "mkdir -p /usr/local/lib/python3.9/site-packages/opencv_python && printf '%s\\n' "
        "'from cv2 import *' > /usr/local/lib/python3.9/site-packages/opencv_python/__init__.py",
        "echo '151.101.0.223 pypi.org files.pythonhosted.org' >> /etc/hosts",
        "Rscript -e 'install.packages(\"deSolve\", repos=\"https://cloud.r-project.org\")'",
        "pip install https://files.pythonhosted.org/packages/source/v/vivarium-cell/vivarium-cell-0.0.23.tar.gz",
        "ln -s /usr/bin/python3 /usr/local/bin/python",
    ]
    for cmd in refused:
        assert patch.forbidden_pre_install(cmd), cmd
        try:
            patch.apply(spec(), "ADD_PRE_INSTALL_CMD", cmd)
            raise AssertionError(f"accepted: {cmd}")
        except patch.PatchError:
            pass
    # Legitimate environment preparation still goes through.
    for cmd in ["mkdir -p /root/.cache/R", "pip config set global.timeout 120",
                "printf 'Acquire::Retries \"5\";\\n' > /etc/apt/apt.conf.d/80-retries",
                "mkdir -p /model && ln -s /model/biomodels/coreClock/dat /model/dat"]:
        assert patch.forbidden_pre_install(cmd) is None, cmd
        patch.apply(spec(), "ADD_PRE_INSTALL_CMD", cmd)


@check("driver: infra-failed attempts are not charged against the budget")
def _d_infra_budget():
    cfg = driver.load_config(None)
    st = {"attempt": 4, "infra_retries": 2, "infra_seconds": 900.0,
          "started_at": __import__("time").time() - 1000, "closed": False}
    assert driver.charged_attempts(st) == 2
    assert driver._budget_check(cfg, st) is None, "2 charged of 5, 100 s net of infra"
    st["infra_retries"] = 0
    assert driver._budget_check(cfg, st) is None
    st["attempt"] = 5
    assert "attempt budget" in driver._budget_check(cfg, st)


@check("driver: the attempt deadline is derived from the pod budgets, not a magic number")
def _d_deadline():
    cfg = driver.load_config(None)
    d = driver.attempt_deadline_s(cfg)
    assert d == (cfg.getint("budgets", "build_timeout_s") + 2 * cfg.getint("budgets", "verify_timeout_s")
                 + cfg.getint("budgets", "l3_timeout_max_s") + 180)
    assert d < 36000, "the 10-hour attempt in the first corpus must be impossible to record as anything but TIMEOUT"


@check("driver: model ids must look like <scheme>:<path>")
def _d_model_id():
    ok = ["mism:model/mbmm", "bench:tumor-tcell", "local:repo_name"]
    bad = ["gpt-5.6-luna", "anthropic/claude-opus-4-5", "mbmm", ""]
    for m in ok:
        assert driver._MODEL_ID.match(m), m
    for m in bad:
        assert not driver._MODEL_ID.match(m), m


@check("driver: run provenance is read from the environment onto every row")
def _d_provenance():
    import os as _os
    _os.environ.update({"ENVBUILD_RUN_ID": "run-42", "AI_MODEL": "some/model", "ENVBUILD_CACHE_REPO": ""})
    try:
        cfg = driver.load_config(None)
        prov = driver.run_provenance(cfg)
        assert prov["run_id"] == "run-42" and prov["agent_model"] == "some/model", prov
        assert prov["kaniko_image"].startswith("gcr.io/kaniko-project/executor:"), prov
    finally:
        for k in ("ENVBUILD_RUN_ID", "AI_MODEL", "ENVBUILD_CACHE_REPO"):
            _os.environ.pop(k, None)


@check("builder: Kaniko layer cache is off by default and on when a cache repo is configured")
def _bd_cache_flag():
    off = mk_builder()._pod_manifest("p", "img:t", "/workspace/context", 60)
    args = off["spec"]["containers"][0]["args"]
    assert "--cache=false" in args and not any(a.startswith("--cache-repo") for a in args)
    on = mk_builder(cache_repo="docker.io/mismplatform/envbuild-cache")._pod_manifest("p", "img:t", "/workspace/context", 60)
    args = on["spec"]["containers"][0]["args"]
    assert "--cache=true" in args and "--cache-repo=docker.io/mismplatform/envbuild-cache" in args


@check("record: rev-3 rows carry provenance, charge and lockfile fields")
def _r_rev3_fields():
    for k in ("run", "charged", "lockfile_sha256"):
        assert k in record.ATTEMPT_REQUIRED, k
    for k in ("run", "charged_attempts", "lockfile_sha256"):
        assert k in record.VERDICT_REQUIRED, k


# ---------------------------------------------------------------- rev 4: repo examples / annotation corrections
# Success is "the container runs the example the repo itself provides". These
# checks build small repos on disk in the shapes the 7-model corpus has.
import yaml                                            # noqa: E402
import examples                                        # noqa: E402


def _repo(tree: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="envbuild-ex-"))
    for rel, text in tree.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def _files(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


@check("examples: README run lines are discovered, install lines and prose are not")
def _ex_readme():
    root = _repo({
        "README.md": "Install:\n```bash\npip install -e .\n```\nRun the quick demo:\n```python\n"
                     "uv run python sim/run_demo.py --steps 10\n$ pytest -m 'not slow'\n```\n"
                     "Then python is great. Reproduce the paper:\n```\npython scripts/reproduce_paper.py\n```\n",
        "sim/run_demo.py": "print('hi')\n", "scripts/reproduce_paper.py": "pass\n",
        "tests/test_x.py": "def test_a(): pass\n", "pytest.ini": "[pytest]\nmarkers = slow\n",
    })
    ex = examples.discover(root, _files(root))
    cmds = [e["command"] for e in ex]
    assert "python sim/run_demo.py --steps 10" in cmds, cmds          # `uv run` stripped: the image is the env
    assert "pytest -m 'not slow'" in cmds, cmds
    assert not any(c.startswith("pip") for c in cmds), cmds
    assert not any("python is great" in c for c in cmds), cmds
    tiers = {e["command"]: e["tier"] for e in ex}
    assert tiers["python scripts/reproduce_paper.py"] == "full", tiers
    assert tiers["python sim/run_demo.py --steps 10"] == "smoke", tiers
    assert ex[0]["source"] == "README.md", "README order is the author's ranking"


@check("examples: a documented script that no longer exists is not an example")
def _ex_missing_file():
    root = _repo({"README.md": "```\npython gone.py\npython here.py\n```\n", "here.py": ""})
    cmds = [e["command"] for e in examples.discover(root, _files(root))]
    assert cmds == ["python here.py"], cmds


@check("examples: inst/examples and testthat are discovered for an R package; infra scripts are skipped")
def _ex_r_layout():
    root = _repo({"DESCRIPTION": "Package: mbmm\n", "inst/examples/01_demo.R": "cat('x')\n",
                  "tests/testthat.R": "library(testthat)\n", "tests/testthat/test-a.R": "",
                  "scripts/ec2_cluster.py": "", "scripts/build_and_push_image.py": ""})
    cmds = [e["command"] for e in examples.discover(root, _files(root))]
    assert "Rscript inst/examples/01_demo.R" in cmds, cmds
    assert "Rscript -e 'testthat::test_local()'" in cmds, cmds
    assert not any("ec2" in c or "push" in c for c in cmds), cmds


@check("examples: structural candidates are the fallback when nothing is documented")
def _ex_structure_fallback():
    root = _repo({"biomodels/coreClock/run_clock_model.py": "import numpy as np\n"
                  "params = np.loadtxt('dat/params.txt')\n", "biomodels/coreClock/dat/params.txt": "1\n"})
    ex = examples.discover(root, _files(root), candidates=["biomodels\\coreClock\\run_clock_model.py"])
    assert ex == [{"command": "python biomodels/coreClock/run_clock_model.py", "source": "structure",
                   "tier": "smoke", "file": "biomodels/coreClock/run_clock_model.py", "placeholders": False}], ex


@check("examples: derive_workdir fires only for script-relative data paths (circadian-clock)")
def _ex_workdir():
    root = _repo({"biomodels/coreClock/run_clock_model.py": "import numpy as np\n"
                  "y0 = np.loadtxt(\"dat/y0.txt\")\n", "biomodels/coreClock/dat/y0.txt": "0\n",
                  "pkg/main.py": "open('data/root.csv')\n", "data/root.csv": "a\n",
                  "pkg/nodata.py": "print(1)\n"})
    assert examples.derive_workdir(root, "biomodels/coreClock/run_clock_model.py") == "biomodels/coreClock"
    assert examples.derive_workdir(root, "pkg/main.py") is None, "resolves from the root already"
    assert examples.derive_workdir(root, "pkg/nodata.py") is None
    assert examples.derive_workdir(root, None) is None


@check("examples: check_annotation flags the corpus's actual annotation defects")
def _ex_findings():
    root = _repo({"README.md": "```\npython chemotaxis/experiments/paper_experiments.py 7b\n```\n",
                  "chemotaxis/experiments/paper_experiments.py": "", "inst/examples/01.R": ""})
    ex = examples.discover(root, _files(root))
    ann = {"entry_points": [
        {"command": "R"},                                                       # mbmm
        {"command": "GAMA GUI: open HybridTB.gaml"},                            # hybrid-model-tb
        {"command": "python chemotaxis/processes/gone.py"},                     # file missing
        {"command": "python chemotaxis/experiments/paper_experiments.py <EXPERIMENT>"},  # placeholder
    ]}
    codes = {(f["code"], f["entry"]): f for f in examples.check_annotation(root, ann, ex)}
    assert ("not_a_command", "R") in codes
    assert ("not_a_command", "GAMA GUI: open HybridTB.gaml") in codes
    assert ("file_missing", "python chemotaxis/processes/gone.py") in codes
    ph = codes[("placeholder_args", "python chemotaxis/experiments/paper_experiments.py <EXPERIMENT>")]
    assert ph["suggestion"] == "python chemotaxis/experiments/paper_experiments.py 7b", ph
    assert not any(c == "headline_missing" for c, _ in codes), "the headline's file IS among the entries"
    # No entry points at all, but the repo has examples.
    f = examples.check_annotation(root, {}, ex)
    assert f and f[0]["code"] == "no_entry_points" and f[0]["suggestion"]


@check("examples: grounded() accepts documented examples and existing scripts, refuses the rest")
def _ex_grounded():
    root = _repo({"README.md": "```\npytest -m 'not slow'\n```\n", "tests/test_a.py": "",
                  "run.py": "", "pytest.ini": "[pytest]\nmarkers = slow\n"})
    ex = examples.discover(root, _files(root))
    assert examples.grounded(root, "pytest -m 'not slow'", ex) is None
    assert examples.grounded(root, "python run.py --steps 5", ex) is None
    assert examples.grounded(root, "python nope.py", ex)
    assert examples.grounded(root, "python run.py <N>", ex)
    assert examples.grounded(root, "R", ex)
    assert examples.grounded(root, "some_console_script --flag", ex), "no script, not documented"


@check("driver: choose_entry prefers a runnable annotation entry, else the repo's smoke example")
def _d_choose_entry():
    ev = {"examples": [{"command": "python scripts/reproduce.py", "tier": "full", "source": "README.md",
                        "placeholders": False},
                       {"command": "pytest", "tier": "smoke", "source": "tests", "placeholders": False}]}
    ann = {"entry_points": [{"command": "R"}, {"command": "Rscript inst/examples/01.R"}]}
    findings = [{"code": "not_a_command", "entry": "R"}]
    cmd, src, corr = driver.choose_entry(ann, ev, findings)
    assert (cmd, src, corr) == ("Rscript inst/examples/01.R", "annotation", None)
    ann = {"entry_points": [{"command": "R"}]}
    cmd, src, corr = driver.choose_entry(ann, ev, findings)
    assert (cmd, src) == ("pytest", "repo_example") and corr["was"] == "R" and corr["outcome"] == "pending", corr
    cmd, src, corr = driver.choose_entry({}, {"examples": []}, [])
    assert (cmd, src, corr) == ("", "none", None)


@check("driver: init records entry source, findings and the workdir correction")
def _d_init_corrections():
    root = _repo({"biomodels/coreClock/run_clock_model.py": "import numpy as np\n"
                  "y0 = np.loadtxt(\"dat/y0.txt\")\n", "biomodels/coreClock/dat/y0.txt": "0\n",
                  "metadata-package/execution.yaml":
                      "execution:\n  language: python\n  entry_points:\n"
                      "    - command: python biomodels/coreClock/run_clock_model.py\n"})
    outdir = Path(tempfile.mkdtemp(prefix="envbuild-out-"))
    cfg = driver.load_config(None)
    cfg.set("paths", "outputs", str(outdir))
    saved = baseselect.resolve_digest
    baseselect.resolve_digest = lambda img: "sha256:" + "d" * 64
    try:
        import io as _io, contextlib
        buf = _io.StringIO()
        ns = type("A", (), {"repo": str(root), "annotation": None, "model_id": "bench:clock", "job_id": "job-t-clock"})
        with contextlib.redirect_stdout(buf):
            driver.cmd_init(ns, cfg)
    finally:
        baseselect.resolve_digest = saved
    st = driver.load_state(cfg, "job-t-clock")
    assert st["entrypoint_source"] == "annotation" and st["command"] == "python biomodels/coreClock/run_clock_model.py"
    assert st["spec"]["mount"]["workdir"] == "/model/biomodels/coreClock", st["spec"]["mount"]
    wd = [c for c in st["annotation_corrections"] if c["field"] == "mount.workdir"]
    assert wd and wd[0]["applied_by"] == "init" and wd[0]["outcome"] == "pending", st["annotation_corrections"]
    assert any(f["code"] == "needs_workdir" for f in st["annotation_findings"])
    out = json.loads(buf.getvalue())
    assert out["entrypoint_source"] == "annotation" and out["annotation_corrections"] == st["annotation_corrections"]


@check("driver: correction outcomes follow the rung; unverified ones never reach the patch file")
def _d_correction_outcomes():
    def st(*outs):
        return {"annotation_corrections": [{"field": "f", "was": "a", "now": "b", "outcome": o} for o in outs]}
    s = st("pending"); driver.resolve_corrections(s, "L3", True, None)
    assert s["annotation_corrections"][0]["outcome"] == "verified"
    s = st("pending"); driver.resolve_corrections(s, "L2", False, "RUNTIME_ERROR")
    assert s["annotation_corrections"][0]["outcome"] == "helped"
    s = st("pending"); driver.resolve_corrections(s, "L1", False, "ENTRYPOINT_UNKNOWN")
    assert s["annotation_corrections"][0]["outcome"] == "rejected"
    s = st("pending"); driver.resolve_corrections(s, "L0", False, "UNKNOWN")
    assert s["annotation_corrections"][0]["outcome"] == "pending", "L0 says nothing about the entry"
    s = st("pending", "helped"); fin = driver.finalize_corrections(s)
    assert [c["outcome"] for c in fin] == ["unverified", "helped"]
    outdir = Path(tempfile.mkdtemp(prefix="envbuild-out-"))
    cfg = driver.load_config(None); cfg.set("paths", "outputs", str(outdir))
    state = {"job_id": "job-t-p", "model_id": "bench:x", "code_revision": "abc", "annotation_path": None,
             "annotation_findings": []}
    assert driver.write_annotation_patch(cfg, state, [{"field": "f", "now": "b", "outcome": "rejected"}]) is None
    path = driver.write_annotation_patch(cfg, state, fin)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))["annotation_patch"]
    assert doc["schema"] == "envbuild-annotation-patch/1" and len(doc["changes"]) == 1
    assert doc["changes"][0]["outcome"] == "helped"


@check("patch: SET_ENTRYPOINT sets the spec's entrypoint and is in the enum")
def _p_set_entrypoint():
    r = patch.apply(spec(), "SET_ENTRYPOINT", "python run.py --steps 10")
    assert r.spec.entrypoint == ["python", "run.py", "--steps", "10"] and not r.needs_digest
    try:
        patch.apply(spec(), "SET_ENTRYPOINT", "")
        raise AssertionError("empty accepted")
    except patch.PatchError:
        pass


@check("driver: L3 gets its own timeout, sized by the annotation when it says so")
def _d_l3_timeout():
    cfg = driver.load_config(None)
    st = {"command": "python run.py", "annotation": {"entry_points": [{"command": "python run.py"}]}}
    assert driver.l3_timeout_s(cfg, st) == cfg.getint("budgets", "l3_timeout_s") == 600
    st["annotation"]["entry_points"][0]["expected_runtime_s"] = 400
    assert driver.l3_timeout_s(cfg, st) == 660                     # 1.5x + 60 s
    st["annotation"]["entry_points"][0]["expected_runtime_s"] = 100000
    assert driver.l3_timeout_s(cfg, st) == cfg.getint("budgets", "l3_timeout_max_s")
    assert driver.attempt_deadline_s(cfg) > cfg.getint("budgets", "l3_timeout_max_s")


@check("ladder: the lockfile is found when the verifier returns a merged log stream (K8s)")
def _l_lock_merged_stream():
    class V:
        def run(self, image_ref, code_ref, cmd, mount, timeout_s, env=None, **kw):
            probe = (env or {}).get("ENVBUILD_PROBE", "")
            if "ENVBUILD_DISTS" in probe:
                # K8sVerifier shape: stdout empty, everything in stderr.
                return verifier.RunResult(True, 0, "", "L1 ok: 3 distributions probed\nENVBUILD_LOCK_BEGIN pip\nnumpy==1.26.4\npint==0.19.2\nENVBUILD_LOCK_END\n")
            return verifier.RunResult(False, 1, "", "ENVBUILD_ENTRYPOINT_MISSING: x")

        def outputs_listing(self):
            return []
    res = ladder.climb(V(), "img", "/models/m/1.0", spec(), {"kind": "file", "value": "run.py"}, "python run.py", 60)
    assert res.notes["lockfile"] and res.notes["lockfile"]["entries"] == ["numpy==1.26.4", "pint==0.19.2"], res.notes


@check("ladder: climb passes l3_timeout_s to L3 only and records it")
def _l_l3_timeout():
    seen = []

    class V:
        def run(self, image_ref, code_ref, cmd, mount, timeout_s, env=None, **kw):
            seen.append(timeout_s)
            probe = (env or {}).get("ENVBUILD_PROBE", "")
            if "ENVBUILD_DISTS" in probe:
                return verifier.RunResult(True, 0, "ENVBUILD_LOCK_BEGIN pip\nnumpy==1.0\nENVBUILD_LOCK_END")
            return verifier.RunResult(True, 0, "ok")

        def outputs_listing(self):
            return ["out.csv"]
    res = ladder.climb(V(), "img", "/models/m/1.0", spec(), {"kind": "file", "value": "run.py"},
                       "python run.py", 60, l3_timeout_s=900)
    assert res.reached == "L3" and seen == [60, 60, 900], seen
    assert res.notes["l3_timeout_s"] == 900


@check("record: rev-4 fields are required on rows")
def _r_rev4_fields():
    for k in ("entrypoint_used", "entrypoint_source"):
        assert k in record.ATTEMPT_REQUIRED and k in record.VERDICT_REQUIRED, k
    assert "annotation_corrections" in record.VERDICT_REQUIRED


# ---------------------------------------------------------------- rev 4.1: what the first live pass (aks-rev4-1) taught
# Every shape below is the actual error text of a job in that pass.

@check("ladder: a relative script is anchored to code_path when workdir moves (chemotaxis a4, spatio-flux a2, clock a1)")
def _l_resolve_command():
    import dataclasses as _dc
    m0 = MountContract()
    mv = _dc.replace(m0, workdir="/outputs")
    assert ladder.resolve_command("python chemotaxis/processes/x.py", m0) == ["python", "chemotaxis/processes/x.py"]
    assert ladder.resolve_command("python chemotaxis/processes/x.py", mv) == ["python", "/model/chemotaxis/processes/x.py"]
    assert ladder.resolve_command("python -u scripts/run.py cfg.py", mv) == ["python", "-u", "/model/scripts/run.py", "cfg.py"]
    assert ladder.resolve_command("Rscript inst/examples/01.R", _dc.replace(m0, workdir="/model/x")) == ["Rscript", "/model/inst/examples/01.R"]
    assert ladder.resolve_command("python -m pytest tests", mv) == ["python", "-m", "pytest", "tests"]
    assert ladder.resolve_command("python /model/scripts/x.py", mv) == ["python", "/model/scripts/x.py"]
    assert ladder.resolve_command("pytest -m 'not slow'", mv) == ["pytest", "-m", "not slow"]
    # and run_l3 uses it
    seen = {}

    class V:
        def run(self, image_ref, code_ref, cmd, mount, timeout_s, **kw):
            seen["cmd"] = cmd
            return verifier.RunResult(True, 0, "", "")

        def outputs_listing(self):
            return []
    ladder.run_l3(V(), "img", "/models/m/1.0", EnvSpec(**{**spec().to_dict(), "mount": mv}) if False else
                  _dc.replace(spec(), mount=mv), "python biomodels/coreClock/run_clock_model.py", 60)
    assert seen["cmd"] == ["python", "/model/biomodels/coreClock/run_clock_model.py"], seen


@check("examples: grounded() accepts the in-container absolute form (spatio-flux, chemotaxis refusals)")
def _ex_grounded_abs():
    root = _repo({"scripts/reproduce.py": ""})
    assert examples.grounded(root, "python /model/scripts/reproduce.py", []) is None
    assert examples.grounded(root, "python /model/scripts/gone.py", []) == "scripts/gone.py does not exist in the repo"
    assert "outside /model" in examples.grounded(root, "python /opt/x.py", [])
    assert examples.grounded(root, "python /code/scripts/reproduce.py", [], code_path="/code") is None


@check("classify: pip's Requires-Python hint -> CHANGE_INTERPRETER_VERSION, not UNPIN (tumor-tcell a1)")
def _c_requires_python():
    text = ("ERROR: Ignored the following versions that require a different python version: "
            "1.5.7 Requires-Python >=3.9, <3.12; 1.6.0 Requires-Python >=3.9, <3.12\n"
            "ERROR: Could not find a version that satisfies the requirement vivarium-core==1.6.0 "
            "(from versions: 0.0.1, 1.5.6)\nERROR: No matching distribution found for vivarium-core==1.6.0")
    c = classify.classify(text, rung="L0")
    assert (c.failure_class, c.action, c.arg) == ("DEP_RESOLUTION_CONFLICT", "CHANGE_INTERPRETER_VERSION", "3.11"), c
    # no hint -> the old behaviour
    c = classify.classify("ERROR: Could not find a version that satisfies the requirement foo==9 (from versions: 1.0)\n"
                          "ERROR: No matching distribution found for foo==9")
    assert (c.action, c.arg) == ("UNPIN_PKG", "foo==9"), c
    assert classify.requires_python_pick(">=3.9, <3.12") == "3.11"
    assert classify.requires_python_pick(">=3.12") == "3.13"
    assert classify.requires_python_pick("<3.6") is None


@check("classify: a cv2 soname miss names the whole opencv runtime family (tumor-tcell a2-a4)")
def _c_soname_bundle():
    c = classify.classify("cv2 (from opencv-python): ImportError: libGL.so.1: cannot open shared object file: "
                          "No such file or directory", rung="L1")
    assert c.action == "ADD_APT_PKG" and c.arg == classify.SONAME_BUNDLE["cv2"], c
    c = classify.classify("lxml (from lxml): ImportError: libxml2.so.2: cannot open shared object file", rung="L1")
    assert c.arg == "libxml2", c
    # patch stores a family one package per entry and dedupes against it
    r = patch.apply(spec(), "ADD_APT_PKG", "libgl1 libglib2.0-0 libxcb1")
    assert r.spec.apt_packages[-3:] == ["libgl1", "libglib2.0-0", "libxcb1"]
    try:
        patch.apply(r.spec, "ADD_APT_PKG", "libgl1")
        raise AssertionError("dup accepted")
    except patch.PatchError:
        pass
    try:
        patch.apply(spec(), "ADD_APT_PKG", "rm -rf /")
        raise AssertionError("shell accepted")
    except patch.PatchError:
        pass


@check("classify: 'cannot import name' at L3 is ABI_MISMATCH on the dependent's dist (tumor-tcell a5)")
def _c_cannot_import_name():
    c = classify.classify("ImportError: cannot import name 'Quantity' from 'vivarium.core.serialize' "
                          "(/usr/local/lib/python3.8/site-packages/vivarium/core/serialize.py)", rung="L3", exit_code=1)
    assert (c.failure_class, c.action, c.arg) == ("ABI_MISMATCH", "PIN_PKG", "vivarium-core"), c


@check("classify: a toolchain named as a package is UNSUPPORTED_TOOLCHAIN (hybrid-model-tb)")
def _c_unsupported_tool():
    c = classify.classify("Error in library(GAMA) : there is no package called ‘GAMA’", rung="L1")
    assert (c.failure_class, c.routes_to) == ("UNSUPPORTED_TOOLCHAIN", "dead-letter"), c
    c = classify.classify("Error in library(deSolve) : there is no package called ‘deSolve’", rung="L1")
    assert (c.failure_class, c.arg) == ("MISSING_DEPENDENCY", "deSolve"), c


@check("driver: draft_spec drops toolchain 'dependencies' and init records the finding")
def _d_dropped_deps():
    ev = {"python": {}, "r": {"deps": ["deSolve", "GAMA Platform1.8", "ggplot2"]}, "system_hints": {"apt": []},
          "local_module_paths": {}, "local_modules": [], "files": [], "language": "r"}
    ann = {"language": "r", "declared_deps": ["deSolve", "GAMA Platform1.8"]}
    choice = baseselect.select(ev, ann)
    s = driver.draft_spec(ev, ann, choice, "sha256:" + "a" * 64, "kaniko")
    assert "GAMA Platform1.8" not in s.pkg_specs and "deSolve" in s.pkg_specs, s.pkg_specs
    assert driver.draft_spec.dropped_deps == ["GAMA Platform1.8"]


# ---------------------------------------------------------------- rev 4.2: what the second live pass (aks-rev4-2) taught

@check("classify: mount_denied repairs by WHERE the write went (chemotaxis vs spatio-flux)")
def _c_mount_denied_path_aware():
    # file-relative write into the code mount -> mount the writable volume there
    c = classify.classify("OSError: [Errno 30] Read-only file system: '/model/out/processes'", rung="L3", exit_code=1)
    assert (c.action, c.arg) == ("FIX_MOUNT_CONTRACT", "output_path=/model/out"), c
    # cwd-relative write -> writable copy
    c = classify.classify("OSError: [Errno 30] Read-only file system: 'studies/reference_demo_x2y2/charts'", rung="L3", exit_code=1)
    assert c.arg == "writable_copy=true", c
    # the output mount itself: no canned argument
    c = classify.classify("PermissionError: [Errno 13] Permission denied: '/outputs/x'", rung="L3", exit_code=1)
    assert (c.failure_class, c.arg) == ("MOUNT_CONTRACT_ERROR", None), c
    # a different code_path is honoured
    c = classify.classify("OSError: [Errno 30] Read-only file system: '/code/out/x'", rung="L3", exit_code=1,
                          mount={"code_path": "/code", "workdir": "/code"})
    assert c.arg == "output_path=/code/out", c


@check("classify: a cwd-relative file missing after a workdir move is a writable-copy repair; otherwise MISSING_DATA_FILE")
def _c_cwd_relative_missing():
    txt = "FileNotFoundError: [Errno 2] No such file or directory: './investigations/spatio-flux-test-suite/investigation.yaml'"
    c = classify.classify(txt, rung="L3", exit_code=1, mount={"code_path": "/model", "workdir": "/outputs"})
    assert (c.failure_class, c.action, c.arg) == ("MOUNT_CONTRACT_ERROR", "FIX_MOUNT_CONTRACT", "writable_copy=true"), c
    c = classify.classify(txt, rung="L3", exit_code=1, mount={"code_path": "/model", "workdir": "/model"})
    assert (c.failure_class, c.routes_to) == ("MISSING_DATA_FILE", "submitter"), c
    # an absolute /model path is the older rule's business, not this one's
    c = classify.classify("FileNotFoundError: [Errno 2] No such file or directory: '/model/data/x.csv'", rung="L3", exit_code=1)
    assert c.failure_class == "MOUNT_CONTRACT_ERROR" and c.rule == "mount_missing_dir", c


@check("mount: writable_copy is a contract field, patchable, default off, round-trips")
def _m_writable_copy():
    import dataclasses as _dc
    assert MountContract().writable_copy is False
    assert MountContract.from_dict({"code_path": "/model"}).writable_copy is False      # pre-4.2 state.json
    r = patch.apply(spec(), "FIX_MOUNT_CONTRACT", "writable_copy=true")
    assert r.spec.mount.writable_copy is True and r.spec.mount.workdir == "/model"
    assert EnvSpec.from_dict(r.spec.to_dict()).mount.writable_copy is True
    try:
        patch.apply(r.spec, "FIX_MOUNT_CONTRACT", "writable_copy=true")
        raise AssertionError("no-op accepted")
    except patch.PatchError:
        pass
    try:
        patch.apply(spec(), "FIX_MOUNT_CONTRACT", "writable_copy=maybe")
        raise AssertionError("garbage accepted")
    except patch.PatchError:
        pass
    # the rendered image does not change: this is a run-time contract
    assert render(spec()).text == render(_dc.replace(spec(), mount=r.spec.mount)).text


@check("ladder: writable_copy runs L3 from a copy of the code, relative command intact")
def _l_writable_copy():
    import dataclasses as _dc
    seen = {}

    class V:
        def run(self, image_ref, code_ref, cmd, mount, timeout_s, **kw):
            seen["cmd"] = cmd
            return verifier.RunResult(True, 0, "", "")

        def outputs_listing(self):
            return []
    s = _dc.replace(spec(), mount=_dc.replace(MountContract(), writable_copy=True))
    ladder.run_l3(V(), "img", "/models/m/1.0", s, "python scripts/reproduce.py", 60)
    c = seen["cmd"]
    assert c[:2] == ["sh", "-c"] and c[3:7] == ["/model", "/scratch/model", "/scratch/model", "/outputs"], c
    assert c[7:] == ["python", "scripts/reproduce.py"], c          # workdir == code_path: not anchored
    assert 'cp -a "$0"/. "$1"/' in c[2] and 'cd "$2"' in c[2] and '"$@"; rc=$?' in c[2]
    # rev 4.7: what the run created inside the copy is carried to output_path
    # (aks-rev4-7: 19 studies reproduced, failed for "no output")
    assert '-newer "$m"' in c[2] and 'cp -p "$1" "$0/$1"' in c[2] and 'exit $rc' in c[2]
    # workdir inside the code tree follows the copy; the script anchors to the copy
    s = _dc.replace(spec(), mount=_dc.replace(MountContract(), writable_copy=True, workdir="/model/biomodels/coreClock"))
    ladder.run_l3(V(), "img", "/models/m/1.0", s, "python biomodels/coreClock/run.py", 60)
    c = seen["cmd"]
    assert c[5] == "/scratch/model/biomodels/coreClock" and c[7:] == ["python", "/scratch/model/biomodels/coreClock/run.py"], c


# ---------------------------------------------------------------- rev 4.3: what the third live pass (aks-rev4-3) taught

@check("evidence: a repo lockfile is read in full and reported (spatio-flux uv.lock, 268 KB)")
def _e_lockfile():
    pad = "\n".join(f'[[package]]\nname = "filler-{i}"\nversion = "0.{i}"\n' for i in range(6000))   # > 200 KB
    root = _repo({"uv.lock": 'version = 1\n[[package]]\nname = "Process_Bigraph"\nversion = "1.4.12"\n\n'
                             '[[package]]\nname = "bigraph-schema"\nversion = "1.3.2"\n\n' + pad})
    lk = evidence._parse_lockfile(root)
    assert lk and lk["kind"] == "uv" and lk["versions"]["process-bigraph"] == "1.4.12", lk and lk["kind"]
    assert len(lk["versions"]) == 6002
    root = _repo({"requirements.txt": "numpy==1.26.4\nscipy==1.10.1  # pinned\n"})
    lk = evidence._parse_lockfile(root)
    assert lk["kind"] == "requirements-pinned" and lk["versions"] == {"numpy": "1.26.4", "scipy": "1.10.1"}, lk
    root = _repo({"requirements.txt": "numpy>=1.20\nscipy\n"})
    assert evidence._parse_lockfile(root) is None, "a floor is not a lock"
    root = _repo({"renv.lock": '{"Packages": {"deSolve": {"Version": "1.40"}}}'})
    lk = evidence._parse_lockfile(root)
    assert lk["kind"] == "renv" and lk["usable"] is False and lk["versions"] == {"deSolve": "1.40"}
    assert evidence._parse_lockfile(_repo({"README.md": "x"})) is None


@check("driver: draft_spec pins declared deps to the repo lockfile, keeps extras, respects explicit pins")
def _d_lock_pins():
    ev = {"python": {"declared_deps": ["process-bigraph[ray]", "bigraph-schema", "numpy==1.26.4", "cobra>=0.29"],
                     "requires_python": ">=3.11,<3.13", "sources": ["pyproject.toml"], "scripts": []},
          "r": {}, "conda": {}, "system_hints": {"apt": []}, "local_module_paths": {}, "local_modules": [],
          "files": ["pyproject.toml", "uv.lock"], "language": "python",
          "lockfile": {"kind": "uv", "file": "uv.lock",
                       "versions": {"process-bigraph": "1.4.12", "bigraph-schema": "1.3.2", "numpy": "2.4.0", "cobra": "0.30.0"}}}
    ann = {"language": "python"}
    choice = baseselect.select(ev, ann)
    s = driver.draft_spec(ev, ann, choice, "sha256:" + "a" * 64, "kaniko")
    assert s.pkg_specs == ["process-bigraph[ray]==1.4.12", "bigraph-schema==1.3.2", "numpy==1.26.4", "cobra==0.30.0"], s.pkg_specs
    assert len(driver.draft_spec.lock_pins) == 3, driver.draft_spec.lock_pins
    # an R renv.lock is reported but not applied (renderer cannot pin R yet)
    ev2 = {**ev, "python": {}, "r": {"deps": ["deSolve"], "r_version": "4.4.1"}, "language": "r", "files": ["DESCRIPTION"],
           "lockfile": {"kind": "renv", "file": "renv.lock", "versions": {"deSolve": "1.40"}, "usable": False}}
    ann2 = {"language": "r", "declared_deps": ["deSolve"]}
    s2 = driver.draft_spec(ev2, ann2, baseselect.select(ev2, ann2), "sha256:" + "a" * 64, "kaniko")
    assert s2.pkg_specs == ["deSolve"] and driver.draft_spec.lock_pins == [], (s2.pkg_specs, driver.draft_spec.lock_pins)


# ---------------------------------------------------------------- rev 4.4: what the fourth live pass (aks-rev4-4) taught

@check("evidence/baseselect: a README that installs the project itself flips install_mode (spatio-flux)")
def _e_project_install_hint():
    root = _repo({"pyproject.toml": "[project]\nname='x'\nversion='1'\n", "README.md": "```\nuv sync\nuv run python scripts/reproduce.py\n```\n"})
    ev = evidence.scan(root)
    assert ev["compiled"]["project_install_hint"] is True
    assert baseselect.guess_install_mode(ev)[0] == "installed"
    root = _repo({"pyproject.toml": "[project]\nname='x'\nversion='1'\n", "README.md": "```\npip install -r requirements.txt\npython run.py\n```\n"})
    ev = evidence.scan(root)
    assert ev["compiled"]["project_install_hint"] is False and baseselect.guess_install_mode(ev)[0] == "mounted"
    # the phrase without a project to install is not a hint
    root = _repo({"README.md": "```\nuv run python run.py\n```\n", "run.py": ""})
    assert evidence.scan(root)["compiled"]["project_install_hint"] is False


@check("classify: plugin discovery over installed dists failing in mounted mode is BUILD_MODE_MISMATCH")
def _c_plugin_not_discovered():
    txt = "Exception: no link found at address: {'protocol': 'local', 'data': 'DynamicFBA'}"
    c = classify.classify(txt, rung="L3", exit_code=1)
    assert (c.failure_class, c.action, c.arg) == ("BUILD_MODE_MISMATCH", "SWITCH_INSTALL_MODE", "installed"), c
    c = classify.classify(txt, rung="L3", exit_code=1, install_mode="installed")
    assert (c.failure_class, c.action) == ("RUNTIME_ERROR", None), c
    c = classify.classify("stevedore.exception.NoMatches: No 'x.plugins' driver found", rung="L3", exit_code=1)
    assert c.failure_class == "BUILD_MODE_MISMATCH", c


@check("classify: the interpreter pick intersects pip's hint with the project's requires-python")
def _c_requires_python_intersect():
    txt = ("ERROR: Ignored the following versions that require a different python version: 9.12.0 Requires-Python >=3.12\n"
           "ERROR: Could not find a version that satisfies the requirement ipython==9.12.0 (from versions: 8.0.0)\n"
           "ERROR: No matching distribution found for ipython==9.12.0")
    assert classify.classify(txt, rung="L0").arg == "3.13"                                    # no project constraint
    assert classify.classify(txt, rung="L0", requires_python=">=3.11,<3.13").arg == "3.12"     # spatio-flux
    c = classify.classify(txt, rung="L0", requires_python="<3.12")
    assert (c.action, c.arg) == ("UNPIN_PKG", "ipython==9.12.0"), c                          # nothing fits: fall back


# ---------------------------------------------------------------- rev 4.5: what the fifth live pass (aks-rev4-5) taught

@check("evidence: undeclared third-party imports inside the repo's own packages (spatio-flux xarray)")
def _e_undeclared_imports():
    root = _repo({
        "pyproject.toml": "[project]\nname='sf'\nversion='1'\ndependencies=['numpy','opencv-python','pyyaml']\n",
        "sf/__init__.py": "", "sf/store.py": "import os\nimport numpy as np\nimport cv2\nimport yaml\nfrom sf.util import x\n\ndef w():\n    import xarray as xr\n",
        "sf/util.py": "from scripts.helper import y\nimport mpl_toolkits.mplot3d\n",
        "scripts/helper.py": "import boto3\n",                     # scripts are not scanned
        "tests/test_x.py": "import pytest\n", "sf/tests/__init__.py": "", "sf/tests/test_y.py": "import hypothesis\n",
    })
    ev = evidence.scan(root)
    assert ev["python"]["undeclared_imports"] == {"xarray": "xarray", "mpl_toolkits": "matplotlib"}, ev["python"]["undeclared_imports"]
    # a lockfile entry counts as declared
    root2 = _repo({"pyproject.toml": "[project]\nname='sf'\nversion='1'\ndependencies=[]\n",
                   "uv.lock": 'version=1\n[[package]]\nname="xarray"\nversion="2024.1.0"\n',
                   "sf/__init__.py": "import xarray\n"})
    assert evidence.scan(root2)["python"]["undeclared_imports"] == {}


@check("driver: draft_spec appends undeclared imports unpinned, deduplicated, with a finding")
def _d_undeclared_in_draft():
    ev = {"python": {"declared_deps": ["numpy"], "requires_python": None, "sources": [], "scripts": [],
                     "undeclared_imports": {"xarray": "xarray", "mpl_toolkits": "matplotlib", "matplotlib": "matplotlib"}},
          "r": {}, "conda": {}, "system_hints": {"apt": []}, "local_module_paths": {}, "local_modules": [],
          "files": ["pyproject.toml"], "language": "python", "lockfile": None}
    ann = {"language": "python"}
    s = driver.draft_spec(ev, ann, baseselect.select(ev, ann), "sha256:" + "a" * 64, "kaniko")
    assert s.pkg_specs == ["numpy", "matplotlib", "xarray"], s.pkg_specs
    assert [d for _, d in driver.draft_spec.undeclared] == ["matplotlib", "xarray"]


@check("driver: the wall-clock budget excludes time the model's example spent running at L3")
def _d_budget_excludes_l3():
    state = {"started_at": time.time() - 1500, "infra_seconds": 0.0, "l3_seconds": 900.0, "attempt": 3, "infra_retries": 0}
    assert 590 < driver.budget_elapsed(state) < 610, driver.budget_elapsed(state)
    state["l3_seconds"] = 0.0
    assert driver.budget_elapsed(state) > 1490


# ---------------------------------------------------------------- rev 4.6: what the sixth live pass (aks-rev4-6) taught

@check("classify: the example killed at the L3 deadline gets one typed extension; build/probe deadlines stay dead-letter")
def _c_l3_deadline():
    c = classify.classify("ENVBUILD_TIMEOUT: pod exceeded activeDeadlineSeconds", rung="L3", exit_code=137, l3_timeout_s=600)
    assert (c.failure_class, c.action, c.arg, c.routes_to) == ("TIMEOUT", "SET_L3_TIMEOUT", "1200", "retry"), c
    c = classify.classify("context deadline exceeded", rung="L0")
    assert (c.failure_class, c.action, c.routes_to) == ("TIMEOUT", None, "dead-letter"), c
    c = classify.classify("ENVBUILD_TIMEOUT", rung="L1", l3_timeout_s=600)          # a probe, not the example
    assert c.action is None, c


@check("spec/patch: SET_L3_TIMEOUT is a run-time field -- same image, different spec hash, must extend")
def _p_set_l3_timeout():
    s = spec()
    r = patch.apply(s, "SET_L3_TIMEOUT", "1200")
    assert r.spec.l3_timeout_s == 1200 and EnvSpec.from_dict(r.spec.to_dict()).l3_timeout_s == 1200
    assert render(s).text == render(r.spec).text                    # the image does not change
    assert s.hash() != r.spec.hash()                                # the attempt does
    assert EnvSpec.from_dict({k: v for k, v in s.to_dict().items() if k != "l3_timeout_s"}).l3_timeout_s is None  # old state
    for bad in ("900", "abc", "12", ""):
        try:
            patch.apply(r.spec if bad == "900" else s, "SET_L3_TIMEOUT", bad)
            raise AssertionError(f"accepted {bad!r}")
        except patch.PatchError:
            pass


@check("driver: l3_timeout_s honours the spec override, capped at l3_timeout_max_s")
def _d_l3_timeout_override():
    import configparser
    cfg = configparser.ConfigParser(); cfg.read_dict({"budgets": {"l3_timeout_s": "600", "l3_timeout_max_s": "1800"}})
    st = {"spec": {"l3_timeout_s": None}, "annotation": {}, "command": "python run.py"}
    assert driver.l3_timeout_s(cfg, st) == 600
    st["spec"]["l3_timeout_s"] = 1200
    assert driver.l3_timeout_s(cfg, st) == 1200
    st["spec"]["l3_timeout_s"] = 99999
    assert driver.l3_timeout_s(cfg, st) == 1800


# ---------------------------------------------------------------- rev 4.7: the annotation's runtime reaches L3

@check("annotation: compute.typical_runtime sizes every entry point's expected_runtime_s")
def _a_typical_runtime():
    assert evidence.typical_runtime_s({"value": 15, "unit": "minutes"}) == 900
    assert evidence.typical_runtime_s("2 hours") == 7200 and evidence.typical_runtime_s(900) == 900
    assert evidence.typical_runtime_s({"value": None, "unit": None}) is None and evidence.typical_runtime_s(None) is None
    root = _repo({"metadata-package/execution.yaml":
                  "execution:\n  language: python\n  compute:\n    typical_runtime: {value: 20, unit: minutes}\n"
                  "  entry_points:\n    - command: python a.py\n    - command: python b.py\n      expected_runtime_s: 30\n"})
    ann = evidence.read_annotation(root / "metadata-package")
    assert [e["expected_runtime_s"] for e in ann["entry_points"]] == [1200.0, 30.0], ann["entry_points"]
    import configparser
    cfg = configparser.ConfigParser(); cfg.read_dict({"budgets": {"l3_timeout_s": "600", "l3_timeout_max_s": "1800"}})
    st = {"spec": {}, "annotation": ann, "command": "python a.py"}
    assert driver.l3_timeout_s(cfg, st) == 1800          # 1200*1.5+60 capped at max
    st["command"] = "python b.py"
    assert driver.l3_timeout_s(cfg, st) == 105


# ---------------------------------------------------------------- report
if __name__ == "__main__":
    for line in PASSED:
        print(f"  ok  {line}")
    print(f"\n{len(PASSED)} checks passed")
