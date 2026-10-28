# envbuild hold-out set — 20 models (selected 2026-09-24)

**Purpose.** Measure how the builder generalises beyond the seven models it was tuned on. Protocol: every
model runs **once**, on **one** harness image, with **no fixes between models**. Afterwards each
non-success is sorted into *harness bug* / *new general mechanism* / *repo defect* / *rule-table entry*,
and generalisation is reported as the fraction that verified **untouched**.

**Selection.** Ten from `vivarium-collective` (the platform's home ecosystem: process-bigraph, `pyproject`,
`uv`) and ten from elsewhere, chosen to hit families the seven-model corpus never exercised: compiled
extensions (Rust, C++ `make`/CMake, Rcpp), conda environments, R packages without `renv`, second
runtimes (node, Java, NEURON), and one unsupported-toolchain control (Julia). Every repo is pinned to
today's HEAD — most vivarium repos are pushed to daily.

**Draft expectations are not ground truth.** `verified` = a documented, runnable Python/R example and no
obvious blocker; `probe` = the model is legitimate but exercises something envbuild has never handled,
so the honest outcome may be `escalated`; `control:escalated` = must not verify. `bench/ground_truth.yaml`
entries will be written from the annotator's `entry_points` once available, all `human_verified: false`.

| id | repo @ sha | language | MB | family / why it is here | draft expectation |
|---|---|---|---|---|---|
| `bench:viva-tumor-tcell` | [vivarium-collective/viva-tumor-tcell](https://github.com/vivarium-collective/viva-tumor-tcell/tree/30e417325687afc30ebcfa2cb89ce90246319560) @ `30e41732` | HTML | 143 | process-bigraph port of tumor-tcell; pyproject; README: pytest | verified |
| `bench:viva-cpm` | [vivarium-collective/viva-cpm](https://github.com/vivarium-collective/viva-cpm/tree/2fda7a8d3009738d2f47cc9c42a01e853928e3c5) @ `2fda7a8d` | HTML | 86 | Rust core via maturin/pyo3 — compiled extension family, never tested; uv.lock | probe |
| `bench:viva-mgen` | [vivarium-collective/viva-Mgen](https://github.com/vivarium-collective/viva-Mgen/tree/6a258f185df7a7a7302df0ddf82300fbc20191cf) @ `6a258f18` | HTML | 98 | Karr 2012 whole-cell reproduction; pyproject; README: pytest | verified |
| `bench:viva-munk` | [vivarium-collective/Viva-munk](https://github.com/vivarium-collective/Viva-munk/tree/8852919fb4ad74b502f31ebe5af8edd4929f8658) @ `8852919f` | HTML | 796 | pymunk wrapper; 800 MB repo; README: python -m viva_munk.experiments.test_suite | verified |
| `bench:viva-membrane-actin-composite` | [vivarium-collective/viva-membrane-actin-composite](https://github.com/vivarium-collective/viva-membrane-actin-composite/tree/75c33f12f47d8acc173e034bd9b415075846d3c1) @ `75c33f12` | HTML | 18 | composite of two compiled sims (mem3dg, readdy — conda-only packages?); uv.lock | probe |
| `bench:viva-artistoo` | [vivarium-collective/viva-artistoo](https://github.com/vivarium-collective/viva-artistoo/tree/279638ffcbd8bdc1a563d13ebb47f039200381ab) @ `279638ff` | Python | 10 | wrapper for a JS CPM run via node — needs a second runtime | probe |
| `bench:viva-yalla` | [vivarium-collective/viva-yalla](https://github.com/vivarium-collective/viva-yalla/tree/4264734260be9b0bcde9ead5ef8722cb92412f8b) @ `42647342` | Python | 4 | NumPy port, pure Python; Dockerfile shipped | verified |
| `bench:viva-simbio` | [vivarium-collective/viva-simbio](https://github.com/vivarium-collective/viva-simbio/tree/41e3d5caa88e2c23c416ed25dabd93e2a67e0e95) @ `41e3d5ca` | Python | 6 | CRN simulator wrapper; uv.lock; Dockerfile | verified |
| `bench:viva-amici` | [vivarium-collective/viva-amici](https://github.com/vivarium-collective/viva-amici/tree/7ab0413e43bccd8db5b391acc9a112d5d254157e) @ `7ab0413e` | Python | 5 | AMICI compiles ODE models with a C++ toolchain at run time — build-deps family | probe |
| `bench:viva-biofilm` | [vivarium-collective/viva-biofilm](https://github.com/vivarium-collective/viva-biofilm/tree/d4eac5d7e9accc311964c76e2cb38facd9fb5aa1) @ `d4eac5d7` | HTML | 46 | Rust individual-based biofilm; README: pytest | probe |
| `bench:covasim` | [InstituteforDiseaseModeling/covasim](https://github.com/InstituteforDiseaseModeling/covasim/tree/fd749822fcd423e847fc542c4926674b834504be) @ `fd749822` | Python | 41 | Python epi ABM; pyproject; examples/ directory, no README command | verified |
| `bench:starsim` | [starsimhub/starsim](https://github.com/starsimhub/starsim/tree/3521adb4dda5a76d43ec38f5e8cb55deab08ece9) @ `3521adb4` | Python | 194 | Python ABM framework; pyproject; tests/ | verified |
| `bench:physicell` | [MathCancer/PhysiCell](https://github.com/MathCancer/PhysiCell/tree/69a23dbe7eaac4f41dc9548ad85072eff7921a03) @ `69a23dbe` | C++ | 1059 | C++ `make` build, 1 GB; sample projects — C++ toolchain family | probe |
| `bench:epimodel` | [EpiModel/EpiModel](https://github.com/EpiModel/EpiModel/tree/0c96feca076047be964f37378ed3ddba2f2b4b3f) @ `0c96feca` | R | 42 | R package, CRAN deps only (DESCRIPTION) — like mbmm without renv | verified |
| `bench:epidemics` | [epiverse-trace/epidemics](https://github.com/epiverse-trace/epidemics/tree/ac11c6fd15483f5a9fa2f053b714d04dd3c407b7) @ `ac11c6fd` | R | 39 | R package with Rcpp compiled code | probe |
| `bench:covid-sim` | [mrc-ide/covid-sim](https://github.com/mrc-ide/covid-sim/tree/b719de9df5909f8fc47753dd1d2baf89fc415943) @ `b719de9d` | C++ | 108 | C++ CMake, ships a Dockerfile; last push 2023 | probe |
| `bench:c302` | [OpenWorm/c302](https://github.com/OpenWorm/c302/tree/6cd861f8ca4d3241ee9cf4627884caa930dab53c) @ `6cd861f8` | Python | 64 | Python + NeuroML; simulation needs jNeuroML (Java) or NEURON | probe |
| `bench:biosimulators_tellurium` | [BioSimulators/Biosimulators_tellurium](https://github.com/BioSimulators/Biosimulators_tellurium/tree/c53b699587616a1874cba63312b264d04e12eca1) @ `c53b6995` | Python | 4 | pip package with Dockerfile; SBML simulator wrapper; default branch `dev` | verified |
| `bench:covid19model` | [ImperialCollegeLondon/covid19model](https://github.com/ImperialCollegeLondon/covid19model/tree/fcc30e2b8d046ddf3ef10dfc222e42b5cd732622) @ `fcc30e2b` | Stan | 66 | conda environment.yml + R/Stan — conda family, never tested; archived-in-practice (2021) | probe |
| `bench:catalyst-jl` | [SciML/Catalyst.jl](https://github.com/SciML/Catalyst.jl/tree/f1c28edf10901233307dd8a27020a0312bafbebe) @ `f1c28edf` | Julia | 340 | Julia — unsupported toolchain; the honest verdict is `escalated` | control:escalated |

Total ≈ 3.1 GB on the models claim. Two vivarium candidates were left out on size/shape:
`v2ecoli` (1.1 GB, whole-cell, hours of runtime) and `vivarium-tyssue` (notebooks as the entry point).
Both are reasonable additions later.

## Hand-off to the annotator

For each row, the annotator produces `metadata-package/` (`execution.yaml` at minimum) for the repo
**at the pinned sha**. If `execution.compute.typical_runtime` is filled in, envbuild sizes the L3
deadline from it (rev 4.7); otherwise the 600 s default applies and a slow example will escalate.

Layout expected on the claim, per model: `/models/bench-<name>/1.0/` = repo checkout,
`/models/bench-<name>/1.0/metadata-package/` = annotation. Loading (clone at sha + copy annotation) is
scripted from `holdout_manifest.yaml`; tell me where the annotations land and I will load and launch.

## What to expect

On the seven-model corpus the builder reached 4/5 after seven tuning passes. On twenty unseen repos
the honest prior is roughly **half to two-thirds verified on the first pass**, LLM-fallback share rising
from 0 % into the tens, and several `probe` rows escalating for a reason that becomes the next general
mechanism. That is the measurement; the number is not the goal of this pass, the sorted failure list is.
