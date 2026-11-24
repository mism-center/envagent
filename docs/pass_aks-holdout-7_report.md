# Hold-out pass `aks-holdout-7` — 2026-11-13 to 2026-11-23, and the live-mode smoke `smoke-rev5.1`

## Pass 7

Image `mismplatform/pi-envagent:rev4.20@sha256:4397c8a4…` (= `kaniko` at `d878a5d`, hash-probed; pi 1.0.4),
agent `gpt-5.6-luna`, memory off (`ENVBUILD_NO_MEMORY=1`), sequential, no fixes between models, sources from
`envbuild-bench-models` at the manifest shas. Canaries `gate-rev4.20` (13 Nov) and `gate-rev4.20b` (23 Nov, before
the resume) both verified. Raw rows: `docs/records/{attempts,verdicts}_aks-holdout-7.jsonl`; score in
`score_aks-holdout-7.txt`.

**Interrupted, not altered.** The first seven repos ran on 13 Nov. Overnight the launching laptop lost its network;
its runner read a still-running Job as a timeout and halted, and viva-yalla's trial died on the same outage inside the
cluster (`error`, void). The pass resumed on 23 Nov with the same image; the gap means dependencies on the index
moved for the last 12 repos. Runner fixes (cap enforced by the Job, network loss waited out, detached, LF endings) do
not touch the agent.

### Result

**10 of 19 verified; 10 of 15 in scope; 0 false-verified.** Pass 6b: 4/19. (The scorer prints 10/16 and 21 trials
because it counts viva-yalla's void trial separately.)

| model | verdict | builds | what happened |
|---|---|---|---|
| viva-tumor-tcell | verified | 2 | timeout rule, then the study run (~30 min) |
| viva-cpm | verified | 1 | README code block: a 50×50 CPM world, 100 Monte Carlo sweeps |
| viva-mgen | verified | 3 | README block is stale (imports a name the repo lacks); moved to the documented study run |
| viva-simbio | verified | 3 | npm rule, then `process-bigraph==1.5.0` (index-checked pin) |
| viva-amici | verified | 3 | compiler rule, then `bigraph_viz2`; first ever outside the noise run |
| viva-biofilm | verified | 3 | `kaleido_chrome` → chromium (rev 4.19 policy) — first success on its example |
| covasim | verified | 2 | README block: default `cv.Sim().run()` |
| starsim | verified | 4 | README blocks: `ss.demo()` and a 5,000-agent SIR run (rev 4.19 policy) |
| epimodel | verified | 2 | vignette `Intro.Rmd` (rev 4.19 policy); `libglpk40` found by the model |
| c302 | verified | 2 | legacy-sdist rule → Python 3.10 |
| viva-munk | failed | 2 | full test suite outruns 2400 s |
| viva-membrane-actin | escalated | 5 | rollback fix worked; then `pymem3dg` (C++/NetCDF/CMake) ate the budget |
| viva-artistoo | escalated | 2 | run-time `npm install` from GitHub (network denied) |
| viva-yalla | escalated | 2 | README demo ends in macOS `open -a Safari` (repo defect) |
| epidemics | escalated | 4 | vignette needs `wpp2024`, declared only via R `Remotes:` |
| physicell, covid-sim, biosimulators_tellurium, covid19model | escalated | — | out of scope by policy, closed honestly |
| catalyst-jl | failed | 1 | control: Julia, no entry point |

Policy bundle (rev 4.19) verified live: three of its four targets succeeded (starsim, epimodel, viva-biofilm);
epidemics needs R `Remotes:` support, viva-artistoo's npm fetch happens at run time.

### Audits

Every success's command was read: the README blocks of viva-cpm, covasim and starsim each run a simulation; no
success ran a test suite, a helper or a banner. One rule fired wrongly and did no harm: `cannot_import_name` pinned
viva-mgen's **own** package to an index release (installed mode reinstalled the repo over it; mounted mode would
not). Fixed on `resolve-explore` (2c27da4).

### Caveat

This hold-out has been tuned against for seven passes. 10/19 says the fixes work on the repos they were made for; it
does not say they generalise. That is what the pre-registered fresh set (`bench/fresh_manifest.yaml`) is for.

## Live mode in the cluster (`smoke-rev5.1`)

Image `rev5.1-live@sha256:afa2ebed…` (= `resolve-explore` at `2fdce55`), memory off, three spent hold-out repos:

| model | verdict | image builds | live rounds | minutes | compare (noise run, 3 trials) |
|---|---|---|---|---|---|
| covasim | verified | 1 | 1 | 5.7 | 2 builds each |
| viva-amici | verified | 1 | 2 | 14.6 | 4–5 builds, 16–25 min |
| viva-membrane-actin | escalated | 0 | 7 | 16.0 | 4–5 builds, escalated |

A live round re-runs only the steps a repair changed: amici's second round ran 1 step and reused 6 (118 s vs 302 s).
membrane-actin got further than any build-loop trial (compiler, NetCDF, CMake policy, L2 passed) before an L3
runtime defect (`no link found at address ReaDDyProcess`). But it escalated **straight from a live round**, so its
verdict rested on a container with the network on and seven rounds of leftovers, and records L0. Fixed in rev 5.2:
no verdict, positive or negative, without a clean attempt (`verdict_refusal`).

Bugs live mode surfaced on the way (all fixed before the fresh run): Azure Files refuses `chmod` (first canary's live
round crashed); micromamba runs as uid 57439 and activates its env via `SHELL` (real-path mounts, image SHELL read
from the registry, start-up handshake).
