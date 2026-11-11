# envbuild hold-out pass `aks-holdout-6b` — 2026-11-11

Image `mismplatform/pi-envagent:rev4.16@sha256:20320396…` (= rev 4.16 tree, hash-probed; pi 1.0.4), agent
`gpt-5.6-luna`, the same 20 repos in manifest order, sequential, 2 h cap, no fixes between models, memory empty for
every hold-out model. 3 h 37 min. Canary before launch: `bench:mbmm` verified at L3 in one attempt (`gate-rev4.16`).
Scored with `scripts/score.py --run-id aks-holdout-6b` (scorer updated after the pass: a trial whose `code_revision`
is not the pinned commit is void). Raw rows: `docs/records/{attempts,verdicts}_aks-holdout-6b.jsonl`; per-model
sort: `pass_aks-holdout-6b_sorted.csv`.

The first launch, `aks-holdout-6` on `rev4.15`, is void (Kaniko `--kaniko-dir`; see the change log).

## Headline

| metric | p4 | p5 | **p6b** |
|---|---|---|---|
| build success | 4/19 | 4/19 | **3/17 = 17.6 %** (CI 6–41 %) |
| void trials (source not the pinned commit) | 0 | 0 | **2** (c302, biosimulators_tellurium) |
| verified on a test suite only (not counted) | 0 | 1 | **1** (epimodel) |
| false-verified | 0 | 0 | **0** |
| `error` verdicts | 1 | 0 | **1** (viva-munk) |
| LLM-fallback share | 38.3 % | 40.3 % | **51.9 %** |

**Pre-registered 6–7/19; measured 3/17. The pass missed, and the harness is not the main reason.** Three things
decided it:

1. **One git-sourced dependency is not pip-installable.** Seven viva repos declare `vivarium-workbench` from git —
   four at `main`, three at a locked commit (`b8bd706`, `3b7d58a`). Neither form installs with pip: the package
   requires `viva-basic-processes` / `viva-marketplace`, which exist only as git sources inside *its own*
   `[tool.uv.sources]`, and `b8bd706`'s build hook needs an npm bundle. Six of the seven died at L0 on it (viva-cpm,
   viva-artistoo, viva-yalla, viva-amici, viva-membrane-actin, and viva-simbio's first two attempts); viva-biofilm
   got past it only because the model removed the package. Whether upstream also moved since October is not known
   from the records and does not need to be: the locked commits fail too.
2. **Passes 4–5 succeeded on viva-simbio and viva-amici through a substitution.** Their specs carried the bare name
   `vivarium-workbench`, which pip resolved from PyPI — not the git source the repo declares. Rev 4.14 (M1) follows
   the declared source, and that is what fails. Both are regressions in the count, not in correctness.
   viva-tumor-tcell and viva-mgen still verify with the bare name from PyPI (their manifests declare it that way).
3. **The models claim lost source files between 8 Oct and 11 Nov.** `bench-biosimulators_tellurium` is gone;
   `bench-c302` lost `.git`, the `c302/` package, `cells/` and 61 example files (458 of 2017). Nothing in the harness
   noticed. c302's `verified` verdict is on that damaged tree and is struck as void.

## Successes, audited by hand

| model | entry that ran | repairs | audit |
|---|---|---|---|
| viva-tumor-tcell | `python studies/tumor-microenvironment/sims/run.py` (annotation) | writable copy, `SET_L3_TIMEOUT 1200` | ran 937 s, wrote results + figures. Genuine. |
| viva-mgen | `python workspace/studies/fig2-growth/sims/run.py` (README) | writable copy | **new**: the documented study run, not `pytest` as in rerun-6. Wrote study outputs. Genuine. `viva-parsimony` comes from unpinned `main`. |
| covasim | `python examples/simple.py` (= ground truth) | `REMOVE_PKG distutils` | ran clean, prints only (`l3_claim`: no declared outputs). Genuine. |

Not counted:

- **c302 — void.** `verified` on `python examples/parametersweep/GenerateExamples.py`, `code_revision: unknown`. On the
  intact pinned tree `init` chooses the README's `python c302/c302_Pharyngeal.py B`; the script that ran does not
  import the package the checkout was missing. Counting it would have been an easy win.
- **epimodel — `verified_tests`.** First time it verifies at all (`testthat::test_local()`, 516 s). Policy: a test
  suite is not the example.

## Every non-success, sorted

| model | where it stopped | bucket | what it needs |
|---|---|---|---|
| viva-cpm | L0, `viva-marketplace` unresolvable | new mechanism | transitive git sources of a git dependency |
| viva-artistoo | L0, `viva-basic-processes` unresolvable | new mechanism | same (Node runtime is also unbuilt, by decision) |
| viva-yalla | L0, same | new mechanism | same |
| viva-amici | L0, same — **regression** | new mechanism | same; or keep an extras-only git dependency out of the draft |
| epidemics | L3, 255 tests pass, 9 need `wpp2024` | new mechanism | R `Remotes:` (git-sourced R dependency); tests tier in any case |
| viva-membrane-actin | L0, locked `vivarium-workbench@b8bd706` cannot build without the loom npm bundle | repo defect | upstream; its own skip flag still packages `loom/node_modules` |
| viva-simbio | budget, **regression** | repo defect | `uv.lock` is behind the code (`process-bigraph` 1.4.12 has no `composite_spec`); candidate rule: `cannot import name` from a lock-pinned package → `UNPIN_PKG` to the declared range (one observation, no rule yet) |
| viva-biofilm | L3, Kaleido needs Chrome | repo defect (design item) | not built, by decision |
| physicell | L3, downloads from `api.github.com` | repo defect (design item) | no run-time egress, by decision |
| viva-munk | `error` after two L3 timeouts | **harness bug** | the ServiceAccount token was read once; after 1 h the API returned 401 and the verdict became `error` instead of `failed`/TIMEOUT. Fixed in rev 4.17. The suite still exceeds 2400 s (O9). |
| covid-sim | L2, three attempts on a silent `StartError` | **harness bug** | (a) `init` chose the README's `make test` in a repo with no Makefile; (b) rocker base for a CMake/C++ repo; (c) `StartError` carried no cause — fixed in rev 4.17 (kubelet message). C++ binary is a design item regardless. |
| starsim | L3, pytest collection | **harness bug** | the L1 probe still imports module-less `mkl` / `intel-cmplr-lib-rt` (H24 passes offline, not live) — two attempts lost; `gitpython` needs the `git` binary (rule-table entry). Tests tier in any case. |
| covid19model | L0 | **harness bug** | micromamba base runs the apt layer as non-root; then a `python=3.14` conda pin conflicts with `r-base`. No entry point either (conda-style R names: design item). |
| catalyst-jl (control) | `failed`, expected `escalated` | **harness bug** | O10 is no longer cosmetic: the non-Julia L1 probe on `julia:1.10` is a `StartError`, and the verdict came out `failed`. |
| c302 | void | **harness bug** (source integrity) | restore the checkout; gate must check sources against the manifest |
| biosimulators_tellurium | no trial | **harness bug** (source integrity) | restore the checkout. In the voided first launch the agent ran this model id against *another model's directory* (`/models/cbea98f1-…`); `init` now refuses that (H30). |

Counts: new mechanism 5 · repo defect 4 (2 are design items) · harness bug 5 + 2 void · policy 1.

## What the pass says

- **One mechanism is worth four repos.** viva-cpm, viva-artistoo, viva-yalla and viva-amici stop on the same line:
  a git dependency whose own dependencies are git-sourced. `uv` resolves that from the dependency's
  `[tool.uv.sources]`; pip cannot. The evidence layer already reads the sibling's sources (the verdict reasons name
  them), so the draft could carry them. This is the next mechanism, ahead of README snippets / vignettes.
- **The model's judgement on the same failure was inconsistent**: `REMOVE_PKG vivarium-workbench` on viva-biofilm and
  viva-simbio, immediate `ESCALATE` on viva-amici, viva-artistoo and viva-yalla. Whether an extras-only dependency
  belongs in the draft is a fact about the repo (does the entry import it?) and should be decided in code.
- **LLM-fallback share rose to 52 %.** Most of the new fallbacks are the same three shapes: pip
  `metadata-generation-failed` from a build hook, silent `StartError`, and R `dyn.load` missing sonames. Two are
  rule-table entries once seen twice.
- **Infrastructure assumptions were the cheap losses**: a token lifetime, a shared file share, a base image's default
  user. None is about models; all are now either fixed or in the gate.

## Fixed in the tree after the pass (rev 4.17, 200 checks)

Token re-read per API call; kubelet message on a non-zero exit; `init` refuses a `--repo` that is not the Job's
`MODEL_REPO`; scorer voids a trial not on the pinned commit; gate step for source integrity. See the change log.
**Not fixed, deliberately** (each needs a second observation or a design decision): transitive git sources, R
`Remotes:`, unpin-on-stale-lock, extras-only git dependencies, the README `make test` grounding, the micromamba
user, the Julia probe, live H24.

## Before the next pass

1. Restore `bench-biosimulators_tellurium` and `bench-c302` on `irods-pvc` at the manifest shas (a write to the
   shared models claim — needs a go-ahead) and find out what removed them.
2. Re-run those two on `rev4.16` to complete this pass's table (same tree, so it is still one pass).
3. Build the transitive-git-source mechanism, re-run the four repos it targets singly, then `aks-holdout-7`.
