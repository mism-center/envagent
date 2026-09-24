# Remediation actions

Thirteen typed actions. **Exactly one per attempt** — `envbuild patch` refuses a
second call before the next `attempt`, and logs the discarded action. Multi-action
repairs make attribution impossible, and attribution is the whole point of the
attempt record.

| Action | `--arg` format | Effect on the EnvSpec |
|---|---|---|
| `ADD_APT_PKG` | `libxml2-dev`, or a family `libgl1 libglib2.0-0 libxcb1` | append to `apt_packages` (one entry per package; still one action) |
| `ADD_PKG` | `scipy` or `scipy>=1.10` | append to `pkg_specs` |
| `PIN_PKG` | `numpy==1.26.4` (constraint required) | replace the existing entry for that distribution |
| `UNPIN_PKG` | `numpy` | strip the constraint, keep the name |
| `CHANGE_INTERPRETER_VERSION` | `3.10` | rewrite the base tag, keep the flavour suffix; digest re-resolved |
| `CHANGE_BASE_IMAGE` | `ubuntu:24.04` | replace the base; digest re-resolved |
| `SWITCH_INSTALLER` | `mamba` | `pkg_manager` |
| `SWITCH_INSTALL_MODE` | `installed` | `install_mode` |
| `SET_ENV_VAR` | `PYTHONPATH=/model/src` | `env_vars[k] = v` |
| `ADD_PRE_INSTALL_CMD` | a shell line | append to `pre_install` |
| `FIX_MOUNT_CONTRACT` | `extra_path=/model/src`, `workdir=/model/sim`, `output_path=/model/out`, `writable_copy=true` | `mount.<field>`; `extra_path` appends, everything else replaces |
| `SET_L3_TIMEOUT` | `1200` | `l3_timeout_s` (run-time field; the image is unchanged). Only for the example killed at the **L3** deadline; the driver caps at `l3_timeout_max_s`; a passing run then proposes `expected_runtime_s` to the annotation |
| `SET_ENTRYPOINT` | `python examples/run.py --steps 10` | the job's L2/L3 command (and `entrypoint`); must be grounded in `evidence.examples` or an existing script; max two per job; recorded as an annotation correction |
| `ESCALATE` | — | no spec change; close the job |

## Rules the driver enforces for you

- **Forbidden triples.** `(failure_class, action, arg)` is recorded per job and
  refused on repeat. This is episodic memory: it dies with the job, so it does
  not breach the no-memory constraint, and it is the single largest reducer of
  wasted attempts. If `patch` rejects your action, pick a *different* one — do
  not re-argue the same one with a reworded justification.
- **Best-so-far rollback.** If the previous attempt reached a *lower* rung than
  the best attempt so far, `patch` discards the regressing spec, applies your
  action to the best spec instead, and forbids the regressing triple. One bad
  `CHANGE_BASE_IMAGE` therefore cannot discard four attempts of progress.
- **Digest re-resolution.** Any action that changes the base image clears
  `base_digest` and re-resolves it. A bare tag never reaches a Dockerfile.
- **No patch after a substrate failure.** If the last attempt was
  `INFRA_UNAVAILABLE`, `patch` is refused: re-run `attempt` with the same spec.
- **`ADD_PRE_INSTALL_CMD` is environment preparation, not a back door.** The
  driver refuses a command that writes into `site-packages` or an R library,
  edits `/etc/hosts` or `resolv.conf`, installs a package through the shell
  (`pip install`, `install.packages(`, `micromamba install`, `R CMD INSTALL`),
  or re-links the interpreter. Each of those was used in the first corpus run
  to make a rung pass that the image had not earned — shim modules written so
  the L1 probe would import, a hosts entry to route around DNS. The real fix is
  the typed action the refusal message names; if there is none, the job closes
  honestly.

## Choosing an argument

- `ADD_APT_PKG` — for a missing header, the rule table already resolved the
  Debian package (`classify.HEADER_APT`). If it did not, name the `-dev` package
  that ships the header, not the runtime one.
- `ADD_APT_PKG` — one action may name a *family* when the rule table does
  (`cv2` → the opencv runtime set). Do not pay one attempt per soname.
- `UNPIN_PKG` vs `CHANGE_INTERPRETER_VERSION` — when pip prints `Ignored the
  following versions that require a different python version: … Requires-Python
  >=3.9`, the author's pin is right and the interpreter is wrong; the rule
  table now picks `CHANGE_INTERPRETER_VERSION`. Unpinning there installs an old
  release that lacks symbols the model imports and fails one rung later.
- `FIX_MOUNT_CONTRACT` for a write into the read-only code mount — the rule
  table picks the argument from *where* the write went, and two live passes
  showed each case is different:
  - `'/model/out/…'` (file-relative, `dirname(__file__)/../out`) →
    `output_path=/model/out`: mount the writable volume where the model
    already writes. `workdir` cannot help here.
  - `'studies/…'` (cwd-relative) → `writable_copy=true`: L3 copies the code
    into the container and runs from the copy. Prefer this over
    `workdir=/outputs` — a model that writes cwd-relative usually also *reads*
    cwd-relative (`./investigations/x.yaml`), and only a working copy
    satisfies both. The image is unchanged; the copy dies with the pod.
  - `'/outputs/…'` → the output mount itself is broken; no canned argument.
  A relative script path is anchored to `code_path` (or to the copy) whatever
  `workdir` is, so the command in the record stays as the annotation wrote it.
- `PIN_PKG` — pin to a version that predates the break, not to "latest known
  good". For an `ABI_MISMATCH` against numpy 2, `numpy<2` on the *dependent* is
  usually better than pinning numpy itself.
- `FIX_MOUNT_CONTRACT` — for `IMPORT_PATH_ERROR`, `draft_spec` already seeds
  `extra_path` from every root-level or `src/`-layout package the evidence scan
  found, so you should rarely see this at all for those two layouts. If you do
  (a repair chain that swapped entry points, a layout the scan doesn't cover),
  the fix is still the same shape: the containing directory of whichever local
  package the traceback names, relative to `/model`.
- `SET_ENTRYPOINT` — when the entry you were given is not what the repo says
  to run. `envbuild init` already lists `examples` and `annotation_findings`,
  and has already substituted the repo's smoke example if the annotation's
  entry was not a command; reach for this when L2/L3 shows the *chosen*
  example is the wrong one (a placeholder survived, a script that needs
  arguments, the README names a quicker demo). Prefer the smoke tier. See
  `specs/success_criteria.md` for what happens to the correction afterwards.
- `ESCALATE` — only for `TIMEOUT` and `RUNTIME_ERROR` after a real repair was
  tried. It is not a way to skip thinking.
