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
    "ENTRYPOINT_UNKNOWN": "submitter",
    "MISSING_DATA_FILE": "submitter",
    "LICENSE_REQUIRED": "submitter",
    "UNSUPPORTED_TOOLCHAIN": "dead-letter",
    "TIMEOUT": "dead-letter",
    "RUNTIME_ERROR": "dead-letter",
    "UNKNOWN": "llm",
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
MODULE_PYPI = {
    "sklearn": "scikit-learn", "cv2": "opencv-python-headless", "yaml": "PyYAML",
    "PIL": "Pillow", "Bio": "biopython", "skimage": "scikit-image",
    "mpl_toolkits": "matplotlib", "serial": "pyserial", "OpenGL": "PyOpenGL",
    "dateutil": "python-dateutil", "attr": "attrs", "pkg_resources": "setuptools",
    "google": "protobuf", "Crypto": "pycryptodome", "usb": "pyusb",
    "OpenSSL": "pyOpenSSL", "zmq": "pyzmq", "cairo": "pycairo",
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
        mod = m.group(1).split(".")[0]
        # Rung-aware: the same text means different things at L1 and L2. At L1 no
        # code is mounted, so it can only be a missing dependency. At L2+ a module
        # the repo itself defines means the mount/PYTHONPATH is wrong -- installing
        # a same-named PyPI package would "succeed" while shadowing the real code.
        if ctx.get("rung") in ("L2", "L3") and mod in ctx.get("local_modules", ()):
            return Classification("IMPORT_PATH_ERROR", action="FIX_MOUNT_CONTRACT",
                                  arg=None, evidence=m.group(0))
        return Classification("MISSING_DEPENDENCY", action="ADD_PKG",
                              arg=MODULE_PYPI.get(mod, mod), evidence=m.group(0))

    def r_missing_pkg(m, _ctx):
        return Classification("MISSING_DEPENDENCY", action="ADD_PKG",
                              arg=m.group(1), evidence=m.group(0))

    def simple(cls, action=None, arg=None):
        return lambda m, _ctx: Classification(cls, action=action, arg=arg, evidence=m.group(0))

    return [
        ("missing_header", re.compile(r"fatal error: (\S+\.h(?:pp)?): No such file"), missing_header),
        ("missing_header_r",
         re.compile(r"configure: error: (?:lib)?(\w+)[- ]?(?:dev(?:el)?)? "
                    r"(?:library )?not found"),
         lambda m, _c: Classification("MISSING_SYSTEM_LIB", action="ADD_APT_PKG",
                                      arg=f"lib{m.group(1)}-dev", evidence=m.group(0))),
        ("dep_conflict", re.compile(r"ResolutionImpossible|conflict is caused by|"
                                    r"versions have conflicting dependencies"),
         simple("DEP_RESOLUTION_CONFLICT", "UNPIN_PKG")),
        ("no_matching_dist", re.compile(r"No matching distribution found for (\S+)"),
         lambda m, _c: Classification("DEP_RESOLUTION_CONFLICT", action="UNPIN_PKG",
                                      arg=m.group(1), evidence=m.group(0))),
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
        ("build_mode", re.compile(r"(?:attempted relative import|"
                                  r"cannot import name '\w+' from partially initialized|"
                                  r"\.so: cannot open shared object file|"
                                  r"No module named '\w+\.\w*(?:_C|ext|cython)\w*')"),
         simple("BUILD_MODE_MISMATCH", "SWITCH_INSTALL_MODE", "installed")),
        ("py_module", re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)'?"),
         module_not_found),
        ("r_module", re.compile(r"there is no package called ['‘](\S+?)['’]"), r_missing_pkg),
        ("mount_denied", re.compile(r"(?:Permission denied|Read-only file system).{0,80}?"
                                    r"(/outputs|/inputs|/model)"),
         simple("MOUNT_CONTRACT_ERROR", "FIX_MOUNT_CONTRACT")),
        ("mount_missing_dir", re.compile(r"(?:FileNotFoundError|No such file or directory).{0,80}?"
                                         r"'?(/outputs|/inputs|/model)"),
         simple("MOUNT_CONTRACT_ERROR", "FIX_MOUNT_CONTRACT")),
        ("license", re.compile(r"[Ll]icen[cs]e (?:file|key|server).{0,40}(?:not found|required|invalid)|"
                               r"GUROBI_HOME|CPLEX.*licen"),
         simple("LICENSE_REQUIRED")),
        ("unsupported", re.compile(r"MATLAB|COMSOL|no such host|unsupported architecture|"
                                   r"exec format error"),
         simple("UNSUPPORTED_TOOLCHAIN")),
        ("timeout", re.compile(r"context deadline exceeded|ENVBUILD_TIMEOUT"), simple("TIMEOUT")),
        # Synthetic markers emitted by ladder.py, so its own failures classify
        # through the same table as real stderr.
        ("no_entrypoint", re.compile(r"ENVBUILD_ENTRYPOINT_MISSING:.*"), simple("ENTRYPOINT_UNKNOWN")),
        ("no_output", re.compile(r"ENVBUILD_NO_OUTPUT:.*"),
         simple("MOUNT_CONTRACT_ERROR", "FIX_MOUNT_CONTRACT")),
    ]


RULES = _rules()


def classify(stderr: str, rung: str = "L0", local_modules=(), exit_code: int | None = None
             ) -> Classification:
    """Match the rule table against `stderr`.

    `rung` and `local_modules` disambiguate the same text across the ladder --
    see `module_not_found`. Returns UNKNOWN (routes_to="llm") when nothing fires;
    that is the signal to fall back to the LLM classifier in specs/repair.md.
    """
    text = stderr or ""
    for name, pat, handler in RULES:
        m = pat.search(text)
        if m:
            c = handler(m, {"rung": rung, "local_modules": set(local_modules)})
            c.rule = name
            c.routes_to = ROUTING.get(c.failure_class, "retry")
            return c
    # A non-zero exit with no recognised message at run time is still a runtime
    # failure -- classify it rather than pretending it is unknown.
    if rung == "L3" and exit_code not in (None, 0):
        c = Classification("RUNTIME_ERROR", evidence=(text.strip().splitlines() or [""])[-1])
        c.routes_to = ROUTING["RUNTIME_ERROR"]
        return c
    return Classification("UNKNOWN", classified_by="llm", routes_to="llm",
                          evidence="\n".join((text.strip().splitlines() or [""])[-5:]))
