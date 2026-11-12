# envbuild single-repo reruns `aks-rerun-7` — 2026-11-12

Image `mismplatform/pi-envagent:rev4.17@sha256:dbf7b838…` (= rev 4.17 tree at `747b745`, hash-probed; pi 1.0.4), agent
`gpt-5.6-luna`, sources from `envbuild-bench-models` at the manifest shas, sequential, no fixes between models, no
memory entry for any of the seven. Canary first: `bench:mbmm` verified at L3 in one attempt (`gate-rev4.17`).
Purpose: test M7 (transitive git sources) on the repos pass 6b lost to `vivarium-workbench`. Not a pass; nothing here
changes the hold-out figure (4/19). Raw rows: `docs/records/{attempts,verdicts}_aks-rerun-7.jsonl`, score in
`score_aks-rerun-7.txt`.

## Result

**M7 does what it was built for: all seven repos install the git-sourced `vivarium-workbench` and its git-sourced
dependencies, and none stops on `No matching distribution` any more. One of the seven verifies (viva-cpm, for the
first time in any pass). The other six each reached the next, different, obstacle.** 0 false-verified.

| model | pass 6b | rerun 7 | what stopped it now | bucket |
|---|---|---|---|---|
| viva-cpm | L0 | **verified, L3, 4 attempts** | — | success |
| viva-amici | L0 | failed, image passes L1 | attempts spent: three on `amici` version pins that do not exist for the interpreter (`1.6.2`, then 3.12, then `1.2.0`), one on the toolchain; the fifth found `bigraph_viz2` missing with no budget left | budget / model judgement |
| viva-artistoo | L0 | escalated, L0–L2 pass | the demo starts Node and runs `npm install github:ingewortel/artistoo` at run time | design item (Node runtime, run-time egress) |
| viva-yalla | L0 | escalated, L0–L2 pass | README demo writes `demo/report.html`, then calls macOS `open`; the model switched to `demo/composite_report.py`, which needed two more repairs than the budget had | repo defect, then budget |
| viva-simbio | budget | failed, image passes L1 | three attempts on the workbench build hook (npm), then the stale lock: `process-bigraph` 1.4.12 has no `composite_spec`; the model pinned `==0.0.66`, which does not exist | harness (rule added) + repo defect |
| viva-membrane-actin | L0 | escalated, image passes L1 | two attempts on the same build hook, then the demo imports `pbg_mem3dg`, a git source in an optional group the draft leaves out; the model escalated rather than add it | harness (both fixed in the tree) |
| viva-biofilm | L3 | escalated, L0–L2 pass | Kaleido needs Chrome — the same place as 6b, but with `vivarium-workbench` installed instead of removed | design item |

## The success, audited

viva-cpm ran `python demos/run_cc3d_demos.py` at the pinned sha. Repairs: Python 3.13 (the lock pins `numpy` 2.5.1),
`build-essential` (the Rust extension needs a linker), `output_path=/model/viewer`. I read the script: it runs four
CompuCell3D reference demos on the repo's engine, checks each against expected behaviour, and exits non-zero unless
all four pass. It wrote `viewer/data/{cellsort_2d,cellsort_3d,bacterium_macrophage,growth_mitosis,index}.json`.
Genuine. The README names `demos/` as the runnable demos and the script's docstring gives the command; the ground
truth had no example for this repo and now records this one (not human-run). `tellurium` (O7) was never reached
because the SBML extra is not part of the demo.

## What the reruns say

- **The earlier blocker hid a queue of smaller ones.** Each repo was one or two repairs from L3, and two of them ran
  out of attempts on repairs a rule could have supplied.
- **Model judgement cost more attempts than any missing mechanism.** Invented version numbers on viva-amici and
  viva-simbio; an environment flag set, then unset, on viva-simbio; an escalation with two attempts in hand on
  viva-membrane-actin. LLM-fallback share was 60 %.
- **viva-amici is a knife-edge.** Its verified chain in passes 3–5 used all five attempts (pin, toolchain, add
  `bigraph_viz2`, writable copy, run). One wasted attempt loses it.

## Fixed in the tree after the reruns (203 checks)

- Rule `no_npm`, L0 only: a build hook reporting `npm not found` gets `ADD_APT_PKG nodejs npm` (two repos, both got
  past L0 on exactly that). At L3 the rule declines — a model that needs Node at run time is still a design item.
- Rule `no_compiler` also reads rustc's "linker cc not found" message (viva-cpm, viva-biofilm).
- `ADD_PKG` of a name the repo declares from git is rendered as that git reference at the locked sha, never fetched
  from the index by name; the skill text tells the model to use it for an optional git dependency the example
  imports.
- A rule handler may decline (return nothing) so a rung-gated rule does not shadow later ones.

Not acted on: unpinning a lock-pinned package when the model's own code imports a name it lacks (viva-simbio twice,
but one repo); `compile_failed` proposes a pin with no version, which invites invented ones; viva-yalla's `open`.

## Expectation for `aks-holdout-7`

Five repos have now verified on their own example at the pinned sha (viva-tumor-tcell, viva-mgen, covasim, c302,
viva-cpm). viva-amici, viva-membrane-actin and viva-simbio are possible, none likely. **Pre-registered: 5–6/19,
0 false-verified.** Hold-out memory entries exist for the verified models; archive them first if the pass is to
measure search.
