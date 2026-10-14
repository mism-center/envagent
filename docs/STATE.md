# envbuild — state of the project, 14 October 2026

*For the team. Every number is read from the saved `score_*.txt` files and the attempt/verdict records
(`attempts_<run>.jsonl`, `verdicts_<run>.jsonl`); the per-pass reports in this directory carry the per-model detail.*

## What envbuild is

An agent that turns a registered model repository into a **verified execution environment**, or says honestly that it
cannot. It drafts a structured build spec from repo evidence (and the annotation when present), builds it with Kaniko,
climbs a verification ladder — L0 image builds, L1 dependencies import in the image alone, L2 the code mounts and its
imports resolve, L3 the repo's own example runs clean — classifies whatever broke (rule table first, model second),
applies exactly one typed repair, and repeats within a budget. Every attempt and every verdict is a record.

## Where it stands

| | tuning corpus (7 models) | hold-out (20 unseen repos, pinned commits) |
|---|---|---|
| build success (buildable) | **4/5** on every pass since pass 3 (`aks-rev4-3`), seven passes | **4/19** on passes 4 and 5 (same four repos, identical repair chains); pass 3 scored 5/19 under the current policy |
| false-verified | **0** in all 7 passes | **0** in all 5 passes |
| verdict correctness (incl. controls) | 6/7 since pass 3 | 20 % |
| LLM-fallback share | 69 % (pre-rev-3 history) → 0 % | 45 % → 40 % |
| agent runtime | pi 0.99 → 1.0.4 (pass 5); no effect on the rate | |

**Repositories that verify stably** (headline example ran clean, repeated): viva-tumor-tcell, viva-simbio, viva-amici,
covasim (hold-out); tumor-tcell, vivarium-chemotaxis, mbmm, circadian-clock (corpus). Links, pinned commits and the
entry that ran: `verified_repos_2026-10-09.md` (artifact) / the ground truth. viva-mgen verified on `pytest` in the
single-repo rerun of 8 Oct (git-sourced dependency installed for the first time) but its documented study run has not
yet been tried.

**The platform's own first use** (10 `mism:model/<uuid>` jobs on the `k8s-test` tag, 12–13 Oct): 6 verified, 2 escalated
honestly (a git-sibling dependency; a model bug under the annotation's argument), 2 failed (a `pytest`-less `pytest`
entry, now fixed in the tree; the viva-munk timeout). Three of the reported "failures" were a runner reading the results
differently from the verifier, not builder results — see *Open items*.

## How the number was earned

1. **Review (18 Sep).** Design sound; instrumentation broken: undecoded logs → 69 % `UNKNOWN`, network outages repaired
   as spec bugs, a free-shell repair writing shim modules to pass probes.
2. **Rev 3.** Decoded logs, infra plane (uncharged), step attribution, lockfile in the approval unit, shim guard.
3. **Rev 4 (seven corpus passes).** Repo-example discovery, annotation corrections as proposals, writable-copy mount,
   lockfile pinning, install-mode inference, L3 budget separation, undeclared-import scan → 4/5 stable.
4. **Hold-out protocol (24 Sep).** 20 unseen repos, one pass per revision, no fixes between models, every non-success
   sorted into harness bug / new mechanism / repo defect / rule-table entry. Five passes: 1 → 2 → 5 → 4 → 4.
5. **Sprint (7–8 Oct), by decision.** Git-sourced deps, R binary packages + per-manager build budget, cross-job memory,
   runner dependencies, module-less distributions, heavy optional extras, Kaniko pod resources. Confirmed on six
   single-repo reruns; **not yet measured on a full pass** (`aks-holdout-6`, pre-registered 8–10/19).

## What stops the other fifteen (hold-out pass 5 + rerun 6, from the records)

| bucket | repos | note |
|---|---|---|
| design items, deliberately unbuilt | viva-artistoo (Node), physicell (run-time download, no egress), covid-sim (C++ binary), covid19model (conda-style R names), viva-biofilm (plotly needs Chrome), catalyst-jl (Julia control — correct) | platform policy decisions |
| one layer left after the sprint | starsim, viva-membrane-actin (writable copy), c302 (pytest in image), epidemics (`wpp2024` not on CRAN 4.4.1), epimodel (Suggests pre-installed) | fixed in tree, awaiting pass 6 |
| upstream package defect | viva-cpm, biosimulators_tellurium (`tellurium` imports a module nothing provides) | honest escalation |
| policy | viva-yalla (README demo calls macOS `open`; verifies on `pytest` only), viva-mgen (tests tier) | a shipped demo counts, a test suite never does (decision 7 Oct) |
| budget | viva-munk (annotated suite exceeds the L3 maximum) | honest `failed`/TIMEOUT |

## Engineering state

- Working tree **rev 4.14**, 191 offline checks, pyflakes clean, LF endings. Last built image: `rev4.14` (= rev 4.13
  tree); the rev 4.14 edits (runner deps, L1 module-less skip, heavy extras, Kaniko resources, chooser prefers scripts)
  are **not yet in an image**. Next tag: **`rev4.15`**.
- Pass artefacts: `docs/pass_aks-holdout-{1..5}_report.md`, `docs/rerun_aks-rerun-6_report.md`, cumulative change log
  `docs/envagent_changes.md` (rev 3 → 4.14), architecture page + diagrams, benchmark report (corpus-only; needs a
  hold-out section).
- Cluster (AKS `default`): `envbuild-work` claim holds `records/{attempts,verdicts}.jsonl` and `records/jobs/<job>/`
  for every job ever run; `irods-pvc` holds the 20 hold-out checkouts under `/models/bench-<name>/1.0/` and the
  platform's models under `/models/<uuid>/`. Helper pods `envbuild-reader`/`envbuild-loader` self-expire.

## Open items (see HANDOFF.md for the resume list)

Runner-side contract (`runspec`), weak verification on argument-less CLIs, `UNSUPPORTED_TOOLS` prefix match dropping
`copasi-basico`, `arrow`/`stochastic-arrow` shadowing (one observation), agent Job pod resources, BuildKit for
ephemeral-quota clusters, Pass B with annotator output, COPASI representation in the hold-out.
