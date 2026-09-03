# Failure taxonomy

Lives here, not in code, because it will churn constantly for the first few
hundred repos. `src/classify.py` implements only the *detection* half (the rule
table); the routing and repair semantics below are what the agent applies when
no rule fires.

`classify.py` and this file must agree on the class names and on `ROUTING`.

| Class | Detected at | Typical repair | Routes to |
|---|---|---|---|
| `MISSING_SYSTEM_LIB` | L0 | `ADD_APT_PKG` | agent retry |
| `DEP_RESOLUTION_CONFLICT` | L0 | `UNPIN_PKG`, `SWITCH_INSTALLER` | agent retry |
| `COMPILE_ERROR` | L0 | `PIN_PKG`, `CHANGE_INTERPRETER_VERSION`, `ADD_APT_PKG` | agent retry |
| `MISSING_DEPENDENCY` | L1/L2 | `ADD_PKG` | agent retry |
| `IMPORT_PATH_ERROR` | L2 | `FIX_MOUNT_CONTRACT`, `SET_ENV_VAR` | agent retry |
| `ABI_MISMATCH` | L1 | `PIN_PKG`, `CHANGE_BASE_IMAGE` | agent retry |
| `MOUNT_CONTRACT_ERROR` | L2/L3 | `FIX_MOUNT_CONTRACT` | agent retry |
| `BUILD_MODE_MISMATCH` | L2 | `SWITCH_INSTALL_MODE` | agent retry |
| `ENTRYPOINT_UNKNOWN` | L2 | — | back to submitter |
| `MISSING_DATA_FILE` | L3 | — | back to submitter |
| `LICENSE_REQUIRED` | L0/L3 | — | back to submitter |
| `UNSUPPORTED_TOOLCHAIN` | L0 | — | dead-letter |
| `TIMEOUT` / `RUNTIME_ERROR` | L3 | `SET_ENV_VAR`, `ESCALATE` | dead-letter |
| `UNKNOWN` | any | classify it yourself, then repair | LLM fallback |

## The split that matters

`MISSING_DEPENDENCY` and `IMPORT_PATH_ERROR` are the *same stderr text*
(`ModuleNotFoundError: No module named 'x'`) at different rungs. They are split
because of mounting, and conflating them is the single most damaging mistake
this loop can make:

- At **L1** no code is mounted, so the module can only be a missing dependency →
  `MISSING_DEPENDENCY` → `ADD_PKG`.
- At **L2/L3**, if `x` is a module the repo *itself* defines (see
  `evidence.local_modules`), the module exists and the **mount is wrong** →
  `IMPORT_PATH_ERROR` → `FIX_MOUNT_CONTRACT` or `SET_ENV_VAR`.

Installing a same-named PyPI package to fix an `IMPORT_PATH_ERROR` will appear to
succeed while shadowing the model's own code. You would ship a container that
runs the wrong code and reports it as verified. **On an L2 import failure, prefer
a mount repair; `ADD_PKG` at L2 requires explicit justification in `--why`.**

## Routing meanings

- **agent retry** — apply one typed action and run another attempt.
- **back to submitter** — the annotation or the repo is wrong in a way the agent
  cannot fix. Close with `--status failed` and a reason naming the missing thing.
- **dead-letter** — out of scope for Phase 0. Close with `--status escalated`.
- **LLM fallback** — no rule matched. Classify from this table yourself, pass
  `--classified-by llm --failure-class <CLASS>` when patching, and treat the
  stderr as a candidate new rule for `src/classify.py`.

Watch the `classified_by` distribution over the corpus run: dense `"llm"` means
the rule table is under-built, which is the cheapest available win.
