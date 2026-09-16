# Remediation actions

Twelve typed actions. **Exactly one per attempt** — `envbuild patch` refuses a
second call before the next `attempt`, and logs the discarded action. Multi-action
repairs make attribution impossible, and attribution is the whole point of the
attempt record.

| Action | `--arg` format | Effect on the EnvSpec |
|---|---|---|
| `ADD_APT_PKG` | `libxml2-dev` | append to `apt_packages` |
| `ADD_PKG` | `scipy` or `scipy>=1.10` | append to `pkg_specs` |
| `PIN_PKG` | `numpy==1.26.4` (constraint required) | replace the existing entry for that distribution |
| `UNPIN_PKG` | `numpy` | strip the constraint, keep the name |
| `CHANGE_INTERPRETER_VERSION` | `3.10` | rewrite the base tag, keep the flavour suffix; digest re-resolved |
| `CHANGE_BASE_IMAGE` | `ubuntu:24.04` | replace the base; digest re-resolved |
| `SWITCH_INSTALLER` | `mamba` | `pkg_manager` |
| `SWITCH_INSTALL_MODE` | `installed` | `install_mode` |
| `SET_ENV_VAR` | `PYTHONPATH=/model/src` | `env_vars[k] = v` |
| `ADD_PRE_INSTALL_CMD` | a shell line | append to `pre_install` |
| `FIX_MOUNT_CONTRACT` | `extra_path=/model/src`, `workdir=/model/sim`, `output_path=/outputs` | `mount.<field>`; `extra_path` appends, everything else replaces |
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

## Choosing an argument

- `ADD_APT_PKG` — for a missing header, the rule table already resolved the
  Debian package (`classify.HEADER_APT`). If it did not, name the `-dev` package
  that ships the header, not the runtime one.
- `PIN_PKG` — pin to a version that predates the break, not to "latest known
  good". For an `ABI_MISMATCH` against numpy 2, `numpy<2` on the *dependent* is
  usually better than pinning numpy itself.
- `FIX_MOUNT_CONTRACT` — for `IMPORT_PATH_ERROR`, `draft_spec` already seeds
  `extra_path` from every root-level or `src/`-layout package the evidence scan
  found, so you should rarely see this at all for those two layouts. If you do
  (a repair chain that swapped entry points, a layout the scan doesn't cover),
  the fix is still the same shape: the containing directory of whichever local
  package the traceback names, relative to `/model`.
- `ESCALATE` — only for `TIMEOUT` and `RUNTIME_ERROR` after a real repair was
  tried. It is not a way to skip thinking.
