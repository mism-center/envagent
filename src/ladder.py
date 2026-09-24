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

import hashlib
import re
import time

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

# L1 asks the image, not a table, what each distribution installs. The first
# corpus run guessed module names from distribution names (`vivarium-core` ->
# `vivarium_core`, but the module is `vivarium`; `ipython` -> `ipython`, but it
# is `IPython`) and every wrong guess was a false L1 failure against a working
# image -- which the agent then "repaired" by writing shim modules into
# site-packages so the probe would pass. Reading `importlib.metadata` inside the
# container removes both the guess and the incentive.
#
# Distribution not installed        -> PackageNotFoundError (MISSING_DEPENDENCY)
# Installed, a top-level import fails -> the real exception, attributed to the dist
# All pass                          -> a lockfile of every installed dist on stdout,
#                                      which is what "verified" then refers to.
_L1_PROBE = """
import importlib, importlib.metadata as md, re, sys
def norm(n): return re.sub(r'[-_.]+', '-', n).lower()
guess = dict(x.split('=', 1) for x in os.environ.get('ENVBUILD_MODS', '').split(',') if '=' in x)
try:
    inv = {}
    for mod, dists in md.packages_distributions().items():
        for d in dists: inv.setdefault(norm(d), set()).add(mod)
except Exception:
    inv = {}
def top_level(dist):
    mods = set(inv.get(norm(dist.metadata['Name']), ()))
    txt = dist.read_text('top_level.txt')
    if txt: mods |= set(txt.split())
    if not mods:
        for f in dist.files or []:
            p = f.parts
            if len(p) > 1 and p[1] == '__init__.py': mods.add(p[0])
            elif len(p) == 1 and p[0].endswith('.py'): mods.add(p[0][:-3])
    return sorted(m for m in mods if m and not m.startswith('_') and m not in ('tests', 'test'))
errs = []
for dist_name in [x for x in os.environ['ENVBUILD_DISTS'].split(',') if x]:
    try:
        dist = md.distribution(dist_name)
    except md.PackageNotFoundError:
        errs.append('%s: PackageNotFoundError: distribution %r is not installed' % (dist_name, dist_name))
        continue
    mods = top_level(dist) or ([guess[dist_name]] if dist_name in guess else [])
    for m in mods:
        try:
            importlib.import_module(m)
        except BaseException as e:
            errs.append('%s (from %s): %s: %s' % (m, dist_name, type(e).__name__, e))
if errs:
    sys.stderr.write('\\n'.join(errs) + '\\n')
    sys.exit(1)
lock = sorted('%s==%s' % (norm(d.metadata['Name']), d.version) for d in md.distributions() if d.metadata['Name'])
print('L1 ok: %d distributions probed' % len(os.environ['ENVBUILD_DISTS'].split(',')))
print('ENVBUILD_LOCK_BEGIN pip'); print('\\n'.join(lock)); print('ENVBUILD_LOCK_END')
"""

# R equivalent: library() each requested package, then dump installed.packages()
# as the lockfile. Base packages are excluded from the lock -- they are the R
# version, which the base digest already pins.
_R_L1_TAIL = ('; ip <- installed.packages(); ip <- ip[is.na(ip[,"Priority"]) | ip[,"Priority"] != "base", , drop=FALSE]'
              '; cat("ENVBUILD_LOCK_BEGIN cran\\n"); cat(paste0(ip[,"Package"], "==", ip[,"Version"]), sep="\\n")'
              '; cat("\\nENVBUILD_LOCK_END\\n")')

_LOCK_BLOCK = re.compile(r"ENVBUILD_LOCK_BEGIN (\w+)\n(.*?)\nENVBUILD_LOCK_END", re.S)


def parse_lockfile(stdout: str) -> dict | None:
    """The L1 probe's lock block -> {"format", "entries", "sha256"} or None."""
    m = _LOCK_BLOCK.search(stdout or "")
    if not m:
        return None
    entries = sorted(ln.strip() for ln in m.group(2).splitlines() if ln.strip())
    body = "\n".join(entries)
    return {"format": m.group(1), "entries": entries,
            "sha256": "sha256:" + hashlib.sha256(body.encode()).hexdigest()}

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


SCRATCH = "/scratch"

# $0 code mount (ro)  $1 copy root  $2 workdir inside the copy  $3 output_path
# then the command. POSIX sh only: python:*-slim, rocker and micromamba all
# have it; `find -exec sh -c` keeps paths with spaces intact. The model's own
# exit code is what the pod exits with.
_WRITABLE_COPY_SH = (
    'set -e; mkdir -p "$1"; cp -a "$0"/. "$1"/; '
    'm=/tmp/.envbuild_l3_start; : > "$m"; sleep 1; '
    'cd "$2"; src="$0"; copy="$1"; out="$3"; shift 3; '
    'set +e; "$@"; rc=$?; set -e; '
    'cd "$copy" && find . -type f -newer "$m" ! -name "*.pyc" ! -path "*/__pycache__/*" '
    '! -path "./.git/*" ! -path "*/.pytest_cache/*" '
    '-exec sh -c \'mkdir -p "$0/$(dirname "$1")" && cp -p "$1" "$0/$1"\' "$out" {} \\; ; '
    'exit $rc'
)


def resolve_command(command, mount, anchor: str | None = None) -> list[str]:
    """The command as L3 executes it: a relative script path is anchored to
    `code_path`, so the command means the same thing whatever `workdir` is.

    Entry points are written relative to the repo root (`python
    chemotaxis/processes/x.py`). The moment a repair or `init` moves
    `mount.workdir` -- to the output volume because the model writes cwd-relative
    files, or to the script's own directory because it reads cwd-relative
    data -- that relative path stops resolving and L3 dies with "can't open
    file '/outputs/chemotaxis/...'". Three of seven models in the first rev-4
    pass failed exactly there. Anchoring the script token here means the two
    legitimate repairs (`FIX_MOUNT_CONTRACT workdir=...`) stay legitimate.
    Arguments are left alone: they may be paths relative to the workdir on
    purpose.
    """
    toks = quote_cmd(command or [])
    if mount is None or mount.workdir == mount.code_path:
        return toks
    anchor = anchor or mount.code_path
    out, anchored = [], False
    for i, t in enumerate(toks):
        if (i > 0 and not anchored and t.endswith((".py", ".R", ".r", ".jl", ".sh"))
                and not t.startswith(("-", "/")) and "=" not in t
                and out[-1] not in ("-m", "-c", "-e")):
            out.append(f"{anchor}/{t}")
            anchored = True                      # only the script itself, never a .py argument
            continue
        out.append(t)
    return out


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


# Fallback only, for callers that construct a rung directly (tests). Every path
# through `climb` passes `budgets.verify_timeout_s` instead. L1 and L2 used to
# hardcode this number, which meant the config knob governed L3 alone: raising
# `verify_timeout_s` to fix an L1 timeout changed nothing, and there was no
# indication why. Seen on the AKS pass, where mbmm's L1 probe hit the hidden
# 180s ceiling on an 857 MB R image.
_DEFAULT_VERIFY_TIMEOUT_S = 180


def dist_names(spec: EnvSpec) -> list[str]:
    """pkg_specs -> normalised distribution names L1 should look up in the image."""
    specs = installable_specs(spec.pkg_manager, spec.pkg_specs)
    names = [req_name(s) for s in specs]
    return sorted(dict.fromkeys(n for n in names if n and n not in _SKIP_DIST))


def _r_probe(mods: list[str]) -> list[str]:
    body = "; ".join(f'library({m})' for m in mods)
    return ["Rscript", "-e", (body or "cat('L1 ok')") + _R_L1_TAIL]


def run_l1(verifier, image_ref: str, spec: EnvSpec,
           timeout_s: int = _DEFAULT_VERIFY_TIMEOUT_S) -> RunResult:
    """Image alone, nothing mounted. A failure here is unambiguously ours.

    Python: the probe receives *distribution* names and resolves their modules
    from the image's own metadata. `ENVBUILD_MODS` carries the table's guess as
    `dist=module` pairs, used only for a dist whose metadata names no module.
    """
    if spec.pkg_manager in ("renv", "pkg"):
        return verifier.run(image_ref, "", _r_probe(import_names(spec)), spec.mount, timeout_s)
    dists = dist_names(spec)
    guesses = ",".join(f"{d}={_PYPI_MODULE.get(d, d.replace('-', '_'))}" for d in dists)
    env = {"ENVBUILD_PROBE": _L1_PROBE, "ENVBUILD_DISTS": ",".join(dists), "ENVBUILD_MODS": guesses}
    return verifier.run(image_ref, "", ["python", "-c", _RUNNER], spec.mount, timeout_s, env=env)


def run_l2(verifier, image_ref: str, code_ref: str, spec: EnvSpec, entry: dict,
           timeout_s: int = _DEFAULT_VERIFY_TIMEOUT_S) -> RunResult:
    """Code mounted. Parses the entry point and resolves its imports without
    executing it -- enough to separate a missing dependency from a broken mount,
    cheap enough to run every attempt."""
    if entry.get("kind") == "module":
        env = {"ENVBUILD_PROBE": _L2_MODULE_PROBE, "ENVBUILD_ENTRY": entry["value"]}
        return verifier.run(image_ref, code_ref, ["python", "-c", _RUNNER], spec.mount,
                            timeout_s, env=env)
    entry_file = entry.get("value") or ""
    path = entry_file if entry_file.startswith("/") else f"{spec.mount.code_path}/{entry_file}"
    if spec.pkg_manager in ("renv", "pkg"):
        return verifier.run(image_ref, code_ref,
                            ["Rscript", "-e", f'if (!file.exists("{path}")) '
                             f'{{cat("ENVBUILD_ENTRYPOINT_MISSING: {path}\\n"); quit(status=1)}}; '
                             f'invisible(parse("{path}"))'],
                            spec.mount, timeout_s)
    env = {"ENVBUILD_PROBE": _L2_PROBE, "ENVBUILD_ENTRY": path}
    return verifier.run(image_ref, code_ref, ["python", "-c", _RUNNER], spec.mount,
                        timeout_s, env=env)


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
    m = spec.mount
    if m.writable_copy:
        # Copy the read-only mount into the container's own filesystem and run
        # from the corresponding directory there. cp(1) is in every base we
        # ship; the copy is per-pod and dies with it. Afterwards every file the
        # run created or modified inside the copy is carried to output_path
        # (relative layout preserved, caches skipped), so the output contract
        # holds and the harness -- not just the model -- can see what it wrote.
        # aks-rev4-7: spatio-flux reproduced 19 studies and was failed for
        # "no output" because this step was missing.
        copy_root = SCRATCH + m.code_path
        wd = copy_root + m.workdir[len(m.code_path):] if m.workdir.startswith(m.code_path) else m.workdir
        inner = resolve_command(command, m, anchor=copy_root)
        cmd = ["sh", "-c", _WRITABLE_COPY_SH, m.code_path, copy_root, wd, m.output_path, *inner]
    else:
        cmd = resolve_command(command, m)
    res = verifier.run(image_ref, code_ref, cmd, m, timeout_s, network=False, env=env)
    listing = verifier.outputs_listing()
    if res.ok and expect_outputs and not listing:
        res = RunResult(False, res.exit_code, res.stdout,
                        res.stderr + f"\nENVBUILD_NO_OUTPUT: nothing written to "
                                     f"{spec.mount.output_path}",
                        res.duration_s)
    return res, listing


def climb(verifier, image_ref: str, code_ref: str, spec: EnvSpec, entry: dict,
          command, verify_timeout_s: int, start: str = "L1",
          expect_outputs: bool = False, l3_timeout_s: int | None = None) -> LadderResult:
    """Walk L1 -> L2 -> L3, stopping at the first failure.

    `start="L2"` is the re-verification path: the code changed, the image did not.
    `l3_timeout_s` sizes the example run separately from the L1/L2 probes; it
    defaults to `verify_timeout_s` and is recorded in `notes["l3_timeout_s"]`.
    """
    l3_timeout_s = l3_timeout_s or verify_timeout_s
    result = LadderResult(reached="L0" if start == "L1" else "L1")

    if start == "L1":
        r1 = run_l1(verifier, image_ref, spec, verify_timeout_s)
        result.stdout, result.stderr = r1.stdout, r1.stderr
        result.exit_code, result.timed_out = r1.exit_code, r1.timed_out
        if not r1.ok:
            result.failed_at = "L1"
            return result
        result.reached = "L1"
        result.notes["l1_dists"] = dist_names(spec) if spec.pkg_manager not in ("renv", "pkg") \
            else import_names(spec)
        # What was actually installed, from inside the image. This is what a
        # `verified` verdict refers to; the spec alone only pins the base.
        # K8sVerifier returns the pod's merged log stream as `stderr` and leaves
        # `stdout` empty (Kubernetes does not separate the two); a local
        # verifier fills `stdout`. Look in both -- the smoke run under rev 3
        # verified L3 with `lockfile_sha256: null` for exactly this reason.
        result.notes["lockfile"] = parse_lockfile(r1.stdout) or parse_lockfile(r1.stderr)

    if not (entry or {}).get("value"):
        # No declared entry point: fail fast rather than burning the budget on
        # a ladder that cannot be climbed.
        result.failed_at = "L2"
        result.stderr = "ENVBUILD_ENTRYPOINT_MISSING: annotation declares no entry point"
        result.exit_code = 1
        return result

    r2 = run_l2(verifier, image_ref, code_ref, spec, entry, verify_timeout_s)
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

    result.notes["l3_timeout_s"] = l3_timeout_s
    t3 = time.time()
    r3, listing = run_l3(verifier, image_ref, code_ref, spec, command, l3_timeout_s,
                         expect_outputs=expect_outputs)
    # The model's own run time is the annotation's business, not the search's.
    # The driver takes it out of the wall-clock budget (see budget_elapsed).
    result.notes["l3_seconds"] = round(time.time() - t3, 2)
    result.stdout, result.stderr = r3.stdout, r3.stderr
    result.exit_code, result.timed_out = r3.exit_code, r3.timed_out
    result.outputs = listing
    if not r3.ok:
        result.failed_at = "L3"
        return result
    result.reached = "L3"
    # Say plainly which L3 was proved, so nobody reads the weaker one as the
    # stronger one six months from now.
    result.notes["l3_claim"] = ("ran clean and wrote to output_path"
                                + (" (harvested from the writable copy)" if spec.mount.writable_copy else "")
                                if listing else "ran clean; no declared file outputs to verify")
    # L4 stays None on purpose: reference-trace comparison is out of scope, but
    # the field exists so the record schema does not change when it lands.
    return result


def rung_index(rung: str) -> int:
    return RUNGS.index(rung) if rung in RUNGS else -1
