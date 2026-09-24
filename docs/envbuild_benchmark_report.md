# envbuild benchmark — status report (22–24 September 2026)

*For developers seeing this for the first time. Everything below is computed from the attempt and
verdict records in `benchmark-envagent/results/` (`attempts_aks-rev4-*.jsonl`, `verdicts_aks-rev4-*.jsonl`).*

## 1. What is being measured

**envbuild** is an agent that takes a registered biological model — a source repository plus a
human-written annotation (language, dependencies, how to run it) — and produces a **verified Docker
image**: one in which the example the repository itself provides actually runs. It works as a
search loop: synthesise a structured build spec, build it, climb a verification ladder, classify
what broke, apply exactly one typed repair, repeat within a budget of 5 attempts. It never emits
Dockerfile text directly and it always ends with a verdict, even when it fails.

The verification ladder is what makes a verdict meaningful:

| rung | proves | a failure here means |
|---|---|---|
| L0 | the image builds | build problem (ours) |
| L1 | dependencies import, image alone | image problem (ours) |
| L2 | model code mounts, its imports resolve | mount contract or code problem |
| L3 | the repo's example runs to completion and writes its outputs | runtime problem |

**Success** is defined as: *the built container runs the example script / sample code the source
repository provides.* A verdict of `verified` means L3 passed. `failed` means the budget ran out;
`escalated` means the loop stopped honestly because no typed repair applied.

## 2. The benchmark corpus

Seven models, chosen before this work started:

| model | language | why it is in the corpus |
|---|---|---|
| vivarium-chemotaxis | Python | legacy stack (numpy < 2, old Pint); writes into its own source tree |
| tumor-tcell | Python | EOL interpreter in the annotation; OpenCV system libraries |
| spatio-flux | Python | plugin discovery over installed packages; cwd-relative I/O; 14-minute reproduction; ships a `uv.lock` |
| circadian-clock | Python | script reads data relative to its own directory, not the repo root |
| mbmm | R | an R package: must be *installed*, not mounted |
| gsmn-tb | SBML only | **control** — no runnable entry point; the correct verdict is `failed` |
| hybrid-model-tb | GAMA + R | **control** — unsupported toolchain; the correct verdict is `escalated` |

Five are buildable; two are controls whose job is to catch a builder that says "verified" when it
should not. **False-verified — a control marked verified — is the one number that must stay zero.**
It has.

## 3. Results

![Benchmark progression](C:/Users/kebedey/.claude-science/orgs/a6fb7daa-d2ac-45a0-a58e-1635d918b3e8/artifacts/proj_892907ee8ed0/371f6a15-c7ca-40ff-8584-a4f219045462/v78f21a3f_benchmark_progression.png)

*(For Confluence: attach `benchmark_progression.png` and replace this image link.)*

| metric | before this work (72 historical trials) | pass 1 (rev 4.0) | **pass 7 (rev 4.6)** |
|---|---|---|---|
| build success (buildable models) | 5 % (3/60) | 40 % (2/5) | **80 % (4/5)** |
| verdict correctness (all 7) | 11 % | 43 % (3/7) | **86 % (6/7)** |
| false-verified | 0 | 0 | **0** |
| failures the LLM had to classify (rule table had no answer) | 69 % | 6.7 % | **0 %** |
| build-step attribution on L0 failures | 10.5 % | 100 % | **100 %** |
| infra retries charged to the model | many (27 of 60 failures were network outages misread as spec bugs) | 0 | **0** |

Per model, per pass — verdict and (charged attempts):

| model | role | pass 1 | pass 2 | pass 3 | pass 4 | pass 5 | pass 6 | pass 7 |
|---|---|---|---|---|---|---|---|---|
| vivarium-chemotaxis | buildable | escalated (4) | failed (5) | ✅ verified (4) | ✅ verified (4) | ✅ verified (5) | ✅ verified (4) | ✅ verified (4) |
| tumor-tcell | buildable | escalated (5) | ✅ verified (3) | ✅ verified (3) | ✅ verified (3) | ✅ verified (3) | ✅ verified (3) | ✅ verified (3) |
| spatio-flux | buildable | escalated (2) | escalated (2) | escalated (2) | escalated (3) | failed (3) | escalated (4) | failed (5) |
| circadian-clock | buildable | ✅ verified (2) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) |
| mbmm | buildable | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) | ✅ verified (1) |
| gsmn-tb | control (must fail) | failed (2) | failed (2) | failed (2) | failed (2) | failed (2) | failed (2) | failed (2) |
| hybrid-model-tb | control (must escalate) | failed (1) | escalated (1) | escalated (1) | escalated (1) | escalated (2) | escalated (2) | escalated (1) |

Four of the five buildable models have verified on **every** pass since their blocking defect was
fixed, with identical repair chains (tumor-tcell 3-3-3-3-3-3 attempts, circadian-clock and mbmm
1 each). Both controls have returned the correct verdict on every pass since pass 2. Chemotaxis's
4-vs-5 variation is the one place the agent still guesses a version (Pint) rather than looking it up.

## 4. What the passes found, and what changed

Each pass was run, scored, and read; each defect was fixed with a regression test built from the
pass's own error text; then the image was rebuilt and the pass repeated. Seven passes, seven
revisions. The headline lesson is that **about half of what blocked the models was the harness's
own instrumentation**, not the models:

| pass | what blocked a model | fix (rev) | general or corpus-specific? |
|---|---|---|---|
| 0 (review) | build logs stored base64-encoded → 63 % of failures "UNKNOWN"; network outages "repaired" as spec bugs; a free-shell repair used to fake import probes | log decoding; infra plane (not charged, not patchable); shim guard (rev 3) | general |
| 1 | relative script path lost when the working directory moved; pip's *Requires-Python* hint ignored; one system library per attempt | anchor the script; read the hint; library families (4.1) | general |
| 2 | a file-relative write into the read-only code mount; a model that reads **and** writes cwd-relative | repair chosen from *where* the write went; `writable_copy` — L3 runs in a copy of the code (4.2) | general |
| 3 | repo ships `uv.lock`; builder ignored it | honour lockfiles (uv/poetry/Pipfile/pinned requirements) (4.3) | general |
| 4 | framework discovers plugins over *installed* packages → nothing found in mounted mode; interpreter picked outside the project's own range | install mode from the README's install line; intersect constraints (4.4) | general (one fallback regex is framework-specific) |
| 5 | budget charged the model's 13-minute runtime to the search; an import the repo never declared | budget excludes L3 run time; static scan for undeclared imports (4.5) | general |
| 6 | example killed at the 10-minute default deadline while working | typed `SET_L3_TIMEOUT`; measured runtime proposed back to the annotation (4.6) | general |
| 7 | model reproduced everything; outputs written inside the copy were not seen | harvest new files from the copy into `/outputs` (4.7, built, not yet run) | general |

Two things the benchmark also produced that were not planned:

- **Corrections to the annotations.** The builder now treats the annotation's execution fields as
  suggestions with a source tag, checks them against what the repo itself documents, and when it
  has to deviate, records the deviation as a *proposal* (`annotation-patch.yaml` per job) judged by
  the rung it reached. It never edits the annotation. Circadian-clock's working-directory fix and
  spatio-flux's measured runtime are the first two proposals.
- **Defects in the repositories.** spatio-flux imports `xarray` and needs `zarr`, and declares
  neither anywhere — its own `uv run` would fail on a fresh machine. The record says so.

## 5. How to read the numbers honestly

- **n = 5 buildable models.** 80 % has a 95 % confidence interval of 38–96 %. The number that
  matters at this size is not the rate but the *shape*: every failure since pass 3 has been one
  deterministic fix away from success, and no fix has regressed another model.
- **The corpus was used for tuning.** Every defect above was found by looking at these seven repos.
  The rate on *unseen* repos will be lower — a realistic expectation for 20 fresh Python/R repos of
  similar shape is that half to two-thirds verify untouched on the first pass, with the LLM
  fallback share rising from 0 % into the tens (which is the design working: rules are the fast
  path, the model is the fallback).
- **What has never been exercised:** conda/mamba environments, Bioconductor or `renv.lock`-pinned
  R (detected, not applied), Julia, GPU, models that download data at run time (L3 has no network
  by design), models with no runnable example, multi-hour reproductions.
- **What is not measured:** whether the verified container produces *scientifically correct*
  output. Rung L4 (comparison against a reference trace) exists in the schema and is always null.

## 6. Next steps

1. Run pass 8 on rev 4.7 (spatio-flux expected to verify → 5/5, 7/7).
2. **Hold-out expansion:** add ~20 unseen models, run them **once** with no fixes in between, then
   sort every failure into harness bug / new general mechanism / repo defect / rule-table entry.
   Report generalisation as the fraction that verified untouched.
3. Cross-job memory over error signatures (chemotaxis has solved the same Pint failure five times).
4. Three repeats per model for a rate with a confidence interval worth quoting.

## Appendix — where things live

- Harness: `envagent/` (`src/driver.py` loop; `src/ladder.py` rungs; `src/classify.py` rule table;
  `specs/` design contracts; `scripts/test_envbuild.py`, 145 checks).
- Corpus and ground truth: `benchmark-envagent/models/`, `envagent/bench/ground_truth.yaml`.
- Scoring: `uv run scripts/score.py <attempts.jsonl> <verdicts.jsonl> --run-id <id>`.
- Per-pass reports: `pass_aks-rev4-{1..7}_report.md`; cumulative change log: `envagent_changes.md`.
