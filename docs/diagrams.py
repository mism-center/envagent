"""Render the architecture diagrams for docs/envbuild_architecture.md.

    python docs/diagrams.py            -> docs/arch_*.png

Needs graphviz (the `dot` binary) and the python-graphviz package. Keep the
node/edge lists here in step with the Mermaid appendix of the page.
"""
from pathlib import Path

import graphviz

OUT = Path(__file__).parent
FONT = "Helvetica"
STORE = dict(shape="cylinder", fillcolor="#eef3f8")
ENVB = dict(fillcolor="#e3eefb", color="#1f77b4")
GREEN = dict(color="#2ca02c", fontcolor="#2ca02c", penwidth="1.4")
LBL = dict(shape="plaintext", style="", fontsize="8.5", margin="0.02", width="0", height="0")


def base(name, rankdir="LR"):
    g = graphviz.Digraph(name, format="png")
    g.attr(rankdir=rankdir, dpi="200", fontname=FONT, fontsize="11", nodesep="0.35", ranksep="0.6",
           bgcolor="white", pad="0.2")
    g.attr("node", fontname=FONT, fontsize="10", shape="box", style="rounded,filled", fillcolor="#f7f9fb",
           color="#4a5568", penwidth="1.1", margin="0.15,0.08")
    g.attr("edge", fontname=FONT, fontsize="8.5", color="#4a5568", arrowsize="0.7")
    return g


def system_context():
    g = base("context", rankdir="TB")
    g.attr(ranksep="0.55", nodesep="0.5", splines="ortho", compound="true")
    with g.subgraph(name="cluster_platform") as c:
        c.attr(label="Rest of the platform", style="rounded,dashed", color="#9aa5b1", fontname=FONT,
               fontsize="11", labeljust="l")
        c.node("REG", "Model registry\n(model_id, version)")
        c.node("ANN", "Annotator\nmetadata-package/")
        c.node("STORE", "Model store — PVC irods-pvc\n/models/<model_id>/<version>", **STORE)
        c.node("L1", "[1] MODEL_ID, MODEL_REPO,\nANNOTATION  (Job env)", **LBL)
        c.node("L23", "[2] ro mount /models\n[3] annotation read as\nsource-tagged suggestions", **LBL)
    with g.subgraph(name="cluster_envbuild") as c:
        c.attr(label="envbuild  (namespace: default)", style="rounded", color="#1f77b4", fontname=FONT,
               fontsize="11", labeljust="l", bgcolor="#f4f8fd")
        c.node("JOB", "Job envbuild-<job>\nimage pi-envagent:revX  (LLM agent + driver)", **ENVB)
        c.node("KAN", "Kaniko build pod\n(one per attempt)", **ENVB)
        c.node("VER", "Verify pods\nL1 / L2 / L3", **ENVB)
        c.node("WORK", "PVC envbuild-work — /work/records\nattempts.jsonl · verdicts.jsonl · jobs/<job>/*",
               shape="cylinder", fillcolor="#e3eefb", color="#1f77b4")
    g.node("REGISTRY", "Image registry\ndocker.io/mismplatform/envbuild:<job>-a<N>", **STORE)
    g.node("LLM", "LLM provider\nSecret envbuild-llm", fillcolor="#fff7e6", color="#b7791f")
    g.node("RUNNER", "Model runner\n(consumes verified images)")
    g.edge("ANN", "STORE", xlabel="execution.yaml\nmetadata.yaml  ")
    g.edge("REG", "L1", arrowhead="none")
    g.edge("L1", "JOB")
    g.edge("STORE", "L23", arrowhead="none")
    g.edge("L23", "JOB")
    g.edge("LLM", "JOB", dir="both")
    g.edge("JOB", "KAN", xlabel="build ")
    g.edge("KAN", "REGISTRY", xlabel="push ")
    g.edge("REGISTRY", "VER", xlabel=" pull by digest")
    g.edge("JOB", "VER", xlabel=" verify")
    g.edge("STORE", "VER", xlabel="ro mount /model ", style="dashed")
    g.edge("JOB", "WORK")
    g.edge("VER", "WORK", style="invis")
    g.edge("WORK", "RUNNER", xlabel="[4] verdict: image_digest, lockfile_sha256,\ncode_revision, mount contract",
           **GREEN)
    g.edge("WORK", "ANN", xlabel="[5] annotation-patch.yaml\n(proposals; never edits the annotation)  ",
           constraint="false", **GREEN)
    return g


def job_loop():
    g = base("job", rankdir="TB")
    g.node("INIT", "envbuild init\nscan repo → evidence.json\nread annotation → findings[]\n"
                   "choose entry point (source-tagged)\ndraft EnvSpec", **ENVB)
    g.node("ATT", "envbuild attempt\nrender Dockerfile → Kaniko → push (L0)\nclimb ladder  L1 → L2 → L3", **ENVB)
    g.node("CLS", "classify\nrule table first;\nLLM only if no rule matches", shape="diamond",
           fillcolor="#fff7e6", color="#b7791f", margin="0.05")
    g.node("PATCH", "envbuild patch — ONE typed action\n"
                    "ADD_PKG · PIN_PKG · ADD_APT_PKG · CHANGE_INTERPRETER_VERSION\n"
                    "SWITCH_INSTALL_MODE · FIX_MOUNT_CONTRACT · SET_ENTRYPOINT\nSET_L3_TIMEOUT · …", **ENVB)
    g.node("INFRA", "substrate failure\n(DNS, registry, scheduling)\nnot charged · not patchable\n"
                    "re-run the same spec", fillcolor="#f1f1f1", color="#888888")
    g.node("VERD", "envbuild verdict\nverified | failed | escalated | error\nalways written · teardown in finally\n"
                   "→ verdicts.jsonl, annotation-patch.yaml", fillcolor="#e6f4ea", color="#2ca02c")
    g.edge("INIT", "ATT")
    g.edge("ATT", "CLS")
    g.edge("CLS", "PATCH", label="spec problem")
    g.edge("PATCH", "ATT", label="next attempt")
    g.edge("CLS", "INFRA", label="infra")
    g.edge("INFRA", "ATT")
    g.edge("CLS", "VERD", label="L3 passed", color="#2ca02c", fontcolor="#2ca02c")
    g.edge("CLS", "VERD", label="budget: 5 attempts /\n1200 s search / no typed repair", color="#c0392b",
           fontcolor="#c0392b")
    with g.subgraph(name="cluster_ladder") as c:
        c.attr(label="verification ladder", style="rounded,dashed", color="#9aa5b1", fontname=FONT,
               fontsize="10", labeljust="l")
        c.attr("node", shape="plaintext", style="", fontsize="9")
        c.node("LAD", '<<table border="0" cellborder="1" cellspacing="0" cellpadding="4">'
               '<tr><td bgcolor="#eef3f8"><b>L0</b></td><td align="left">image builds and pushes</td>'
               '<td align="left">→ spec (failed_step_index)</td></tr>'
               '<tr><td bgcolor="#eef3f8"><b>L1</b></td><td align="left">deps import, image ALONE; lockfile read</td>'
               '<td align="left">→ image (ours)</td></tr>'
               '<tr><td bgcolor="#eef3f8"><b>L2</b></td><td align="left">code mounted; entry imports resolve</td>'
               '<td align="left">→ mount contract / code</td></tr>'
               '<tr><td bgcolor="#eef3f8"><b>L3</b></td><td align="left">example runs, writes /outputs</td>'
               '<td align="left">→ runtime</td></tr></table>>')
    g.edge("ATT", "LAD", style="invis")
    return g


def deployment():
    g = base("deploy")
    with g.subgraph(name="cluster_ns") as c:
        c.attr(label="Kubernetes namespace: default   (all envbuild objects: name envbuild-*, label app=envbuild)",
               style="rounded", color="#1f77b4", fontname=FONT, fontsize="11", labeljust="l", bgcolor="#f4f8fd")
        c.node("SA", "ServiceAccount envbuild\nRole envbuild-runner:\npods create/get/list/delete · pods/log get",
               fillcolor="#f1f1f1")
        c.node("J", "Job envbuild-<job>\nbackoffLimit 0 · ttl 24 h", **ENVB)
        c.node("K", "Pod kaniko (per attempt)\ncontext /work/<job>/ctx\nnetwork: package installs", **ENVB)
        c.node("V", "Pod verify (per rung)\nactiveDeadlineSeconds\nNetworkPolicy envbuild-deny-all", **ENVB)
        c.node("PW", "PVC envbuild-work\n50Gi RWX", **STORE)
        c.node("PM", "PVC irods-pvc\n(models, ro)", **STORE)
        c.node("S1", "Secret envbuild-registry-auth\n.docker/config.json", fillcolor="#fff7e6", color="#b7791f")
        c.node("S2", "Secret envbuild-llm\nAZURE_OPENAI_* | ANTHROPIC_API_KEY", fillcolor="#fff7e6", color="#b7791f")
        c.node("C1", "ConfigMap envbuild-tuning\nconfig.ini overrides", fillcolor="#fff7e6", color="#b7791f")
    g.edge("SA", "J", label="runs as", style="dashed")
    g.edge("J", "K", label="creates")
    g.edge("J", "V", label="creates")
    for n in ("J", "K", "V"):
        g.edge(n, "PW", arrowhead="none", color="#9aa5b1")
    for n in ("J", "V"):
        g.edge(n, "PM", arrowhead="none", color="#9aa5b1")
    g.edge("S1", "K", arrowhead="none", color="#b7791f")
    g.edge("S2", "J", arrowhead="none", color="#b7791f")
    g.edge("C1", "J", arrowhead="none", color="#b7791f")
    return g


if __name__ == "__main__":
    for name, fn in [("arch_1_system_context", system_context), ("arch_2_job_loop", job_loop),
                     ("arch_3_deployment", deployment)]:
        fn().render(str(OUT / name), cleanup=True)
        print(OUT / f"{name}.png")
