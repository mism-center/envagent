# Success criteria — what "verified" is a claim about

**A build succeeds when the container it produced runs the example the source
repo itself provides.** Not "reaches L3", not "runs whatever the annotation
said": the repo's own README example, `inst/examples` script, `tests/`, or
demo — to exit 0, within its time allowance, writing what the repo says it
writes.

This is the definition the benchmark scores (`scripts/score.py` against
`bench/ground_truth.yaml`), and it has three consequences for how the loop
behaves.

## 1. The repo is the ground; the annotation is a reading of it

`evidence.examples[]` (from `examples.py`) lists every runnable example the
repo documents or lays out — README run lines, `pytest`, `testthat`,
`examples/`, `inst/examples/`, `demo/`, CI steps — with a tier: **smoke**
(quick, the thing to verify against) or **full** (a paper reproduction;
stronger claim, not required).

At `init`, the annotation's `entry_points` are checked against that list and
the repo (`examples.check_annotation`). Findings are deterministic and free:

| finding | example from the corpus |
|---|---|
| `not_a_command` | mbmm: `R`, `R -e`; hybrid-model-tb: `GAMA GUI: open …` |
| `file_missing` | script named in the entry is not in the repo |
| `placeholder_args` | spatio-flux: `run_study.py <SLUG>`; tumor-tcell: `main.py -w [workflow id]` |
| `needs_workdir` | circadian-clock: `np.loadtxt("dat/y0.txt")` relative to the script |
| `headline_missing` | chemotaxis: README's `paper_experiments.py` not among the entries |
| `no_entry_points` | annotation declares none, repo has examples |
| `unsupported_tool_dependency` | hybrid-model-tb: `GAMA Platform1.8` listed as a dependency; dropped from `pkg_specs`, the honest verdict is `escalated` |
| `not_a_package_spec` | a dependency entry that is not a package spec; dropped |

The command L3 runs is chosen in this order and its origin is recorded on
every row as `entrypoint_source`:

1. the annotation's first entry that is a real command with an existing script
   and no placeholders — `annotation` (human-reviewed, so it wins when usable);
2. else the repo's first smoke example — `repo_example`, recorded as a
   correction of the annotation;
3. else nothing — `none`; the job ends `ENTRYPOINT_UNKNOWN → submitter` with
   the findings attached.

A `needs_workdir` finding is applied at `init` (`mount.workdir`), also as a
recorded correction.

## 2. The builder may correct the annotation — as a proposal, never a write

`SET_ENTRYPOINT <command>` is a typed action like any other (one per attempt,
`--why`, forbidden triples), with two extra rules the driver enforces:

- **Grounded or refused.** The argument must be an item of `evidence.examples`
  or name a script that exists in the repo, with no placeholders
  (`examples.grounded`). The agent cannot invent an entry point.
- **At most two per job.** After that the honest verdict is `failed`
  (`ENTRYPOINT_UNKNOWN`), and the submitter gets the findings and both tried
  entries — which is a far more useful "back to submitter" than a bare class.

Every correction — by `init` or by the agent — lands in
`annotation_corrections[]` on the verdict as `{field, was, now, evidence,
applied_by, outcome}`. The **outcome is assigned by the ladder**, not by the
agent: `verified` when L3 passed, `helped` when the run got past L2 but L3
failed for a reason that may be the environment's, `rejected` when the
corrected entry itself was missing, `unverified` when no rung ever exercised
it. Only `verified` and `helped` corrections reach
`jobs/<job>/annotation-patch.yaml`, a diff against `execution.yaml` for
`biomodel-annotator` or a human to apply. **The builder never edits the
annotation.** It produces the evidence that lets someone else do so.

Corrections are confined to the execution plane — entry points, workdir,
runtime allowance, dependencies, system libraries, language version. The
scientific description, ontology terms and provenance are not the builder's to
touch.

## 2b. Where a command runs

Entry points are written relative to the repo root. L3 anchors the *script
token* of a relative command to `mount.code_path` before executing it
(`ladder.resolve_command`), so `python chemotaxis/processes/x.py` means the
same file whether `workdir` is `/model`, the script's own directory (a
`needs_workdir` correction) or `/outputs` (a model that writes cwd-relative
files). Arguments are not rewritten: they may be workdir-relative on purpose.
Absolute in-container paths (`python /model/scripts/x.py`) are accepted by
`SET_ENTRYPOINT` and checked as repo-relative.

**Budgets and the model's own runtime.** `wall_clock_s` bounds the *search*:
`budget_elapsed` subtracts time lost to the substrate (`infra_seconds`) and
time the example itself spent running at L3 (`l3_seconds`, bounded separately
by `l3_timeout_s`). A 13-minute reproduction must not consume two thirds of
the repair budget by existing. Verdicts carry `search_seconds` and
`l3_seconds` beside `duration_s`.

With `mount.writable_copy: true` L3 runs in a copy of the code
(`/scratch/model`), so a model that reads and writes cwd-relative paths runs
as its author ran it. After the run, every file created or modified inside
the copy (caches excluded) is copied to `output_path` with its relative layout,
so the output contract is checked the same way as in plain mounted mode and
`outputs_written` lists what the model produced. The model's exit code is
preserved.

## 3. Time is part of the definition

L1 and L2 are probes and share `verify_timeout_s`. **L3 runs the model's own
example and gets its own allowance**: `entry_points[].expected_runtime_s` from
the annotation (×1.5 + 60 s, capped at `l3_timeout_max_s`) when present, else
`l3_timeout_s` (10 min). The allowance is recorded on the row as
`l3_timeout_s`, so a `TIMEOUT` says how long was allowed. A fixed three-minute
L3 made `TIMEOUT` the modal verdict for simulation models.

## What the benchmark reports

Per trial (model × repeat — repeatability is part of the rate), filtered on
`run.run_id`:

- **build success** — verified trials / buildable trials. The 90 % target.
- **verdict correctness** — right verdict / all trials, controls included
  (gsmn-tb must be `failed`, hybrid-model-tb `escalated`).
- **false-verified** — a `verified` verdict with no lockfile, no digest, or a
  shim in its repair chain. Must be 0.
- **attribution** of every non-success on a buildable model: `builder`
  (L0/L1, or image/mount-plane L2/L3), `annotation` (ran something other than
  the ground-truth example and never got to it), `upstream` (the ground-truth
  example itself failed *and* a human has verified it runs), `infra`,
  `budget`. Only `builder` counts against the 90 %.
- **succeeded after correcting the annotation** — the good fourth bucket, and
  a direct measure of annotator quality.

`bench/ground_truth.yaml` is human-curated and independent of the annotator;
its `human_verified` date is what allows an `upstream` attribution. Until it
is set for a model, that model's example failures are `unattributed`.

## Verdict status is the dataset's label

`error` is reserved for substrate failures (the last attempt routed to `infra`,
or the driver itself faulted). The driver refuses `--status error` otherwise.
An example that outran `l3_timeout_max_s` is `failed` (class `TIMEOUT`, the max
recorded); a model the loop cannot help is `escalated`. The first hold-out pass
recorded a correct environment with a slow example as `error`, which hid the
only useful fact about it.

## Installed mode and the source tree

In `installed` mode the renderer copies the repo to `code_path` **and** installs
it, and the image sets `PYTHONSAFEPATH=1`: the installed package is what runs;
the copy at `code_path` is data (entry scripts, examples, configs). Without
this, `workdir == code_path` puts the uncompiled source first on `sys.path`
and any compiled extension imports its own source copy (hold-out 2: viva-cpm,
viva-biofilm). A script that imports a sibling module from its own directory
now fails at L2 with `IMPORT_PATH_ERROR` → `FIX_MOUNT_CONTRACT extra_path=…`,
which is the typed repair for exactly that. The same rule means `extra_path`
never contains `code_path` or a directory under it in installed mode: the
draft does not seed it, `SWITCH_INSTALL_MODE installed` drops it, and the
renderer filters it (hold-out 3: `PYTHONPATH=/model` had undone
`PYTHONSAFEPATH` on viva-cpm and viva-biofilm).

## Memory is a draft, not a verdict

A model that verified before starts from that spec (`spec_source: memory`).
Every rung still runs; the record says the spec came from memory, and the
scorer reports successes-from-memory separately from successes-by-search.
Memory is written only by a `verified` verdict and only read at `init`.

## Alternate examples (decision 2026-10-07)

When the README's example is broken or absent, a *shipped demo script* that
runs clean counts as verified (`alternate_example_ok` in the ground truth
confirms it per model; the scorer flags unconfirmed ones). A *test suite*
never counts as the example.

## A test suite is not the example

A verification whose entry is a test suite the agent fell back to (`pytest`,
`testthat::test_local()`, `make test`) proves the environment, not the
repo's example. The verdict records `entrypoint_tier`; the scorer reports
these as `verified_tests`, outside build success, unless the ground truth's
`example` is the tests or `alternate_example_ok: true` is set by a human.

## A command with no script is still an entry point

`pytest`, `Rscript -e '…'`, `make test`, `julia -e '…'` name no file for the L2
import probe. L2 is recorded as `not probeable` (`notes.l2_claim`) and L3 runs
the command — the run is the test. Only an empty command is
`ENTRYPOINT_MISSING`. Hold-out 3 lost four correct environments (c302,
epidemics, covid-sim, catalyst-jl) to the old message.

## Mount repairs never cover source

`mount_denied` proposes `output_path=<code>/<dir>` only when `<dir>` holds no
source (`evidence.source_tops`); otherwise `writable_copy=true`. A volume over
`demo/` hid `demo/demo_report.py` in hold-out 2.
