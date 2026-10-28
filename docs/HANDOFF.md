# envbuild — handoff for resuming testing (written 14 Oct 2026)

Read this first in a new session. It is the list of what is pending, what is open, and how a pass is run — so the
work continues from the records rather than from memory. `STATE.md` is the narrative; this is the checklist.

## 1. Immediate next steps, in order

1. **Commit the tree** (rev 4.14; this handoff is part of it). Suggested message in `docs/COMMIT_MESSAGE.txt`.
2. **Build `rev4.15`** from the tree (`harness/Dockerfile`, pi pinned via `ARG PI_VERSION=1.0.4`), push, note the digest.
   Rule: **one tag = one tree**; never retag a tree that already has a tag.
3. **Pre-launch gate** (never skip): suite passes; in-cluster probe pod hashes 14 files under `/opt/skills/envbuild`
   + `harness/entrypoint.sh` against the tree; `pi --version`; with dummy `AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_BASE_URL`,
   `pi --list-models` lists `azure gpt-5.6-luna`; server-side dry run of one Job manifest.
4. **Run `aks-holdout-6`**: the same 20 repos (`bench/holdout_manifest.yaml`, pinned shas), `viva-tumor-tcell` first,
   sequentially, **no fixes between models**, 2 h cap per Job. Pre-registered expectation **8–10/19**, 0 false-verified.
   If under 8/19, the harness is not the bottleneck and the report should say so.
   Memory (`outputs/memory/` on the work claim) is empty for the hold-out models, so pass 6 measures *search*; the pass
   after it measures recall. Note: `scripts/score.py` reports `success_from_memory` separately.
5. **Score and sort**: `python scripts/score.py <attempts> <verdicts> --run-id aks-holdout-6` against
   `bench/ground_truth.yaml`; every non-success into harness bug / new mechanism / repo defect / rule-table entry;
   report + sorted CSV + score + raw records saved; dated section appended to `docs/envagent_changes.md`.
6. Then **Pass B**: the platform annotator's `metadata-package/` for the 19 unannotated hold-out repos, same protocol.

## 2. Open issues (none fixed; each has evidence in the records)

| id | issue | evidence | proposed fix | size |
|---|---|---|---|---|
| O1 | **Runner-side mount contract is unwritten.** A runner executing a verified image must reproduce `install_mode`, `output_path`, `workdir`, `writable_copy`, `extra_path` from the verdict/state, and know which paths to harvest or that the example writes only to stdout. | k8s-test runs 12–13 Oct: chemotaxis `86e0` verified with `output_path=/model/out` but the runner mounted `/output` → died in 12 s; covasim/spatio-flux "exit 0, no output files" were print-only examples | `envbuild runspec --job-id X` emitting the exact container spec + harvest list + "stdout only" flag | ½ day |
| O2 | **Weak verification on argument-less CLIs.** `python bin/covasim` with no args prints usage, exits 0, writes nothing — recorded `verified` like a real run. | job-2026-10-13-15ab | `l3_claim` = "ran clean, wrote nothing, no outputs declared" → scorer flags `verified (weak)`; annotator entry should carry a subcommand | small |
| O3 | **`UNSUPPORTED_TOOLS` prefix match drops `copasi-basico`.** The regex `^(gama|...|copasi|...)\b` matches `copasi-basico`, the pip package viva-copasi wraps; dry run of `draft_spec` on viva-copasi's real pyproject dropped it. | dry run 13 Oct (not live) | add `(?![-_][A-Za-z])` after `\b` + regression check (reverted at user's request; re-apply deliberately) | 1 line |
| O4 | **`arrow` vs `stochastic-arrow` shadowing** — `ImportError: cannot import name 'StochasticSystem' from 'arrow'` means the date library is installed where `stochastic-arrow` is needed. One observation; **do not add a rule yet**. | job-2026-10-12-d503 (chemotaxis 678e), budget exhausted | candidate rule: `REMOVE_PKG arrow` after `ADD_PKG stochastic-arrow` | — |
| O5 | **Agent Job pod declares no `resources`.** On a namespace with a LimitRange defaulting to 250m/256Mi it is OOM-killed before `init` → no record. Kaniko pod resources are done (rev 4.14); the agent pod is not. | user's second cluster | `resources` in `deploy/agent-job.yaml` (requests 500m/1Gi/2Gi, limits 2/4Gi/8Gi) | small |
| O6 | **Kaniko cannot run under a small ephemeral-storage quota** (user's cluster: 9 Gi hard, ~700 Mi free): it extracts the image into its own root filesystem. `--kaniko-dir` on the work claim halves usage but cannot remove it. | — | `BuildKitBuilder` (rootless, state on the work PVC) behind the existing `Builder` protocol | ~1 day |
| O7 | **viva-cpm / biosimulators_tellurium**: `tellurium` 2.2.x imports `tesbml`, which nothing on the index provides; pinning `<2.2.11` did not help (disproved in pass 5). | passes 2–5 | honest escalation (current); upstream issue | — |
| O8 | **epidemics** needs CRAN `wpp2024`, unavailable for R 4.4.1. | rerun 6 | check P3M / newer R base; else honest escalation | small |
| O9 | **viva-munk**: annotated 9-experiment suite exceeds `l3_timeout_max_s` (2400 s). | every pass | annotation `expected_runtime_s` or a shorter documented example | annotator |
| O10 | **H23**: the Julia control runs the R L1 probe (cosmetic; verdict still correct). | pass 5 | choose probe by language | small |
| O11 | **COPASI models absent from the hold-out.** Planned as a category (Sep 18 plan), never selected; `viva-copasi` (active, 105 KB, `pytest` + pyproject, git-sourced `viva-superpowers`) is the natural first candidate — needs O3 first. | archive | add to manifest + ground truth, single-repo run | 15 min |
| O12 | **Annotation `default_output_location` understates viva-tumor-tcell's writes** (`viz/` only; the run also writes `results.json`, `reports/figures/`, `.pbg/`). | pass 5 outputs | correction proposal to `annotation-patch.yaml` from `outputs_written` | small |
| O13 | **Benchmark report (`docs/envbuild_benchmark_report.md`) has no hold-out section**; the team brief (`envbuild_state_2026-10-06.md`) predates pass 5. | — | refresh after pass 6 | — |

## 3. Decisions in force

- **Success** = the container runs the example script / sample code the source repo provides (user, Sep).
- **Hold-out protocol**: same 20 pinned repos, one pass per revision, no fixes between models, four-way sort.
- **Alternate examples** (7 Oct): a shipped demo script that runs clean counts when the README example is broken or
  absent (`alternate_example_ok` per model in the ground truth); a test suite (`pytest`, `testthat`, `make test`)
  **never** counts as the example — reported as `verified_tests`.
- **Rules**: deterministic code extracts facts and enforces constraints; the model judges intent; the corpus decides
  which patterns become rules. Do not add a rule from one observation (the `tesbml` pin was added from one and disproved).
- **Not built, by decision**: Node runtime, headless Chrome, run-time egress, conda-style R names — honest escalations.

## 4. Conventions that bit us

- Image tag ↔ tree: `rev4.9` tag = rev 4.8 tree, `rev4.10` = 4.9, `rev4.13` = 4.11 + harness, `rev4.14` = 4.13.
  Always hash-probe the image before launching.
- The harness Dockerfile once installed pi unpinned; pi 1.0 renamed the Azure provider id and killed a whole pass.
  `run.agent_runtime` is on every row now.
- envbuild Jobs do **not** carry `app=envbuild` (only reader/loader pods do); select Jobs by name prefix `envbuild-<stamp>`.
- The verifier returns a pod's merged log as stderr (stdout empty); parse both.
- Files written from Windows arrive CRLF; rewrite with `newline="\n"` before committing. git does not run inside the
  analysis sandbox; commit from a terminal.
- Report claims about attempts/timings/classes are asserted against the records in code before they are written;
  the scorer's figures are authoritative over inline recomputation.

## 5. Where things are

- Records: `envbuild-work` claim → `/work/records/{attempts,verdicts}.jsonl`, `/work/records/jobs/<job_id>/`
  (`state.json`, `evidence.json`, `Dockerfile.aN`, `lockfile.aN.txt`, `annotation-patch.yaml`), `/work/records/memory/`.
  Read via a busybox pod mounting the claim (`envbuild-reader`, `kubectl cp`).
- Models: `irods-pvc` → `/models/bench-<name>/1.0/` (hold-out, shallow checkouts at pinned sha), `/models/<uuid>/0.0.1/`
  (platform-registered, with `metadata-package/`).
- Job template: `deploy/agent-job.yaml` with `__JOB__`, `__MODEL_ID__`, `__MODEL_REPO__`, `__ANNOTATION__`,
  `__AGENT_IMAGE__`, `__RUN_ID__`, `__MODELS_PVC__` placeholders.
- Verified images: `docker.io/mismplatform/envbuild@<image_digest>`, labelled `io.envbuild.model_id` /
  `io.envbuild.code_revision` / `io.envbuild.run_id`; digest + lockfile sha on the verdict row.
- Per-pass artefacts in `docs/`; raw records per pass were saved as session artefacts (`attempts_<run>.jsonl`,
  `verdicts_<run>.jsonl`) and are also on the work claim.
