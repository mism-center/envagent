"""The verification ladder: L0 build, L1 image, L2 mount, L3 run, L4 reserved.

Mounting is what makes the ladder useful, because it splits image correctness
from code correctness:

  L0  image builds and pushes        -> build problem
  L1  deps import, IMAGE ALONE       -> image problem   (unambiguously ours to fix)
  L2  code mounts, entry's imports resolve -> mount or code problem
  L3  a short run completes and writes to output_path   -> runtime problem
  L4  output matches a reference trace -> schema field only; always null in Phase 0

The L1/L2 boundary is the point. An L1 failure is the agent's fault. An L2
failure is a mount-contract or genuine code problem, and those need different
actions -- conflating them ships a container that runs the wrong code.

It also buys the cheap re-verification path: when only the code changed, replay
L2-L3 against the existing image digest. No rebuild, no L0, no L1.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import re

from classify import MODULE_PYPI
from envspec import EnvSpec, installable_specs
from patch import req_name
from verifier import RunResult, quote_cmd

RUNGS = ("L0", "L1", "L2", "L3", "L4")

# Distributions with no importable top-level module worth probing.
_SKIP_DIST = {"pip", "setuptools", "wheel", "build", "hatchling", "poetry-core",
              "flit-core", "meson-python", "scikit-build-core", "twine", "tox"}
_PYPI_MODULE = {v.lower(): k for k, v in MODULE_PYPI.items()}
# Extra dist -> module aliases that a straight reversal of MODULE_PYPI can't
# express: several real distributions share one import module (opencv-python
# and opencv-python-headless both give "cv2"), or differ only in case
# ("ipython" installs "IPython"). Each was a real false L1 "missing" report
# against an installed, working distribution.
_PYPI_MODULE.update({
    "opencv-python": "cv2",
    "opencv-contrib-python": "cv2",
    "ipython": "IPython",
})

# Probe run through `python -c` with the payload in an env var: keeps the docker
# argv free of quoting hazards from LLM-authored package names.
_RUNNER = "import os,sys;exec(os.environ['ENVBUILD_PROBE'])"

_L1_PROBE = """
import importlib, sys
errs = []
for m in [x for x in os.environ['ENVBUILD_MODS'].split(',') if x]:
    try:
        importlib.import_module(m)
    except BaseException as e:
        errs.append('%s: %s: %s' % (m, type(e).__name__, e))
if errs:
    sys.stderr.write('\\n'.join(errs) + '\\n')
    sys.exit(1)
print('L1 ok: %s' % os.environ['ENVBUILD_MODS'])
"""

_L2_PROBE = """
import ast, importlib, pathlib, sys
p = pathlib.Path(os.environ['ENVBUILD_ENTRY'])
if not p.exists():
    sys.stderr.write('ENVBUILD_ENTRYPOINT_MISSING: %s\\n' % p)
    sys.exit(1)
try:
    tree = ast.parse(p.read_text())
except SyntaxError as e:
    sys.stderr.write('SyntaxError: %s\\n' % e)
    sys.exit(1)
mods = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        mods |= {a.name.split('.')[0] for a in n.names}
    elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
        mods.add(n.module.split('.')[0])
sys.path.insert(0, str(p.parent))
errs = []
for m in sorted(mods):
    try:
        importlib.import_module(m)
    except BaseException as e:
        errs.append('%s: %s: %s' % (m, type(e).__name__, e))
if errs:
    sys.stderr.write('\\n'.join(errs) + '\\n')
    sys.exit(1)
print('L2 ok: %d imports resolved' % len(mods))
"""

_L2_MODULE_PROBE = """
import importlib, sys
m = os.environ['ENVBUILD_ENTRY']
try:
    importlib.import_module(m)
except BaseException as e:
    sys.stderr.write('%s: %s: %s\\n' % (m, type(e).__name__, e))
    sys.exit(1)
print('L2 ok: module %s imports' % m)
"""


def entry_from_command(command) -> dict:
    """Run command -> what L2 should probe.

    `python run_sim.py --n 10` -> a file; `python -m pkg.main` -> a module;
    a bare console script -> nothing probeable, so L2 falls back to the file
    named in the annotation.
    """
    toks = quote_cmd(command or [])
    for i, t in enumerate(toks):
        if t == "-m" and i + 1 < len(toks):
            return {"kind": "module", "value": toks[i + 1]}
    for t in toks:
        if t.endswith((".py", ".R", ".r")) and not t.startswith("-"):
            return {"kind": "file", "value": t}
    return {"kind": "none", "value": ""}


@dataclass
class LadderResult:
    reached: str = "L0"                 # highest rung passed
    failed_at: str | None = None        # rung that failed, None if all passed
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    outputs: list[str] = field(default_factory=list)
    l4: None = None                     # reference-trace comparison: Phase 0 never fills this
    notes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def import_names(spec: EnvSpec) -> list[str]:
    """pkg_specs -> the names L1 should try to load.

    Probes exactly what the renderer installs, so a spec listing R base packages
    does not make L1 test something that was never installed.

    R is NOT normalised. CRAN package names are case-sensitive -- `deSolve`,
    `Matrix`, `MASS`, `Rcpp` -- and `req_name()` lowercases for PEP 503, which is
    right for PyPI and wrong here. Lowercasing turned `library(deSolve)` into
    `library(desolve)` and failed L1 for every R model against a perfectly good
    image.

    ponytail: name-mangling plus a small alias table, not a metadata lookup. A
    wrong guess costs one false L1 failure; if that shows up in the corpus
    histogram, read `top_level.txt` from the installed dist instead.
    """
    specs = installable_specs(spec.pkg_manager, spec.pkg_specs)
    if spec.pkg_manager in ("renv", "pkg"):
        names = [re.split(r"[\s(<>=!~,]", s.strip())[0] for s in specs]
        return sorted(dict.fromkeys(n for n in names if n))
    mods = []
    for s in specs:
        dist = req_name(s)
        if dist in _SKIP_DIST:
            continue
        mods.append(_PYPI_MODULE.get(dist, dist.replace("-", "_")))
    return sorted(dict.fromkeys(mods))


def _r_probe(mods: list[str]) -> list[str]:
    body = "; ".join(f'library({m})' for m in mods)
    return ["Rscript", "-e", body or "cat('L1 ok')"]


def run_l1(verifier, image_ref: str, spec: EnvSpec) -> RunResult:
    """Image alone, nothing mounted. A failure here is unambiguously ours."""
    mods = import_names(spec)
    if spec.pkg_manager in ("renv", "pkg"):
        return verifier.run(image_ref, "", _r_probe(mods), spec.mount, 180)
    env = {"ENVBUILD_PROBE": _L1_PROBE, "ENVBUILD_MODS": ",".join(mods)}
    return verifier.run(image_ref, "", ["python", "-c", _RUNNER], spec.mount, 180, env=env)


def run_l2(verifier, image_ref: str, code_ref: str, spec: EnvSpec, entry: dict) -> RunResult:
    """Code mounted. Parses the entry point and resolves its imports without
    executing it -- enough to separate a missing dependency from a broken mount,
    cheap enough to run every attempt."""
    if entry.get("kind") == "module":
        env = {"ENVBUILD_PROBE": _L2_MODULE_PROBE, "ENVBUILD_ENTRY": entry["value"]}
        return verifier.run(image_ref, code_ref, ["python", "-c", _RUNNER], spec.mount, 180, env=env)
    entry_file = entry.get("value") or ""
    path = entry_file if entry_file.startswith("/") else f"{spec.mount.code_path}/{entry_file}"
    if spec.pkg_manager in ("renv", "pkg"):
        return verifier.run(image_ref, code_ref,
                            ["Rscript", "-e", f'if (!file.exists("{path}")) '
                             f'{{cat("ENVBUILD_ENTRYPOINT_MISSING: {path}\\n"); quit(status=1)}}; '
                             f'invisible(parse("{path}"))'],
                            spec.mount, 180)
    env = {"ENVBUILD_PROBE": _L2_PROBE, "ENVBUILD_ENTRY": path}
    return verifier.run(image_ref, code_ref, ["python", "-c", _RUNNER], spec.mount, 180, env=env)


def run_l3(verifier, image_ref: str, code_ref: str, spec: EnvSpec, command,
           timeout_s: int, expect_outputs: bool = False) -> tuple[RunResult, list[str]]:
    """A short real run with the network disabled.

    `expect_outputs` gates the "did anything land in output_path" check, and it
    defaults to **off**. Plenty of legitimate entry points are console-only --
    MBMM's example script is pure `cat`/`print` -- and for those, demanding files
    makes L3 unreachable while pointing the loop at `FIX_MOUNT_CONTRACT` repairs
    that cannot possibly work. "Ran clean" and "wrote files" are two different
    claims; only assert the second when the model record actually makes it.

    Turn it on from the annotation's `entry_points[].default_output_location` --
    the schema field that means exactly "this entry writes results here". With it
    set, an exit-0 run that writes nothing is a real mount-contract failure.

    The listing is relative to `output_path`, so a record reads the same whatever
    the contract happens to be."""
    env = {"ENVBUILD_INPUTS": spec.mount.input_path,
           "ENVBUILD_OUTPUTS": spec.mount.output_path}
    res = verifier.run(image_ref, code_ref, quote_cmd(command), spec.mount,
                       timeout_s, network=False, env=env)
    listing = verifier.outputs_listing()
    if res.ok and expect_outputs and not listing:
        res = RunResult(False, res.exit_code, res.stdout,
                        res.stderr + f"\nENVBUILD_NO_OUTPUT: nothing written to "
                                     f"{spec.mount.output_path}",
                        res.duration_s)
    return res, listing


def climb(verifier, image_ref: str, code_ref: str, spec: EnvSpec, entry: dict,
          command, verify_timeout_s: int, start: str = "L1",
          expect_outputs: bool = False) -> LadderResult:
    """Walk L1 -> L2 -> L3, stopping at the first failure.

    `start="L2"` is the re-verification path: the code changed, the image did not.
    """
    result = LadderResult(reached="L0" if start == "L1" else "L1")

    if start == "L1":
        r1 = run_l1(verifier, image_ref, spec)
        result.stdout, result.stderr = r1.stdout, r1.stderr
        result.exit_code, result.timed_out = r1.exit_code, r1.timed_out
        if not r1.ok:
            result.failed_at = "L1"
            return result
        result.reached = "L1"
        result.notes["l1_modules"] = import_names(spec)

    if not (entry or {}).get("value"):
        # No declared entry point: fail fast rather than burning the budget on
        # a ladder that cannot be climbed.
        result.failed_at = "L2"
        result.stderr = "ENVBUILD_ENTRYPOINT_MISSING: annotation declares no entry point"
        result.exit_code = 1
        return result

    r2 = run_l2(verifier, image_ref, code_ref, spec, entry)
    result.stdout, result.stderr = r2.stdout, r2.stderr
    result.exit_code, result.timed_out = r2.exit_code, r2.timed_out
    if not r2.ok:
        result.failed_at = "L2"
        return result
    result.reached = "L2"

    if not command:
        result.failed_at = "L3"
        result.stderr = "ENVBUILD_ENTRYPOINT_MISSING: annotation declares no run command"
        result.exit_code = 1
        return result

    r3, listing = run_l3(verifier, image_ref, code_ref, spec, command, verify_timeout_s,
                         expect_outputs=expect_outputs)
    result.stdout, result.stderr = r3.stdout, r3.stderr
    result.exit_code, result.timed_out = r3.exit_code, r3.timed_out
    result.outputs = listing
    if not r3.ok:
        result.failed_at = "L3"
        return result
    result.reached = "L3"
    # Say plainly which L3 was proved, so nobody reads the weaker one as the
    # stronger one six months from now.
    result.notes["l3_claim"] = ("ran clean and wrote to output_path" if listing
                                else "ran clean; no declared file outputs to verify")
    # L4 stays None on purpose: reference-trace comparison is out of scope, but
    # the field exists so the record schema does not change when it lands.
    return result


def rung_index(rung: str) -> int:
    return RUNGS.index(rung) if rung in RUNGS else -1
