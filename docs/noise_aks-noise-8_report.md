# envbuild search-noise run `aks-noise-8` — 2026-11-13

Image `mismplatform/pi-envagent:rev4.18@sha256:b6ddaad0…` (= tree at `06c0b0a`, hash-probed; pi 1.0.4), agent
`gpt-5.6-luna`. Canary first: `bench:mbmm` verified at L3 in one attempt (`gate-rev4.18`, 183 s). Then three
repetitions (`aks-noise-8-r1`, `-r2`, `-r3`) of nine repos, each repetition sequential, the three running side by
side, **memory off** (`ENVBUILD_NO_MEMORY=1`) so every trial is a search from the draft. No fixes between models.
Purpose: measure how much of a single-trial result is chance, and what rev 4.18 changed. Not a pass: nine repos, not
nineteen, and all nine were chosen because they were within reach. Raw rows:
`docs/records/{attempts,verdicts}_aks-noise-8.jsonl`; scores (current scorer) in `score_aks-noise-8.txt`.

## Result

**21 of 27 trials succeed (r1 8/9, r2 7/9, r3 6/9). The builder reported 23 `verified`; two of those ran a
maintenance script, not the model, and are not counted.** No infra retries, no timeouts at the Job level.

| model | r1 | r2 | r3 | successes | attempts | note |
|---|---|---|---|---|---|---|
| viva-cpm | ok | ok | ok | 3/3 | 1, 1, 1 | was 4 attempts in rerun 7: the draft now picks 3.13, the compiler and the output path itself |
| viva-mgen | ok | ok | ok | 3/3 | 1, 1, 1 | |
| covasim | ok | ok | ok | 3/3 | 2, 2, 2 | same repair each time (`REMOVE_PKG distutils`, by rule) |
| c302 | ok | ok | ok | 3/3 | 2, 2, 2 | same repair each time (interpreter 3.10, by rule) |
| viva-tumor-tcell | ok | ok | ok | 3/3 | 2, 2, 2 | same repair each time (`SET_L3_TIMEOUT 1200`); ~31 min per trial |
| viva-amici | ok | ok | ok | 3/3 | 4, 5, 4 | first success in any run. One trial used the whole budget |
| viva-simbio | ok | ok | escalated L0 | 2/3 | 4, 4, 3 | first success in any run. r3: **upstream deleted a commit the lock pins**, between trials |
| viva-yalla | ok (alternate) | **lint script** | escalated L2 | 1/3 | 5, 4, 3 | README demo ends in macOS `open`; see below |
| viva-membrane-actin | escalated L1 | escalated L1 | **lint script** | 0/3 | 4, 4, 5 | harness defect, identical in all three; see below |

## What it says about noise

Six of nine repos are deterministic at three trials: same outcome and, for five of them, the same repair chain by
the same rule. Where the rule table decides, there is no noise. All the variance is in the three repos where the
model decides: viva-amici (which nonexistent-in-effect `amici` pin it tries first; 4 or 5 attempts), viva-yalla
(three different endings from the same start) and one outside event (viva-simbio). So a single trial is a fair
reading for a repo that resolves by rule, and close to a coin for one that reaches the fifth attempt.

## The two false claims (the important finding)

`viva-yalla` r2 and `viva-membrane-actin` r3 closed `verified` on `python scripts/lint-workspace.py` and
`bash scripts/check-no-local-paths.sh`. In both the README demo was out of reach, the model used `SET_ENTRYPOINT`
to move to a file that exists and exits 0, and `grounded()` accepted it because the file exists. The verdict rows
record the command honestly, and the scorer's honesty checks (lockfile, digest, shims) passed, so the scorer counted
both as "alternate shipped example". **This is the first time the builder has claimed a success that is not one**;
passes 1–6 report 0 false-verified, and that figure only held because no trial had tried this before.

Fixed in the tree (rev 4.20):

- `examples.helper_reason`: a script under `scripts/`, or with a maintenance name (`lint`, `setup`, `deploy`, … —
  the list layout discovery already skips), is not an example unless the root README presents that exact command.
- `SET_ENTRYPOINT` to such a script is refused, uncharged.
- `entry_tier` records `helper` for one that got through; the scorer does not count `helper`, and recomputes it from
  the command for rows written before the tier existed (so these two rows score correctly now).

The yalla r1 success ran `demo/composite_report.py` (a shipped demo; wrote `demo/yalla-springs-demo-report.html`).
It counts under the 2026-10-07 alternate-example decision and is flagged for a human to confirm in the ground truth.

Also seen, not fixed: `outputs_written` for the lint run lists `demo/report.html`, written by an *earlier* attempt's
demo. Output harvesting does not start from a clean copy, so it can credit one attempt with another's files.

## viva-membrane-actin: rollback discards the repair it needs

All three trials: npm (rule) → demo imports `pbg_mem3dg` → `ADD_PKG pbg_mem3dg` (rule; rev 4.18 renders the
declared git source) → its dependency `pymem3dg` needs a C++ compiler, build fails at L0 → the model adds `g++`.
Because L0 is below the best rung (L1), the driver rolled the spec back to the best one — the spec *without*
`pbg_mem3dg` — applied `g++` to it, and forbade `ADD_PKG pbg_mem3dg`. Attempt 4 then fails exactly as attempt 2 did,
with nothing left to try. Fixed: an `ADD_APT_PKG` that follows a broken build is applied to the regressed spec
(`driver.follows_through`). `no_compiler` also learned CMake's wording, so the compiler is a rule repair, not a
model one.

## viva-simbio r3: upstream moved

`fatal: remote error: upload-pack: not our ref 00c91d28…` for `pbg-emitters`, a commit pinned by viva-simbio's
`uv.lock`. r1 and r2 fetched it at ~10:44 and ~10:51; r3 failed at ~10:54 and it is still gone. The repo at its
pinned sha can no longer be built from its lock by anyone. This is the dependency-drift risk named in the critical
review, observed. It will fail in the next pass unless the lock-pinned commit is dropped in favour of the declared
branch — not built; it needs a decision (it means not honouring the lock).

## Sorted

| non-success | bucket | state |
|---|---|---|
| viva-membrane-actin ×3 | harness bug (rollback) + builder false claim | both fixed in tree |
| viva-yalla r2 | builder false claim | fixed in tree |
| viva-yalla r3 | repo defect (`open -a Safari`), then budget | open |
| viva-simbio r3 | upstream drift (deleted commit) | open, needs a decision |

Attempts are still spent where the model guesses: viva-amici's first one or two go to `PIN_PKG amici==…` when the
failure is a missing compiler inside a CMake sub-build (`compile_failed` proposes a pin). The log tail of those rows
does not show the CMake line, so no rule was written from it.
