"""Helpers for the live-run tests (ADR 0010): command writers in the server's and the pane's way,
outcome and event readers, and the source of a C FMU that discards a step without terminating.

Collaborators: ``tests/live/conftest.py`` (the fixtures that use them) and the test modules,
which import this module by name because pytest puts this directory on ``sys.path``.
``agentic_fmi.store.inbox`` writes server commands; pane commands are written as the pane does
(one non-atomic write of the final name). The Discarder FMU is compiled with ``gcc`` against
FMPy's FMI 2.0 headers; only worker processes load it.
"""

from __future__ import annotations

import json
import subprocess
import time
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import fmpy
import pytest

from agentic_fmi.errors import ProtocolError
from agentic_fmi.fmu.inspect import read_description, sha256_file
from agentic_fmi.live import SINGLE_FMU
from agentic_fmi.store import inbox, runstore
from agentic_fmi.store.protocol import (
    Command,
    CommandOutcome,
    ComponentSpec,
    Experiment,
    FileRef,
    FmuRef,
    MasterAlgorithm,
    OmsVersion,
    SystemConnection,
    SystemSpec,
    Versions,
    utc_now,
)


def command(source: inbox.Source, action: str, **arguments: Any) -> Command:
    """A valid command with a fresh ID; IDs made within one test keep their creation order."""
    time.sleep(0.002)  # distinct milliseconds, so name order is creation order
    return Command(
        id=inbox.new_command_id(source),
        source=source,
        issued_at=utc_now(),
        action=action,  # type: ignore[arg-type]
        **arguments,
    )


def send(run_path: Path, action: str, **arguments: Any) -> str:
    """Write a server command (exclusive create) and return its ID."""
    sent = command("server", action, **arguments)
    inbox.write_command(run_path, sent)
    return sent.id


def send_pane(run_path: Path, action: str, **arguments: Any) -> str:
    """Write a pane command the way the pane does: the final name, written in place."""
    sent = command("pane", action, **arguments)
    path = run_path / inbox.COMMANDS_DIR / f"{sent.id}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(sent.model_dump_json())
    return sent.id


def outcome_of(run_path: Path, command_id: str, timeout_s: float = 20.0) -> CommandOutcome:
    """Poll for the outcome of ``command_id``; fail the test after ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            found = inbox.read_outcome(run_path, command_id)
        except ProtocolError:
            found = None
        if found is not None:
            return found
        if time.monotonic() > deadline:
            log = (run_path / runstore.LOG_FILE).read_text(errors="replace")[-2000:]
            pytest.fail(f"no outcome for {command_id} within {timeout_s} s; log:\n{log}")
        time.sleep(0.02)


def command_events(store: Path, run_id: str) -> list[dict[str, Any]]:
    """The ``command`` events of one run, in order."""
    lines = (store / runstore.EVENTS_FILE).read_text().splitlines()
    events = [json.loads(line) for line in lines if line]
    return [e for e in events if e["run_id"] == run_id and e["type"] == "command"]


DISCARDER_C = r"""/* Discarder: y = t; a step past t_discard is discarded, never terminated. */
#include <stdlib.h>
#include "fmi2Functions.h"

typedef struct { double t, t_discard; } M;
#define U(x) (void)(x)

const char *fmi2GetTypesPlatform(void) { return fmi2TypesPlatform; }
const char *fmi2GetVersion(void) { return fmi2Version; }
fmi2Status fmi2SetDebugLogging(fmi2Component c, fmi2Boolean on, size_t n, const fmi2String k[]) {
    U(c); U(on); U(n); U(k); return fmi2OK; }
fmi2Component fmi2Instantiate(fmi2String name, fmi2Type type, fmi2String guid, fmi2String res,
                              const fmi2CallbackFunctions *f, fmi2Boolean vis, fmi2Boolean log) {
    U(name); U(type); U(guid); U(res); U(f); U(vis); U(log);
    M *m = calloc(1, sizeof(M));
    if (m) m->t_discard = 0.5;
    return m;
}
void fmi2FreeInstance(fmi2Component c) { free(c); }
fmi2Status fmi2SetupExperiment(fmi2Component c, fmi2Boolean td, fmi2Real tol, fmi2Real t0,
                               fmi2Boolean sd, fmi2Real stop) {
    U(td); U(tol); U(sd); U(stop); ((M *)c)->t = t0; return fmi2OK; }
fmi2Status fmi2EnterInitializationMode(fmi2Component c) { U(c); return fmi2OK; }
fmi2Status fmi2ExitInitializationMode(fmi2Component c) { U(c); return fmi2OK; }
fmi2Status fmi2Terminate(fmi2Component c) { U(c); return fmi2OK; }
fmi2Status fmi2Reset(fmi2Component c) { U(c); return fmi2OK; }
fmi2Status fmi2GetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Real v[]) {
    M *m = c;
    for (size_t i = 0; i < n; i++) {
        if (vr[i] == 1) v[i] = m->t;
        else if (vr[i] == 2) v[i] = m->t_discard;
        else return fmi2Error;
    }
    return fmi2OK;
}
fmi2Status fmi2SetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n,
                       const fmi2Real v[]) {
    M *m = c;
    for (size_t i = 0; i < n; i++) {
        if (vr[i] != 2) return fmi2Error;
        m->t_discard = v[i];
    }
    return fmi2OK;
}
#define NONE(T) \
    fmi2Status fmi2Get##T(fmi2Component c, const fmi2ValueReference r[], size_t n, fmi2##T v[]) \
    { U(c); U(r); U(v); return n ? fmi2Error : fmi2OK; } \
    fmi2Status fmi2Set##T(fmi2Component c, const fmi2ValueReference r[], size_t n, \
                          const fmi2##T v[]) \
    { U(c); U(r); U(v); return n ? fmi2Error : fmi2OK; }
NONE(Integer)
NONE(Boolean)
NONE(String)
fmi2Status fmi2GetFMUstate(fmi2Component c, fmi2FMUstate *s) { U(c); U(s); return fmi2Error; }
fmi2Status fmi2SetFMUstate(fmi2Component c, fmi2FMUstate s) { U(c); U(s); return fmi2Error; }
fmi2Status fmi2FreeFMUstate(fmi2Component c, fmi2FMUstate *s) { U(c); U(s); return fmi2Error; }
fmi2Status fmi2SerializedFMUstateSize(fmi2Component c, fmi2FMUstate s, size_t *n) {
    U(c); U(s); U(n); return fmi2Error; }
fmi2Status fmi2SerializeFMUstate(fmi2Component c, fmi2FMUstate s, fmi2Byte b[], size_t n) {
    U(c); U(s); U(b); U(n); return fmi2Error; }
fmi2Status fmi2DeSerializeFMUstate(fmi2Component c, const fmi2Byte b[], size_t n,
                                   fmi2FMUstate *s) {
    U(c); U(b); U(n); U(s); return fmi2Error; }
fmi2Status fmi2GetDirectionalDerivative(fmi2Component c, const fmi2ValueReference a[], size_t na,
                                        const fmi2ValueReference b[], size_t nb,
                                        const fmi2Real dv[], fmi2Real r[]) {
    U(c); U(a); U(na); U(b); U(nb); U(dv); U(r); return fmi2Error; }
fmi2Status fmi2SetRealInputDerivatives(fmi2Component c, const fmi2ValueReference vr[], size_t n,
                                       const fmi2Integer o[], const fmi2Real v[]) {
    U(c); U(vr); U(n); U(o); U(v); return fmi2Error; }
fmi2Status fmi2GetRealOutputDerivatives(fmi2Component c, const fmi2ValueReference vr[], size_t n,
                                        const fmi2Integer o[], fmi2Real v[]) {
    U(c); U(vr); U(n); U(o); U(v); return fmi2Error; }
fmi2Status fmi2DoStep(fmi2Component c, fmi2Real t, fmi2Real h, fmi2Boolean np) {
    M *m = c; U(np);
    if (t + h > m->t_discard + 1e-9) return fmi2Discard;
    m->t = t + h;
    return fmi2OK;
}
fmi2Status fmi2CancelStep(fmi2Component c) { U(c); return fmi2Error; }
fmi2Status fmi2GetStatus(fmi2Component c, const fmi2StatusKind s, fmi2Status *v) {
    U(c); U(s); *v = fmi2OK; return fmi2OK; }
fmi2Status fmi2GetRealStatus(fmi2Component c, const fmi2StatusKind s, fmi2Real *v) {
    U(s); *v = ((M *)c)->t; return fmi2OK; }
fmi2Status fmi2GetIntegerStatus(fmi2Component c, const fmi2StatusKind s, fmi2Integer *v) {
    U(c); U(s); U(v); return fmi2Error; }
fmi2Status fmi2GetBooleanStatus(fmi2Component c, const fmi2StatusKind s, fmi2Boolean *v) {
    U(c); U(s); *v = fmi2False; return fmi2OK; }
fmi2Status fmi2GetStringStatus(fmi2Component c, const fmi2StatusKind s, fmi2String *v) {
    U(c); U(s); U(v); return fmi2Error; }
"""

DISCARDER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="Discarder"
  guid="{8c4e810f-3df3-4a00-8276-176fa3c9f001}" generationTool="Agentic FMI tests"
  numberOfEventIndicators="0">
  <CoSimulation modelIdentifier="Discarder" canHandleVariableCommunicationStepSize="true"/>
  <DefaultExperiment startTime="0" stopTime="1" stepSize="0.1"/>
  <ModelVariables>
    <ScalarVariable name="y" valueReference="1" causality="output" variability="continuous"
      initial="exact"><Real start="0"/></ScalarVariable>
    <ScalarVariable name="t_discard" valueReference="2" causality="parameter"
      variability="fixed" initial="exact"><Real start="0.5"/></ScalarVariable>
  </ModelVariables>
  <ModelStructure>
    <Outputs><Unknown index="1"/></Outputs>
  </ModelStructure>
</fmiModelDescription>
"""


def build_discarder(directory: Path) -> Path:
    """Compile and zip the Discarder FMU (FMI 2.0 CS, linux64) into ``directory``."""
    headers = Path(fmpy.__file__).parent / "c-code"
    library = directory / "binaries" / "linux64" / "Discarder.so"
    library.parent.mkdir(parents=True)
    source = directory / "Discarder.c"
    source.write_text(DISCARDER_C)
    command = ["gcc", "-shared", "-fPIC", "-O2", f"-I{headers}", str(source), "-o", str(library)]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    fmu = directory / "Discarder.fmu"
    with zipfile.ZipFile(fmu, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("modelDescription.xml", DISCARDER_XML)
        archive.write(library, "binaries/linux64/Discarder.so")
    return fmu


def descriptions_of(path: Path) -> Mapping[str, Any]:
    """``{SINGLE_FMU: model description}`` of one FMU (XML only)."""
    return {SINGLE_FMU: read_description(path)}


def system_spec(
    run_id: str,
    components: Mapping[str, Path],
    connections: Sequence[tuple[str, str]],
    outputs: Sequence[str],
) -> SystemSpec:
    """A live ``SystemSpec`` of ``components`` (no file on disk is read but the FMUs' hashes)."""
    return SystemSpec(
        run_id=run_id,
        created_at=utc_now(),
        name="loop",
        system=FileRef(path="/nowhere/loop.json", sha256="0" * 64, size_bytes=1),
        components={
            name: ComponentSpec(
                fmu=FmuRef(
                    path=str(path),
                    sha256=sha256_file(path),
                    size_bytes=path.stat().st_size,
                    fmi_version=read_description(path).fmiVersion,
                    model_name=read_description(path).modelName,
                )
            )
            for name, path in components.items()
        },
        connections=[SystemConnection(from_=a, to=b) for a, b in connections],
        master=MasterAlgorithm(step_size=0.01),
        experiment=Experiment(start_time=0.0, stop_time=1.0, output_interval=0.01),
        outputs=list(outputs),
        timeout_s=60.0,
        versions=Versions.current(),
        omsimulator=OmsVersion(version="0", sha256="0" * 64),
        live=True,
        idle_limit_s=60.0,
    )
