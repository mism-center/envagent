# envbuild hold-out pass `aks-holdout-3` — 2026-10-02

Image `mismplatform/pi-envagent:rev4.10@sha256:704d5575…` (= rev 4.9 tree, verified byte-identical in-cluster),
agent `gpt-5.6-luna`, same 20 repos at the same pinned shas, same order, same annotation state (only
viva-tumor-tcell annotated), **no fixes between models**. 4 h 27 min. Scored with
`scripts/score.py --run-id aks-holdout-3`. This pass measures the rev 4.9 fixes (H7, H8, H11–H15).

## Headline

| metric | pass 1 (rev 4.7) | pass 2 (rev 4.8) | **pass 3 (rev 4.9)** |
|---|---|---|---|
| build success (19 buildable) | 1/19 = 5 % | 2/19 = 10.5 % | **6/19 = 31.6 %** (95 % CI 15–54 %) |
| false-verified | 0 | 0 | **0** |
| verdicts `error` | 9 | 0 | **1** (a real API outage mid-job) |
| infra retries | 15 (all spurious) | 0 | **1** (real: a registry `ErrImagePull` on viva-simbio's L1, uncharged, retried, then verified) |
| LLM-fallback share | 45.5 % | 45.7 % | **35.6 %** |
| median charged attempts | 2.5 | 4 | 3.5 |

**Movers, as predicted.** viva-simbio (H7), viva-amici (H11), covasim (H13) verified. epidemics builds and
passes L1 for the first time (LinkingTo). epimodel and covid19model are now honest `TIMEOUT`s on R from-source
compile (H8) instead of `UNKNOWN`. starsim verified on an agent-grounded test file. viva-yalla verified on an
alternate shipped demo after the README's demo failed on a macOS-only call — see the flag below.

**Not as predicted.** viva-cpm and viva-biofilm did not move: `PYTHONSAFEPATH=1` was rendered, but the
mounted-mode draft's `extra_path=['/model']` survived the switch to installed mode, and `PYTHONPATH=/model`
shadows the installed package regardless (H12b). And **c302 regressed** from pass 2's `verified`: the agent
chose `pytest` as the entry this time; a command with no script token is reported by the ladder as *"annotation
declares no entry point"* and the job dies at L2 even though the environment is correct (H16). The same path
took epidemics, covid-sim and the Julia control. H16 is the single largest remaining harness bug: four models,
and it makes a verification depend on which of two grounded entries the agent happens to pick.

## Per model

| model | pass 2 | **pass 3** | attempts | min | bucket | what the records say | ref |
|---|---|---|---|---|---|---|---|
| `viva-tumor-tcell` | verified@L3 None | **verified@L3 None** | 4 | 36 | verified | annotation entry; 4 attempts, 36 min — third identical chain | — |
| `viva-simbio` | failed@L2 ENTRYPOINT_UNKNOWN | **verified@L3 None** | 5 | 12 | verified | **new** — H7: write into `demo/` now gets `writable_copy`; L3 wrote `demo/report.html` | — |
| `viva-amici` | failed@L0 MISSING_DEPENDENCY | **verified@L3 None** | 5 | 21 | verified | **new** — H11: L1 no longer probes amici's vendored `SuiteSparse`/`sundials`; 5 attempts (amici pin, build toolchain, one ADD_PKG, mount fix); `demo/.amici_cache` written | — |
| `covasim` | failed@L0 DEP_RESOLUTION_CONFLICT | **verified@L3 None** | 4 | 8 | verified | **new** — H13: the frozen requirements file is ignored (27 specs, not 82); one REMOVE_PKG, one ADD_PKG, entry corrected to `examples/simple.py` | — |
| `starsim` | failed@L1 ENTRYPOINT_UNKNOWN | **verified@L3 None** | 3 | 7 | verified | **new** — no README command; the agent grounded `pytest tests/test_library.py` (file exists) and it ran clean. Ground truth had no example — human should confirm this counts as the repo's example | — |
| `viva-yalla` | escalated@L2 RUNTIME_ERROR | **verified@L3 None** | 4 | 14 | verified (flag) | README demo writes its report then calls macOS `open -a Safari` (repo defect, passes 1–2). Agent switched to `demo/composite_report.py`, a second shipped demo without the call; L3 exit 0, `demo/report.html` harvested. Environment correct; **verified on an alternate repo example** — recorded in `annotation-patch.yaml` | — |
| `viva-cpm` | escalated@L0 ABI_MISMATCH | **failed@L0 UNKNOWN_DISTRIBUTION** | 5 | 11 | harness bug | `PYTHONSAFEPATH=1` was in the image, but the spec kept `extra_path=['/model']` from the mounted-mode draft → `PYTHONPATH=/model` shadows the installed package anyway (`cannot import name 'cpm_core' … /model/cpm/__init__.py`). Then `tesbml` (R1) | H12b + R1 |
| `viva-biofilm` | failed@L1 BUILD_MODE_MISMATCH | **failed@L1 BUILD_MODE_MISMATCH** | 5 | 17 | harness bug | same signature (`partially initialized module 'viva_biofilm' (/model/viva_biofilm/…)`) — same cause | H12b |
| `c302` | verified@L3 None | **failed@L1 ENTRYPOINT_UNKNOWN** | 4 | 8 | harness bug | **regressed from pass 2.** Build fixed in 2 attempts (termcolor, 3.10) and L1 passed; the agent then chose `pytest` as entry. A command with no script token is `kind=none`, and the ladder reports it as *"annotation declares no entry point"* → ENTRYPOINT_UNKNOWN. Pass 2 happened to pick a `.py` example | H16 |
| `epidemics` | failed@L0 MISSING_DEPENDENCY | **failed@L1 ENTRYPOINT_UNKNOWN** | 2 | 8 | harness bug | **builds and passes L1 for the first time** (LinkingTo → BH/Rcpp/RcppEigen declared). Entry `Rscript -e 'testthat::test_local()'` is grounded (H4) but unprobeable → ENTRYPOINT_UNKNOWN | H16 |
| `covid-sim` | failed@L1 UNKNOWN | **failed@L1 ENTRYPOINT_UNKNOWN** | 4 | 3 | harness bug | `make test` — same: grounded, no script token, reported as no entry point | H16 |
| `catalyst-jl` | failed@L0 UNKNOWN | **failed@L1 ENTRYPOINT_UNKNOWN** | 2 | 1 | control | Julia; `julia --project=docs/ -e …` → same H16 path; status `failed` defensible | H16 |
| `viva-mgen` | escalated@L0 UNKNOWN_DISTRIBUTION | **escalated@L0 UNKNOWN_DISTRIBUTION** | 1 | 1 | new mechanism | git-sourced `viva-parsimony`; escalated at init in 1 attempt (identical to pass 2) | M1 |
| `viva-membrane-actin-composite` | escalated@L0 UNKNOWN_DISTRIBUTION | **escalated@L0 UNKNOWN_DISTRIBUTION** | 1 | 1 | new mechanism | git-sourced `pbg-readdy` + siblings; escalated in 1 attempt (identical) | M1 |
| `viva-artistoo` | escalated@L2 RUNTIME_ERROR | **error@L2 RUNTIME_ERROR** | 1 | 8 | infra | Node runtime (M3) on attempt 1; then the Kubernetes API refused connections while launching attempt 2 — a genuine substrate fault, `error` is the correct label (the driver faulted) | M3 |
| `physicell` | escalated@L2 RUNTIME_ERROR | **escalated@L2 RUNTIME_ERROR** | 3 | 3 | new mechanism | example downloads at run time; no egress in the verify pod (identical to pass 2) | M7 |
| `epimodel` | escalated@L0 UNKNOWN | **escalated@L0 TIMEOUT** | 1 | 10 | new mechanism | R from-source compile outran the 600 s build budget — now recorded **`TIMEOUT`/build_deadline** (H8 worked; pass 2: `UNKNOWN`), escalated in 1 attempt | M6 |
| `covid19model` | failed@L0 UNKNOWN | **escalated@L0 TIMEOUT** | 2 | 11 | new mechanism | conda-style R names → base change → R compile outran the build budget → `TIMEOUT` (H8) | M5 + M6 |
| `biosimulators_tellurium` | failed@L0 MISSING_DEPENDENCY | **escalated@L0 UNKNOWN_DISTRIBUTION** | 5 | 8 | rule-table / upstream | `tellurium` → `tesbml`: `UNKNOWN_DISTRIBUTION` after ADD/PIN attempts; escalated honestly | R1 |
| `viva-munk` | failed@L2 TIMEOUT | **failed@L2 TIMEOUT** | 3 | 54 | correct label | env correct through L2; example exceeds the L3 maximum → `failed`/TIMEOUT (identical to pass 2) | — |

## Findings by weight

**Harness bugs (2, affecting 6 models):**

- **H16 — bare commands are unprobeable and get reported as "no entry point" (c302, epidemics, covid-sim,
  catalyst-jl).** `entry_from_command` returns `kind=none` for `pytest`, `Rscript -e '…'`, `make test`,
  `julia -e '…'`; `climb` then emits `ENVBUILD_ENTRYPOINT_MISSING: annotation declares no entry point` at L2 —
  the message is false (an entry exists) and the class sends the agent to SET_ENTRYPOINT, burning its two
  changes. Fix: for `kind=none` with a non-empty command, record L2 as "not probeable — no script token" and
  proceed to L3; the run itself is the test.
- **H12b — installed mode must not put `code_path` on `PYTHONPATH` (viva-cpm, viva-biofilm).** The draft
  adds `extra_path=[code_path]` for mounted mode; when the mode is `installed` (draft or `SWITCH_INSTALL_MODE`),
  entries at or under `code_path` must be dropped, in `draft_spec`, in `patch.apply`, and in the renderer.

**Design items, escalated honestly (6 models):** M1 VCS install (viva-mgen, viva-membrane-actin-composite);
M3 Node (viva-artistoo, before the outage); M5 conda-style names (covid19model); M6 R from-source compile vs the
600 s build budget (epimodel, covid19model — binaries via P3M, or a per-language build budget);
M7 run-time egress (physicell).

**Rule-table / upstream (1):** `tellurium` → `tesbml` (biosimulators_tellurium; also cpm's attempts 4–5).
**Correct label (1):** viva-munk. **Infra (1):** viva-artistoo's second attempt.

## A flag for a human: viva-yalla and starsim

Both verified on an entry the **agent** chose (`entrypoint_source: agent`), grounded in a file the repo ships:
- viva-yalla: the README's `demo/demo_report.py` writes its report and then runs `open -a Safari` (macOS). The
  agent moved to `demo/composite_report.py`, a second demo in the same directory with no such call; it ran clean
  and `demo/report.html` was harvested. The environment is right; the README example is a repo defect; whether
  "an alternate shipped demo" satisfies the success definition is a policy call. `annotation-patch.yaml`
  carries both steps with outcomes.
- starsim: no README run command; `pytest tests/test_library.py` ran clean. Ground truth had `example: null`.

Neither is a false-verified in the sense that matters (the loop did not fake a pass — the models ran and wrote
outputs), but both are **verifications of something other than the README's headline example**, and the scorer
currently counts them. Suggest a ground-truth field `alternate_example_ok: true|false` per model.

## Repeatability (same image lineage, passes 1→2→3)

viva-tumor-tcell 4-4-4 attempts, identical chain. viva-mgen, viva-membrane-actin-composite, physicell,
viva-munk: identical verdicts and chains in passes 2 and 3. c302: 5 attempts → verified, then 4 attempts →
failed on the agent's entry choice — the one non-repeat, and it is H16.

## Next

1. Fix H16 and H12b offline (regression checks from these logs); both are small.
2. Rerun the same 20 once as `aks-holdout-4`. Expected movers: c302, epidemics, covid-sim (to a real L3 result),
   viva-cpm and viva-biofilm (to their next layer: `tesbml`; compiled `biofilm_core`).
3. Then M1 (VCS install) and M6 (R binaries) — together they cover five of the remaining models.
4. Pass B with annotator output.

## Artifacts

`score_aks-holdout-3.txt`, `attempts_aks-holdout-3.jsonl`, `verdicts_aks-holdout-3.jsonl`,
`holdout-3_sorted_failures.csv`, this report; cumulative change log `envagent_changes.md`.
