# Repair — one failure → one typed action

You are called only when the rule table could not finish the job: either no rule
matched (`classification.classified_by == "llm"`), or a rule identified the class
but not an argument.

## Procedure

1. **Read the attribution first, not the stderr.** `failed_step` names the index,
   kind and *EnvSpec field* of the instruction that failed. `apt` means the
   problem is in `apt_packages`; `pkg` means `pkg_specs`. That collapses the
   action space before you read a single line of log.
2. **Read the rung.** `failed_rung` is `L0` (build), `L1` (image alone, no code
   mounted), `L2` (code mounted), or `L3` (short run). The same message means
   different things at different rungs — see `failure_taxonomy.md`.
3. **Assign a class** from `failure_taxonomy.md`. Use the exact spelling.
4. **Pick exactly one action** from `remediation_actions.md`, with the narrowest
   argument that could fix it.
5. **Apply it:**

```bash
envbuild patch --job-id <ID> \
  --action ADD_APT_PKG --arg libxml2-dev \
  --classified-by llm --failure-class MISSING_SYSTEM_LIB \
  --why "configure could not find libxml/parser.h at the apt step"
```

6. **Run the next attempt.** One action, then measure. Never patch twice in a row.

## Constraints

- **One action.** The driver refuses a second `patch` before the next `attempt`.
  If you were about to do two things, do the one that addresses the *attributed*
  step and let the next attempt tell you whether the second was needed.
- **Never repeat a forbidden triple.** `attempt` prints the job's `forbidden`
  list. A rejected patch means "that was already tried" — change the action or
  the argument, not the wording.
- **Do not fight a regression.** If your last action lowered the rung, the driver
  has already rolled back to the best spec. Take that as data: the direction was
  wrong.
- **`ADD_PKG` at L2 needs justification.** An import failure at L2 for a module
  the repo itself defines is a *mount* bug. Installing a same-named package from
  PyPI will look like it worked and will ship a container running the wrong code.
  Check `evidence.local_modules` before reaching for `ADD_PKG`.
- **Budget awareness.** `attempts_left` and `seconds_left` are in every attempt
  result. With one attempt left, prefer the highest-probability action over the
  most elegant one; if nothing is likely, close the job honestly rather than
  spending the last attempt on a guess.

## When to stop

Close the job instead of patching when:

| Situation | Verdict |
|---|---|
| Verified through L3 | `--status verified` |
| `ENTRYPOINT_UNKNOWN`, `MISSING_DATA_FILE`, `LICENSE_REQUIRED` | `--status failed`, reason names the missing thing |
| `UNSUPPORTED_TOOLCHAIN`, or `TIMEOUT`/`RUNTIME_ERROR` after a real repair | `--status escalated` |
| Budget gone | the driver writes `budget_exhausted` itself |
| Anything else went wrong | `--status error`, reason says what |

An honest `failed` verdict with a named cause is a *useful row*. A job that ends
without a verdict is the one failure mode that corrupts the dataset.

## Every fallback you handle is a missing rule

If you classified something the table missed, say so in the final summary: the
stderr pattern, the class, and the action. Growing `src/classify.py`'s table is
the main *output* of the corpus run, not just a means to it.
