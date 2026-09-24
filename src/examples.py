"""What the repo itself says to run, and how the annotation compares.

Success for envbuild is "the built container runs the example the source repo
provides". The annotation's `entry_points` are the annotator's *reading* of
that, and the first corpus showed how often the reading is off: `R` and `R -e`
recorded as commands, per-process unit scripts recorded where the README's
headline experiment should be, an entry that only works from its own directory
recorded without a workdir. Each of those cost the builder attempts and ended
in a verdict that blamed nobody.

This module does the deterministic half of fixing that:

  discover()          -- every runnable example the repo documents or lays out,
                         with a tier guess (smoke vs full) and where it came from
  check_annotation()  -- findings about the annotation's entry points, judged
                         against the repo (not a command, file missing, placeholder
                         arguments, needs a workdir, headline example not listed)
  derive_workdir()    -- the directory an entry point must run from, when its
                         source opens files by a path relative to itself
  grounded()          -- is a proposed SET_ENTRYPOINT argument something the repo
                         actually contains

The agent may then *correct* an entry point at run time with SET_ENTRYPOINT,
but only to something `grounded()` accepts, and every correction is recorded
on the verdict as a proposal against the annotation -- the builder never edits
the annotation itself. See specs/success_criteria.md.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# A command line that runs something. Anchored to the start of a line (after
# an optional shell prompt) so prose that merely mentions `python` is skipped.
_RUN_LINE = re.compile(
    r"^\s*(?:\$\s*|>\s*|%\s*)?"
    r"((?:uv run |poetry run |pipenv run |conda run \S+ )?"
    r"(?:python3?|Rscript|julia|pytest|R CMD BATCH|make|bash|sh|java|gama-headless\S*)\b[^\n`]*)",
    re.M,
)
# Any fence language: READMEs put shell lines in ```python blocks all the time,
# and _RUN_LINE is what decides whether a line is a command.
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
# Layout scanning skips scripts that are plainly not model examples.
_INFRA_SCRIPT = re.compile(r"ec2|aws|gcp|azure|deploy|docker|push|rsync|bake|cluster|ci\b|release|"
                           r"publish|upload|download|setup|install|build[-_]?image|export|scaffold|"
                           r"lint|format|bump", re.I)
_PLACEHOLDER = re.compile(r"<[A-Za-z_][\w ]*>|\[[\w\- ]+(?:,[\w\- ]+)*\]|\{[\w]+\}")
_INSTALL_ONLY = re.compile(r"^\s*(?:uv run |pipenv run )?(?:pip3?|python3? -m pip|conda|mamba|micromamba|uv pip|"
                           r"npm|apt(?:-get)?|brew|Rscript -e ['\"]?(?:install\.packages|remotes::|devtools::)|"
                           r"R CMD INSTALL|git clone|docker|make install)\b")
_FULL_HINT = re.compile(r"reproduce|paper|figure|fig\d|all_?figures|full|benchmark|sweep|batch|train", re.I)
_SMOKE_HINT = re.compile(r"example|demo|quick|smoke|minimal|simple|tutorial|test", re.I)
_DATA_EXT = (".txt", ".csv", ".tsv", ".json", ".xml", ".sbml", ".yaml", ".yml", ".dat",
             ".npy", ".npz", ".h5", ".hdf5", ".pkl", ".rds", ".rda", ".mat", ".ini", ".toml")
# `open("dat/x.txt")`, `np.loadtxt('y0.txt')`, `read.csv("data/a.csv")`, `readRDS("m.rds")`
_REL_FILE = re.compile(r"""['"]((?![/~$]|[A-Za-z]:)[\w][\w./\-]*\.(?:%s))['"]"""
                       % "|".join(e.lstrip(".") for e in _DATA_EXT))

EXAMPLE_DIRS = ("examples", "example", "demo", "demos", "scripts", "inst/examples",
                "inst/scripts", "vignettes", "notebooks", "tutorials")
_MAX_EXAMPLES = 40


def _tier(command: str, source: str) -> str:
    if _FULL_HINT.search(command) and not _SMOKE_HINT.search(command):
        return "full"
    if source.startswith(("tests", "inst/examples", "examples", "demo")):
        return "smoke"
    return "smoke"


def _clean(cmd: str) -> str:
    cmd = cmd.strip().rstrip("\\").strip()
    cmd = re.sub(r"\s+#.*$", "", cmd)                 # trailing comment
    cmd = re.sub(r"^(?:uv run|poetry run|pipenv run)\s+", "", cmd)  # the image *is* the env
    return cmd.strip()


def _file_token(command: str) -> str | None:
    """The script a command runs, if it names one: `python a/b.py --x` -> a/b.py."""
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    for t in toks:
        if re.search(r"\.(py|R|r|jl|Rmd|sh|gaml)$", t) and not t.startswith("-"):
            return t
    return None


def _from_readme(root: Path, text: str, source: str) -> list[dict]:
    out = []
    blocks = _FENCE.findall(text) or [text]
    for block in blocks:
        for m in _RUN_LINE.finditer(block):
            cmd = _clean(m.group(1))
            if not cmd or _INSTALL_ONLY.match(cmd):
                continue
            f = _file_token(cmd)
            if f and not (root / f).exists():
                continue                              # documented but gone; not runnable
            out.append({"command": cmd, "source": source, "tier": _tier(cmd, source),
                        "file": f, "placeholders": bool(_PLACEHOLDER.search(cmd))})
    return out


def _from_layout(root: Path, files: list[str]) -> list[dict]:
    out = []
    for f in files:
        p = Path(Path(f).as_posix())
        parts = p.as_posix()
        if not any(parts.startswith(d + "/") for d in EXAMPLE_DIRS):
            continue
        if _INFRA_SCRIPT.search(p.stem):
            continue
        if p.suffix == ".py":
            if p.name.startswith(("_", "test_", "conftest")) or "__init__" in p.name:
                continue
            out.append({"command": f"python {parts}", "source": parts.split("/")[0], "tier": _tier(parts, parts),
                        "file": parts, "placeholders": False})
        elif p.suffix in (".R", ".r"):
            out.append({"command": f"Rscript {parts}", "source": parts.split("/")[0], "tier": _tier(parts, parts),
                        "file": parts, "placeholders": False})
        elif p.suffix == ".jl":
            out.append({"command": f"julia {parts}", "source": parts.split("/")[0], "tier": _tier(parts, parts),
                        "file": parts, "placeholders": False})
    return out


def _from_tests(root: Path, files: list[str], readme: str) -> list[dict]:
    out = []
    has_pytests = any(Path(f).name.startswith("test_") and f.endswith(".py") for f in files)
    if has_pytests:
        # `pytest -m 'not slow'` when the repo defines the marker; plain pytest otherwise.
        cfg = "".join(_read(root / n) for n in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"))
        cmd = "pytest -m 'not slow'" if re.search(r"\bslow\b", cfg + readme) else "pytest"
        out.append({"command": cmd, "source": "tests", "tier": "smoke", "file": None, "placeholders": False})
    if (root / "tests" / "testthat").is_dir() or (root / "tests" / "testthat.R").exists():
        out.append({"command": "Rscript -e 'testthat::test_local()'", "source": "tests", "tier": "smoke",
                    "file": None, "placeholders": False})
    return out


def _from_ci(ci: list[dict]) -> list[dict]:
    out = []
    for wf in ci or []:
        for step in wf.get("runs", []) or []:
            for m in _RUN_LINE.finditer(step):
                cmd = _clean(m.group(1))
                if cmd and not _INSTALL_ONLY.match(cmd):
                    out.append({"command": cmd, "source": f"ci:{Path(wf.get('file', '')).as_posix()}", "tier": "smoke",
                                "file": _file_token(cmd), "placeholders": False})
    return out


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, ValueError):
        return ""


def discover(root: str | Path, files: list[str], ci: list[dict] | None = None,
             candidates: list[str] | None = None) -> list[dict]:
    """Every runnable example the repo documents or lays out, deduplicated by
    command, README first (that is the author's own ranking).

    `candidates` are evidence.scan's structural entry-point guesses (`run_*.py`,
    `main.py`); they are used only when nothing documented was found, and are
    labelled `structure` so the agent knows they carry less weight.
    """
    root = Path(root)
    readme = ""
    found: list[dict] = []
    for name in sorted(files):
        if re.match(r"(?i)readme(\.(md|rst|txt))?$", Path(name).name) and len(Path(name).parts) <= 2:
            text = _read(root / name)
            readme += text
            found += _from_readme(root, text, name)
    found += _from_tests(root, files, readme)
    found += _from_layout(root, files)
    found += _from_ci(ci or [])
    if not found:
        for c in candidates or []:
            c = Path(c).as_posix()
            interp = "Rscript" if c.endswith((".R", ".r")) else "python"
            found.append({"command": f"{interp} {c}", "source": "structure", "tier": "smoke",
                          "file": c, "placeholders": False})
    seen, out = set(), []
    for ex in found:
        key = re.sub(r"\s+", " ", ex["command"])
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    # Smoke before full within each source; README order otherwise preserved.
    out.sort(key=lambda e: (0 if e["source"].lower().startswith("readme") else 1,
                            0 if e["tier"] == "smoke" else 1))
    return out[:_MAX_EXAMPLES]


def derive_workdir(root: str | Path, entry_file: str | None) -> str | None:
    """Directory (relative to the repo) an entry must run from, or None.

    Only when the script opens a file by a path that resolves relative to the
    script's own directory and NOT relative to the repo root -- that is the
    exact situation where running from `/model` fails and running from the
    script's directory works. circadian-clock's `np.loadtxt("dat/y0.txt")` is
    the corpus case.
    """
    if not entry_file:
        return None
    root = Path(root)
    script = root / entry_file
    if not script.is_file() or script.parent == root:
        return None
    text = _read(script, 100_000)
    for m in _REL_FILE.finditer(text):
        rel = m.group(1)
        if (script.parent / rel).exists() and not (root / rel).exists():
            return script.parent.relative_to(root).as_posix()
    return None


def is_command(command: str) -> bool:
    """Something a shell could run to completion -- not `R`, not `GUI: open X`,
    not a bare interpreter, not prose."""
    c = (command or "").strip()
    if not c or ":" in c.split()[0] or re.search(r"\b(GUI|open the|click|IDE|notebook)\b", c, re.I):
        return False
    if re.fullmatch(r"(R|python3?|julia|Rscript|R -e|python -c|jupyter)\s*", c):
        return False
    return True


def check_annotation(root: str | Path, ann: dict, examples: list[dict]) -> list[dict]:
    """Findings about the annotation's entry points, judged against the repo.

    Each finding is `{code, entry, detail, suggestion}`. Codes:
      not_a_command    -- `R`, `R -e`, `GUI: open …`
      file_missing     -- the script the command names is not in the repo
      placeholder_args -- `<SLUG>`, `[workflow id]`: cannot run as written
      needs_workdir    -- the script opens files relative to its own directory
      headline_missing -- the README's first example is not among the entries
      no_entry_points  -- the annotation declares none, but the repo has examples
    """
    root = Path(root)
    entries = [e.get("command") or "" for e in (ann.get("entry_points") or [])]
    findings: list[dict] = []
    if not entries and examples:
        findings.append({"code": "no_entry_points", "entry": None,
                         "detail": "annotation declares no entry points",
                         "suggestion": examples[0]["command"]})
    for cmd in entries:
        if not is_command(cmd):
            findings.append({"code": "not_a_command", "entry": cmd, "detail": "not runnable as written",
                             "suggestion": next((e["command"] for e in examples if e["tier"] == "smoke"), None)})
            continue
        f = _file_token(cmd)
        if f and not (root / f).exists():
            near = [e["command"] for e in examples if e.get("file") and Path(e["file"]).name == Path(f).name]
            findings.append({"code": "file_missing", "entry": cmd, "detail": f"{f} does not exist in the repo",
                             "suggestion": near[0] if near else None})
            continue
        if _PLACEHOLDER.search(cmd):
            concrete = [e["command"] for e in examples
                        if e.get("file") == f and not e["placeholders"]]
            findings.append({"code": "placeholder_args", "entry": cmd,
                             "detail": "contains a placeholder argument",
                             "suggestion": concrete[0] if concrete else None})
        wd = derive_workdir(root, f)
        if wd:
            findings.append({"code": "needs_workdir", "entry": cmd,
                             "detail": f"{f} opens files relative to {wd}/",
                             "suggestion": f"workdir=/model/{wd}"})
    readme_first = next((e for e in examples if e["source"].lower().startswith("readme")), None)
    if readme_first and entries:
        norm = lambda s: re.sub(r"\s+", " ", s.strip())
        if not any(norm(readme_first["command"]) in norm(c) or (readme_first.get("file") and readme_first["file"] in c)
                   for c in entries):
            findings.append({"code": "headline_missing", "entry": None,
                             "detail": "the README's first example is not among the annotation's entry points",
                             "suggestion": readme_first["command"]})
    return findings


def grounded(root: str | Path, command: str, examples: list[dict],
             code_path: str = "/model") -> str | None:
    """Why a SET_ENTRYPOINT argument is refused, or None when it is grounded:
    either a discovered example, or a command whose script exists in the repo
    with no placeholders. A script given as its in-container absolute path
    (`python /model/scripts/x.py`) is checked as `scripts/x.py` -- that is the
    natural way to write it once the code is mounted, and refusing it cost two
    jobs their budget in the first rev-4 pass."""
    root = Path(root)
    c = re.sub(r"\s+", " ", (command or "").strip())
    if not is_command(c):
        return "not a runnable command"
    if _PLACEHOLDER.search(c):
        return "contains a placeholder argument; pick concrete values the README uses"
    if any(re.sub(r"\s+", " ", e["command"]) == c for e in examples):
        return None
    f = _file_token(c)
    if f is None:
        return ("names no script in the repo and is not a documented example; "
                "SET_ENTRYPOINT must be grounded in evidence.examples or an existing file")
    rel = f
    if f.startswith(code_path.rstrip("/") + "/"):
        rel = f[len(code_path.rstrip("/")) + 1:]
    elif f.startswith("/"):
        return f"{f} is an absolute path outside {code_path}; the model is mounted at {code_path}"
    if not (root / rel).exists():
        return f"{rel} does not exist in the repo"
    return None
