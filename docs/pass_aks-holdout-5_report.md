# envbuild hold-out pass `aks-holdout-5` — 2026-10-06

Image `mismplatform/pi-envagent:rev4.13@sha256:2b1e4efd…` (= rev 4.11 tree + harness fixes, verified byte-identical
in-cluster incl. the entrypoint; **pi 1.0.4** — every earlier pass ran pi 0.99.0), agent `gpt-5.6-luna`, same 20 repos,
same shas, same order, no fixes between models. 4 h 58 min. Scored with `scripts/score.py --run-id aks-holdout-5`
(scorer updated this pass: a verification on a test suite the agent fell back to is reported separately).

A first launch of this pass on `rev4.12` was aborted: pi had been installed unpinned, 1.0.4 renamed the Azure
provider id, all 20 Jobs died before `init`. See the change log.

## Headline

| metric | p1 | p2 | p3 | p4 | **p5** |
|---|---|---|---|---|---|
| build success (19 buildable) | 1/19 | 2/19 | 5/19* | 4/19 | **4/19 = 21.1 %** (CI 8-43 %) |
| verified on a test suite only (not counted) | 0 | 0 | 1* | 0 | **1** (viva-yalla) |
| false-verified | 0 | 0 | 0 | 0 | **0** |
| `error` verdicts | 9 | 0 | 1 | 1 | **0** |
| LLM-fallback share | 45.5 % | 45.7 % | 32.3 % | 38.3 % | **40.3 %** |
| agent runtime | pi 0.99.0 | 0.99.0 | 0.99.0 | 0.99.0 | **pi 1.0.4** |

\* pass 3 re-scored under the new rule: 6 → 5 (starsim's `pytest`); viva-yalla's alternate demo stays counted there
because the tier heuristic cannot see that `composite_report.py` was not the README's example — the
`alternate_example_ok` ground-truth field is still the right fix.

**The stable figure is 4/19 for the third pass running** — the same four repos, the same chains. The agent runtime
change did not move it. Everything else in this pass is the next layer being reached, which is what the fixes
were for:

- **H18** (R install fails loud) carried epidemics to **L3 for the first time**; it now fails inside
  `testthat::test_local()`'s compile step.
- **H12c** (pytest importlib) carried viva-biofilm through its test suite to the **study itself**, which ran until
  plotly asked for Chrome.
- **H19** held: the Julia control reached an honest `escalated` instead of crashing `init`.
- **H17** was not exercised: c302 did not reach L3 this time (see below).
- **R2 was disproved**: the `tellurium<2.2.11` pin installed and 2.2.10 still imports `tesbml`. Reverted to an honest
  escalation carrying the evidence. One corpus-sourced entry, and it was wrong — the right lesson about adding rules
  from a single observation.

## Per model

| model | pass 4 | **pass 5** | attempts | min | bucket | what the records say | ref |
|---|---|---|---|---|---|---|---|
| `viva-tumor-tcell` | verified@L3 None | **verified@L3 None** | 4 | 37 | verified | 4 attempts, 37 min — fifth identical chain; first on pi 1.0.4 | — |
| `viva-simbio` | verified@L3 None | **verified@L3 None** | 5 | 10 | verified | repeat (5 attempts) | — |
| `viva-amici` | verified@L3 None | **verified@L3 None** | 5 | 23 | verified | repeat (5 attempts) | — |
| `covasim` | verified@L3 None | **verified@L3 None** | 4 | 8 | verified | repeat (4 attempts) | — |
| `viva-yalla` | escalated@L2 RUNTIME_ERROR | **verified@L3 None** | 5 | 15 | verified on tests only | README demo (macOS `open`) → alternate demo (needs composites the repo lacks) → **`pytest`**, the agent's second and last entry change; 20 unit tests pass, `.pytest_tmp` harvested. Environment proven, example not. Now reported as `verified_tests`, not success | H20 |
| `c302` | escalated@L2 RUNTIME_ERROR | **escalated@L0 MISSING_DEPENDENCY** | 3 | 2 | agent variance / rule gap | **pass 4's repair (Python 3.10) was not retried**; the agent pinned `owmeta_core` instead and escalated with 2 attempts left. Root cause is a legacy `termcolor~=1.1.0` sdist whose `setup.py` imports termcolor during metadata generation — `ADD_PKG termcolor` cannot help inside pip's isolated build. Classifier said MISSING_DEPENDENCY | R3 |
| `epidemics` | escalated@L2 MISSING_DEPENDENCY | **escalated@L2 RUNTIME_ERROR** | 4 | 21 | next layer | **H18 worked**: the silent R install failed loud → `libuv1-dev` → testthat installed → **L3 reached for the first time**. `testthat::test_local()` calls `pkgbuild::compile_dll` and the compile fails; the captured tail does not show the compiler's error | — |
| `viva-biofilm` | escalated@L2 BUILD_MODE_MISMATCH | **escalated@L2 RUNTIME_ERROR** | 5 | 19 | new mechanism | **H12c worked**: `pytest` ran without shadowing; the agent moved to the study script, `writable_copy`, and the study ran until plotly/kaleido demanded **Chrome** for static image export | M8 |
| `starsim` | escalated@L2 RUNTIME_ERROR | **escalated@L2 MOUNT_CONTRACT_ERROR** | 4 | 9 | harness bug | `writable_copy` was applied but `extra_path=/model` (set one attempt earlier) kept `tests` importing from the read-only mount: rootdir `/scratch/model`, write to `/model/tests/temp` | H12d |
| `viva-cpm` | failed@L0 MISSING_DEPENDENCY | **failed@L0 UNKNOWN_DISTRIBUTION** | 5 | 13 | rule disproved | R2's `tellurium<2.2.11` pin applied (2.2.10 installed) — **still imports `tesbml`**. Also the init entry was the README's `python -m venv … && source …` line | R2, H22 |
| `biosimulators_tellurium` | failed@L0 MISSING_DEPENDENCY | **failed@L0 DEP_RESOLUTION_CONFLICT** | 5 | 11 | rule disproved | same `tesbml`; plus the agent pinned `setuptools==69.5.1` into a conflict with `biosimulators-utils` (needs ≥74.1.1) | R2 |
| `catalyst-jl` | error@L0 None | **escalated@L2 RUNTIME_ERROR** | 3 | 2 | control | `escalated` (correct status). H19 held: init no longer crashes. The L1 probe for `pkg_manager=pkg` runs the **R** probe, so the agent added `r-base` to a Julia image | H23 |
| `covid-sim` | failed@L2 UNKNOWN | **escalated@L2 UNKNOWN** | 5 | 4 | new mechanism | needs the compiled C++ `CovidSim` binary (identical to pass 4) | M2 |
| `viva-mgen` | escalated@L0 UNKNOWN_DISTRIBUTION | **escalated@L0 UNKNOWN_DISTRIBUTION** | 1 | 0 | new mechanism | git-sourced dep (identical ×4) | M1 |
| `viva-membrane-actin-composite` | escalated@L0 UNKNOWN_DISTRIBUTION | **escalated@L0 UNKNOWN_DISTRIBUTION** | 1 | 1 | new mechanism | git-sourced deps (identical ×4) | M1 |
| `viva-artistoo` | escalated@L2 RUNTIME_ERROR | **escalated@L2 RUNTIME_ERROR** | 4 | 12 | new mechanism | Node runtime (identical) | M3 |
| `physicell` | escalated@L2 RUNTIME_ERROR | **escalated@L2 RUNTIME_ERROR** | 3 | 2 | new mechanism | run-time download, no egress (identical ×4) | M7 |
| `epimodel` | escalated@L0 TIMEOUT | **escalated@L0 TIMEOUT** | 1 | 10 | new mechanism | R compile > build budget → `TIMEOUT` (identical) | M6 |
| `covid19model` | failed@L0 UNKNOWN | **escalated@L0 TIMEOUT** | 2 | 10 | new mechanism | conda-style R names → R compile > build budget → `TIMEOUT` (H8; pass 4 was `failed`) | M5+M6 |
| `viva-munk` | budget_exhausted@L2 TIMEOUT | **budget_exhausted@L2 TIMEOUT** | 3 | 56 | budget | identical to pass 4 | — |

## Findings by weight

**Policy, now instrumented (H20).** viva-yalla was "verified" on `pytest` after two entry changes. The verdict now
carries `entrypoint_tier` (`headline` | `tests`), and the scorer reports `verified_tests` separately, attributed
`policy:alternate_example`. Ground truth still needs `alternate_example_ok` per model so a human, not a regex,
decides when a test suite is the example.

**Harness bugs (2, fixed in rev 4.12 tree):**
- **H12d** `writable_copy` left `extra_path` pointing at the read-only mount (starsim). Entries under `code_path` now
  move into the copy.
- **H22** `python -m venv .venv && source .venv/bin/activate` was accepted as an entry point at init (viva-cpm). Not
  fixed yet: `is_command` should reject environment-setup lines (`venv`, `source`, `activate`, `pip install`, `cd`).

**Agent variance (c302).** Pass 4 built c302 with `CHANGE_INTERPRETER_VERSION 3.10`; pass 5's agent pinned
`owmeta_core` instead and escalated with two attempts left. The underlying `metadata-generation-failed` on a legacy
sdist is a rule gap (**R3**): the classifier's `MISSING_DEPENDENCY → ADD_PKG termcolor` cannot work inside pip's
isolated build; two observations now say the typed move is an older interpreter. Not added as a rule yet — see R2.

**Harness note (H23).** `pkg_manager=pkg` (Julia) runs the R L1 probe. Julia is unsupported by design; the probe
should say so instead of asking for `r-base`.

**Design items (8 models):** M1 VCS install (mgen, membrane-actin); M2 compiled C++ (covid-sim); M3 Node (artistoo);
M5 conda names + M6 R compile budget (covid19model, epimodel); M7 egress (physicell); **M8 headless browser for
plotly/kaleido static export (viva-biofilm — new)**. **Budget (1):** viva-munk.

## Repeatability

tumor-tcell 4 attempts, identical chain, five passes, two agent runtimes. simbio/amici/covasim identical to pass 4.
Six design-item repos identical verdicts ×3–4. The variance is c302 (agent choice) and viva-yalla (entry fallback).

## Next

1. H22 and R3 offline; `alternate_example_ok` in ground truth (human-set: yalla, starsim).
2. **M1 VCS install** — unchanged recommendation; the largest remaining mechanism.
3. epidemics: read the full L3 log for the compiler error (the row's tail does not contain it) — it is one step from
   verifying.
4. Pass B with annotator output.
