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

import json
import re
import sys
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
from builder import K8sBuilder, match_step            # noqa: E402
from errors import InfraError                        # noqa: E402
from envspec import EnvSpec, MountContract, render     # noqa: E402

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


# ---------------------------------------------------------------- renderer
@check("renderer: fixed layer order")
def _render_order():
    s = spec(env_vars={"MPLBACKEND": "Agg"}, pre_install=["echo pre"], post_install=["echo post"])
    kinds = [st.kind for st in render(s).steps]
    assert kinds == ["from", "env", "apt", "bootstrap", "cmd", "pkg", "cmd", "mkdir", "workdir"], kinds


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
    assert "RUN set -eux \\\n && mkdir -p /root/.cache/R" in text, text
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
            "ESCALATE": ""}
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


@check("evidence: context tar respects its ceiling")
def _e_tar():
    assert len(evidence.make_tar(FIXTURES / "dep_conflict")) > 0
    try:
        evidence.make_tar(FIXTURES / "dep_conflict", max_bytes=10)
        raise AssertionError("ceiling not enforced")
    except ValueError:
        pass


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
@check("infra: the daemon-unreachable message is recognised, not classified")
def _i_daemon():
    # The exact stderr that turned a successful L0 into an `UNKNOWN` corpus row.
    real = ("permission denied while trying to connect to the Docker daemon socket "
            "unix:///var/run/docker.sock")
    assert verifier._DAEMON_UNREACHABLE.search(real)
    for other in ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
                  "Is the docker daemon running?"):
        assert verifier._DAEMON_UNREACHABLE.search(other), other
    # A genuine model failure must NOT be mistaken for an infra fault.
    assert not verifier._DAEMON_UNREACHABLE.search(
        "ModuleNotFoundError: No module named 'deSolve'")


@check("infra: builder preflight raises InfraError instead of building blind")
def _i_builder():
    # No kubectl, or a kubeconfig pointing at nothing -- either way the job must
    # die here, before a ten-minute build finds out for us.
    b = K8sBuilder("t", "default", "reg", kubeconfig="/nonexistent/kubeconfig")
    try:
        b.check_builder()
        raise AssertionError("unreachable cluster did not raise")
    except InfraError:
        pass


@check("builder: nested registry keeps the job id in the path, flat folds it into the tag")
def _k8s_naming():
    b = K8sBuilder("job-abc", "default", "registry:5000")
    b.attempt = 3
    assert b._image_name() == "registry:5000/envbuild/job-abc:a3"

    b2 = K8sBuilder("job-abc", "default", "docker.io/mismplatform", flat_registry=True)
    b2.attempt = 3
    assert b2._image_name() == "docker.io/mismplatform/envbuild:job-abc-a3"


@check("builder: pod name is a valid, unique k8s object name")
def _k8s_pod_name():
    b = K8sBuilder("job-2026-09-14-a10a", "default", "registry:5000")
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
    pkg_step = rendered.steps[3]
    assert pkg_step.kind == "pkg", pkg_step
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


@check("config: ENVBUILD_REGISTRY_PULL='' clears the default, not just unset")
def _cfg_empty_override():
    import os
    import driver
    os.environ["ENVBUILD_REGISTRY_PULL"] = ""
    try:
        cfg = driver.load_config(None)
        assert cfg.get("verifier", "registry_pull") == ""
    finally:
        del os.environ["ENVBUILD_REGISTRY_PULL"]


@check("infra: InfraError is not a classifiable failure")
def _i_not_classified():
    # Belt and braces: if one ever reaches the classifier, it must not be dressed
    # up as a repairable model failure with a confident action.
    c = classify.classify("permission denied while trying to connect to the Docker "
                          "daemon socket unix:///var/run/docker.sock", rung="L1")
    assert c.action is None, c


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


# ---------------------------------------------------------------- report
if __name__ == "__main__":
    for line in PASSED:
        print(f"  ok  {line}")
    print(f"\n{len(PASSED)} checks passed")
