# envbuild hold-out pass `aks-holdout-1` — 2026-09-29

Image `mismplatform/pi-envagent:rev4.8@sha256:374ca230…` (= rev 4.7 tree, byte-checked), agent `gpt-5.6-luna`,
20 Jobs sequential, **4 h 05 min**. 19 models ran on repo evidence alone (no annotation); `viva-tumor-tcell`
carried the platform's annotation. Protocol: **no fixes between models**; every failure sorted below.

## Headline

| metric | hold-out (unseen, 1 pass) | tuning corpus (7 models, pass 7) |
|---|---|---|
| build success (buildable) | **1/19 = 5 %** (95 % CI 1–25 %) | 4/5 = 80 % |
| verdict correctness | 1/20 (control correct on status, wrong class) | 6/7 |
| false-verified | **0** | 0 |
| LLM-fallback share | 44.8 % | 0 % |
| infra retries (uncharged) | 15 — **all spurious** (see H1) | 0 |
| `failed_step_index` coverage | 97 % | 100 % |

The pre-registered expectation was one-half to two-thirds. The result is 1/19, and the reason is concentrated:
**one classifier rule closed 7 of 19 jobs as `error`** — four of them in about a minute with no repair attempted, three (viva-cpm, viva-simbio, viva-amici) after one or two unrelated repairs, when the first genuinely missing distribution was reached. Remove those
seven and the picture is the one the hold-out was designed to produce — a list of families envbuild has never
handled, each an honest escalation.

## What happened, sorted four ways

Buckets: **harness bug** (envbuild's own code, wrong on any repo) · **new mechanism** (a family the
7-model corpus never contained) · **repo defect** (the repository itself) · **rule-table entry** (a fact one
tool emits that the table does not yet parse). Counts over the 18 non-success buildable models:
harness bug 8, new mechanism 5, rule-table entry 3, repo defect 2.

| model | verdict @ rung | min | bucket | cause |
|---|---|---|---|---|
| `catalyst-jl` | escalated @ L0 | 1 | **control ✓** | Julia → `escalated` at L0 (UNKNOWN); `julia:1.10` base was chosen but the toolchain is unsupported. Correct status; class should be UNSUPPORTED_TOOLCHAIN (ground truth expects it) |
| `biosimulators_tellurium` | error @ L0 | 2 | **harness bug** | `SWITCH_INSTALLER mamba` rendered `micromamba install …` on `python:3.11-slim`, where micromamba does not exist → exit 127. The renderer allows a pkg_manager the base does not provide; also the agent spent 2 attempts on `docker^7.1.0` (a Poetry caret spec the renderer passed to pip verbatim) |
| `c302` | error @ L0 | 1 | **harness bug** | same rule, `SpreadsheetDataReader` — a module vendored inside the repo, mis-scanned as third-party |
| `covasim` | error @ L0 | 1 | **harness bug** | same rule, `ConnPlotter==0.7a0` — undeclared-import guess that is not on PyPI (module lives in the repo's own `data/` tooling) |
| `viva-amici` | error @ L0 | 9 | **harness bug** | same rule, `SuiteSparse` — a system library name that surfaced as a pip spec; the real need is `libsuitesparse-dev` + a C++ toolchain (AMICI compiles models at run time) |
| `viva-cpm` | error @ L0 | 1 | **harness bug** | `infra_network` matched pip's `(from versions: none)` for `viva-tellurium==0.1.0` (a git-sourced sibling pinned from uv.lock); no network fault; job closed `error` in 1 min |
| `viva-membrane-actin-composite` | error @ L0 | 1 | **harness bug** | same rule, `pbg-readdy==0.1.0` (git source) |
| `viva-mgen` | error @ L0 | 1 | **harness bug** | same rule, `viva-parsimony` (git source in `[tool.uv.sources]`) |
| `viva-simbio` | error @ L0 | 1 | **harness bug** | same rule, `libsbml` — import name emitted by the undeclared-import scan; the distribution is `python-libsbml` |
| `covid-sim` | failed @ L1 | 4 | **new mechanism** | C++ CMake project; `guess_base` picked `rocker/r-ver` (an R script in `data/`), then `python` was not found on that base; no C++ toolchain base exists in `baseselect`. The `probe` expectation met, but the *verdict* `BASE_IMAGE_MISMATCH` after 5 attempts is a builder gap |
| `covid19model` | failed @ L0 | 3 | **new mechanism** | conda `environment.yml` with R/Stan: `SWITCH_INSTALLER conda` rendered a solve on `miniconda3:latest` that failed (unsat `_r-mutex`); no entry point in repo either. First conda repo ever seen |
| `physicell` | escalated @ L2 | 2 | **new mechanism** | C++ `make` project; the only README command is `python beta/get_physicell.py` which **downloads** the release (`Temporary failure in name resolution` under the deny-all network policy). Needs a `make` toolchain base + an offline example |
| `viva-artistoo` | escalated @ L2 | 15 | **new mechanism** | environment built; the demo runs `npm install github:ingewortel/artistoo` at run time → second runtime (node) plus egress from the verify pod. `probe` expectation met: honest `escalated` |
| `viva-munk` | error @ L2 | 61 | **new mechanism** | environment correct (L2 passed, mount fixed); the example `experiments.test_suite` did not finish in 1800 s even after two `SET_L3_TIMEOUT`s — L3 max reached. Verdict `error` is wrong: this is a slow example, i.e. `failed`/TIMEOUT with `l3_timeout_max_s` hit, and the repo should be asked for a smoke-tier example |
| `viva-biofilm` | escalated @ L1 | 16 | **repo defect** | `ImportError: cannot import name 'biofilm_core' … (partially initialized module)` — the Rust extension is not built by `pip install .` without maturin; agent switched install mode 5×. Partly M2 (compiled-extension build deps) but the circular import is the repo's |
| `viva-yalla` | escalated @ L2 | 14 | **repo defect** | environment correct; `demo_report.py` ends with `subprocess.run(['open','-a','Safari', path])` — a macOS-only browser launch after writing the report. Escalated honestly; the fix is upstream (guard the `open` call) or an annotation entry with `--no-open` |
| `epidemics` | failed @ L1 | 16 | **rule-table entry** | same as epimodel |
| `epimodel` | failed @ L1 | 22 | **rule-table entry** | R package: agent chose `Rscript -e 'testthat::test_local()'`, refused by grounding (`not_a_command` — inline `-e` is not a script). R packages need an R-specific example shape: `R CMD check`/`testthat::test_local()`/a vignette |
| `starsim` | failed @ L1 | 10 | **rule-table entry** | no README run command; agent grounded `pytest test_*.py -n auto …` from CI, and `SET_ENTRYPOINT` grounding rejected the glob (`/model/test_*.py` not a file). Annotation would resolve it; also: grounding should accept a glob that matches ≥1 file |
| `viva-tumor-tcell` | verified @ L3 | 37 | **verified** | annotation entry `studies/tumor-microenvironment/sims/run.py`; 4 charged attempts, 37 min (installed mode, output harvest worked — rev 4.7 live-tested) |

## Findings, by weight

**H1 — `infra_network` is too broad (7 jobs).** The rule keys on pip's `(from versions: none)`. Pip prints that
whenever *no distribution exists* — a typo, a git-sourced sibling, an import-name guess — not only during an
outage. Every one of the 15 "infra retries" this pass was a real dependency problem misrouted to the
substrate plane, where patching is refused by design; once the rule fired, no further repair was possible and the job closed `error` (c302, covasim, viva-membrane-actin-composite, viva-mgen: 1 min, 0 charged attempts; viva-simbio: 1 charged; viva-cpm: 2 charged; viva-amici: 2 charged, 9 min). The rule
needs a genuine network marker (`Temporary failure`, `NewConnectionError`, `Could not fetch URL`, registry
`dial tcp`); absent one, `No matching distribution found for X` is `MISSING_DEPENDENCY` with X as evidence.
*This is the single fix that changes the number the most, and it is a one-line rule change plus tests built from
these seven logs.*

**H2 — the undeclared-import scan emits guesses that are not distributions (3 jobs).** `libsbml` (dist is
`python-libsbml`), `ConnPlotter`, `SpreadsheetDataReader` (both vendored inside the repos). The scan must treat
any module resolvable from the repo tree as local (it missed `data/` and non-package dirs) and should only pin
names that resolve on the index — or emit them as *findings* for the agent rather than as `pkg_specs`.

**M1 — git/path-sourced dependencies (3 jobs).** `[tool.uv.sources]` / `uv.lock` `source = { git = … }` entries
were pinned as PyPI specs. The lockfile reader must carry the source, and the renderer must install
`name @ git+URL@rev` (which also needs `git` in the base and egress at L0 — a policy question for the platform).

**H5/H6 — renderer accepts what the base cannot run (1 job).** `SWITCH_INSTALLER mamba` on `python:*-slim`
renders `micromamba install` → exit 127; Poetry's `docker^7.1.0` was passed to pip verbatim. `patch.apply`
should refuse a pkg_manager the base image does not provide, and `evidence` should normalise caret/tilde specs.

**H3 — an example that outruns `l3_timeout_max_s` is `failed`, not `error` (1 job).** viva-munk's environment
is correct; its documented example is a full test suite that needs more than 30 minutes. The verdict should
be `failed/TIMEOUT` with the max recorded, and the correction proposal should ask for a smoke-tier example.

**H4 — entry-point grounding needs two more shapes (3 jobs).** A CI glob that matches files (`test_*.py`) and R's
inline form (`Rscript -e 'testthat::test_local()'`) are both legitimate; grounding rejected both. Both R packages
otherwise reached L1 with a correct base — the R family is closer than it looks.

**M2–M5 — never-seen families (5 jobs).** Compiled extensions needing build deps (viva-biofilm), a second
runtime at run time (viva-artistoo, node + egress), C++ toolchain bases (covid-sim, PhysiCell), conda
environments (covid19model). viva-amici belongs to the compiled family too but is counted under H1 above, because the rule closed it before the family could be exercised. Verdict *statuses* here were defensible (escalated/failed); the *classes* were not always right — covid-sim ended as `BASE_IMAGE_MISMATCH` after five attempts and physicell as `RUNTIME_ERROR` and covid19model as `UNKNOWN` — and the scorer counts each as incorrect. Each is a design item, not a bug. PhysiCell also shows a policy edge: its only documented command downloads a release,
which the deny-all network policy correctly blocks.

**Repo defects (2).** viva-yalla's demo calls macOS `open` after writing its report (the *environment was
correct*); viva-biofilm imports its Rust core before it is built.

## Honest reading

- 1/19 is the builder's **generalisation number today**, and it stands as measured. It is dominated by one
  bug that the tuning corpus could never have exposed (all seven corpus repos had every dependency on PyPI).
- Excluding H1's seven jobs — i.e. *had that rule been right* — the remaining 12 would have produced at most
  2–4 more verifications (starsim, epimodel/epidemics with an R entry shape, viva-munk with a smoke example).
  The compiled/conda/second-runtime families (5) are real design work. So the realistic ceiling for this
  revision on this set is ~5–6/19, not the 10–13 predicted; the prediction was too optimistic about
  dependency hygiene in the wild.
- **0 false-verified** held. The verification ladder did what it must: nothing wrong was called correct.
- The vivarium half revealed that ecosystem repos routinely depend on each other via git sources; the
  platform will meet this everywhere, so M1 is a priority alongside H1.
- rev 4.7's output harvest was exercised live for the first time (viva-tumor-tcell, installed mode, outputs
  written): it worked.

## Next

1. Fix H1, H2, H5/H6, H3 offline with regression checks built from these logs (each is a tool message, not a
   repo idiosyncrasy) — then **re-run the same 20 once** as `aks-holdout-2`. The delta is the value of the fixes.
2. Design items M1 (git sources) and M4 (C++ toolchain base) next; M5 (conda) and M3 (second runtime) after.
3. Pass B with the platform annotator's `metadata-package/` for all 20, which resolves H4 and viva-munk's
   example shape by construction.

Records: `attempts_aks-holdout-1.jsonl` / `verdicts_aks-holdout-1.jsonl`; score: `score_aks-holdout-1.txt`.
