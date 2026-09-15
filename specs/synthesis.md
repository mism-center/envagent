# Synthesis — evidence → EnvSpec

**You emit a structured `EnvSpec` JSON object. You never write Dockerfile text.**

This is the decision everything else depends on. As a string, a repair means
regex surgery or a full rewrite, and a rewrite has no reusable unit. As a
structured object, every typed action is a one-field mutation, rendering is
deterministic code with unit tests, and a failing build step maps back to the
field that caused it.

## Inputs

`envbuild init` prints everything you need:

- `evidence_summary` / `evidence.json` — the deterministic repo scan
- `annotation` — the human-reviewed model record (language, declared deps,
  system deps, entry points, expected inputs/outputs)
- `base_choice` — the rule table's pick, with its resolved digest
- `draft_spec` — a mechanical first spec you are expected to improve

## Output

Write an `EnvSpec` JSON file and install it:

```bash
envbuild spec --job-id <ID> --set-spec spec.json
```

Shape (all fields required except where noted):

```json
{
  "schema_version": "envspec/1",
  "install_mode": "mounted",
  "base_image": "python:3.11-slim",
  "base_digest": "sha256:...",
  "pkg_manager": "pip",
  "apt_packages": ["libxml2-dev"],
  "pkg_specs": ["numpy<2", "tellurium==2.2.10"],
  "pre_install": [],
  "post_install": [],
  "env_vars": {"MPLBACKEND": "Agg"},
  "mount": {"code_path": "/model", "input_path": "/inputs",
            "output_path": "/outputs", "workdir": "/model", "extra_path": []},
  "entrypoint": ["python", "run_sim.py"],
  "builder_version": "v0.24.0"
}
```

Leave `base_digest` out or empty and it is resolved for you. Do not invent one.

## How to fill it

**`pkg_specs`** — start from the annotation's declared dependencies; fall back to
the scanned `requirements.txt` / `pyproject.toml` when the annotation is sparse.
Keep declared order (constraint semantics depend on it). Carry the author's
version constraints across verbatim; do not tighten them, and do not add pins
that nobody asked for — an unnecessary pin is a `DEP_RESOLUTION_CONFLICT` waiting
to happen. Drop `pip`, `setuptools` and `wheel`: the bootstrap layer handles them.

**`apt_packages`** — seed from `system_hints.apt` (harvested from the repo's own
Dockerfile and CI). Prefer `-dev` packages; the image only needs to *build*
against a library that a wheel then links.

**CI is the best evidence in the repo.** If a workflow installs system packages
before running tests, those packages are *proven* necessary on a known OS. Trust
them over your priors about what a library needs.

**`install_mode`** — take `base_choice.install_mode` unless you see stronger
evidence. See `base_selection.md`.

**`env_vars`** — only what the model actually needs. `MPLBACKEND=Agg` for
anything that plots (there is no display in a verification container).
`PYTHONUNBUFFERED=1` if you want usable L3 logs. Do not set `PYTHONPATH` here —
use `mount.extra_path`, which renders into the right variable per language.

**`mount`** — `draft_spec` already seeds `extra_path` from every local package
`evidence.local_module_paths` found (root-level and `src/`-layout both), so the
common case arrives pre-fixed; leave it alone unless you have stronger
evidence. It only knows about a package directly under repo root or under
`src/` — if the entry point needs some other container directory (a `lib/`
convention, a nested namespace package, a monorepo subpackage), add it
yourself rather than waiting for the L2 failure to rediscover it.

**`entrypoint`** — record the annotation's command. It is *not* baked into the
image in mounted mode; it is supplied at run time. Recording it is what lets the
verification ladder run and lets one image serve many models.

## Do not

- Do not copy an existing Dockerfile through, in whole or in part. It is
  evidence, not a starting point.
- Do not add packages "in case". Every speculative dependency is a resolution
  risk and a bigger image, and it makes the failure histogram lie.
- Do not bake `ENTRYPOINT`, `CMD`, or the model's code into a mounted-mode image.
- Do not use a bare tag as `base_image` in a way that skips digest resolution.
