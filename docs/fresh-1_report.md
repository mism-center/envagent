# Fresh set, first and only blind pass: `fresh-1-base` vs `fresh-1-live` — 2026-11-23

20 repos sampled and ground-truthed before any run (`bench/fresh_manifest.yaml`, `bench/fresh_ground_truth.yaml`;
11 in scope, 9 out by policy). Two arms side by side, memory off, one image each, no fixes during the run:

- **base** `rev4.20@sha256:4397c8a4…` (the pass-7 image)
- **live** `rev5.2-live@sha256:9cf667b9…` (resolver, live scratch container, no verdict without a clean attempt)

All 40 Jobs completed under their cap; no infra retries. Raw rows and scorer output: `docs/records/*fresh-1-*`.

## Headline

| | base | live |
|---|---|---|
| scorer: verified (counted) | 3/20 | 7/20 |
| **audited: ran the ground-truth example** | **2/20** (bonesis, pasmopy) | **2/20** (bonesis, myokit) |
| audited: shipped alternate, plausible | 0 | 1 (opqua) |
| **audited: verified, but no simulation ran** | **1** | **4** |
| verified on tests only (scorer already excludes) | 1 | 2 |
| trials that ended at L0 (build never passed) | 11 | 6 |
| median charged builds / trial | 3 | 1 |
| total wall clock | 100 min | 139 min |

The scorer's `false-verified 0` is wrong for this set: its honesty check cannot see a run that exits 0 without
simulating, and it counts any clean non-ground-truth command as an "alternate" success, flagged for a human. This
is that human pass.

## The five false successes

| arm | model | ran | why it is not the example |
|---|---|---|---|
| both | brian2cuda | README python block | imports and calls `set_device("cuda_standalone")`; no network is built or run. Ground truth predicted exactly this. |
| live | fly-brain | `python fly_chat.py` | interactive; on EOF it `break`s out of the input loop before `run_simulation` (`fly_chat.py:533`). 20 s of imports. |
| live | pasmopy | `make html` (in `docs/`) | Sphinx docs build. init chose it from the README; the agent added `make`, sphinx, and the workdir to make it pass. |
| live | lad-seir-calibration | `Rscript install_packages.R` | dependency installer ("All requested packages are already installed."). See the invariant break below. |

**Invariant break (lad-seir, live).** The agent put `entrypoint: ["Rscript","run_analysis.R"]` in the EnvSpec, so
the image bakes that ENTRYPOINT, but L3 ran init's chosen command (`install_packages.R`). The agent read the clean L3
as "run_analysis.R verified" and said so in its reason. The shipped image's entrypoint was never run.

## What the comparison says

1. **Live mode helps the build side, clearly.** Trials that ended at L0: 11 → 6. Median builds per trial 3 → 1.
   Live reached L2+ in 12 trials against 8; six repos moved off L0 (biosimspace, reina-model, cbmpy,
   on-growth-and-form, fly-brain, pymanga).
2. **It does not help the outcome, because the bottleneck moved.** On repos nobody tuned against, the agent no longer
   fails mainly at building. It fails at **knowing what to run** and **knowing whether it ran**. Cheap iteration made
   that worse: with 8 live rounds pasmopy pushed a docs build through to L3; the base arm abandoned `make html`.
3. **Example discovery mis-chose on ≥7 of 20 repos.** `src/examples.py` picked `make html` (pasmopy),
   `install_packages.R` (lad-seir), `docs/make_banner.py` (on-growth-and-form), `python -m myokit icons` (interactive),
   `fly_chat.py` (interactive), and `pytest` where a real example exists (cbmpy, pymanga). The hold-out never exposed
   this because seven passes tuned discovery to its 19 repos.

## Mechanism fixes this points to (none applied; the fresh set is now spent as a blind test)

- **One command, one truth.** The image ENTRYPOINT (installed mode) must be the L3 command: the driver derives one
  from the other or refuses the spec when they differ. Correctness bug, small.
- **Setup is never the example.** Commands from a README's install/build/docs sections (`install*`, `setup*`,
  `make html|docs`, `sphinx-build`, `mkdocs`, banner/figure scripts) are not candidates, README or not.
- **Proof of work at L3.** "Exit 0" is not "simulated". Require evidence the repo's code ran past import: for Python,
  a `sitecustomize` profile hook counting calls into the model's own files (catches brian2cuda and fly_chat); for all
  languages, stdin closed and an interactive-prompt detector, plus outputs or a stdout witness.
- **Keep the L3 output.** Successful attempt rows keep no stdout/stderr tail; this audit needed the agent's console
  logs. Record `l3_output_tail` on every attempt.
- **Independent judge before `verified`.** The ground-truth subagent, reading only the repo, predicted the
  brian2cuda and fly_chat traps before any run. A fresh-context check of (README, command, L3 output) is the scalable
  form of this audit.

A new blind set is needed to measure any of these.
