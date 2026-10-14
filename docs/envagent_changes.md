# envagent rev 3 — implementation of the review findings

All changes are in `C:\Users\kebedey\projects\mism-center\envagent`. Offline suite:
`uv run scripts/test_envbuild.py` → **107 checks pass** (79 pre-existing + 28 new).
Nothing here needs a cluster; nothing in `deploy/envbuild.yaml` (RBAC, NetworkPolicy)
changed. The Kaniko pin is unchanged.

## Effect on the historical corpus (offline replay)

`uv run scripts/reclassify.py outputs/attempts.jsonl` over the 176 existing rows:

| class | recorded | re-derived |
|---|---:|---:|
| `INFRA_UNAVAILABLE` (new, uncharged) | 0 | 51 |
| `UNKNOWN` (→ LLM) | **111** | **11** |
| `MISSING_SYSTEM_LIB` | 0 | 19 |
| `MISSING_DEPENDENCY` | 13 | 21 |
| `ABI_MISMATCH` | 4 | 10 |
| `DEP_RESOLUTION_CONFLICT` | 3 | 10 |
| `BASE_IMAGE_MISMATCH` (new) | 0 | 8 |
| `SPEC_INVALID` (new) | 0 | 2 |
| `RUNTIME_ERROR` / `ENTRYPOINT_UNKNOWN` / `TIMEOUT` / `IMPORT_PATH_ERROR` / `MOUNT_CONTRACT_ERROR` | unchanged | unchanged |

The 11 residual `UNKNOWN` rows are BuildKit-era rows whose real message was cut off
*before* the 8 KB window was taken; they are unrecoverable historically and cannot
recur, because the builder now decodes before truncating.

## Files

### New
- `src/logs.py` — `decode()` (ANSI strip, BuildKit rawjson unwrap incl. base64 `data`
  payloads and truncated frames, Kaniko `INFO[t]` prefix strip, duplicate-line collapse;
  idempotent) and `tail()` (decode *then* cut).
- `scripts/reclassify.py` — replays the current rule table over historical rows; prints
  recorded→re-derived histogram, rule hits, signature counts; `-o` writes a copy with
  `*_v2` fields (never in place); `--show CLASS` dumps salient lines.

### `src/classify.py`
- `classify()` decodes its input; handlers receive the full text in `ctx["text"]`.
- New classes in `ROUTING`: `INFRA_UNAVAILABLE` → `"infra"`, `SPEC_INVALID` → retry,
  `BASE_IMAGE_MISMATCH` → retry.
- New rules, in priority order: `infra_network`, `infra_scheduling`, `spec_invalid`,
  `missing_soname` (soname→apt via new `SONAME_APT` table, 50 entries; model-owned
  `.cpython-*.so` → `BUILD_MODE_MISMATCH`), `numpy2_api` (→ `PIN_PKG numpy<2`),
  `r_install_failed`, `r_not_available`, `r_dependency`, `no_interpreter`,
  `dist_not_installed` (new L1 probe message).
- `no_matching_dist` now returns `INFRA_UNAVAILABLE` when the text contains
  `(from versions: none)` — the false `UNPIN_PKG` from the corpus.
- Removed `\.so: cannot open shared object file` from `build_mode` (never matched the
  real `.so.1:` text; wrong repair if it had).

### `src/envspec.py`
- Every rendered `RUN` opens with `echo '::envbuild::step=N kind=K field=F'`;
  `find_step_marker(logs)` returns the last one. `STEP_MARKER`, `_STEP_MARKER_RE`.
- `BOOTSTRAP_PINS = ("pip==24.0", "setuptools==69.5.1", "wheel==0.43.0")` replaces the
  unpinned upgrade; `--no-cache-dir`.
- `_HYGIENE_ENV` per manager (`PIP_NO_CACHE_DIR=1`, `PIP_DISABLE_PIP_VERSION_CHECK=1`,
  `PYTHONDONTWRITEBYTECODE=1`), merged *under* the spec's `env_vars`.
- `render(spec, labels=None)` emits a final `LABEL` step (`io.envbuild.*`: schema,
  base@digest, install_mode, pkg_manager, plus caller labels). Last on purpose; not in
  the spec hash.

### `src/builder.py`
- `match_kaniko_step()` prefers the step marker, falls back to the instruction echo;
  `_norm()` strips a marker prefix so either echo form compares equal.
- `build()` passes labels (job, attempt, envspec hash, model id, code revision, run id)
  and **decodes logs before truncating** in both failure paths.
- `K8sBuilder(cache_repo=, labels=)`; `--cache=true --cache-repo=… --cache-copy-layers=false`
  when configured, `--cache=false` otherwise.

### `src/ladder.py`
- `_L1_PROBE` rewritten: takes `ENVBUILD_DISTS` (distribution names), resolves top-level
  modules from `importlib.metadata.packages_distributions()` / `top_level.txt` / file
  layout inside the image; the alias table (`ENVBUILD_MODS` as `dist=module`) is a
  fallback only. Not installed → `PackageNotFoundError: distribution 'x' is not installed`.
  On success prints `ENVBUILD_LOCK_BEGIN pip … ENVBUILD_LOCK_END` (every installed dist,
  `name==version`, sorted).
- R probe appends `installed.packages()` as `ENVBUILD_LOCK_BEGIN cran …` (base excluded).
- `dist_names(spec)`, `parse_lockfile(stdout)`; `climb()` stores `notes["lockfile"]`
  `{format, entries, sha256}` and `notes["l1_dists"]`.

### `src/patch.py`
- `forbidden_pre_install(cmd)` + `_PRE_INSTALL_FORBIDDEN`: refuses writes to
  `site-packages`/`getsitepackages`/R libraries, `/etc/hosts`/`resolv.conf`, shell package
  installs (`pip install`, `install.packages(`, `micromamba install`, `R CMD INSTALL`),
  interpreter re-linking, and anything touching `ENVBUILD_`/`::envbuild::`. Enforced in
  `apply()` and again in the driver with a message naming the typed action that owns the fix.

### `src/driver.py`
- Config: `[builder] cache_repo`, `[budgets] max_infra_retries=2`, `[run]` section;
  env overrides `ENVBUILD_CACHE_REPO`, `ENVBUILD_RUN_ID`, `ENVBUILD_HARNESS_IMAGE`,
  `AI_MODEL`, `AI_PROVIDER`. `run_provenance(cfg)` stamped on every attempt and verdict.
- `init` validates `--model-id` against `<scheme>:<path>` (the `gpt-5.6-luna` rows).
- `attempt`: infra plane — row written with `charged: false`, `state.infra_retries` /
  `infra_seconds` credited back to both budgets (`charged_attempts()`,
  `budget_elapsed()`), `patch` refused next, agent told to re-run the same spec; after
  `max_infra_retries` the driver writes an `error` verdict itself (exit 4). Per-attempt
  deadline `attempt_deadline_s()` = build + 3·verify + 180 s; an over-deadline
  UNKNOWN/INFRA row is recorded as `TIMEOUT`. Lockfile written to
  `jobs/<job>/lockfile.a<N>.txt`, `lockfile_sha256`/`lockfile_format` on the row and in
  `best`; `classifier_rule` on the row; `error_raw`/`stderr_tail` from decoded text.
- `patch`: refuses after an infra attempt; refuses forbidden `ADD_PRE_INSTALL_CMD`.
- Verdict row: `lockfile_sha256`, `infra_retries`, `charged_attempts`, `run`.

### `src/record.py`
- `ATTEMPT_REQUIRED` += `run, charged, lockfile_sha256`;
  `VERDICT_REQUIRED` += `run, charged_attempts, lockfile_sha256`.

### `src/verifier.py`
- `_models_sub()` returns `as_posix()` — the pre-existing Windows-host bug that failed
  the baseline suite (`subPath: mbmm\1.0`).

### Specs / docs / deploy
- `specs/failure_taxonomy.md`: three rows added, "Three planes" section, `infra retry`
  routing meaning. `specs/remediation_actions.md`: no-patch-after-infra, pre-install
  refusal list. `specs/repair.md`: step 0 "check the plane".
  `specs/record_schema.md`: new fields documented; approval unit is now the triple
  *(image_digest, lockfile_sha256, code_revision)*.
- `config.ini`: `cache_repo`, `max_infra_retries`, `[run]`. `deploy/agent-job.yaml`:
  `ENVBUILD_RUN_ID`, `ENVBUILD_HARNESS_IMAGE`. `run.sh`: `__RUN_ID__` substitution
  (`ENVBUILD_RUN_ID` once per benchmark pass). `harness/entrypoint.sh`: exports resolved
  `AI_MODEL`/`AI_PROVIDER` so rows carry them.
- `CLAUDE.md`: consistency table extended; "Log bytes go through `src/logs.py`" and
  "Three failure planes" sections. `SKILL.md`: three new non-negotiable rules.
  `README.md`: infra retry, markers, lockfile, budgets sentence.

### Tests (`scripts/test_envbuild.py`)
- 3 assertions updated for the new contracts (layer order gains `label`; marker prefix;
  pkg step located by kind).
- 28 new checks: `logs.*` (rawjson, truncated frame, Kaniko, tail-after-decode),
  infra classification incl. the five corpus shapes and rawjson-wrapped, `from versions:
  none`, soname map (+unmapped, +model-owned `.so`), numpy-2, R install/dependency,
  no-interpreter, `SPEC_INVALID`, `dist_not_installed`, ROUTING↔taxonomy agreement,
  markers on every RUN, marker-based attribution (plain and rawjson), hygiene/pins/labels,
  env override, `dist_names`/L1 env contract, **the L1 probe executed in-process**
  against this interpreter (missing dist → error; installed dist → lockfile block
  parsed), lockfile in `climb()` notes, pre-install guard (6 verbatim corpus commands
  refused, 4 legitimate ones accepted), budget crediting, deadline derivation, model-id
  shape, provenance env, Kaniko cache flag, record required keys.

## Not done here (needs a design decision or a cluster)
- **`USER` non-root in the image**: interacts with PVC ownership and R library paths;
  needs a cluster test before it goes in the renderer.
- **Kaniko cache on by default**: knob exists (`cache_repo`), default off until one
  benchmark pass confirms cache hits do not change verdicts.
- **Stack-image catalogue and multi-runtime `EnvSpec`** (review §4.3, §4.6): schema
  change; `envspec/2`.
- **Annotation `smoke_command` / `data_files[]`** (review §4.7): lives in
  `biomodel-annotator`'s schema.
- **Fixtures nightly in-cluster; 3 repeats per model; token accounting from Pi's JSON
  stream**: benchmark-repo work.

## Suggested first cluster run
One pass over the existing 7 annotations with `ENVBUILD_RUN_ID=aks-rev3-1`, then:
`uv run scripts/reclassify.py /work/records/attempts.jsonl` and compare
`failed_step_index` coverage (expect ≈100 % of L0 failures), `charged: false` count,
`lockfile_sha256` non-null on every L1 pass, and residual `UNKNOWN`.


---

# rev 4 — success is the repo's example; the builder may correct the annotation

Offline suite: **121 checks pass** (107 after rev 3 + 14 new). Baseline scored with the
new tool over all historical rows: **build success 5.0 % (3/60 buildable trials, 95 % CI
2–14 %)**, verdict correctness 11.1 %, false-verified 0, LLM-fallback share 69 %,
`failed_step_index` coverage 10.5 %.

## New
- `src/examples.py` — `discover()` (README run lines in any fence, `pytest`/`testthat`,
  `examples/`/`inst/examples/`/`demo/`/`scripts/` minus infra scripts, CI steps, structural
  fallback; smoke/full tier), `check_annotation()` (`not_a_command`, `file_missing`,
  `placeholder_args`, `needs_workdir`, `headline_missing`, `no_entry_points`),
  `derive_workdir()` (fires only for script-relative data paths), `grounded()`.
- `specs/success_criteria.md` — the definition, the entry-selection order, correction
  semantics, timing, and what the benchmark reports.
- `bench/ground_truth.yaml` — human-curated `example`/`timeout_s`/`workdir`/`buildable`/
  `expected_verdict` for the 7 models; `human_verified: null` everywhere until someone
  runs each example once outside the loop.
- `scripts/score.py` — per-trial build success (+ Wilson CI), verdict correctness,
  false-verified (lockfile, digest, shim in chain), attribution
  (builder/annotation/upstream/infra/budget/unattributed), "succeeded after correcting the
  annotation", LLM-fallback share, step-attribution coverage; `--run-id`, `--json`.

## Changed
- `src/evidence.py` — `scan()` emits `examples[]`.
- `src/driver.py` — `init`: `check_annotation` findings, `choose_entry()` (annotation's first
  usable entry → `annotation`; else repo smoke example → `repo_example` + correction;
  else `none`), workdir derived and applied as a recorded correction; state carries
  `entrypoint_source`, `annotation_findings`, `annotation_corrections`, `examples`.
  `patch`: `SET_ENTRYPOINT` grounded via `examples.grounded`, capped at 2/job, updates
  `command`/`entry`, re-derives workdir, records the correction. `attempt`: rows carry
  `entrypoint_used`, `entrypoint_source`, `l3_timeout_s`; `resolve_corrections()` assigns
  `verified`/`helped`/`rejected` from the rung. `verdict`: `finalize_corrections()`
  (pending → `unverified`), `annotation_corrections`/`annotation_findings`/`entrypoint_*`
  on the row, `write_annotation_patch()` → `jobs/<job>/annotation-patch.yaml`
  (schema `envbuild-annotation-patch/1`; only `verified`/`helped` changes).
  `l3_timeout_s(cfg, state)`: `[budgets] l3_timeout_s=600`, annotation
  `expected_runtime_s` ×1.5+60 capped by `l3_timeout_max_s=1800`;
  `attempt_deadline_s` = build + 2·verify + l3_max + 180.
- `src/ladder.py` — `climb(..., l3_timeout_s=)`; L3 alone uses it; `notes["l3_timeout_s"]`.
- `src/patch.py` — `SET_ENTRYPOINT` in `ACTIONS`; sets `spec.entrypoint`.
- `src/record.py` — `entrypoint_used`, `entrypoint_source` required on attempts and
  verdicts; `annotation_corrections` on verdicts.
- Specs/docs: `remediation_actions.md` (13 actions, `SET_ENTRYPOINT` row + guidance),
  `record_schema.md`, `SKILL.md` (two rules + spec table row), `CLAUDE.md` (consistency rows
  + section), `README.md`, `config.ini`.
- Line endings: files written from the Windows host were normalised back to LF.

## Verified on the real corpus repos (offline `init`)
| model | command chosen | source | workdir | findings |
|---|---|---|---|---|
| circadian-clock | `python biomodels/coreClock/run_clock_model.py` | annotation | `/model/biomodels/coreClock` (corrected) | `needs_workdir` ×2 |
| mbmm | `Rscript inst/examples/01_simulated_correlate_of_protection.R` | annotation (2nd entry) | `/model` | `not_a_command` ×2 (`R`, `R -e`) |
| tumor-tcell | `python tumor_tcell/experiments/main.py` | annotation | `/model` | `placeholder_args` |
| spatio-flux | annotation `scripts/reproduce.py` (full tier) | annotation | — | `placeholder_args` (`run_study.py <SLUG>`) |
| vivarium-chemotaxis | annotation per-process script | annotation | — | `placeholder_args` on `paper_experiments.py <…>` |
| gsmn-tb / hybrid-model-tb | none usable → `ENTRYPOINT_UNKNOWN` | — | — | `placeholder_args` / `not_a_command` |

Note for scoring: where the annotation's entry is usable but differs from the ground-truth
example (tumor-tcell `main.py` vs `main.py -w 3`; spatio-flux `reproduce.py` vs `pytest`), a
failed trial will be attributed to `annotation`, which is the intended reading — the
annotator picked a runnable but non-canonical command.

## Still deferred
Non-root `USER`; Kaniko cache default; stack-image catalogue / `envspec/2`; `expected_runtime_s`
and `smoke_command` in `biomodel-annotator`'s schema (envbuild already honours the former if
present); an ingest path in the annotator for `annotation-patch.yaml`.


---

# rev 4.1 — fixes from the first live pass (`aks-rev4-1`)

See `pass_aks-rev4-1_report.md` for the pass itself. Suite: **130 checks** (121 + 9).

- `src/ladder.py` — `resolve_command(command, mount)`: anchors the first relative script token to
  `code_path` when `workdir != code_path`; `run_l3` uses it. `climb()` reads the L1 lockfile from
  stdout *or* stderr (K8s merges streams).
- `src/examples.py` — `grounded(..., code_path="/model")`: `/model/<x>` checked as `<x>`; other
  absolute paths refused with an explanatory message. Driver passes the spec's `code_path`.
- `src/classify.py` — `requires_python_pick()`, `_PY_LADDER`; `no_matching_dist` →
  `CHANGE_INTERPRETER_VERSION` on a Requires-Python hint; `cannot_import_name` rule
  (`ABI_MISMATCH → PIN_PKG <dist>`); `SONAME_BUNDLE` (`cv2`) used by `missing_soname`;
  `UNSUPPORTED_TOOLS` used by `r_missing_pkg`; `mount_denied` suggests `workdir=/outputs` for
  read-only `/model` at L3.
- `src/patch.py` — `ADD_APT_PKG` accepts a space-separated family; validates Debian names; stores
  one per entry; dedupes.
- `src/driver.py` — `draft_spec` drops toolchain/non-spec dependency entries
  (`draft_spec.dropped_deps`); `cmd_init` records `unsupported_tool_dependency` /
  `not_a_package_spec` findings.
- `scripts/score.py` — `_SELF_INFLICTED`, `_ENV_PLANE_L3`: mount-contract and import-time L3
  failures attribute to `builder`.
- `specs/remediation_actions.md`, `specs/success_criteria.md` updated accordingly.


---

# rev 4.2 — fixes from the second live pass (`aks-rev4-2`)

See `pass_aks-rev4-2_report.md`. Suite: **133 checks**.

- `src/envspec.py` — `MountContract.writable_copy: bool = False`; `from_dict` tolerant of old state.
- `src/patch.py` — `FIX_MOUNT_CONTRACT writable_copy=true|false`; `_MOUNT_FIELDS` extended.
- `src/ladder.py` — `SCRATCH`; `resolve_command(..., anchor=)`; `run_l3` wraps the command in
  `sh -c 'cp -a code/. /scratch/code/; cd <workdir-in-copy>; exec "$@"'` when `writable_copy`.
- `src/classify.py` — `mount_denied` handler (path-aware argument: `output_path=<code>/<top>` /
  `writable_copy=true` / none); `cwd_relative_missing` rule; `classify(..., mount=)` passes
  `code_path` and `workdir_moved` to handlers. Driver passes `spec.mount.to_dict()`.
- `bench/ground_truth.yaml` — hybrid-model-tb `expected_failure_class: null` (status-only control).
- `specs/remediation_actions.md`, `specs/success_criteria.md` updated.
- Test suite: the rev-4.1 `workdir=/outputs` assertion removed (superseded by the path-aware check).


---

# rev 4.3 — honour the repo's lockfile (from pass `aks-rev4-3`)

See `pass_aks-rev4-3_report.md`. Suite: **135 checks**.

- `src/evidence.py` — `_parse_lockfile(root)`: uv.lock / poetry.lock (TOML `[[package]]`),
  Pipfile.lock, fully `==`-pinned requirements*.txt; renv.lock reported with `usable: False`.
  Reads the file in full (the 200 KB `_read` cap truncated spatio-flux's 268 KB uv.lock).
  `scan()` emits `ev["lockfile"]`.
- `src/driver.py` — `draft_spec` pins declared deps to locked versions for `pip` specs
  (`draft_spec.lock_pins`); `cmd_init` adds a `lockfile_honoured` finding.
- `specs/base_selection.md` (new "Lockfiles" section), `specs/synthesis.md` (keep the pins).


---

# rev 4.4 — install mode from evidence; plugin discovery; interpreter intersection (from pass `aks-rev4-4`)

See `pass_aks-rev4-4_report.md`. Suite: **138 checks**.

- `src/evidence.py` — `compiled.project_install_hint` (README installs the project + a build file).
- `src/baseselect.py` — `guess_install_mode` honours it → `installed`.
- `src/classify.py` — `plugin_not_discovered` rule → `BUILD_MODE_MISMATCH`/`SWITCH_INSTALL_MODE installed`
  in mounted mode; `classify(..., install_mode=, requires_python=)`; `no_matching_dist` intersects the
  Requires-Python hint with the project constraint.
- `src/driver.py` — `state.requires_python` recorded at init; passed to `classify`.
- `specs/base_selection.md`, `specs/failure_taxonomy.md` updated.


---

# rev 4.5 — search budget net of L3 runtime; undeclared imports (from pass `aks-rev4-5`)

See `pass_aks-rev4-5_report.md`. Suite: **141 checks**.

- `src/ladder.py` — `climb` records `notes["l3_seconds"]`.
- `src/driver.py` — `state.l3_seconds` accumulated; `budget_elapsed` subtracts it; verdict gains
  `search_seconds`, `l3_seconds`. `draft_spec` appends `evidence.python.undeclared_imports`
  (deduplicated, unpinned; `draft_spec.undeclared`); `cmd_init` finding `undeclared_imports`.
- `src/evidence.py` — `_undeclared_imports(root, files, declared, lock)`: AST scan of the repo's
  importable packages; stdlib/local/provided-by-alias excluded; scripts/tests/examples not scanned.
- `specs/synthesis.md`, `specs/success_criteria.md`, `specs/record_schema.md` updated.


---

# rev 4.6 — SET_L3_TIMEOUT for a slow example; measured runtime proposed to the annotation (from pass `aks-rev4-6`)

See `pass_aks-rev4-6_report.md`. Suite: **144 checks**.

- `src/envspec.py` — `EnvSpec.l3_timeout_s: int | None` (run-time field; renderer ignores it).
- `src/patch.py` — `SET_L3_TIMEOUT <seconds>` (≥ 60, must extend).
- `src/classify.py` — `l3_deadline` handler: L3 deadline → `TIMEOUT` + `SET_L3_TIMEOUT 2×`, `retry`;
  a handler-supplied action now routes `retry` even for a dead-letter class; `classify(..., l3_timeout_s=)`.
- `src/driver.py` — `l3_timeout_s()` honours the spec override (capped at `l3_timeout_max_s`);
  passing L3 after an extension appends an `expected_runtime_s` correction (`verified`).
- `bench/ground_truth.yaml` — spatio-flux example `python scripts/reproduce.py`, 1200 s.
- `specs/remediation_actions.md`, `specs/failure_taxonomy.md` updated.
- `scripts/score.py` — an L3 `TIMEOUT` with `l3_timeout_s` below the ground truth's `timeout_s` attributes to `builder`.


---

# rev 4.7 — writable_copy harvests outputs (from pass `aks-rev4-7`)

See `pass_aks-rev4-7_report.md`. Suite: **144 checks**.

- `src/ladder.py` — `_WRITABLE_COPY_SH`: copy → marker → run → `find -newer` → `cp -p` into
  `output_path` (relative layout kept; caches excluded) → `exit $rc`. Tested in-cluster (busybox sh).
  `l3_claim` notes the harvest. `run_l3` passes `output_path` as `$3`.
- `specs/success_criteria.md` — the writable-copy paragraph now describes the harvest instead of the gap.
- `src/evidence.py` — `typical_runtime_s()`; `read_annotation` maps `execution.compute.typical_runtime {value, unit}`
  onto every entry point's `expected_runtime_s` (entry-level value wins). Until now nothing read either field,
  so no annotated runtime ever reached the L3 deadline. Suite: **145 checks**.
- Docs: `envbuild_architecture.md` (integration guide, Mermaid diagrams) and `envbuild_benchmark_report.md`.
- `docs/` — `envbuild_architecture.md`, `envbuild_benchmark_report.md`, `diagrams.py` (Graphviz source for the three
  architecture PNGs; run `python docs/diagrams.py` after editing), rendered `arch_*.png`, `benchmark_progression.png`.
- Pre-push scan (2026-09-24): 145 checks, pyflakes clean, CRLF normalised to LF in three files (two new docs and the pre-existing
  `.github/workflows/docker-develop.yml`), no secret-shaped strings,
  `docs/` added to `.dockerignore`. Rev 4.7 items (`writable_copy` harvest, `typical_runtime` → L3 deadline) are
  unit-tested but **not yet exercised in a live pass**.


---

# Hold-out pass `aks-holdout-1` — 2026-09-29 (no code changes; findings only)

20 unseen repos, one pass, no fixes between models. **1/19 verified, 0 false-verified, 44.8 % LLM fallback.**
See `docs/pass_aks-holdout-1_report.md` for the four-way sort. Open items, by weight: H1 `infra_network` fires
on pip's `(from versions: none)` without a network marker (7 jobs closed `error` unrepaired); H2 undeclared-import
scan emits non-distributions (`libsbml`, vendored modules); M1 git/path-sourced deps from `[tool.uv.sources]`;
H5/H6 renderer accepts a pkg_manager the base lacks and Poetry caret specs; H3 L3-max timeout should be
`failed` not `error`; H4 grounding rejects CI globs and `Rscript -e`; M2–M5 compiled/second-runtime/C++/conda
families. Also: `bench/ground_truth.yaml` gained 20 `holdout: true` entries; `bench/holdout_manifest.yaml` pins shas.

---

# rev 4.8 — the hold-out's harness bugs (H1–H6), before `aks-holdout-2`

Fixes derived from `pass_aks-holdout-1_report.md`; every regression check uses that pass's own error text.
Suite: **155 checks**. No rule was added for a single repo; each change parses a tool's message
or a build file's declared structure.

- **H1** `src/classify.py` — `infra_network` no longer matches pip's `(from versions: none)`; a real network
  marker is required for `INFRA_UNAVAILABLE`. `no_matching_dist` returns the new class **`UNKNOWN_DISTRIBUTION`**
  (routes `retry`) and uses `pkg_origin` to pick the move: undeclared-import guess → `REMOVE_PKG`;
  git/path source → no action, escalate with the URL; declared → agent decides.
- **M1 (lite)** `src/evidence.py` — `_parse_pyproject` reports `vcs_sources` (`[tool.uv.sources]`, Poetry git/path
  tables) and the project `name`; `_parse_lockfile` reports per-package `sources`. `src/driver.py` `draft_spec`
  never lock-pins a VCS-sourced name, records `pkg_origin` per spec entry (`lock` / `declared` / `annotation` /
  `undeclared` / `vcs:<src>`) into `state.pkg_origin`, and `init` emits a `vcs_dependency` finding.
- **H2** `src/evidence.py` `_undeclared_imports` — local = every directory name and module stem in the tree;
  imports inside `try:` bodies or `if __name__ == "__main__":` are skipped; a module sharing a distinctive token
  or substring with a declared/VCS distribution is taken as provided (`neuroml`←pyNeuroML,
  `pbg_parsimony`←viva-parsimony). `MODULE_PYPI` += `libsbml→python-libsbml`, `roadrunner→libroadrunner`,
  `ruamel→ruamel.yaml`, `pylab`/`mpl_toolkits→matplotlib`, `google→protobuf`, `OpenGL→PyOpenGL`.
- **H3** `src/driver.py` `cmd_verdict` — `--status error` is refused after a non-infra attempt (the example that
  outran `l3_timeout_max_s` is `failed`). `SKILL.md` gained a status table and the `UNKNOWN_DISTRIBUTION` playbook.
- **H4** `src/examples.py` `grounded` — a glob entry (`pytest test_*.py`) is grounded when it matches a file;
  `Rscript -e '…'` is grounded when the repo is an R package (`DESCRIPTION`).
- **H5** `src/patch.py` — new action **`REMOVE_PKG`**; `SWITCH_INSTALLER` refuses an installer the base image
  does not provide and names the `CHANGE_BASE_IMAGE` to make first (`_INSTALLER_BASE`).
- **H6** `src/evidence.py` `_pep440` — Poetry `^`/`~` specs become PEP 440 ranges (`docker^7.1.0` → `>=7.1.0,<8`).
- `src/baseselect.py` — R by file count only when R dominates (covasim: 193 .py + 1 .R had drafted a rocker base).
- Specs: `failure_taxonomy.md` (`UNKNOWN_DISTRIBUTION`; the `(from versions: none)` note), `remediation_actions.md`
  (`REMOVE_PKG`, installer guard), `success_criteria.md` (status semantics). Test
  `_c_none_versions` rewritten to the property that survives (never `UNPIN_PKG`; never an outage by itself).
- Dry run on the eight affected hold-out repos (fetched from the claim): zero PyPI guesses left for the five viva
  repos that had them; covasim drafts `python:3.11-slim`/pip with 82 specs; c302 keeps `airspeed`, `lxml`,
  `matplotlib`, `owmeta` (real, on PyPI). Not yet live-tested.

---

# Hold-out pass `aks-holdout-2` — 2026-10-01 (rev 4.8 tree as `rev4.9`; no code changes; findings only)

2/19 verified (viva-tumor-tcell, **c302**), 0 false-verified, 0 `error` verdicts, 0 infra retries (pass 1: 1/19, 9 `error`,
15 spurious retries). H1–H6 behaved as designed; the seven misfires became real classifications. Second layer, sorted in
`pass_aks-holdout-2_report.md`: harness bugs H7 (mount repair over a code dir), H8 (Kaniko deadline kill → TIMEOUT), H11 (L1
top_level over-probing), **H12 (installed-mode source tree shadows compiled extensions — the compiled family's blocker)**,
H13 (frozen requirements under tests/ merged as declaration), H14 (L2 probe chosen by base not entry language), H15 (R plural
"dependencies … are not available"; R `==` pins); mechanisms M1 VCS install, M3 Node, M5 conda names, M6 R from-source build
time, M7 run-time egress.

---

# rev 4.9 — hold-out 2's second layer (H7, H8, H11–H15), before `aks-holdout-3`

From `pass_aks-holdout-2_report.md`; regression checks use that pass's error text. Suite: **164 checks**, pyflakes clean.

- **H12** `src/envspec.py` — installed mode sets `ENV PYTHONSAFEPATH=1` (pip/conda/mamba): the installed package is
  what runs, the copy at `code_path` is data. A sibling-module import from a script now surfaces as
  `IMPORT_PATH_ERROR` → `FIX_MOUNT_CONTRACT extra_path=…` (typed) instead of silently importing uncompiled source.
- **H7** `src/evidence.py` `source_tops`; `src/classify.py` `mount_denied` proposes `writable_copy=true` when the
  write target's top-level dir holds source; `state.source_tops` → `classify(source_tops=)`.
- **H8** `src/driver.py` — `build_deadline` rule: a failed build with `duration >= build_timeout_s - 5` and no
  classification is `TIMEOUT` at L0.
- **H11** `src/ladder.py` `_L1_PROBE` — a distribution passes if its canonical module (name-derived or alias) imports;
  all `top_level.txt` names are tried only without a canonical candidate; one line reported per failing dist.
- **H13** `src/evidence.py` — declaration precedence: pyproject/setup.py; root `requirements*.txt` only when they
  declare nothing; files under `tests/`/`ci/`/`docs/` or named `*frozen*`/`*freeze*`/`*lock*` are listed in
  `python.ignored_requirements` and never merged.
- **H14** `src/ladder.py` `run_l2` — probe chosen by the entry's suffix (`.R`/`.r` → R, else python; bare → by
  `pkg_manager`).
- **H15** `src/classify.py` `r_dependencies` (plural) → one `ADD_PKG` naming all; `src/patch.py` `ADD_PKG` accepts a
  space-separated family of bare names; `PIN_PKG` refused for R specs; `src/evidence.py` DESCRIPTION `LinkingTo`
  counted as declared.
- Specs: `failure_taxonomy.md` (build-deadline `TIMEOUT`), `remediation_actions.md` (`ADD_PKG` family, R `PIN_PKG`),
  `success_criteria.md` (installed mode and the source tree; mount repairs never cover source).
- Dry run on the cached hold-out sources: covasim 27 specs (was 82; `tests/requirements_frozen.txt` ignored),
  `PYTHONSAFEPATH` rendered for the seven installed-mode repos, `demo` in viva-simbio's `source_tops`.
  Not yet live-tested. Not addressed (design items): M1 VCS install, M3 Node, M5 conda names, M6 R binaries, M7 egress.

---

# Hold-out pass `aks-holdout-3` — 2026-10-02 (rev 4.9 tree as `rev4.10`; no code changes; findings only)

**6/19 verified** (viva-tumor-tcell, viva-simbio, viva-amici, covasim, starsim, viva-yalla†), 0 false-verified, 1 `error`
(real API outage), 1 infra retry (real ErrImagePull), LLM fallback 35.6 % (was 45.7 %). H7/H8/H11/H13/H15 each moved the
model they targeted; epidemics builds and passes L1 for the first time. Two harness bugs remain, sorted in
`pass_aks-holdout-3_report.md`: **H16** a command with no script token (`pytest`, `Rscript -e`, `make test`, `julia -e`)
is reported as "annotation declares no entry point" at L2 — c302 regressed on it, epidemics/covid-sim/catalyst-jl stopped
on it; **H12b** installed mode kept `extra_path=['/model']` → `PYTHONPATH=/model` defeats `PYTHONSAFEPATH` (viva-cpm,
viva-biofilm). † viva-yalla and starsim verified on an agent-grounded alternate example (README demo calls macOS `open`;
no README command) — flagged for a human policy call; proposed ground-truth field `alternate_example_ok`.

---

# rev 4.10 — H16 and H12b (from `aks-holdout-3`), before `aks-holdout-4`

Suite: **168 checks**, pyflakes clean. Not yet live-tested.

- **H16** `src/ladder.py` `climb` — an entry with no script token but a non-empty command (`pytest`, `Rscript -e`,
  `make test`, `julia -e`) no longer fails L2 as "annotation declares no entry point"; L2 is recorded
  `notes.l2_claim = not probeable` and L3 runs the command. Only an empty command is `ENTRYPOINT_MISSING`.
- **H12b** `src/envspec.py` renderer filters `extra_path` entries at/under `code_path` in installed mode;
  `src/patch.py` `SWITCH_INSTALL_MODE installed` drops them (noted in the result); `src/driver.py` `draft_spec` does not
  seed `extra_path` from local modules when the draft is `installed`. Test `_d_draft_spec_mount` updated to the new
  contract (the `installed_mode` fixture drafts `installed`).
- `specs/success_criteria.md` — both contracts written down.

---

# Hold-out pass `aks-holdout-4` — 2026-10-05 (rev 4.10 tree as `rev4.11`) and rev 4.11

**4/19 verified** (tumor-tcell, simbio, amici, covasim — identical chains to pass 3), 0 false-verified, LLM fallback 38.3 %.
H16 and H12b both did their job (every bare command reached L3; no shadowing at L1/L2); the number fell from 6 because
pass 3's two alternate-example verifications did not repeat — viva-yalla's pass-3 output came from the README demo's run,
not the command credited; starsim's depends on install mode. **Stable hold-out rate: 4/19 across two passes.**
Full sort in `pass_aks-holdout-4_report.md`.

**rev 4.11** (173 checks, pyflakes clean, not live-tested):
- **H17** `src/k8s.py` `wait_terminated` returns `ENVBUILD_EXIT: container exited N (reason)`; `src/verifier.py` substitutes it
  when the log is empty (c302's L3 row had exit None and no text).
- **H18** `src/envspec.py` R install step checks `installed.packages()` and fails the build naming missing packages
  (`ENVBUILD_R_NOT_INSTALLED`) — `install.packages()` exits 0 on failure (epidemics).
- **H12c** `src/ladder.py` `run_l3`: installed-mode `pytest` gets `PYTEST_ADDOPTS=--import-mode=importlib -p no:cacheprovider
  --rootdir=<code_path> --basetemp=<output_path>/.pytest_tmp` (viva-biofilm, starsim).
- **H19** `src/verifier.py` `quote_cmd` falls back to whitespace split on an unbalanced quote; `src/examples.py` `is_command`
  rejects such lines (catalyst-jl init crash).
- **R2** `src/classify.py` `UPSTREAM_BROKEN_IMPORT` — `tesbml` → `PIN_PKG tellurium<2.2.11` (cpm, biosimulators_tellurium).
- Test `_render_kaniko_portable` heredoc assertion narrowed (the R marker string contains "ENVBUILD").

---

# `aks-holdout-5` aborted — 2026-10-06 — agent runtime drift, not a model result

All 20 Jobs on `rev4.12` (= rev 4.11 tree, verified) failed within ~2 min, before `init`: `Error: Unknown provider
"azure-openai-responses"`. `harness/Dockerfile` installed `@earendil-works/pi-coding-agent` **unpinned**; the Oct 6 build
pulled **pi 1.0.4** (every earlier image: 0.99.0), and pi 1.0 renamed the provider id to `azure` (`gpt-5.6-luna` is listed
under it). No attempt or verdict rows were written; the 20 Jobs were deleted. The per-file hash probe covers
`/opt/skills/envbuild` only and could not have caught this.

Fixes (tree, rev 4.11 + harness):
- `harness/Dockerfile` — `ARG PI_VERSION=1.0.4`; install pinned. Bump deliberately.
- `harness/entrypoint.sh` — provider id resolved by asking the installed pi (`--list-models` lists `azure` → use it, else
  `azure-openai-responses`); exports `AI_RUNTIME_VERSION`.
- `src/driver.py` `run_provenance` — new field `run.agent_runtime` on every row, so a pass report can state the agent
  version the way it states the image digest.

Caveat for the next pass: pi 1.0.4 is a **different agent runtime** from the one every previous pass used. `aks-holdout-5`
on it is measured against passes 1–4 with that change in the open; if results shift in ways the four code fixes do not
explain, the runtime is the first suspect.

---

# Hold-out pass `aks-holdout-5` — 2026-10-06 (rev 4.11 tree + harness as `rev4.13`, pi 1.0.4) and rev 4.12 tree

**4/19 verified** (tumor-tcell, simbio, amici, covasim — third pass with the same four), 0 false-verified, 0 `error`,
LLM fallback 40.3 %; viva-yalla verified on `pytest` only → reported separately. H18 took epidemics to L3 for the first time;
H12c took viva-biofilm to its study (plotly/kaleido needs Chrome: new M8); H19 held. **R2 disproved**: `tellurium<2.2.11`
installed and still imports `tesbml`. Full sort in `pass_aks-holdout-5_report.md`.

**rev 4.12 tree** (175 checks, pyflakes clean, not live-tested):
- **H20** `src/driver.py` `entry_tier()` + verdict `entrypoint_tier`; `scripts/score.py` reports `verified_tests` outside
  build success (attribution `policy:alternate_example`), honours `alternate_example_ok` in ground truth. Passes 3–5 re-scored.
- **H12d** `src/ladder.py` `run_l3`: under `writable_copy`, `extra_path` entries under `code_path` move into the copy.
- **R2** `src/classify.py` `UPSTREAM_BROKEN_IMPORT["tesbml"]` → `UNSUPPORTED_TOOLCHAIN`/`ESCALATE` with the evidence; the pin is gone.
- `specs/success_criteria.md` — "A test suite is not the example".
- Open: H22 (`venv`/`source` lines accepted as entries), R3 (legacy-sdist metadata failure), H23 (Julia runs the R L1 probe).

---

# rev 4.13 — the sprint before `aks-holdout-6` (2026-10-07; not live-tested)

Decisions: scope = M1 + M6 + cross-job memory, single-repo reruns only until done; alternate-example policy = a shipped
demo counts, a test suite never does. Pre-registered expectation for pass 6: **8–10/19**. Suite **184 checks**, pyflakes clean.

- **M1** `src/driver.py` `git_direct_ref()`: git sources → `name @ git+url@<sha>` (sha from uv/poetry lock, incl. uv's
  `?branch=…#sha` form; lock overrides pyproject's branch); `vcs_installed`/`vcs_unpinned`, finding
  `vcs_dependency_installed`; `src/envspec.py` adds `git` to apt when a direct ref is present; `src/classify.py`
  `git_fetch_failed` → `UNKNOWN_DISTRIBUTION` naming the ref; toolchain-name filter exempts PEP 508 direct refs;
  `src/evidence.py` carries `rev/tag/branch` on pyproject git sources. Dry-run: membrane-actin gets 3 sha-pinned refs,
  mgen 1 branch ref (unpinned).
- **M6** `src/envspec.py` `r_repos()`: rocker/r-ver bases install from P3M binaries (jammy/noble/focal by R minor) with
  source CRAN fallback and the `HTTPUserAgent` P3M needs; `src/driver.py` `build_timeout_s(cfg, spec)` per manager
  (`build_timeout_<mgr>_s`, else renv/pkg ×3, conda/mamba ×2); `config.ini` `build_timeout_renv_s = 1800`;
  the deadline rules follow it.
- **Cross-job memory** `src/memory.py` (`remember` on `verified` only, `recall` at `init`, `forget`); `init` drafts from
  the remembered spec (`spec_source: memory`, `memory.{job_id,run_id,same_revision,charged_attempts_then}`,
  `draft_from_evidence` kept), `--no-memory`, `envbuild forget`; rows/verdicts carry `spec_source`, `memory_job_id`;
  scorer reports `success_from_memory`. SKILL.md Step 1 tells the agent to run a memory draft as-is first.
- **H22** `src/examples.py` `is_command` rejects environment-setup lines (`venv`, `source …activate`, `pip install`,
  `conda create`, `cd`, `export`, `git clone`).
- **R3** `src/classify.py` `legacy_sdist`: pip `metadata-generation-failed` + inner ModuleNotFoundError →
  `DEP_RESOLUTION_CONFLICT` / `CHANGE_INTERPRETER_VERSION` one rung down (ESCALATE at the floor); `python_version`
  added to the classifier context from the base tag.
- **epidemics** `src/classify.py` `toolchain_readonly`: "can't create X.o: Read-only file system" → `writable_copy`.
- **H20 scorer** aligned with the decision: `verified_tests` never counts; `verified_alternate` counts and is flagged
  until `alternate_example_ok` confirms it (set true for viva-yalla, starsim). Pass 3 re-scores to 5/19.
- `bench/ground_truth.yaml` — `alternate_example_ok`, `policy_note`, `design_item` fields. Specs updated.
- Open: H23 (Julia runs the R L1 probe; cosmetic). Needs a build as **`rev4.14`** and a pi smoke before launch.

---

# rev 4.14 — rerun-6's layer + build-pod resources (2026-10-08; 191 checks, pyflakes clean, not live-tested)

Image `rev4.14` (= rev 4.13 tree) ran six single-repo reruns (`rerun_aks-rerun-6_report.md`): M1, M6, R3 and
`toolchain_readonly` each moved their target one layer; viva-mgen verified (tests tier). The new layer:

- **H26** `src/driver.py` `runner_deps()` + `is_test_runner()`: the entry's runner (`pytest`; `testthat` + DESCRIPTION
  Suggests) is added to `pkg_specs` at `init` (finding `runner_dependency_added`). c302's installed image had
  `ENTRYPOINT ["pytest"]` and no pytest → StartError 128. `src/evidence.py` parses `Suggests` (`r.suggests`).
- **H24** `src/ladder.py` `_L1_PROBE`: distributions with no top-level module are skipped (starsim: intel-cmplr-lib-rt, mkl).
- **H25** `envbuild patch --classified-by agent` accepted (alias of `llm`).
- **H27** `src/evidence.py` `_HEAVY_GROUP`: optional groups named `sim|gpu|all|mpi|docs|…` are left out of the draft and
  reported as `optional_groups_skipped` (membrane-actin spent 3 attempts compiling pymem3dg/readdy for a demo that skips them).
- **Policy** `choose_entry` prefers a script example over a test runner; `entry_tier` uses `is_test_runner`.
  `bench/ground_truth.yaml` viva-mgen example → `python workspace/studies/fig2-growth/sims/run.py`.
- **Build pod resources** `src/builder.py` `BUILD_RESOURCES` + `resources_from_config()`; `config.ini [builder]`
  `request_cpu|memory|ephemeral`, `limit_cpu|memory|ephemeral` (defaults 1 / 2Gi / 8Gi, 4 / 8Gi / 32Gi); Kaniko
  `--kaniko-dir` on the work claim. A namespace LimitRange defaulting to 250m/256Mi/256Mi-ephemeral kills Kaniko at the
  first layer. **Known limit:** Kaniko extracts the image into its own root filesystem, which counts as ephemeral storage
  and cannot be redirected to a volume; a namespace with a 9 Gi ephemeral quota (~700 Mi free) cannot run it at any
  setting. The structural fix is a volume-backed builder (BuildKit rootless, state on the work PVC) behind the existing
  `Builder` protocol — scoped as the next design item after `aks-holdout-6`.
