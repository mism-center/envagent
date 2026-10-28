# Base image selection

A rule table, not a classifier. At this stage a table you can read beats a model
you cannot debug, and every row is something you can point at when a base choice
turns out wrong. Implemented in `src/baseselect.py`; this file is the rationale
and the override guidance.

```
conda env file (environment.yml)  → mambaorg/micromamba:1.5   (mamba)
R / DESCRIPTION / *.R             → rocker/r-ver:<DESCRIPTION R version | 4.4.1>
julia / Project.toml              → julia:1.10                (pkg)
python evidence                   → python:<version>-slim      (pip)
unknown                           → ubuntu:24.04
```

Conda beats plain python on purpose: an `environment.yml` means the author
already fought the solver and won, and re-fighting it with pip usually loses.

## Interpreter version, in priority order

1. **A green CI workflow** (`.github/workflows/*.yml`, `setup-python`). This is
   the highest-value signal in most repos: a passing `test.yml` is a *verified*
   install sequence on a known OS, not an aspiration.
2. The annotation's declared `language_version`.
3. `requires-python` / `.python-version` — but only when it looks like a pin.
   `>=3.9` is a floor nobody tested against, not a target.
4. `3.11`.

## Digest pinning is not optional

`baseselect.resolve_digest()` resolves the tag once, at synthesis time, and both
the tag and the digest are stored. `EnvSpec.base_image` must never reach the
rendered Dockerfile as a bare tag — otherwise a model that verified in March
silently stops verifying in June and "verified" becomes a lie. Resolution failure
is a job failure, not a reason to fall back to a tag.

## `install_mode`

`mounted` is the default and the point: the image is a **dependency stack**, the
model's code arrives at run time, and many models can share one image. No
`ENTRYPOINT` is baked in mounted mode — the entrypoint belongs to the model
record.

`installed` is the escape hatch for repos that genuinely entangle installation
and source. Evidence that flips the guess:

- `ext_modules` / `Extension(` / `cythonize(` in `setup.py`
- `.pyx` sources, or a build backend requiring cython/pybind11/meson/maturin
- a `Makefile` or `CMakeLists.txt`
- docs that call for `pip install -e .`
- a README whose install line installs the *project* (`pip install .`,
  `uv sync` / `uv run`, `poetry install`) alongside a `pyproject.toml` or
  `setup.py`. Frameworks that discover plugins over installed distributions
  (process-bigraph's `allocate_core()`, pluggy, stevedore) find nothing when
  the package is merely on `PYTHONPATH` — spatio-flux failed four passes at L3
  with `no link found at address` for exactly this reason.

Forcing such a repo into mounted mode produces weird failures in the tail, so
when the guess is wrong, `SWITCH_INSTALL_MODE` is a legitimate typed action —
not a workaround.

## Lockfiles

A committed `uv.lock`, `poetry.lock`, `Pipfile.lock` or fully `==`-pinned
`requirements.txt` is the author's own resolution and outranks every other
dependency signal. `draft_spec` pins each declared dependency to its locked
version (extras kept, other constraints replaced; an explicit `==` in the
annotation is left alone) and `init` reports `lockfile_honoured`. Direct
dependencies only: pip resolves the transitive closure against the pins, so the
spec stays readable and every pin is attributable. spatio-flux locked
`process-bigraph 1.4.12`; three passes resolved `1.8.4` and died on the API
drift at L3. `renv.lock` is reported but not yet applied — the R renderer would
need `remotes::install_version`.

## Existing Dockerfiles

Feed them as **evidence only** (`evidence.dockerfiles`, `system_hints.apt`).
Never pass one through. A repo's Dockerfile anchors synthesis to whatever
rotten choices its author made years ago; a fresh `EnvSpec` is required. Revisit
this once there is a corpus large enough to measure whether it costs anything.
