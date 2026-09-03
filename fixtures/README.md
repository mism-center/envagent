# Fixtures

One tiny repo per failure class. Each triggers **exactly one** class,
deterministically, in well under thirty seconds.

This is how the loop gets developed and how it gets a regression suite that runs
in CI. Real repos are for milestone 9, not for development — waiting on
multi-minute real builds to test a classifier change is how a week disappears.

| Fixture | Class | Rung | Expected repair |
|---|---|---|---|
| `missing_system_lib/` | `MISSING_SYSTEM_LIB` | L0 | `ADD_APT_PKG libxml2-dev` |
| `dep_conflict/` | `DEP_RESOLUTION_CONFLICT` | L0 | `UNPIN_PKG numpy` |
| `missing_dependency/` | `MISSING_DEPENDENCY` | L2 | `ADD_PKG requests` |
| `import_path_error/` | `IMPORT_PATH_ERROR` | L2 | `FIX_MOUNT_CONTRACT extra_path=/model/src` |
| `installed_mode/` | `BUILD_MODE_MISMATCH` | L2 | `SWITCH_INSTALL_MODE installed` |
| `no_entrypoint/` | `ENTRYPOINT_UNKNOWN` | L2 | none — back to submitter |
| `bad_output_path/` | `MOUNT_CONTRACT_ERROR` | L3 | `FIX_MOUNT_CONTRACT` |

`import_path_error/` is the important one. `mypkg` is a module the repo itself
defines, so it appears in `evidence.local_modules`. A loop that classifies this
as `MISSING_DEPENDENCY` and installs a same-named PyPI package will report
success while running the wrong code.

## Running one end to end

```bash
SKILL_DIR=..           # from this directory
uv run "$SKILL_DIR/src/driver.py" init \
  --repo ./import_path_error --annotation ./import_path_error/annotation.yaml \
  --model-id fixture:import_path_error
```

Docker and the compose stack are required from `attempt` onwards. `init`, `spec`
and `render` work offline.
