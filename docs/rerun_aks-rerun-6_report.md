# envbuild single-repo reruns `aks-rerun-6` — 2026-10-08

Image `mismplatform/pi-envagent:rev4.14` (= rev 4.13 tree: M1 git deps, M6 R binaries + per-manager build budget,
cross-job memory, H22, R3, toolchain_readonly), pi 1.0.4. Six repos the sprint targeted, run once each, 88 min total.
Memory was empty for all six (no prior verified job on this claim), so every run measured search.

| repo | pass 5 | **rerun** | attempts | min | what the records say |
|---|---|---|---|---|---|
| viva-mgen | escalated (git dep) | **verified** (tests tier) | 3 | 10 | **M1 worked**: `viva-parsimony @ git+…@main` rendered (UNPINNED, no lockfile), image built with git, `pytest` 177 passed. But the README also documents `python workspace/studies/fig2-growth/sims/run.py` and the chooser took `pytest` first → under the policy this is *verified on tests only*. Fixed (below); the study run is the ground truth now. |
| epidemics | escalated (L3 compile EROFS) | escalated | 5 | 10 | `toolchain_readonly` → `writable_copy` worked (reached L2 compile). Then two attempts on `testthat` and `socialmixr` — both **Suggests** in DESCRIPTION, i.e. what the tests import — then `wpp2024`, which CRAN does not have for R 4.4.1 (`r_not_available`). Honest escalation; two attempts were avoidable (H26). |
| epimodel | failed (TIMEOUT) | failed | 5 | 22 | **M6 worked**: P3M binaries — the R build that timed out at 600 s in four passes now completes (687 s and 1291 s rows are build+verify under the 1800 s budget). Then `testthat` (Suggests, H26), `ergm.ego`, an agent `libglpk40` guess that regressed and rolled back; attempts exhausted at L2. |
| starsim | failed (EROFS tests/temp) | failed | 5 | 18 | Two attempts lost to the L1 probe failing `intel-cmplr-lib-rt` and `mkl` — distributions that ship no Python module (H24); then an agent `ADD_APT_PKG git` guess (UNKNOWN) and `extra_path=/model`. The real fix (`writable_copy`) was proposed by the rule on the last row with no attempts left. Separately, the agent's first `REMOVE_PKG` call was rejected by the CLI for `--classified-by agent` (H25) — that cost a turn and log noise, not a charged attempt. |
| viva-membrane-actin-composite | escalated (git dep) | failed | 5 | 17 | **M1 worked** (3 sha-pinned refs). Three attempts then went to compiling the `sim` extra (pymem3dg/readdy: g++, netcdf, cmake policy) that the demo does not need (H27); built, reached L2, then the same `demo/_bigraph_tmp` EROFS as pass 5 with no attempts left. |
| c302 | escalated | escalated | 2 | 4 | **R3 worked**: `legacy_sdist` → Python 3.10 → built and passed L1/L2 first try. L3 then `StartError 128`: installed mode baked `ENTRYPOINT ["pytest"]` into an image with **no pytest installed** (H26). H17 recorded the exit; the agent correctly said no typed repair applies. |

## Reading

Every sprint mechanism did what it was built for — git deps install (2/2), R binaries fit the budget (1/1), the legacy-sdist
rule gets c302 past L0 — and each repo then stopped one layer later. The new layer is almost entirely one shape:
**dependencies the entry command implies but nobody declares** (pytest for a pytest entry; testthat and the
DESCRIPTION Suggests for a testthat entry) and **attempts spent on probes that should not run** (module-less
distributions; a heavy optional extra). Four of six lost 2–3 attempts to that before reaching their real failure.

## Fixed in the tree (rev 4.14, 191 checks)

- **H26** `driver.runner_deps`: the entry's runner is a dependency — `pytest` for a pytest entry, `testthat` + DESCRIPTION
  Suggests for an R test entry — added at `init` with origin recorded (`runner_dependency_added`). `evidence` parses Suggests.
- **H24** L1 probe: a distribution with no top-level module (intel-cmplr-lib-rt, mkl, nvidia-*) is not probed.
- **H25** `--classified-by agent` accepted as an alias of `llm` (the taxonomy's own word).
- **H27** optional-dependency groups named like heavy alternative backends (`sim`, `gpu`, `all`, `mpi`, …) stay out of the
  draft and are recorded (`optional_groups_skipped`); `demo`/`viz`/`dev` stay in.
- **Policy** `choose_entry` prefers a documented script over a test runner; `is_test_runner` feeds `entry_tier`.
  Ground truth for viva-mgen corrected to the study run (my `pytest` entry at selection time was wrong under the policy).
- **Kaniko pod** declares `resources` (config `[builder] request_*/limit_*`; defaults 1/2Gi/8Gi → 4/8Gi/32Gi) and keeps its
  working directory on the work claim (`--kaniko-dir`). On a namespace whose ephemeral quota is below the unpacked image
  (user's cluster: 9 Gi hard, ~700 Mi free) no Kaniko setting helps — see the BuildKit note in the change log.

## Expected on the next full pass

mgen on the study run (first real test of M1 at L3); c302 past StartError to a real pytest result; epidemics and epimodel
with Suggests pre-installed (epidemics still needs `wpp2024`, which CRAN lacks for 4.4.1 → honest escalation unless P3M
has it); starsim and membrane-actin to `writable_copy` with attempts to spare. Pre-registered 8–10/19 stands.
