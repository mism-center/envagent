"""Errors shared by the two seams.

One class, deliberately its own module: `Builder` and `Verifier` are peers, and
neither should have to import the other just to name a shared failure mode.
"""


class InfraError(RuntimeError):
    """The build/verify substrate is unavailable.

    Daemon unreachable, socket permission denied, no cluster to build in. This
    is a property of the *deployment*, never of the model -- so it must never
    become an attempt row. A corpus row saying `UNKNOWN` when the real answer was
    "the agent could not reach dockerd" is a row that teaches the memory layer a
    lie, and it burns an attempt of the job's budget to do it.

    Carries remediation text: whoever sees this needs to fix their setup, and the
    fix is never obvious from "permission denied".
    """
