"""Non-LLM repo scan -> evidence.json, plus the annotation-YAML reader and the
context/code tar builder.

Everything here is deterministic: no model call, no network. The output is the
sole input to base selection and the main input to spec synthesis, so it needs
to be cheap enough to run on every job and boring enough to trust.

The CI-workflow extractor is deliberately prominent: a passing `test.yml` is a
*verified* install sequence on a known OS, which makes it the highest-value file
in most repos.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import tomllib
from pathlib import Path

import yaml

# Files whose mere presence tells us something. Kept flat so the scan is one pass.
MARKERS = [
    "README.md", "README.rst", "README.txt", "LICENSE", "CITATION.cff",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "environment.yml", "environment.yaml", "conda.yaml", "poetry.lock",
    "Dockerfile", "docker-compose.yml", "compose.yaml", "Makefile", "CMakeLists.txt",
    ".python-version", "runtime.txt", "package.json",
    "Project.toml", "Manifest.toml", "DESCRIPTION", "NAMESPACE", "renv.lock",
    "Snakefile", "nextflow.config", "manifest.xml",
]

# Never tarred: VCS metadata, caches, virtualenvs, and the result dirs that make
# a 3 MB repo into a 900 MB context.
IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".venv", "venv", "env", "node_modules", ".ipynb_checkpoints",
    "build", "dist", ".eggs", "site-packages", ".idea", ".vscode",
    "out", "outputs", "output", "results", "result", "figures", "figs",
}
IGNORE_SUFFIXES = {".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".class"}

LANG_EXT = {
    ".py": "python", ".ipynb": "python", ".pyx": "python",
    ".R": "r", ".r": "r", ".Rmd": "r",
    ".jl": "julia", ".m": "matlab", ".c": "c", ".cpp": "cpp", ".cc": "cpp",
    ".f90": "fortran", ".xml": "xml", ".sbml": "sbml", ".cellml": "cellml",
    ".bngl": "bngl", ".nml": "neuroml", ".mod": "neuron", ".hoc": "neuron",
}

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][\w.\-]*\s*(?:\[[^\]]+\])?\s*[=<>!~]*[^#;]*)")
_APT_LINE = re.compile(r"apt(?:-get)?\s+install[^\n&|;]*", re.IGNORECASE)


def _walk(root: Path):
    """Yield every non-ignored file under root, as (relative Path, size)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".git")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix in IGNORE_SUFFIXES:
                continue
            try:
                yield p.relative_to(root), p.stat().st_size
            except OSError:
                continue


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _apt_from(text: str) -> list[str]:
    """Pull package names out of any `apt-get install ...` line."""
    out = []
    for chunk in _APT_LINE.findall(text):
        for tok in chunk.split()[2:]:
            # skip flags, pinned versions, line continuations and the verb itself
            if tok.startswith("-") or "=" in tok or tok in ("install", "apt", "apt-get", "\\"):
                continue
            out.append(tok)
    return sorted(set(out))


def _parse_requirements(text: str) -> list[str]:
    deps = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_LINE.match(line)
        if m:
            deps.append(m.group(1).strip())
    return deps


def _parse_pyproject(path: Path) -> dict:
    try:
        data = tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    proj = data.get("project", {})
    poetry = data.get("tool", {}).get("poetry", {})
    deps = list(proj.get("dependencies") or [])
    for group in (proj.get("optional-dependencies") or {}).values():
        deps.extend(group)
    if not deps and poetry.get("dependencies"):
        deps = [k if v in ("*", None) else f"{k}{v}" if isinstance(v, str) else k
                for k, v in poetry["dependencies"].items() if k.lower() != "python"]
    return {
        "requires_python": proj.get("requires-python") or poetry.get("python"),
        "deps": deps,
        "build_requires": (data.get("build-system") or {}).get("requires") or [],
        "scripts": list((proj.get("scripts") or {}).keys()),
    }


def _parse_conda(path: Path) -> dict:
    try:
        data = yaml.safe_load(_read(path)) or {}
    except yaml.YAMLError:
        return {}
    deps, pip = [], []
    for d in data.get("dependencies") or []:
        if isinstance(d, dict):
            pip.extend(d.get("pip") or [])
        else:
            deps.append(str(d))
    return {"channels": data.get("channels") or [], "deps": deps, "pip": pip}


def _parse_description(text: str) -> dict:
    """R DESCRIPTION: Imports/Depends, minus the R version constraint itself."""
    fields, cur = {}, None
    for line in text.splitlines():
        if re.match(r"^\S+:", line):
            cur, _, val = line.partition(":")
            fields[cur.strip()] = val.strip()
        elif cur and line.startswith((" ", "\t")):
            fields[cur] += " " + line.strip()
    pkgs, rver, rver_raw = [], None, None
    for key in ("Depends", "Imports"):
        for item in re.split(r",\s*", fields.get(key, "")):
            item = item.strip()
            if not item:
                continue
            if item.startswith("R ") or item == "R":
                m = re.search(r"[\d.]+", item)
                rver, rver_raw = (m.group(0) if m else rver), item
            else:
                pkgs.append(re.split(r"\s*\(", item)[0])
    # Keep the raw constraint: `R (>= 4.0)` is a floor nobody tested against, not
    # a target. baseselect needs the operator to tell those apart -- the same trap
    # `requires-python` sets on the Python side.
    return {"deps": sorted(set(pkgs)), "r_version": rver, "r_version_raw": rver_raw,
            "is_package": bool(fields.get("Package"))}


def _parse_ci(root: Path) -> list[dict]:
    """Extract the install sequence a green CI run already proved works."""
    out = []
    wf_dir = root / ".github" / "workflows"
    for wf in sorted(wf_dir.glob("*.y*ml")) if wf_dir.is_dir() else []:
        try:
            data = yaml.safe_load(_read(wf)) or {}
        except yaml.YAMLError:
            continue
        runs, pyvers, images, oses = [], [], [], []
        for job in (data.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            if job.get("runs-on"):
                oses.append(str(job["runs-on"]))
            if isinstance(job.get("container"), (str, dict)):
                c = job["container"]
                images.append(c if isinstance(c, str) else c.get("image", ""))
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                if step.get("run"):
                    runs.append(str(step["run"]).strip())
                uses = str(step.get("uses") or "")
                if "setup-python" in uses:
                    v = (step.get("with") or {}).get("python-version")
                    pyvers.extend([str(v)] if not isinstance(v, list) else [str(x) for x in v])
        out.append({
            "file": str(wf.relative_to(root)), "runs": runs[:40],
            "python_versions": sorted(set(pyvers)), "containers": images,
            "runs_on": sorted(set(oses)), "apt": _apt_from("\n".join(runs)),
        })
    return out


def _local_modules(root: Path) -> list[str]:
    """Top-level importable names the repo itself defines.

    This is what stops the loop from "fixing" an IMPORT_PATH_ERROR by installing
    a same-named package off PyPI (see classify.module_not_found).
    """
    mods = set()
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.name in IGNORE_DIRS or child.name.startswith("."):
                continue
            if child.is_dir() and (child / "__init__.py").exists():
                mods.add(child.name)
            elif child.suffix == ".py" and child.stem != "setup":
                mods.add(child.stem)
    return sorted(mods)


def scan(root: str | Path, max_bytes: int = 256 * 1024 * 1024) -> dict:
    """Walk the repo once and return everything base selection and synthesis need."""
    root = Path(root).resolve()
    files, total = [], 0
    for rel, size in _walk(root):
        files.append(str(rel))
        total += size

    ev: dict = {
        "root": str(root),
        "file_count": len(files),
        "total_bytes": total,
        "oversize": total > max_bytes,
        "max_bytes": max_bytes,
        "top_level": sorted({f.split("/")[0] for f in files})[:80],
        "markers": {m: m for m in MARKERS if (root / m).exists()},
        "languages": {},
        "local_modules": _local_modules(root),
        "python": {}, "r": {}, "julia": {}, "conda": {},
        "system_hints": {"apt": [], "from_dockerfile": [], "from_ci": []},
        "ci": _parse_ci(root),
        "compiled": {},
        "entrypoint_candidates": [],
        "dockerfiles": [],
    }

    for f in files:
        lang = LANG_EXT.get(Path(f).suffix)
        if lang:
            ev["languages"][lang] = ev["languages"].get(lang, 0) + 1

    # ---- python ----
    py: dict = {"declared_deps": [], "sources": [], "requires_python": None, "scripts": []}
    for req in sorted(root.glob("requirements*.txt")) + sorted(root.glob("*/requirements*.txt")):
        py["declared_deps"].extend(_parse_requirements(_read(req)))
        py["sources"].append(str(req.relative_to(root)))
    if (root / "pyproject.toml").exists():
        pp = _parse_pyproject(root / "pyproject.toml")
        py["declared_deps"].extend(pp.get("deps", []))
        py["requires_python"] = pp.get("requires_python")
        py["scripts"] = pp.get("scripts", [])
        py["build_requires"] = pp.get("build_requires", [])
        py["sources"].append("pyproject.toml")
    if (root / ".python-version").exists():
        py["requires_python"] = py["requires_python"] or _read(root / ".python-version").strip()
    setup_py = _read(root / "setup.py") if (root / "setup.py").exists() else ""
    if setup_py:
        py["sources"].append("setup.py")
        for m in re.finditer(r"install_requires\s*=\s*\[(.*?)\]", setup_py, re.S):
            py["declared_deps"].extend(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
    py["declared_deps"] = sorted(set(py["declared_deps"]))
    ev["python"] = py

    # ---- conda / R / julia ----
    for name in ("environment.yml", "environment.yaml", "conda.yaml"):
        if (root / name).exists():
            ev["conda"] = {"file": name, **_parse_conda(root / name)}
            break
    if (root / "DESCRIPTION").exists():
        ev["r"] = _parse_description(_read(root / "DESCRIPTION"))
    if (root / "Project.toml").exists():
        try:
            proj = tomllib.loads(_read(root / "Project.toml"))
            ev["julia"] = {"deps": sorted((proj.get("deps") or {}).keys()),
                           "version": (proj.get("compat") or {}).get("julia")}
        except (tomllib.TOMLDecodeError, ValueError):
            ev["julia"] = {}

    # ---- compiled-extension evidence (drives the install_mode guess) ----
    ev["compiled"] = {
        "ext_modules": bool(re.search(r"ext_modules|Extension\(|cythonize\(", setup_py)),
        "build_requires_compiler": any(
            re.match(r"(cython|pybind11|numpy|scikit-build|meson|maturin)", d, re.I)
            for d in py.get("build_requires", [])),
        "pyx": [f for f in files if f.endswith(".pyx")][:20],
        "c_sources": [f for f in files if f.endswith((".c", ".cpp", ".cc", ".f90"))][:20],
        "makefile": (root / "Makefile").exists(),
        "cmake": (root / "CMakeLists.txt").exists(),
        "editable_hint": bool(re.search(r"pip install\s+-e", setup_py + _read(root / "README.md"))),
    }

    # ---- entrypoint candidates (structural, not documented) ----
    cands = []
    for f in files:
        p = Path(f)
        if p.suffix != ".py":
            continue
        if p.name in ("main.py", "__main__.py", "run.py") or p.name.startswith("run_"):
            cands.append(f)
        elif len(p.parts) <= 2 and "__main__" in _read(root / f, 20_000):
            cands.append(f)
    ev["entrypoint_candidates"] = sorted(set(cands))[:20]

    # ---- existing Dockerfiles: evidence only, never passed through ----
    for f in files:
        if Path(f).name.startswith("Dockerfile"):
            text = _read(root / f, 40_000)
            base = (re.search(r"^FROM\s+(\S+)", text, re.M) or [None, None])[1]
            apt = _apt_from(text)
            ev["dockerfiles"].append({"path": f, "base": base, "apt": apt})
            ev["system_hints"]["from_dockerfile"].extend(apt)
    for ci in ev["ci"]:
        ev["system_hints"]["from_ci"].extend(ci["apt"])
    ev["system_hints"]["apt"] = sorted(set(ev["system_hints"]["from_dockerfile"]
                                           + ev["system_hints"]["from_ci"]))
    return ev


# --------------------------------------------------------------------------
# Annotation YAML: the five-field subset the agent actually reads.
# Accepts either a biomodel-annotator `metadata-package/` dir or a single YAML.
# --------------------------------------------------------------------------
def _unwrap(v):
    """Annotation leaves are `{value, source, confidence}` envelopes."""
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def _dep_str(d) -> str:
    """One annotation dependency entry -> an installable requirement string.

    Entries are `{name, version_constraint, ...}`, not bare strings, and the
    constraint may be null. `deSolve` + `>=1.40` -> `deSolve>=1.40`.
    """
    d = _unwrap(d)
    if isinstance(d, str):
        return d.strip()
    if not isinstance(d, dict):
        return ""
    name = str(_unwrap(d.get("name")) or "").strip()
    con = str(_unwrap(d.get("version_constraint")) or _unwrap(d.get("version")) or "").strip()
    return f"{name}{con}" if name and con else name


def annotation_deps(ex: dict) -> tuple[list[str], list[str]]:
    """`execution.dependencies` -> (runtime requirements, system packages).

    The real biomodel-annotator schema groups them: `{runtime: [...],
    optional: [...], system: [...]}`. Iterating that mapping as a list yields the
    *group names* -- which is how `pkg_specs` ends up as ["runtime", "optional",
    "system"] and the build installs three packages that do not exist.

    `optional` is Suggests: not needed to run, so it is deliberately dropped.
    """
    deps = _unwrap(ex.get("dependencies"))
    runtime, system = [], []
    if isinstance(deps, dict):
        for group, items in deps.items():
            target = system if group == "system" else runtime
            if group == "optional":
                continue
            for item in (items or []):
                target.append(_dep_str(item))
    elif isinstance(deps, list):
        runtime = [_dep_str(d) for d in deps]
    system += [_dep_str(d) for d in (_unwrap(ex.get("system_dependencies")) or [])]
    return ([d for d in runtime if d], [d for d in system if d])


def language_version(lang) -> str | None:
    """The annotator writes `version_constraint`, not `version`."""
    if not isinstance(lang, dict):
        return None
    return _unwrap(lang.get("version_constraint")) or _unwrap(lang.get("version"))


def read_annotation(path: str | Path) -> dict:
    """-> {language, language_version, declared_deps, system_deps, entry_points,
           expected_inputs, expected_outputs}. Missing keys come back empty."""
    path = Path(path)
    docs = []
    if path.is_dir():
        for name in ("metadata.yaml", "execution.yaml"):
            if (path / name).exists():
                docs.append(yaml.safe_load(_read(path / name)) or {})
    elif path.exists():
        docs.append(yaml.safe_load(_read(path)) or {})
    merged: dict = {}
    for d in docs:
        merged.update(d if isinstance(d, dict) else {})

    ex = merged.get("execution") or {}
    io_ = merged.get("io") or {}
    runtime_deps, system_deps = annotation_deps(ex)
    entries = []
    for e in _unwrap(ex.get("entry_points")) or []:
        if not isinstance(e, dict):
            continue
        entries.append({
            "name": _unwrap(e.get("name")),
            "command": _unwrap(e.get("command")),
            "arguments": _unwrap(e.get("arguments")) or [],
            "default_output_location": _unwrap(e.get("default_output_location")),
        })
    return {
        "model_name": _unwrap((merged.get("model") or {}).get("name")),
        "language": _unwrap((ex.get("language") or {}).get("name") if isinstance(ex.get("language"), dict)
                            else ex.get("language")),
        "language_version": language_version(ex.get("language")),
        "declared_deps": runtime_deps,
        "system_deps": system_deps,
        "entry_points": entries,
        "expected_inputs": _unwrap(io_.get("inputs")) or {},
        "expected_outputs": _unwrap(io_.get("outputs")) or [],
    }


# --------------------------------------------------------------------------
def make_tar(root: str | Path, max_bytes: int = 256 * 1024 * 1024) -> bytes:
    """Tar the repo (ignore list applied) for the build context and code volume.

    Hard ceiling, because a repo carrying hundreds of MB of result data will
    otherwise silently make every attempt slow and every cache useless.
    """
    root = Path(root).resolve()
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, size in sorted(_walk(root)):
            total += size
            if total > max_bytes:
                raise ValueError(
                    f"context exceeds {max_bytes} bytes at {rel}; "
                    "extend evidence.IGNORE_DIRS or raise budgets.max_context_bytes")
            tar.add(root / rel, arcname=str(rel), recursive=False)
    return buf.getvalue()


if __name__ == "__main__":          # `python evidence.py <repo>` -> evidence.json
    import sys
    print(json.dumps(scan(sys.argv[1]), indent=2))
