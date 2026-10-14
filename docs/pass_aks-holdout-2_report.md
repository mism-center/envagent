# envbuild hold-out pass `aks-holdout-2` — 2026-10-01

Image `mismplatform/pi-envagent:rev4.9@sha256:c7044788…` (= rev 4.8 tree, verified byte-identical in-cluster),
agent `gpt-5.6-luna`, same 20 repos at the same pinned shas as `aks-holdout-1`, same order, same annotation
state (only viva-tumor-tcell annotated), **no fixes between models**. 4 h 48 min. Scored with
`scripts/score.py --run-id aks-holdout-2`. This pass measures the rev 4.8 fixes (H1–H6) — the delta against
pass 1 is their value.

## Headline

| metric | `aks-holdout-1` (rev 4.7) | **`aks-holdout-2` (rev 4.8)** |
|---|---|---|
| build success (19 buildable) | 1/19 = 5 % | **2/19 = 10.5 %** (95 % CI 3–31 %) |
| false-verified | 0 | **0** |
| jobs closed `error` by the `infra_network` misfire | 7 | **0** |
| spurious infra retries | 15 | **0** |
| verdicts recorded `error` | 9 | **0** (all 20 are `verified`/`failed`/`escalated`) |
| LLM-fallback share | 45.5 % | 45.7 % |
| median charged attempts | 2.5 | 4 |

**What changed.** c302 verified — it had died in one minute in pass 1 on the misfiring rule; with the rule
fixed it took five attempts and reached L3 with outputs. The seven H1 jobs all reached a real
classification; three of them (viva-mgen, viva-membrane-actin-composite, viva-amici) now stop on an honest
`UNKNOWN_DISTRIBUTION` → escalated for git-sourced siblings, which is the correct verdict for a revision that
cannot install from VCS. viva-munk is now `failed` instead of `error` (H3). biosimulators_tellurium got past the
exit-127 installer and the caret spec (H5, H6) to a new, real blocker. The median attempt count rose
because jobs that used to die in a minute now spend their budget — which is what exposed the next layer.

**What did not change.** The LLM-fallback share is unchanged at 44 %: the hold-out's error shapes are still
mostly ones the rule table has never seen (R plural dependency errors, Kaniko deadline kills, conda-style
names, Julia). The rate is far below the tuning corpus and below my pre-registered guess; the sorted list
below is the result, not the number.

## Per model

| model | pass 1 | **pass 2** | attempts | min | bucket | what the records say | ref |
|---|---|---|---|---|---|---|---|
| `viva-tumor-tcell` | verified@L3 None | **verified@L3 None** | 4 | 38 | verified | annotation entry; 4 attempts, 38 min — identical chain to pass 1 (mount fix, SET_L3_TIMEOUT) | — |
| `c302` | error@L0 INFRA_UNAVAILABLE | **verified@L3 None** | 5 | 19 | verified | **new** — H1 fix let it past L0: `cannot_import_name`→Python 3.10, two SET_ENTRYPOINT (pytest, then the parametersweep example), writable_copy; L3 wrote `examples/parametersweep/*.json` | — |
| `viva-cpm` | error@L0 INFRA_UNAVAILABLE | **escalated@L0 ABI_MISMATCH** | 4 | 13 | harness bug | build now succeeds (3.13 + build-essential; Rust core compiled). L1 then imports `cpm` from the **source tree at /model**, which shadows the installed package and lacks the compiled `cpm_core` | H12 |
| `viva-mgen` | error@L0 INFRA_UNAVAILABLE | **escalated@L0 UNKNOWN_DISTRIBUTION** | 1 | 0 | new mechanism | `vcs_dependency` finding at init (viva-parsimony from git); escalated in 1 attempt with the URL in the reason — the honest verdict for this revision | M1 |
| `viva-munk` | error@L2 TIMEOUT | **failed@L2 TIMEOUT** | 3 | 55 | correct label | env correct through L2; the test-suite example exceeded 600→1200→max. Now `failed`/TIMEOUT (pass 1: `error`) — H3 worked | — |
| `viva-membrane-actin-composite` | error@L0 INFRA_UNAVAILABLE | **escalated@L0 UNKNOWN_DISTRIBUTION** | 1 | 1 | new mechanism | same: `pbg-readdy` and 7 siblings from `[tool.uv.sources]`; escalated at L0 with UNKNOWN_DISTRIBUTION | M1 |
| `viva-artistoo` | escalated@L2 RUNTIME_ERROR | **escalated@L2 RUNTIME_ERROR** | 1 | 5 | new mechanism | `Node.js is required … node was not found on PATH` — second runtime | M3 |
| `viva-yalla` | escalated@L2 RUNTIME_ERROR | **escalated@L2 RUNTIME_ERROR** | 5 | 18 | repo defect | `[Errno 2] No such file or directory: 'open'` — macOS-only call in the demo (same as pass 1) | — |
| `viva-simbio` | error@L0 INFRA_UNAVAILABLE | **failed@L2 ENTRYPOINT_UNKNOWN** | 5 | 10 | harness bug | README example `python demo/demo_report.py` exists; the path-aware mount repair chose `output_path=/model/demo`, mounting an empty volume **over the directory that holds the entry script** → ENTRYPOINT_MISSING | H7 |
| `viva-amici` | error@L0 INFRA_UNAVAILABLE | **failed@L0 MISSING_DEPENDENCY** | 5 | 16 | harness bug | L1 probes every name in amici's `top_level.txt` (`SuiteSparse`, `sundials` — vendored build dirs, not modules); the loop chased them as packages. `vivarium-workbench` is also git-sourced (M1) | H11 + M1 |
| `viva-biofilm` | escalated@L1 BUILD_MODE_MISMATCH | **failed@L1 BUILD_MODE_MISMATCH** | 5 | 17 | harness bug | same shadowing at L2: `cannot import name 'biofilm_core' from partially initialized module 'viva_biofilm' (/model/viva_biofilm/…)` — the compiled extension is in site-packages, the source dir is first on sys.path | H12 |
| `covasim` | error@L0 INFRA_UNAVAILABLE | **failed@L0 DEP_RESOLUTION_CONFLICT** | 5 | 2 | harness bug | `tests/requirements_frozen.txt` (a pip freeze of a NEST install: `ConnPlotter`, `PyNEST`, `Topology==2.16.0`, 80 pins) was merged into the declaration although `pyproject.toml` declares the real deps → UNKNOWN_DISTRIBUTION ×2, idna pin conflict | H13 |
| `starsim` | failed@L1 ENTRYPOINT_UNKNOWN | **failed@L1 ENTRYPOINT_UNKNOWN** | 2 | 4 | annotation needed | no README run command; SET_ENTRYPOINT attempted once, then ENTRYPOINT_UNKNOWN — by design without an annotation | — |
| `physicell` | escalated@L2 RUNTIME_ERROR | **escalated@L2 RUNTIME_ERROR** | 3 | 3 | new mechanism | installed + mount fix, then L3: `Temporary failure in name resolution` — the example **downloads at run time** inside the no-egress verify pod. Policy question, not a bug | M7 |
| `epimodel` | failed@L1 ENTRYPOINT_UNKNOWN | **escalated@L0 UNKNOWN** | 2 | 21 | harness bug | two L0 builds killed at the 600 s build deadline while compiling Rcpp/robustbase from source — recorded `UNKNOWN`, not `TIMEOUT` (no deadline text in a Kaniko kill). Underneath: R from-source compile needs binaries or a bigger build budget | H8 + M6 |
| `epidemics` | failed@L1 ENTRYPOINT_UNKNOWN | **failed@L0 MISSING_DEPENDENCY** | 5 | 14 | harness bug | R: `dependencies ‘BH’, ‘Rcpp’, ‘RcppEigen’ are not available` — the plural form is not in the rule table (3 LLM attempts); then `PIN_PKG BH==1.75.0` rendered as an R package named `BH==1.75.0` | H15 |
| `covid-sim` | failed@L1 BASE_IMAGE_MISMATCH | **failed@L1 UNKNOWN** | 4 | 9 | harness bug | mixed C++/R/Python repo drafted a rocker base; the grounded entry `python data/run_sample.py` was then **parsed by R** at L2 (probe chosen by pkg_manager, not by the entry's language) → `Error in parse(…)` | H14 |
| `biosimulators_tellurium` | error@L0 UNKNOWN | **failed@L0 MISSING_DEPENDENCY** | 5 | 11 | rule-table / upstream | H5/H6 worked (no exit-127, no caret). Now: `tellurium` imports `tesbml`, a legacy dist with no wheel for 3.11; 5 attempts of ADD/PIN/UNPIN around it | R1 |
| `covid19model` | failed@L0 UNKNOWN | **failed@L0 UNKNOWN** | 5 | 4 | new mechanism | conda-style `r-base`, `r-abind`, `r-BH` dependency names; 5 LLM-classified attempts (base change, installer switch, two REMOVE_PKG) | M5 |
| `catalyst-jl` | escalated@L0 UNKNOWN | **failed@L0 UNKNOWN** | 2 | 1 | control | Julia; `failed@L0 UNKNOWN` ×2 (pass 1: escalated). Status defensible, class not — no Julia rules exist | — |

## Findings by weight

**Harness bugs (7, affecting 8 models) — each a tool's message or a contract, not a repo:**

- **H12 — installed-mode source shadowing (viva-cpm, viva-biofilm).** In `installed` mode the renderer
  `COPY`s the repo to `/model` *and* installs it; `workdir=/model` puts the source tree first on `sys.path`,
  so any package with a compiled extension imports the uncompiled source copy. Fix: `ENV PYTHONSAFEPATH=1`
  in installed-mode images (Python ≥ 3.11) and run the L1 probe from `/`. This is the compiled-extension
  family's blocker — it was never visible on the corpus because no corpus repo compiled anything.
- **H7 — mount repair over a code directory (viva-simbio).** `mount_denied`'s path-aware argument
  `output_path=<code>/<dir>` must refuse when `<dir>` contains source files or the entry script, and
  propose `writable_copy=true` instead.
- **H11 — L1 over-probing (viva-amici).** `top_level.txt` lists vendored build directories for some sdists.
  A distribution passes L1 if its canonical module imports; other top-level names are reported only when
  none import.
- **H13 — frozen requirements as declaration (covasim).** `*/requirements*.txt` must not be merged when
  `pyproject.toml`/`setup.py` declare dependencies; files under `tests/` or named `*frozen*`/`*freeze*`
  are evidence of an environment, not a declaration.
- **H14 — L2 probe by base, not by entry language (covid-sim).** Choose the probe from the entry's
  suffix; a `.py` entry on an R base is `no_interpreter` → `CHANGE_BASE_IMAGE`/`ADD_APT_PKG python3`.
- **H8 — build deadline unclassified (epimodel).** A Kaniko pod killed at `activeDeadlineSeconds` leaves
  no text; the driver knows `duration ≥ build_timeout_s` and must record `TIMEOUT` at L0.
- **H15 — R rule gaps (epidemics).** `dependencies ‘A’, ‘B’ are not available` (plural) → `MISSING_DEPENDENCY`
  with all names; `PIN_PKG` under `renv` must render `remotes::install_version()` or be refused.

**New mechanisms (5 models, plus viva-amici's second blocker) — design items, honestly escalated:** M1 install from git/path sources
(viva-mgen, viva-membrane-actin-composite, and viva-amici's `vivarium-workbench`); M3 a second runtime
(Node, viva-artistoo); M5 conda-style R names (covid19model); M6 R from-source compile time — binaries via
P3M or a per-language build budget (epimodel, epidemics); M7 run-time egress — physicell's example downloads
data; the verify pod has none by design, so this is a policy decision (allow-list, pre-fetch, or `escalated`).

**Rule-table / upstream (1):** `tellurium` → `tesbml` (biosimulators_tellurium; also hit viva-cpm's L1).
**Repo defect (1):** viva-yalla. **Annotation needed (1):** starsim. **Correct labels (2):** viva-munk
(`failed`, slow example), catalyst-jl (Julia control, status defensible, class `UNKNOWN`).

## Honest reading

- 2/19 verified. The CI (3–31 %) says nothing finer than "far from 90 %". The fixes bought exactly what they
  were predicted to buy — the seven misfires became real classifications — and one of those became a
  verification. The rest exposed a second layer, dominated by one bug (H12) that blocks the whole
  compiled-extension family, plus five design items.
- Nothing was fixed during the pass; every number above is from the records; no verdict is `error`.
- 0 false-verified across 40 hold-out trials and 49 corpus trials.

## Next

1. Fix H7, H8, H11–H15 offline with regression checks from these logs (each is a message or a contract).
2. Rerun the same 20 once as `aks-holdout-3`. Expected movers: viva-cpm, viva-biofilm (H12), viva-simbio (H7),
   covasim (H13), covid-sim (H14, to an honest base-mismatch at least), epidemics (H15 — if P3M binaries
   resolve the compile time, else M6).
3. Then M1 (VCS install) — it unblocks three vivarium repos and is the largest single design item.
4. Pass B with annotator output, when available.

## Artifacts

`score_aks-holdout-2.txt`, `attempts_aks-holdout-2.jsonl`, `verdicts_aks-holdout-2.jsonl`,
`holdout-2_sorted_failures.csv`, this report; cumulative change log `envagent_changes.md`.
