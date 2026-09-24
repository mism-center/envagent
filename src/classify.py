"""Build/verify stderr -> a failure class, and where possible a typed action.

Rules first, LLM second. Most build errors are regex-matchable, and a table is
faster, free, deterministic and testable. Growing this table is the main
*output* of the corpus run: every time the LLM fallback fires, that stderr is a
candidate rule.

The routing/repair semantics live in specs/failure_taxonomy.md -- this module
only implements the detection half.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

import logs

# Where a class goes when no repair applies. Mirrors specs/failure_taxonomy.md.
ROUTING = {
    "MISSING_SYSTEM_LIB": "retry",
    "DEP_RESOLUTION_CONFLICT": "retry",
    "COMPILE_ERROR": "retry",
    "MISSING_DEPENDENCY": "retry",
    "IMPORT_PATH_ERROR": "retry",
    "ABI_MISMATCH": "retry",
    "MOUNT_CONTRACT_ERROR": "retry",
    "BUILD_MODE_MISMATCH": "retry",
    # The base has no interpreter for the model's language: the scan guessed
    # wrong (ubuntu:24.04 for a Python repo). One CHANGE_BASE_IMAGE fixes it.
    "BASE_IMAGE_MISMATCH": "retry",
    "ENTRYPOINT_UNKNOWN": "submitter",
    "MISSING_DATA_FILE": "submitter",
    "LICENSE_REQUIRED": "submitter",
    "UNSUPPORTED_TOOLCHAIN": "dead-letter",
    "TIMEOUT": "dead-letter",
    "RUNTIME_ERROR": "dead-letter",
    # The substrate failed, not the spec: DNS, a registry, a package index, pod
    # scheduling. Routes to "infra": the driver re-runs the SAME spec and does
    # not charge the attempt. Never shown to the agent as something to repair --
    # in the first corpus run 51 attempts (29%) were this, all recorded as
    # UNKNOWN, and 26 of them were "fixed" with typed actions -- including an
    # /etc/hosts entry for pypi.org and an unpin of the author's exact version
    # because pip said "from versions: none".
    "INFRA_UNAVAILABLE": "infra",
    # The spec itself is malformed (a digest the registry cannot parse). A
    # repair of the offending field, not of the model.
    "SPEC_INVALID": "retry",
    "UNKNOWN": "llm",
}

_PY_LADDER = ("3.9", "3.10", "3.11", "3.12", "3.13")


def requires_python_pick(spec: str) -> str | None:
    """`>=3.9, <3.12` -> the newest interpreter in our ladder that satisfies it
    (3.11). Newest, because a newer patch line has the longer support window;
    None when nothing in the ladder fits."""
    def key(v):
        a, b = v.split(".")[:2]
        return int(a), int(b)
    ok = list(_PY_LADDER)
    for part in re.split(r"\s*,\s*", spec.strip()):
        mm = re.match(r"(>=|<=|>|<|==|!=|~=)\s*(\d+\.\d+)", part)
        if not mm:
            continue
        op, v = mm.group(1), key(mm.group(2))
        ok = [x for x in ok if {"<": key(x) < v, "<=": key(x) <= v, ">": key(x) > v,
                                ">=": key(x) >= v, "==": key(x)[:2] == v, "!=": key(x) != v,
                                "~=": key(x) >= v}[op]]
    return ok[-1] if ok else None


# Wheels that need a whole family of system libraries at once. One soname
# repair per attempt costs an attempt per library (tumor-tcell spent three on
# opencv); when the importing module is known, name the family in one action.
SONAME_BUNDLE = {
    "cv2": "libgl1 libglib2.0-0 libxcb1 libxext6 libsm6 libxrender1",
}

# Toolchains no Phase 0 base can provide. A dependency or "missing package"
# naming one of these is UNSUPPORTED_TOOLCHAIN -> dead-letter, not a package to
# install: hybrid-model-tb's `GAMA Platform1.8` was rendered as an R package
# and chased as MISSING_DEPENDENCY.
UNSUPPORTED_TOOLS = re.compile(r"^(?:gama|netlogo|matlab|simulink|comsol|copasi|mathematica|"
                               r"labview|stella|vensim|anylogic|cellblender|morpheus)\b", re.I)

# Shared-library soname -> Debian runtime package. The L1 probe surfaces these as
# `ImportError: libxcb.so.1: cannot open shared object file` when a wheel links
# against a system library the -slim base does not ship. 17 rows of the first
# corpus run were exactly this and all went to the LLM. Runtime packages, not
# -dev: nothing is being compiled at L1.
SONAME_APT = {
    "libxcb.so.1": "libxcb1",
    "libGL.so.1": "libgl1",
    "libGLU.so.1": "libglu1-mesa",
    "libEGL.so.1": "libegl1",
    "libOpenGL.so.0": "libopengl0",
    "libgthread-2.0.so.0": "libglib2.0-0",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libgomp.so.1": "libgomp1",
    "libX11.so.6": "libx11-6",
    "libXext.so.6": "libxext6",
    "libXrender.so.1": "libxrender1",
    "libSM.so.6": "libsm6",
    "libICE.so.6": "libice6",
    "libfontconfig.so.1": "libfontconfig1",
    "libfreetype.so.6": "libfreetype6",
    "libpng16.so.16": "libpng16-16",
    "libjpeg.so.8": "libjpeg62-turbo",
    "libtiff.so.6": "libtiff6",
    "libtiff.so.5": "libtiff5",
    "libxml2.so.2": "libxml2",
    "libxslt.so.1": "libxslt1.1",
    "libcurl.so.4": "libcurl4",
    "libssl.so.3": "libssl3",
    "libcrypto.so.3": "libssl3",
    "libffi.so.8": "libffi8",
    "libsqlite3.so.0": "libsqlite3-0",
    "libhdf5.so.310": "libhdf5-310",
    "libhdf5_serial.so.310": "libhdf5-310",
    "libnetcdf.so.19": "libnetcdf19",
    "libgfortran.so.5": "libgfortran5",
    "libquadmath.so.0": "libquadmath0",
    "libopenblas.so.0": "libopenblas0",
    "liblapack.so.3": "liblapack3",
    "libblas.so.3": "libblas3",
    "libgsl.so.27": "libgsl27",
    "libglpk.so.40": "libglpk40",
    "libcairo.so.2": "libcairo2",
    "libgraphviz.so": "libgraphviz",
    "libcgraph.so.6": "libcgraph6",
    "libgvc.so.6": "libgvc6",
    "libsndfile.so.1": "libsndfile1",
    "libudunits2.so.0": "libudunits2-0",
    "libproj.so.25": "libproj25",
    "libgdal.so.32": "libgdal32",
    "libmpi.so.40": "libopenmpi3",
    "libstdc++.so.6": "libstdc++6",
    "libz.so.1": "zlib1g",
    "libbz2.so.1.0": "libbz2-1.0",
    "liblzma.so.5": "liblzma5",
    "libzmq.so.5": "libzmq5",
}

# C header -> Debian dev package. Makes the common MISSING_SYSTEM_LIB repair
# fully deterministic: a meaningful share of Phase 0 repairs never reach an LLM.
HEADER_APT = {
    "libxml/parser.h": "libxml2-dev",
    "libxslt/xslt.h": "libxslt1-dev",
    "zlib.h": "zlib1g-dev",
    "Python.h": "python3-dev",
    "openssl/ssl.h": "libssl-dev",
    "ffi.h": "libffi-dev",
    "hdf5.h": "libhdf5-dev",
    "sundials/sundials_types.h": "libsundials-dev",
    "gsl/gsl_math.h": "libgsl-dev",
    "cblas.h": "libopenblas-dev",
    "lapacke.h": "liblapack-dev",
    "jpeglib.h": "libjpeg-dev",
    "png.h": "libpng-dev",
    "curl/curl.h": "libcurl4-openssl-dev",
    "sqlite3.h": "libsqlite3-dev",
    "graphviz/cgraph.h": "libgraphviz-dev",
    "mpi.h": "libopenmpi-dev",
    "X11/Xlib.h": "libx11-dev",
    "ft2build.h": "libfreetype-dev",
    "glpk.h": "libglpk-dev",
    "cairo.h": "libcairo2-dev",
    "pcre2.h": "libpcre2-dev",
    "udunits2.h": "libudunits2-dev",
    "proj.h": "libproj-dev",
    "netcdf.h": "libnetcdf-dev",
    "gdal.h": "libgdal-dev",
    "boost/version.hpp": "libboost-dev",
    "fftw3.h": "libfftw3-dev",
}
# Header basename fallback: `sundials/nvector_serial.h` still resolves.
_HEADER_DIR_APT = {
    "sundials": "libsundials-dev", "openssl": "libssl-dev", "libxml": "libxml2-dev",
    "libxslt": "libxslt1-dev", "gsl": "libgsl-dev", "graphviz": "libgraphviz-dev",
    "curl": "libcurl4-openssl-dev", "X11": "libx11-dev", "boost": "libboost-dev",
    "freetype2": "libfreetype-dev", "hdf5": "libhdf5-dev",
}

# Import name -> PyPI distribution, for the cases where they differ.
# Also feeds ladder.py's L1 probe (reversed: dist -> import name), so an entry
# here fixes both "what do I pip install for this missing import" and "what do
# I actually try to import for this installed distribution".
MODULE_PYPI = {
    "sklearn": "scikit-learn", "cv2": "opencv-python-headless", "yaml": "PyYAML",
    "PIL": "Pillow", "Bio": "biopython", "skimage": "scikit-image",
    "mpl_toolkits": "matplotlib", "serial": "pyserial", "OpenGL": "PyOpenGL",
    "dateutil": "python-dateutil", "attr": "attrs", "pkg_resources": "setuptools",
    "google": "protobuf", "Crypto": "pycryptodome", "usb": "pyusb",
    "OpenSSL": "pyOpenSSL", "zmq": "pyzmq", "cairo": "pycairo",
    # vivarium-core's importable package is "vivarium", not "vivarium_core" --
    # the naive dist.replace("-", "_") fallback guesses wrong and L1 falsely
    # reports the (installed) distribution as a missing module.
    "vivarium": "vivarium-core",
}


@dataclass
class Classification:
    """One classification verdict. `classified_by` is how we measure whether the
    rule table is winning -- watch its distribution over the corpus run."""

    failure_class: str
    classified_by: str = "rule"         # "rule" | "llm"
    rule: str | None = None             # the pattern name that fired
    action: str | None = None           # suggested typed action
    arg: str | None = None
    evidence: str = ""                  # the stderr line that matched
    routes_to: str = "retry"

    def to_dict(self) -> dict:
        return asdict(self)


def _apt_for_header(header: str) -> str | None:
    if header in HEADER_APT:
        return HEADER_APT[header]
    base = header.rsplit("/", 1)[-1]
    for k, v in HEADER_APT.items():
        if k.rsplit("/", 1)[-1] == base:
            return v
    if "/" in header:
        return _HEADER_DIR_APT.get(header.split("/", 1)[0])
    return None


# (name, pattern, handler) -- handler(match, ctx) -> Classification | None.
# `ctx` carries the rung and the repo's own top-level module names.
def _rules():
    def missing_header(m, _ctx):
        pkg = _apt_for_header(m.group(1))
        return Classification("MISSING_SYSTEM_LIB", action="ADD_APT_PKG" if pkg else None,
                              arg=pkg, evidence=m.group(0))

    def module_not_found(m, ctx):
        full = m.group(1)
        mod = full.split(".")[0]
        # Rung-aware: the same text means different things at L1 and L2. At L1 no
        # code is mounted, so it can only be a missing dependency. At L2+ a module
        # the repo itself defines means the mount/PYTHONPATH is wrong -- installing
        # a same-named PyPI package would "succeed" while shadowing the real code.
        if ctx.get("rung") in ("L2", "L3") and mod in ctx.get("local_modules", ()):
            return Classification("IMPORT_PATH_ERROR", action="FIX_MOUNT_CONTRACT",
                                  arg=None, evidence=m.group(0))
        if "." in full:
            # Dotted path ("pint.quantity"): Python only reports the submodule
            # this way when the top-level package already imported fine -- the
            # installed version just lacks that internal module. That's an API
            # break, not a missing package; re-adding the top-level package is a
            # no-op and gets the repair loop stuck retrying it forever.
            return Classification("ABI_MISMATCH", action="PIN_PKG",
                                  arg=MODULE_PYPI.get(mod, mod), evidence=m.group(0))
        return Classification("MISSING_DEPENDENCY", action="ADD_PKG",
                              arg=MODULE_PYPI.get(mod, mod), evidence=m.group(0))

    def r_missing_pkg(m, _ctx):
        if UNSUPPORTED_TOOLS.match(m.group(1)):
            return Classification("UNSUPPORTED_TOOLCHAIN", evidence=m.group(0))
        return Classification("MISSING_DEPENDENCY", action="ADD_PKG",
                              arg=m.group(1), evidence=m.group(0))

    def mount_denied(m, ctx):
        # Which repair depends on WHERE the model tried to write:
        #   '/model/out/processes'   file-relative into the code mount -> mount the
        #                            writable volume there: output_path=/model/out
        #   'studies/x/charts'       cwd-relative -> run in a writable copy of the
        #                            code (reads may be cwd-relative too)
        #   '/outputs/...'           the output mount itself is broken: no canned arg
        path = m.group(1)
        code = ctx.get("code_path", "/model")
        if ctx.get("rung") != "L3":
            return Classification("MOUNT_CONTRACT_ERROR", action="FIX_MOUNT_CONTRACT", evidence=m.group(0))
        if path.startswith(code + "/"):
            top = path[len(code) + 1:].split("/")[0]
            return Classification("MOUNT_CONTRACT_ERROR", action="FIX_MOUNT_CONTRACT",
                                  arg=f"output_path={code}/{top}", evidence=m.group(0))
        if not path.startswith("/"):
            return Classification("MOUNT_CONTRACT_ERROR", action="FIX_MOUNT_CONTRACT",
                                  arg="writable_copy=true", evidence=m.group(0))
        return Classification("MOUNT_CONTRACT_ERROR", action="FIX_MOUNT_CONTRACT", evidence=m.group(0))

    def cwd_relative_missing(m, ctx):
        if ctx.get("rung") == "L3" and ctx.get("workdir_moved"):
            return Classification("MOUNT_CONTRACT_ERROR", action="FIX_MOUNT_CONTRACT",
                                  arg="writable_copy=true", evidence=m.group(0))
        # Workdir untouched and the file is still missing: it really is missing.
        if ctx.get("rung") == "L3":
            return Classification("MISSING_DATA_FILE", evidence=m.group(0))
        return Classification("UNKNOWN", classified_by="llm", evidence=m.group(0))

    def l3_deadline(m, ctx):
        # A build or probe that hits its deadline is a TIMEOUT, full stop. The
        # model's own example hitting the *L3* deadline may simply be slow:
        # spatio-flux's reproduction ran clean for 600 s and was killed while
        # writing its last studies. Extend once (doubling, the driver caps it
        # at l3_timeout_max_s); the forbidden-triple rule stops a second
        # identical extension, and the max stops the escalation.
        cur = ctx.get("l3_timeout_s")
        if ctx.get("rung") == "L3" and cur:
            return Classification("TIMEOUT", action="SET_L3_TIMEOUT", arg=str(int(cur) * 2),
                                  evidence=f"L3 killed at the {cur} s deadline", routes_to="retry")
        return Classification("TIMEOUT", evidence=m.group(0))

    def simple(cls, action=None, arg=None):
        return lambda m, _ctx: Classification(cls, action=action, arg=arg, evidence=m.group(0))

    def missing_soname(m, _ctx):
        # `ImportError: libxcb.so.1: cannot open shared object file`. Three cases
        # share the text: a system library the base image lacks (lib*.so.N ->
        # apt), the model's OWN compiled extension failing under mounted mode
        # (something.cpython-311-x86_64-linux-gnu.so -> BUILD_MODE_MISMATCH), or
        # a library nobody has mapped yet (class only, no argument).
        path = m.group(1)
        name = path.rsplit("/", 1)[-1]
        if "cpython" in name or "/model/" in path or not name.startswith("lib"):
            return Classification("BUILD_MODE_MISMATCH", action="SWITCH_INSTALL_MODE",
                                  arg="installed", evidence=m.group(0))
        pkg = SONAME_APT.get(name)
        # `cv2 (from opencv-python): ImportError: libGL.so.1 ...` -- the L1 probe
        # names the importing module; if it is one with a known family, fix the
        # family in one action instead of one soname per attempt.
        line = next((ln for ln in _ctx.get("text", "").splitlines() if m.group(0) in ln), "")
        mod = line.split(":")[0].split(" ")[0].strip() if ":" in line else ""
        if mod in SONAME_BUNDLE:
            pkg = SONAME_BUNDLE[mod]
        return Classification("MISSING_SYSTEM_LIB", action="ADD_APT_PKG" if pkg else None,
                              arg=pkg, evidence=m.group(0))

    def no_matching_dist(m, ctx):
        # `No matching distribution found for X` follows one of two lines:
        #   ERROR: Could not find a version that satisfies ... (from versions: none)
        #   ERROR: Could not find a version that satisfies ... (from versions: 1.0, 1.1)
        # The first means the index was unreachable -- pip saw *nothing* -- and
        # unpinning X repairs a fiction. The `infra_network` rule runs before
        # this one and catches most of it; this guard is for a log whose network
        # line was cut off but whose "none" survived.
        text = ctx.get("text", "")
        if "(from versions: none)" in text:
            return Classification("INFRA_UNAVAILABLE", evidence=m.group(0))
        # pip says WHY the pinned version was skipped one line earlier:
        #   Ignored the following versions that require a different python
        #   version: 1.6.0 Requires-Python >=3.9, <3.12
        # The author's pin is right and the interpreter is wrong. Unpinning
        # instead (rev-4 pass 1, tumor-tcell) installed a 2021 vivarium-core
        # that lacked a symbol the model imports, and L3 died on it.
        want = m.group(1).split("==")[0].split("[")[0]
        pinned = m.group(1).split("==")[1] if "==" in m.group(1) else None
        for hint in re.finditer(r"(\S+) Requires-Python ([^;\n]+)", text):
            if pinned and hint.group(1) != pinned:
                continue
            # Intersect with the PROJECT's own requires-python: spatio-flux
            # says `<3.13`, ipython 9.12 says `>=3.12`; the answer is 3.12, and
            # 3.13 (the newest that satisfied ipython alone) broke the lock.
            spec = hint.group(2) + ("," + ctx["requires_python"] if ctx.get("requires_python") else "")
            ver = requires_python_pick(spec)
            if ver:
                return Classification("DEP_RESOLUTION_CONFLICT", action="CHANGE_INTERPRETER_VERSION",
                                      arg=ver, evidence=f"{want} {hint.group(1)} Requires-Python {hint.group(2)}")
        return Classification("DEP_RESOLUTION_CONFLICT", action="UNPIN_PKG", arg=m.group(1),
                              evidence=m.group(0))

    def r_not_available(m, _ctx):
        # CRAN has no package by that name *today*: an archived package, a
        # Bioconductor name handed to install.packages, or a typo in the
        # annotation. Not a network fault (that text is caught earlier) and not
        # something ADD_PKG of the same name can fix -- class only.
        return Classification("MISSING_DEPENDENCY", action=None, arg=m.group(1),
                              evidence=m.group(0))

    return [
        # -- infrastructure first: these co-occur with text further down the
        # table ("No matching distribution", "Error in download.packages") and
        # must win when they do.
        ("infra_network",
         re.compile(r"Temporary failure resolving|Could not resolve host|"
                    r"NewConnectionError|Failed to establish a new connection|"
                    r"Name or service not known|nodename nor servname|getaddrinfo failed|"
                    r"no such host|dial tcp.*(?:i/o timeout|connection refused)|"
                    r"TLS handshake timeout|Read timed out\.|Connection timed out|"
                    r"cannot open URL '|Some index files failed to download|"
                    r"\(from versions: none\)|"
                    r"toomanyrequests|429 Too Many Requests|"
                    r"failed to resolve source metadata|error pulling image|"
                    r"ImagePullBackOff|ErrImagePull|"
                    r"UNAUTHORIZED: authentication required|unauthorized: incorrect username"),
         simple("INFRA_UNAVAILABLE")),
        ("infra_scheduling",
         re.compile(r"FailedScheduling|Unschedulable|Insufficient (?:cpu|memory)|"
                    r"The node was low on resource|Evicted|"
                    r"PodInitializing|CreateContainerConfigError"),
         simple("INFRA_UNAVAILABLE")),
        # -- the spec is malformed. Caught by EnvSpec.from_dict now; the rule
        # stays so historical rows and any new path reclassify the same way.
        ("spec_invalid", re.compile(r"could not parse reference|invalid reference format"),
         simple("SPEC_INVALID")),
        ("missing_header", re.compile(r"fatal error: (\S+\.h(?:pp)?): No such file"), missing_header),
        # Before the generic `py_module`/`build_mode` rules: the message names
        # a shared object, and which kind decides the class.
        ("missing_soname",
         re.compile(r"ImportError: (\S+\.so(?:\.\d+)*): cannot open shared object file"),
         missing_soname),
        # numpy 2 removed `cumproduct`, `product`, `alltrue`, `NaN`, ... Any
        # pre-2024 stack hits this on the first import that touches them. The
        # dependent is the problem, but `numpy<2` on the stack is the one-line
        # repair that works for all of them at once.
        ("numpy2_api",
         re.compile(r"AttributeError: module 'numpy' has no attribute '\w+'|"
                    r"AttributeError: `np\.\w+` was removed in the NumPy 2\.0 release"),
         simple("ABI_MISMATCH", "PIN_PKG", "numpy<2")),
        # R: the package exists but its own build failed inside install.packages.
        ("r_install_failed",
         re.compile(r"installation of package ['‘](\S+?)['’] had non-zero exit status"),
         lambda m, _c: Classification("COMPILE_ERROR", action=None, arg=m.group(1),
                                      evidence=m.group(0))),
        ("r_not_available",
         re.compile(r"package ['‘](\S+?)['’] is not available"), r_not_available),
        # `R CMD INSTALL /model` (installed mode) needs the DESCRIPTION's Imports
        # already present; this names the one that is not. 13 corpus rows.
        ("r_dependency",
         re.compile(r"dependency ['‘](\S+?)['’] is not available for package"),
         lambda m, _c: Classification("MISSING_DEPENDENCY", action="ADD_PKG", arg=m.group(1),
                                      evidence=m.group(0))),
        # The base has no interpreter at all (ubuntu:24.04 picked for a repo
        # whose language the scan could not see). Not a package problem: a base
        # problem. 8 corpus rows, all UNKNOWN.
        ("no_interpreter",
         re.compile(r"(?:/bin/sh: \d+: )?(python3?|Rscript|julia): not found"),
         lambda m, _c: Classification("BASE_IMAGE_MISMATCH", action="CHANGE_BASE_IMAGE",
                                      arg={"python": "python:3.11-slim", "python3": "python:3.11-slim",
                                           "Rscript": "rocker/r-ver:4.4.1",
                                           "julia": "julia:1.10"}[m.group(1)],
                                      evidence=m.group(0))),
        ("missing_header_r",
         re.compile(r"configure: error: (?:lib)?(\w+)[- ]?(?:dev(?:el)?)? "
                    r"(?:library )?not found"),
         lambda m, _c: Classification("MISSING_SYSTEM_LIB", action="ADD_APT_PKG",
                                      arg=f"lib{m.group(1)}-dev", evidence=m.group(0))),
        ("dep_conflict", re.compile(r"ResolutionImpossible|conflict is caused by|"
                                    r"versions have conflicting dependencies"),
         simple("DEP_RESOLUTION_CONFLICT", "UNPIN_PKG")),
        ("no_matching_dist", re.compile(r"No matching distribution found for (\S+)"),
         no_matching_dist),
        ("no_compiler", re.compile(r"(?:unable to execute '(?:gcc|cc|g\+\+)'|"
                                   r"(?:gcc|cc|g\+\+): (?:command )?not found)"),
         simple("COMPILE_ERROR", "ADD_APT_PKG", "build-essential")),
        ("no_cmake", re.compile(r"cmake: (?:command )?not found|CMake must be installed"),
         simple("COMPILE_ERROR", "ADD_APT_PKG", "cmake")),
        ("compile_failed", re.compile(r"error: command '.*(?:gcc|g\+\+|cc1plus|clang)'.* failed|"
                                      r"error: Microsoft Visual C\+\+|"
                                      r"Failed building wheel for"),
         simple("COMPILE_ERROR", "PIN_PKG")),
        ("abi", re.compile(r"undefined symbol:|GLIBCXX_\d|GLIBC_\d|"
                           r"numpy\.dtype size changed|incompatible with .* ABI"),
         simple("ABI_MISMATCH", "PIN_PKG")),
        # `.so: cannot open shared object file` used to live here too. It never
        # fired on the real text (`libxcb.so.1:` has a version suffix) and when
        # it would have, SWITCH_INSTALL_MODE was the wrong repair for a missing
        # system library. `missing_soname` above owns that message now.
        ("build_mode", re.compile(r"(?:attempted relative import|"
                                  r"cannot import name '\w+' from partially initialized|"
                                  r"No module named '\w+\.\w*(?:_C|ext|cython)\w*')"),
         simple("BUILD_MODE_MISMATCH", "SWITCH_INSTALL_MODE", "installed")),
        # The L1 probe found no metadata for a requested distribution: the
        # install step did not deliver it. At L1 that is unambiguous.
        # `ImportError: cannot import name 'Quantity' from 'vivarium.core.serialize'`:
        # the package imported, the symbol is gone -- an API break between the
        # model's era and the resolved version. The dependent's dist (via the
        # module table) is the thing to pin; the version is the agent's call.
        # A framework that discovers plugins over INSTALLED distributions finds
        # nothing when the model is only on PYTHONPATH. process-bigraph's
        # `allocate_core()` is the corpus case (spatio-flux, four passes); pluggy
        # and stevedore report the same shape. Only meaningful in mounted mode.
        ("plugin_not_discovered",
         re.compile(r"no link found at address: \{'protocol': 'local'|"
                    r"No (?:process|step|plugin|entry ?point) (?:registered|found) (?:for|named) |"
                    r"stevedore\.exception\.NoMatches|PluginValidationError"),
         lambda m, c: Classification("BUILD_MODE_MISMATCH", action="SWITCH_INSTALL_MODE", arg="installed",
                                     evidence=m.group(0)) if c.get("install_mode", "mounted") == "mounted"
         else Classification("RUNTIME_ERROR", evidence=m.group(0))),
        ("cannot_import_name",
         re.compile(r"ImportError: cannot import name '\w+' from '([\w.]+)'"),
         lambda m, _c: Classification("ABI_MISMATCH", action="PIN_PKG",
                                      arg=MODULE_PYPI.get(m.group(1).split(".")[0], m.group(1).split(".")[0]),
                                      evidence=m.group(0))),
        ("dist_not_installed",
         re.compile(r"PackageNotFoundError: distribution '([\w.\-]+)' is not installed"),
         lambda m, _c: Classification("MISSING_DEPENDENCY", action="ADD_PKG", arg=m.group(1),
                                      evidence=m.group(0))),
        ("py_module", re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)'?"),
         module_not_found),
        ("r_module", re.compile(r"there is no package called ['‘](\S+?)['’]"), r_missing_pkg),
        # A model that writes cwd-relative files (`out/`, `studies/`, `report/`)
        # hits the read-only code mount. The repair is to run it FROM the
        # writable output volume -- `workdir=/outputs` -- which is only safe
        # because ladder.resolve_command anchors the script to code_path.
        ("mount_denied", re.compile(r"(?:Permission denied|Read-only file system)[^\n]{0,40}?'([^'\n]+)'"),
         mount_denied),
        # `FileNotFoundError: './investigations/x.yaml'` at L3 after the workdir
        # was moved off code_path: the model reads cwd-relative too, so neither
        # workdir nor output_path can satisfy it -- run it in a writable copy.
        ("cwd_relative_missing",
         re.compile(r"(?:FileNotFoundError|No such file or directory)[^\n]{0,40}?'(\.{1,2}/[^'\n]*|[\w.\-]+/[^'\n]*)'"),
         cwd_relative_missing),
        ("mount_missing_dir", re.compile(r"(?:FileNotFoundError|No such file or directory).{0,80}?"
                                         r"'?(/outputs|/inputs|/model)"),
         simple("MOUNT_CONTRACT_ERROR", "FIX_MOUNT_CONTRACT")),
        ("license", re.compile(r"[Ll]icen[cs]e (?:file|key|server).{0,40}(?:not found|required|invalid)|"
                               r"GUROBI_HOME|CPLEX.*licen"),
         simple("LICENSE_REQUIRED")),
        ("unsupported", re.compile(r"MATLAB|COMSOL|no such host|unsupported architecture|"
                                   r"exec format error"),
         simple("UNSUPPORTED_TOOLCHAIN")),
        ("timeout", re.compile(r"context deadline exceeded|ENVBUILD_TIMEOUT|DeadlineExceeded"), l3_deadline),
        # Synthetic markers emitted by ladder.py, so its own failures classify
        # through the same table as real stderr.
        ("no_entrypoint", re.compile(r"ENVBUILD_ENTRYPOINT_MISSING:.*"), simple("ENTRYPOINT_UNKNOWN")),
        ("no_output", re.compile(r"ENVBUILD_NO_OUTPUT:.*"),
         simple("MOUNT_CONTRACT_ERROR", "FIX_MOUNT_CONTRACT")),
    ]


RULES = _rules()


def classify(stderr: str, rung: str = "L0", local_modules=(), exit_code: int | None = None,
             mount: dict | None = None, install_mode: str = "mounted",
             requires_python: str | None = None, l3_timeout_s: int | None = None) -> Classification:
    """Match the rule table against `stderr`.

    `rung` and `local_modules` disambiguate the same text across the ladder --
    see `module_not_found`. Returns UNKNOWN (routes_to="llm") when nothing fires;
    that is the signal to fall back to the LLM classifier in specs/repair.md.
    """
    # Decode first. The rules regex plain text; a rawjson progress frame or an
    # ANSI-coloured Kaniko line matches nothing and the row becomes UNKNOWN.
    text = logs.decode(stderr)
    for name, pat, handler in RULES:
        m = pat.search(text)
        if m:
            mount = mount or {}
            c = handler(m, {"rung": rung, "local_modules": set(local_modules), "text": text,
                            "code_path": mount.get("code_path", "/model"),
                            "install_mode": install_mode, "requires_python": requires_python,
                            "l3_timeout_s": l3_timeout_s,
                            "workdir_moved": bool(mount) and mount.get("workdir") != mount.get("code_path")})
            c.rule = name
            # A class that normally dead-letters may still carry one typed
            # action from its handler (TIMEOUT at L3 -> SET_L3_TIMEOUT). The
            # action is the routing then; the class keeps its name.
            c.routes_to = "retry" if c.action else ROUTING.get(c.failure_class, "retry")
            return c
    # A non-zero exit with no recognised message at run time is still a runtime
    # failure -- classify it rather than pretending it is unknown.
    if rung == "L3" and exit_code not in (None, 0):
        c = Classification("RUNTIME_ERROR", evidence=(text.strip().splitlines() or [""])[-1])
        c.routes_to = ROUTING["RUNTIME_ERROR"]
        return c
    return Classification("UNKNOWN", classified_by="llm", routes_to="llm",
                          evidence="\n".join((text.strip().splitlines() or [""])[-5:]))
