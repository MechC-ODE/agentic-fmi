"""Spike (i): live step control in FMPy and OMSimulator (the live-run engine probe).

Throwaway evidence for docs/spikes.md, section (i); not product code. The parent builds the
probe FMUs into ``.cache/spikes/i/fmus`` and runs every probe in a fresh child Python process
(``--child NAME CFG``), the way a worker would; the parent never imports FMPy's FMU classes or
OMSimulator. Each child runs a step gate like the live worker's: commands are applied in the
simulation thread after a step, a paused gate blocks there while another thread (the inbox
reader's role) feeds it ``step``, ``set`` and ``resume``, and a heartbeat thread keeps beating.

Probes:
- FMPy Co-Simulation, ``simulate_fmu(step_finished=...)`` against our own doStep loop on
  ``FMU2Slave``/``FMU3Slave``: set a tunable and an input, pause, single-step, resume, stop
  (a pythonfmu integrator, BouncingBall 2.0/3.0, the C probe FMU), bit for bit and against
  closed forms; an input that FMPy's input table also drives.
- FMPy Model Exchange, the callback with Event Mode against our own loop on FMPy's
  ``CVodeSolver``: BouncingBall ``e``, VanDerPol pause and a refused ``mu``, and the C probe's
  tunable ``k``, state ``jump``, termination time ``t_term`` and input ``u`` against closed forms.
- What FMPy and OMSimulator do when an FMU discards a step or asks to terminate (the C probe,
  a pythonfmu slave, Stair 2.0/3.0).
- OMSimulator after ``initialize()``: ``setValue`` on a tunable parameter, an unconnected input,
  a connected input and an output in a pythonfmu PI + exact-plant loop against the Jacobi
  recurrence; PI + the omc MSD (a fixed parameter); BouncingBall 2.0/3.0 ``e``; a 3 s pause.

The C probe ``LiveProbe`` (FMI 2.0, ME and CS): x' = -k*x + u; ``k``, ``jump``, ``t_term`` and
``t_discard`` are tunable (settable only in initialization or Event Mode in ME, like the
Reference FMUs); in Event Mode a non-zero ``jump`` is added to x (states changed); ``t_term``
is a time event that requests termination; in CS a step that reaches ``t_term`` ends there with
``fmi2Discard`` + ``fmi2Terminated`` and one that would pass ``t_discard`` is discarded.

Collaborators: FMPy (children only), OMSimulator (children only), pythonfmu (build CLI), omc
and gcc (builds), the Reference FMUs 0.0.41. Outputs go to ``.cache/spikes/i`` and the
container-local ``/tmp/afmi-i`` (removed at the end).

Run inside the dev container::

    uv run --no-sync python spikes/i_live_control.py
"""

from __future__ import annotations

import importlib.util
import inspect
import itertools
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import warnings
import zipfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT = REPO / ".cache" / "spikes" / "i"
TMP = Path("/tmp/afmi-i")  # container-local: OMSimulator instantiates 20 times faster here
FMUS = OUT / "fmus"
SRC = OUT / "src"
RUNS = OUT / "runs"
REF = REPO / ".cache" / "reference-fmus" / "0.0.41"
PI_SRC = REPO / "skills" / "fmu-authoring" / "examples" / "pi_controller.py"
MSD_SRC = REPO / "skills" / "fmu-authoring" / "examples" / "MSD.mo"
H, KP, KI, R, K, TAU = 0.01, 10.0, 15.0, 1.0, 2.0, 0.5
CHILD_TIMEOUT = 120

INTEGRATOR_PY = '''"""x <- x + k*u*h and y = k*u (pythonfmu CS slave); k tunable, u an input."""

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Fmi2Variability, Real


class Integrator(Fmi2Slave):
    author = "probe"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.k, self.u, self.x, self.y = 1.0, 1.0, 0.0, 0.0
        self.register_variable(
            Real("k", causality=Fmi2Causality.parameter, variability=Fmi2Variability.tunable)
        )
        self.register_variable(Real("u", causality=Fmi2Causality.input))
        self.register_variable(Real("x", causality=Fmi2Causality.output, initial=Fmi2Initial.exact))
        self.register_variable(Real("y", causality=Fmi2Causality.output, initial=Fmi2Initial.exact))

    def do_step(self, current_time, step_size):
        self.y = self.k * self.u
        self.x += self.y * step_size
        return True
'''

PLANT_PY = '''"""First-order plant, exact zero-order hold, extra unconnected input d (pythonfmu).

x <- a*x + K*(1-a)*(F + d), a = exp(-h/tau).
"""

import math

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Fmi2Variability, Real

TUNABLE = {"causality": Fmi2Causality.parameter, "variability": Fmi2Variability.tunable}


class Plant(Fmi2Slave):
    author = "probe"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.K, self.tau, self.F, self.d, self.x = 2.0, 0.5, 0.0, 0.0, 0.0
        self.register_variable(Real("K", **TUNABLE))
        self.register_variable(Real("tau", **TUNABLE))
        self.register_variable(Real("F", causality=Fmi2Causality.input))
        self.register_variable(Real("d", causality=Fmi2Causality.input))
        self.register_variable(Real("x", causality=Fmi2Causality.output, initial=Fmi2Initial.exact))

    def do_step(self, current_time, step_size):
        a = math.exp(-step_size / self.tau)
        self.x = a * self.x + self.K * (1.0 - a) * (self.F + self.d)
        return True
'''

DISCARD_PY = '''"""y = t until t = 0.5; then do_step returns False (pythonfmu CS slave)."""

from pythonfmu import Fmi2Causality, Fmi2Initial, Fmi2Slave, Real


class Discard(Fmi2Slave):
    author = "probe"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.y = 0.0
        self.register_variable(Real("y", causality=Fmi2Causality.output, initial=Fmi2Initial.exact))

    def do_step(self, current_time, step_size):
        if current_time + step_size > 0.5 + 1e-9:
            return False
        self.y = current_time + step_size
        return True
'''

MSD_MOS = """setCommandLineOptions("--fmiFlags=s:cvode"); getErrorString();
loadFile("{mo}"); getErrorString();
buildModelFMU(MSD, version="2.0", fmuType="me_cs"); getErrorString();
"""

LIVEPROBE_C = r"""/* LiveProbe: FMI 2.0 ME + CS probe FMU for spike (i); see the spike. */
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "fmi2Functions.h"

enum { VR_X = 1, VR_DX = 2, VR_K = 3, VR_U = 4, VR_JUMP = 5, VR_TTERM = 6, VR_TDISCARD = 7 };
typedef enum { M_INSTANTIATED, M_INIT, M_EVENT, M_CT, M_STEP, M_TERMINATED } Mode;
typedef struct {
    fmi2CallbackFunctions cb;
    char name[64];
    int cs;
    Mode mode;
    double t, x, k, u, jump, t_term, t_discard, last_ok;
    int terminated;
} Model;
#define EPS 1e-12
#define UNUSED(x) (void)(x)

static void say(Model *m, fmi2Status s, const char *msg) {
    if (m->cb.logger) m->cb.logger(m->cb.componentEnvironment, m->name, s, "probe", "%s", msg);
}
static void defaults(Model *m) {
    m->t = 0; m->x = 1; m->k = 1; m->u = 0; m->jump = 0;
    m->t_term = 1e9; m->t_discard = 1e9; m->last_ok = 0; m->terminated = 0;
}
static void advance(Model *m, double dt) {
    if (fabs(m->k) < 1e-15) m->x += m->u * dt;
    else m->x = (m->x - m->u / m->k) * exp(-m->k * dt) + m->u / m->k;
}

const char *fmi2GetTypesPlatform(void) { return fmi2TypesPlatform; }
const char *fmi2GetVersion(void) { return fmi2Version; }
fmi2Status fmi2SetDebugLogging(fmi2Component c, fmi2Boolean on, size_t n, const fmi2String k[]) {
    UNUSED(c); UNUSED(on); UNUSED(n); UNUSED(k); return fmi2OK;
}
fmi2Component fmi2Instantiate(fmi2String name, fmi2Type type, fmi2String guid, fmi2String res,
                              const fmi2CallbackFunctions *f, fmi2Boolean vis, fmi2Boolean log) {
    UNUSED(guid); UNUSED(res); UNUSED(vis); UNUSED(log);
    Model *m = calloc(1, sizeof(Model));
    if (!m) return NULL;
    if (f) m->cb = *f;
    strncpy(m->name, name ? name : "probe", sizeof(m->name) - 1);
    m->cs = type == fmi2CoSimulation;
    m->mode = M_INSTANTIATED;
    defaults(m);
    return m;
}
void fmi2FreeInstance(fmi2Component c) { free(c); }
fmi2Status fmi2SetupExperiment(fmi2Component c, fmi2Boolean td, fmi2Real tol, fmi2Real t0,
                               fmi2Boolean sd, fmi2Real stop) {
    UNUSED(td); UNUSED(tol); UNUSED(sd); UNUSED(stop);
    Model *m = c; m->t = t0; m->last_ok = t0; return fmi2OK;
}
fmi2Status fmi2EnterInitializationMode(fmi2Component c) {
    ((Model *)c)->mode = M_INIT; return fmi2OK;
}
fmi2Status fmi2ExitInitializationMode(fmi2Component c) {
    Model *m = c; m->mode = m->cs ? M_STEP : M_EVENT; return fmi2OK;
}
fmi2Status fmi2Terminate(fmi2Component c) { ((Model *)c)->mode = M_TERMINATED; return fmi2OK; }
fmi2Status fmi2Reset(fmi2Component c) {
    Model *m = c; defaults(m); m->mode = M_INSTANTIATED; return fmi2OK;
}
fmi2Status fmi2GetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n, fmi2Real v[]) {
    Model *m = c;
    for (size_t i = 0; i < n; i++) {
        switch (vr[i]) {
        case VR_X: v[i] = m->x; break;
        case VR_DX: v[i] = -m->k * m->x + m->u; break;
        case VR_K: v[i] = m->k; break;
        case VR_U: v[i] = m->u; break;
        case VR_JUMP: v[i] = m->jump; break;
        case VR_TTERM: v[i] = m->t_term; break;
        case VR_TDISCARD: v[i] = m->t_discard; break;
        default: say(m, fmi2Error, "unknown value reference"); return fmi2Error;
        }
    }
    return fmi2OK;
}
fmi2Status fmi2SetReal(fmi2Component c, const fmi2ValueReference vr[], size_t n,
                       const fmi2Real v[]) {
    Model *m = c;
    for (size_t i = 0; i < n; i++) {
        int early = m->mode == M_INSTANTIATED || m->mode == M_INIT;
        int tunable_ok = early || m->mode == M_EVENT || m->mode == M_STEP;
        switch (vr[i]) {
        case VR_X:
            if (!early) { say(m, fmi2Error, "x can only be set before initialization ends");
                          return fmi2Error; }
            m->x = v[i]; break;
        case VR_U:
            if (m->mode == M_TERMINATED) return fmi2Error;
            m->u = v[i]; break;
        case VR_K: case VR_JUMP: case VR_TTERM: case VR_TDISCARD:
            if (!tunable_ok) {
                say(m, fmi2Error, "a tunable parameter can only be set in initialization or "
                                  "event mode");
                return fmi2Error;
            }
            if (vr[i] == VR_K) m->k = v[i];
            else if (vr[i] == VR_JUMP) m->jump = v[i];
            else if (vr[i] == VR_TTERM) m->t_term = v[i];
            else m->t_discard = v[i];
            break;
        default: say(m, fmi2Error, "cannot set this value reference"); return fmi2Error;
        }
    }
    return fmi2OK;
}
#define NOVAL(T, CONST) \
    fmi2Status fmi2Get##T(fmi2Component c, const fmi2ValueReference r[], size_t n, fmi2##T v[]) \
    { UNUSED(c); UNUSED(r); UNUSED(v); return n ? fmi2Error : fmi2OK; } \
    fmi2Status fmi2Set##T(fmi2Component c, const fmi2ValueReference r[], size_t n, \
                          CONST fmi2##T v[]) \
    { UNUSED(c); UNUSED(r); UNUSED(v); return n ? fmi2Error : fmi2OK; }
NOVAL(Integer, const)
NOVAL(Boolean, const)
NOVAL(String, const)
fmi2Status fmi2GetFMUstate(fmi2Component c, fmi2FMUstate *s) {
    UNUSED(c); UNUSED(s); return fmi2Error; }
fmi2Status fmi2SetFMUstate(fmi2Component c, fmi2FMUstate s) {
    UNUSED(c); UNUSED(s); return fmi2Error; }
fmi2Status fmi2FreeFMUstate(fmi2Component c, fmi2FMUstate *s) {
    UNUSED(c); UNUSED(s); return fmi2Error; }
fmi2Status fmi2SerializedFMUstateSize(fmi2Component c, fmi2FMUstate s, size_t *n) {
    UNUSED(c); UNUSED(s); UNUSED(n); return fmi2Error; }
fmi2Status fmi2SerializeFMUstate(fmi2Component c, fmi2FMUstate s, fmi2Byte b[], size_t n) {
    UNUSED(c); UNUSED(s); UNUSED(b); UNUSED(n); return fmi2Error; }
fmi2Status fmi2DeSerializeFMUstate(fmi2Component c, const fmi2Byte b[], size_t n,
                                   fmi2FMUstate *s) {
    UNUSED(c); UNUSED(b); UNUSED(n); UNUSED(s); return fmi2Error; }
fmi2Status fmi2GetDirectionalDerivative(fmi2Component c, const fmi2ValueReference a[], size_t na,
                                        const fmi2ValueReference b[], size_t nb,
                                        const fmi2Real dv[], fmi2Real r[]) {
    UNUSED(c); UNUSED(a); UNUSED(na); UNUSED(b); UNUSED(nb); UNUSED(dv); UNUSED(r);
    return fmi2Error;
}
fmi2Status fmi2EnterEventMode(fmi2Component c) { ((Model *)c)->mode = M_EVENT; return fmi2OK; }
fmi2Status fmi2NewDiscreteStates(fmi2Component c, fmi2EventInfo *e) {
    Model *m = c;
    e->newDiscreteStatesNeeded = fmi2False;
    e->terminateSimulation = fmi2False;
    e->nominalsOfContinuousStatesChanged = fmi2False;
    e->valuesOfContinuousStatesChanged = fmi2False;
    if (m->jump != 0) {
        m->x += m->jump; m->jump = 0;
        e->valuesOfContinuousStatesChanged = fmi2True;
        say(m, fmi2OK, "jump applied in event mode");
    }
    if (m->t >= m->t_term - EPS) {
        e->terminateSimulation = fmi2True;
        say(m, fmi2OK, "t_term reached: requesting termination");
    }
    e->nextEventTimeDefined = (m->t_term > m->t + EPS && m->t_term < 1e8) ? fmi2True : fmi2False;
    e->nextEventTime = m->t_term;
    return fmi2OK;
}
fmi2Status fmi2EnterContinuousTimeMode(fmi2Component c) {
    ((Model *)c)->mode = M_CT; return fmi2OK; }
fmi2Status fmi2CompletedIntegratorStep(fmi2Component c, fmi2Boolean np, fmi2Boolean *ev,
                                       fmi2Boolean *term) {
    UNUSED(c); UNUSED(np); *ev = fmi2False; *term = fmi2False; return fmi2OK; }
fmi2Status fmi2SetTime(fmi2Component c, fmi2Real t) { ((Model *)c)->t = t; return fmi2OK; }
fmi2Status fmi2SetContinuousStates(fmi2Component c, const fmi2Real x[], size_t n) {
    UNUSED(n); ((Model *)c)->x = x[0]; return fmi2OK; }
fmi2Status fmi2GetDerivatives(fmi2Component c, fmi2Real dx[], size_t n) {
    Model *m = c; UNUSED(n); dx[0] = -m->k * m->x + m->u; return fmi2OK; }
fmi2Status fmi2GetEventIndicators(fmi2Component c, fmi2Real z[], size_t n) {
    UNUSED(c); UNUSED(z); UNUSED(n); return fmi2OK; }
fmi2Status fmi2GetContinuousStates(fmi2Component c, fmi2Real x[], size_t n) {
    UNUSED(n); x[0] = ((Model *)c)->x; return fmi2OK; }
fmi2Status fmi2GetNominalsOfContinuousStates(fmi2Component c, fmi2Real x[], size_t n) {
    UNUSED(c); UNUSED(n); x[0] = 1.0; return fmi2OK; }
fmi2Status fmi2SetRealInputDerivatives(fmi2Component c, const fmi2ValueReference vr[], size_t n,
                                       const fmi2Integer o[], const fmi2Real v[]) {
    UNUSED(c); UNUSED(vr); UNUSED(n); UNUSED(o); UNUSED(v); return fmi2Error; }
fmi2Status fmi2GetRealOutputDerivatives(fmi2Component c, const fmi2ValueReference vr[], size_t n,
                                        const fmi2Integer o[], fmi2Real v[]) {
    UNUSED(c); UNUSED(vr); UNUSED(n); UNUSED(o); UNUSED(v); return fmi2Error; }
fmi2Status fmi2DoStep(fmi2Component c, fmi2Real t, fmi2Real h, fmi2Boolean np) {
    Model *m = c; UNUSED(np);
    double end = t + h;
    if (m->jump != 0) { m->x += m->jump; m->jump = 0; }
    if (m->t_discard > t + EPS && m->t_discard <= end + EPS) {
        say(m, fmi2Discard, "t_discard inside this step: discarding it");
        return fmi2Discard;
    }
    if (m->t_term > t + EPS && m->t_term <= end + EPS) {
        advance(m, m->t_term - t);
        m->t = m->t_term; m->last_ok = m->t_term; m->terminated = 1;
        say(m, fmi2Discard, "t_term reached: step ends early, terminated");
        return fmi2Discard;
    }
    advance(m, h);
    m->t = end; m->last_ok = end;
    return fmi2OK;
}
fmi2Status fmi2CancelStep(fmi2Component c) { UNUSED(c); return fmi2Error; }
fmi2Status fmi2GetStatus(fmi2Component c, const fmi2StatusKind s, fmi2Status *v) {
    UNUSED(c); UNUSED(s); *v = fmi2OK; return fmi2OK; }
fmi2Status fmi2GetRealStatus(fmi2Component c, const fmi2StatusKind s, fmi2Real *v) {
    if (s != fmi2LastSuccessfulTime) return fmi2Error;
    *v = ((Model *)c)->last_ok; return fmi2OK; }
fmi2Status fmi2GetIntegerStatus(fmi2Component c, const fmi2StatusKind s, fmi2Integer *v) {
    UNUSED(c); UNUSED(s); UNUSED(v); return fmi2Error; }
fmi2Status fmi2GetBooleanStatus(fmi2Component c, const fmi2StatusKind s, fmi2Boolean *v) {
    if (s != fmi2Terminated) return fmi2Error;
    *v = ((Model *)c)->terminated ? fmi2True : fmi2False; return fmi2OK; }
fmi2Status fmi2GetStringStatus(fmi2Component c, const fmi2StatusKind s, fmi2String *v) {
    UNUSED(c); UNUSED(s); UNUSED(v); return fmi2Error; }
"""

LIVEPROBE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="2.0" modelName="LiveProbe"
  guid="{5f1c2a3e-7b6d-4c1e-9a0f-1b2c3d4e5f60}" generationTool="Agentic FMI spike i"
  description="x' = -k*x + u; tunable k, jump, t_term, t_discard" numberOfEventIndicators="0">
  <ModelExchange modelIdentifier="LiveProbe" completedIntegratorStepNotNeeded="true"/>
  <CoSimulation modelIdentifier="LiveProbe" canHandleVariableCommunicationStepSize="true"/>
  <DefaultExperiment startTime="0" stopTime="3" stepSize="0.01"/>
  <ModelVariables>
    <ScalarVariable name="x" valueReference="1" causality="output" variability="continuous"
      initial="exact"><Real start="1"/></ScalarVariable>
    <ScalarVariable name="der(x)" valueReference="2" causality="local" variability="continuous"
      initial="calculated"><Real derivative="1"/></ScalarVariable>
    <ScalarVariable name="k" valueReference="3" causality="parameter" variability="tunable"
      initial="exact"><Real start="1"/></ScalarVariable>
    <ScalarVariable name="u" valueReference="4" causality="input" variability="continuous">
      <Real start="0"/></ScalarVariable>
    <ScalarVariable name="jump" valueReference="5" causality="parameter" variability="tunable"
      initial="exact"><Real start="0"/></ScalarVariable>
    <ScalarVariable name="t_term" valueReference="6" causality="parameter" variability="tunable"
      initial="exact"><Real start="1e9"/></ScalarVariable>
    <ScalarVariable name="t_discard" valueReference="7" causality="parameter"
      variability="tunable" initial="exact"><Real start="1e9"/></ScalarVariable>
  </ModelVariables>
  <ModelStructure>
    <Outputs><Unknown index="1"/></Outputs>
    <Derivatives><Unknown index="2"/></Derivatives>
    <InitialUnknowns><Unknown index="2"/></InitialUnknowns>
  </ModelStructure>
</fmiModelDescription>
"""


# ------------------------------------------------------------------------------------------
# child side: heartbeat, step gate, FMPy helpers
# ------------------------------------------------------------------------------------------


class Heartbeat(threading.Thread):
    """Beats every 50 ms, as the worker's status thread does; records the beat times."""

    def __init__(self, period: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.period = period
        self.beats: list[float] = []
        self.halt = threading.Event()

    def run(self) -> None:
        while not self.halt.wait(self.period):
            self.beats.append(time.monotonic())

    def window(self, start: float, end: float) -> dict[str, Any]:
        inside = [b for b in self.beats if start <= b <= end]
        edges = [start, *inside, end]
        gaps = [b - a for a, b in itertools.pairwise(edges)]
        return {
            "span_s": round(end - start, 3),
            "beats": len(inside),
            "max_gap_ms": round(max(gaps) * 1000, 1) if gaps else None,
        }

    def overall(self) -> dict[str, Any]:
        gaps = [b - a for a, b in zip(self.beats, self.beats[1:], strict=False)]
        return {
            "beats": len(self.beats),
            "max_gap_ms": round(max(gaps) * 1000, 1) if gaps else None,
        }


class Gate:
    """The live step gate, driven by a script (commands keyed by simulation time) and by a
    control queue another thread fills while the run is paused (the inbox reader's role).

    Called in the simulation thread after every step with the time reached: applies commands
    there and never from another thread, blocks while paused, returns False to stop.
    ``apply(name, value)`` performs a ``set`` and returns a dict (``terminate`` stops)."""

    def __init__(self, script: list[dict], apply: Callable[[str, float], dict]) -> None:
        self.script = sorted(script, key=lambda c: c["at"])
        self.apply = apply
        self.control: queue.Queue[dict] = queue.Queue()
        self.paused = False
        self.steps = 0
        self.log: list[dict] = []
        self.calls = 0
        self.t: float | None = None
        self.first_t: float | None = None
        self.reached_pause = threading.Event()
        self.stop_reason: str | None = None

    def _handle(self, cmd: dict, t: float) -> bool:
        entry = {"t": t, "wall": time.monotonic(), **cmd}
        self.log.append(entry)
        kind = cmd["cmd"]
        if kind == "set":
            try:
                result = self.apply(cmd["name"], cmd["value"])
            except Exception as error:  # the FMU refused: record it, keep the run if possible
                result = {"error": f"{type(error).__name__}: {error}"}
            entry["result"] = result
            if result.get("terminate"):
                self.stop_reason = "terminate requested during set"
                return False
        elif kind == "pause":
            self.paused = True
            self.reached_pause.set()
        elif kind == "resume":
            self.paused = False
        elif kind == "step":
            self.steps += cmd.get("n", 1)
        elif kind == "stop":
            self.stop_reason = "stop"
            return False
        return True

    def __call__(self, t: float) -> bool:
        self.calls += 1
        self.t = t
        if self.first_t is None:
            self.first_t = t
        while self.script and self.script[0]["at"] <= t + 1e-9:
            if not self._handle(self.script.pop(0), t):
                return False
        while not self.control.empty():
            if not self._handle(self.control.get_nowait(), t):
                return False
        while self.paused and self.steps == 0:
            try:
                cmd = self.control.get(timeout=0.05)
            except queue.Empty:
                continue
            if not self._handle(cmd, t):
                return False
        if self.paused:
            self.steps -= 1  # a single step: advance once, then block again
        return True


def controller(gate: Gate, cfg: dict, record: dict) -> None:
    """The inbox reader while paused: hold, single-step n times, set, resume."""
    if not gate.reached_pause.wait(timeout=60):
        record["error"] = "pause never reached"
        return
    t_paused = gate.t
    record["paused_at_t"] = t_paused
    record["pause_wall"] = time.monotonic()
    time.sleep(cfg.get("hold_s", 1.0))
    record["t_after_hold"] = gate.t
    advanced = []
    for _ in range(cfg.get("steps", 0)):
        before = gate.t
        gate.control.put({"cmd": "step"})
        deadline = time.monotonic() + 5
        while gate.t == before and time.monotonic() < deadline:
            time.sleep(0.001)
        time.sleep(0.2)  # it must block again after one step
        advanced.append({"from": before, "to": gate.t})
    record["single_steps"] = advanced
    for cmd in cfg.get("while_paused", []):
        gate.control.put(dict(cmd))
    time.sleep(0.1)
    gate.control.put({"cmd": "resume"})
    record["resume_wall"] = time.monotonic()


def variable_info(md: Any) -> dict[str, tuple[int, str, str]]:
    return {v.name: (v.valueReference, v.causality, v.variability) for v in md.modelVariables}


def event_iteration(fmu: Any, fmi3: bool) -> dict[str, Any]:
    """newDiscreteStates (FMI 2.0) or updateDiscreteStates (FMI 3.0) until settled."""
    needed, terminate, changed, defined, t_next = True, False, False, False, 0.0
    while needed and not terminate:
        update = fmu.updateDiscreteStates() if fmi3 else fmu.newDiscreteStates()
        needed, terminate, _, values_changed, defined, t_next = update
        changed = changed or values_changed
    return {
        "states_changed": bool(changed),
        "terminate": bool(terminate),
        "next_event": float(t_next) if defined else None,
    }


def make_apply(
    holder: list, md: Any, interface: str, direct: list[bool] | None = None
) -> Callable[[str, float], dict]:
    """A ``set`` as the worker would do it: CS sets directly; ME wraps a parameter in Event
    Mode (enterEventMode, set, discrete-state iteration, enterContinuousTimeMode) and sets a
    continuous input directly. While ``direct[0]`` (right after exitInitializationMode, when an
    ME FMU is still in its initial Event Mode) every set is direct."""
    info = variable_info(md)
    fmi3 = md.fmiVersion.startswith("3")

    def apply(name: str, value: float) -> dict:
        fmu = holder[0]
        vr, causality, _variability = info[name]
        setter = fmu.setFloat64 if fmi3 else fmu.setReal
        at_start = bool(direct and direct[0])
        if interface == "ModelExchange" and causality == "parameter" and not at_start:
            fmu.enterEventMode()
            try:
                setter([vr], [value])
                outcome = event_iteration(fmu, fmi3)
            finally:
                fmu.enterContinuousTimeMode()
            return outcome | {"event_mode": True}
        setter([vr], [value])
        return {"event_mode": False, "at_start": at_start}

    return apply


def start_pause_controller(gate: Gate, cfg: dict) -> tuple[threading.Thread | None, dict]:
    record: dict = {}
    if not cfg.get("pause"):
        return None, record
    thread = threading.Thread(target=controller, args=(gate, cfg["pause"], record), daemon=True)
    thread.start()
    return thread, record


def finish(hb: Heartbeat, gate: Gate, pause: dict, extra: dict) -> dict:
    hb.halt.set()
    report = {
        "gate_calls": gate.calls,
        "gate_first_t": gate.first_t,
        "gate_log": [{k: v for k, v in e.items() if k != "wall"} for e in gate.log],
        "stop_reason": gate.stop_reason,
        "heartbeat": hb.overall(),
        "pause": {k: v for k, v in pause.items() if not k.endswith("_wall")},
    }
    if "pause_wall" in pause and "resume_wall" in pause:
        report["pause"]["heartbeat"] = hb.window(pause["pause_wall"], pause["resume_wall"])
    return report | extra


# ------------------------------------------------------------------------------------------
# children: FMPy
# ------------------------------------------------------------------------------------------


def child_fmpy_callback(cfg: dict) -> dict:
    """simulate_fmu with the gate in step_finished; sets go through recorder.fmu."""
    import numpy as np
    from fmpy import read_model_description, simulate_fmu

    hb = Heartbeat()
    hb.start()
    md = read_model_description(cfg["fmu"])
    holder: list = [None]
    direct = [False]
    gate = Gate(cfg.get("script", []), make_apply(holder, md, cfg["interface"], direct))
    thread, pause = start_pause_controller(gate, cfg)
    tail: deque[str] = deque(maxlen=4)
    target, instance = cfg["fmu"], None
    if cfg.get("gate_at_start"):
        target, instance = gated_instance(cfg, md, gate, holder, direct, tail)

    def step_finished(t: float, recorder: Any) -> bool:
        holder[0] = recorder.fmu
        return gate(t)

    inputs = None
    if cfg.get("input_table"):
        rows = [tuple(r) for r in cfg["input_table"]["rows"]]
        dtype = [("time", np.float64)] + [(n, np.float64) for n in cfg["input_table"]["names"]]
        inputs = np.array(rows, dtype=dtype)
    error = None
    rows_out: list = []
    try:
        result = simulate_fmu(
            target,
            fmi_type=cfg["interface"],
            stop_time=cfg["stop"],
            output_interval=cfg["h"],
            output=cfg["outputs"],
            start_values=cfg.get("start_values", {}),
            input=inputs,
            step_finished=step_finished,
            fmi_call_logger=tail.append,
            fmu_instance=instance,
        )
        rows_out = [[float(row[0]), *[float(row[n]) for n in cfg["outputs"]]] for row in result]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if instance is not None:
            instance.freeInstance()  # simulate_fmu frees only the instances it made
    if thread:
        thread.join(timeout=10)
    return finish(hb, gate, pause, {"rows": rows_out, "error": error, "fmi_tail": list(tail)})


def gated_instance(
    cfg: dict, md: Any, gate: Gate, holder: list, direct: list[bool], tail: deque[str]
) -> tuple[str, Any]:
    """An instance for ``simulate_fmu(fmu_instance=...)`` whose exitInitializationMode runs the
    gate at the start time: step_finished first fires only after the first step."""
    from fmpy import extract
    from fmpy.simulation import instantiate_fmu

    unzipdir = extract(cfg["fmu"], unzipdir=os.environ["TMPDIR"] + "/fmu")
    instance = instantiate_fmu(unzipdir, md, cfg["interface"], fmi_call_logger=tail.append)
    original = instance.exitInitializationMode

    def exit_then_gate(*args: Any, **kwargs: Any) -> Any:
        status = original(*args, **kwargs)
        holder[0], direct[0] = instance, True
        try:
            if not gate(0.0):
                raise RuntimeError("stopped at the start time")
        finally:
            direct[0] = False
        return status

    instance.exitInitializationMode = exit_then_gate
    return unzipdir, instance


def instantiate_own(cfg: dict, interface: str) -> tuple[Any, Any, bool]:
    from fmpy import extract, read_model_description
    from fmpy.fmi2 import FMU2Model, FMU2Slave
    from fmpy.fmi3 import FMU3Model, FMU3Slave

    unzipdir = extract(cfg["fmu"], unzipdir=os.environ["TMPDIR"] + "/fmu")
    md = read_model_description(unzipdir)
    fmi3 = md.fmiVersion.startswith("3")
    cs = interface == "CoSimulation"
    cls = (FMU3Slave if cs else FMU3Model) if fmi3 else (FMU2Slave if cs else FMU2Model)
    ident = (md.coSimulation if cs else md.modelExchange).modelIdentifier
    fmu = cls(guid=md.guid, unzipDirectory=unzipdir, modelIdentifier=ident, instanceName="live")
    fmu.instantiate()
    return fmu, md, fmi3


def child_fmpy_own_cs(cfg: dict) -> dict:
    """Our own Co-Simulation loop on FMU2Slave/FMU3Slave with the same gate."""
    from fmpy.fmi1 import FMICallException
    from fmpy.fmi2 import fmi2Discard, fmi2LastSuccessfulTime, fmi2Terminated

    hb = Heartbeat()
    hb.start()
    fmu, md, fmi3 = instantiate_own(cfg, "CoSimulation")
    info = variable_info(md)
    gate = Gate(cfg.get("script", []), make_apply([fmu], md, "CoSimulation"))
    thread, pause = start_pause_controller(gate, cfg)
    t0, stop, h = 0.0, cfg["stop"], cfg["h"]
    vrs = [info[n][0] for n in cfg["outputs"]]
    get = fmu.getFloat64 if fmi3 else fmu.getReal
    if fmi3:
        fmu.enterInitializationMode(startTime=t0, stopTime=stop)
    else:
        fmu.setupExperiment(startTime=t0, stopTime=stop)
        fmu.enterInitializationMode()
    fmu.exitInitializationMode()
    rows = [[t0, *get(vrs)]]
    t, n, ended = t0, 0, None
    if cfg.get("gate_at_start") and not gate(t0):
        stop = t0  # stopped before the first step
    try:
        while not (t > stop or math.isclose(t, stop)):
            target = t0 + (n + 1) * h
            if target > stop and not math.isclose(target, stop):
                target = stop
            if fmi3:
                _event, terminate, early, last = fmu.doStep(t, target - t)
                t = last if early and last < target else target
            else:
                terminate = False
                try:
                    fmu.doStep(t, target - t)
                    t = target
                except FMICallException as exc:
                    if exc.status != fmi2Discard:
                        raise
                    if not fmu.getBooleanStatus(fmi2Terminated):
                        ended = f"discarded at t={t:g} without terminating"
                        break
                    t, terminate = fmu.getRealStatus(fmi2LastSuccessfulTime), True
            if math.isclose(t, target):
                n += 1
            rows.append([t, *get(vrs)])
            if terminate:
                ended = f"terminated by the FMU at t={t:g}"
                break
            if not gate(t):
                ended = "gate stopped"
                break
    finally:
        fmu.terminate()
        fmu.freeInstance()
    if thread:
        thread.join(timeout=10)
    return finish(hb, gate, pause, {"rows": rows, "ended": ended, "error": None})


def child_fmpy_own_me(cfg: dict) -> dict:
    """Our own Model Exchange loop on FMU2Model/FMU3Model and FMPy's CVodeSolver: after a
    command it resets the solver and takes the next event time and termination from the
    command's event iteration."""
    from fmpy.simulation import Input
    from fmpy.sundials import CVodeSolver

    hb = Heartbeat()
    hb.start()
    fmu, md, fmi3 = instantiate_own(cfg, "ModelExchange")
    info = variable_info(md)
    t0, stop, h = 0.0, cfg["stop"], cfg["h"]
    if fmi3:
        fmu.enterInitializationMode(startTime=t0, stopTime=stop)
    else:
        fmu.setupExperiment(startTime=t0, stopTime=stop)
        fmu.enterInitializationMode()
    fmu.exitInitializationMode()
    first = event_iteration(fmu, fmi3)
    fmu.enterContinuousTimeMode()
    next_event = first["next_event"]
    solver = CVodeSolver(
        nx=md.numberOfContinuousStates,
        nz=md.numberOfEventIndicators,
        get_x=fmu.getContinuousStates,
        set_x=fmu.setContinuousStates,
        get_dx=fmu.getContinuousStateDerivatives if fmi3 else fmu.getDerivatives,
        get_z=fmu.getEventIndicators,
        get_nominals=fmu.getNominalsOfContinuousStates,
        set_time=fmu.setTime,
        input=Input(fmu, md, None),
        startTime=t0,
        maxStep=(stop - t0) / 50.0,
        relativeTolerance=1e-5,
    )
    pending: list[dict] = []
    base_apply = make_apply([fmu], md, "ModelExchange")

    def apply(name: str, value: float) -> dict:
        outcome = base_apply(name, value)
        pending.append(outcome)
        return outcome

    gate = Gate(cfg.get("script", []), apply)
    thread, pause = start_pause_controller(gate, cfg)
    vrs = [info[n][0] for n in cfg["outputs"]]
    get = fmu.getFloat64 if fmi3 else fmu.getReal
    needs_cis = md.modelExchange.needsCompletedIntegratorStep
    rows = [[t0, *get(vrs)]]
    t, n, ended = t0, 0, None
    if cfg.get("gate_at_start"):
        gate(t0)
        for outcome in pending:
            if outcome.get("event_mode"):
                next_event = outcome["next_event"]
        if pending:
            solver.reset(t0)
    try:
        while not (t > stop or math.isclose(t, stop)):
            regular = t0 + (n + 1) * h
            target = regular
            if (
                next_event is not None
                and target > next_event
                and not math.isclose(target, next_event)
            ):
                target = next_event
            target = min(target, stop)
            time_event = next_event is not None and math.isclose(target, next_event)
            state_event, _roots, t = solver.step(t, target)
            fmu.setTime(t)
            if math.isclose(t, regular):
                n += 1
            step_event, terminate = fmu.completedIntegratorStep() if needs_cis else (False, False)
            if terminate:
                ended = f"terminated by the FMU at t={t:g}"
                break
            if time_event or state_event or step_event:
                fmu.enterEventMode()
                update = event_iteration(fmu, fmi3)
                if update["terminate"]:
                    rows.append([t, *get(vrs)])
                    ended = f"terminated by the FMU at t={t:g}"
                    break
                fmu.enterContinuousTimeMode()
                next_event = update["next_event"]
                solver.reset(t)
            rows.append([t, *get(vrs)])
            pending.clear()
            if not gate(t):
                ended = gate.stop_reason
                break
            if pending:  # a command touched the FMU: new dynamics, maybe new states or events
                for outcome in pending:
                    if outcome.get("event_mode"):
                        next_event = outcome["next_event"]
                solver.reset(t)
    finally:
        fmu.terminate()
        fmu.freeInstance()
        del solver
    if thread:
        thread.join(timeout=10)
    return finish(hb, gate, pause, {"rows": rows, "ended": ended, "error": None})


def child_fmpy_terminal(cfg: dict) -> dict:
    """simulate_fmu as the current engine calls it; the gate only watches for a stall."""
    from fmpy import simulate_fmu

    seen: dict[str, Any] = {"calls": 0, "same": 0, "last": None, "max_same": 0}

    def step_finished(t: float, _recorder: Any) -> bool:
        seen["calls"] += 1
        seen["same"] = seen["same"] + 1 if seen["last"] is not None and t <= seen["last"] else 0
        seen["max_same"] = max(seen["max_same"], seen["same"])
        seen["last"] = t
        return seen["same"] < cfg.get("stall_limit", 10**9)

    tail: deque[str] = deque(maxlen=6)
    out: dict[str, Any] = {"error": None}
    started = time.perf_counter()
    try:
        result = simulate_fmu(
            cfg["fmu"],
            fmi_type=cfg["interface"],
            stop_time=cfg["stop"],
            output_interval=cfg.get("h"),
            start_values=cfg.get("start_values", {}),
            step_finished=None if cfg.get("no_gate") else step_finished,
            fmi_call_logger=tail.append,
        )
        out |= {"rows": len(result), "t_end": float(result["time"][-1])}
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out |= {"wall_s": round(time.perf_counter() - started, 3), "fmi_tail": list(tail)}
    return out | {k: v for k, v in seen.items() if k != "same"}


# ------------------------------------------------------------------------------------------
# children: OMSimulator
# ------------------------------------------------------------------------------------------


def oms_model(cfg: dict) -> Any:
    from OMSimulator import SSP, CRef

    model = SSP(temp_dir=os.environ["TMPDIR"] + "/ssp")
    for name, path in cfg["components"].items():
        model.addResource(path, new_name=f"resources/{name}.fmu")
        model.addComponent(CRef("default", name), f"resources/{name}.fmu")
    for src, dst in cfg.get("connections", []):
        model.addConnection(
            CRef("default", *src.split(".", 1)), CRef("default", *dst.split(".", 1))
        )
    model.newSolver({"name": "ma", "method": "oms_ma", "fixedStepSize": cfg["h"]})
    for name in cfg["components"]:
        model.setSolver(CRef("default", name), "ma")
    model.activeVariant.stopTime = cfg["stop"]
    return model


def oms_set(inst: Any, cref: Any, value: float) -> dict:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        status = inst.setValue(cref, value)
    return {"status": str(status), "warnings": [str(w.message)[:160] for w in caught]}


def child_oms(cfg: dict) -> dict:
    """Build through the API, initialize, doStep with setValue ops between steps."""
    from OMSimulator import CRef
    from OMSimulator.capi import Capi, Status

    hb = Heartbeat()
    hb.start()
    model = oms_model(cfg)
    inst = model.instantiate()
    inst.setResultFile("")
    for ref, value in cfg.get("init_set", {}).items():
        oms_set(inst, CRef("default", *ref.split(".", 1)), value)
    inst.initialize()
    crefs = [CRef("default", *ref.split(".", 1)) for ref in cfg["record"]]

    def row() -> list[float]:
        return [float(inst.getTime()), *[float(inst.getValue(c)) for c in crefs]]

    rows = [row()]
    ops: dict[int, list[dict]] = {}
    for op in cfg.get("ops", []):
        ops.setdefault(op["step"], []).append(op)
    log: list[dict] = []
    pause_info: dict = {}
    stalled_at = None
    same = 0
    steps = round(cfg["stop"] / cfg["h"])
    stepping_walls: list[float] = []
    statuses: dict[str, dict] = {}
    failed = False
    for n in range(1, steps + 1):
        w0 = time.perf_counter()
        status = Capi.doStep(inst.modelName)  # inst.doStep() raises on anything but ok
        stepping_walls.append(time.perf_counter() - w0)
        seen = statuses.setdefault(str(status), {"first_t_before": rows[-1][0], "count": 0})
        seen["count"] += 1
        if status in (Status.error, Status.fatal):
            failed = True
            break
        rows.append(row())
        same = same + 1 if rows[-1][0] <= rows[-2][0] else 0
        if same >= 3:
            stalled_at = rows[-1][0]
            break
        for op in ops.get(n, []):
            entry: dict[str, Any] = {"step": n, "t": rows[-1][0]}
            if "set" in op:
                cref = CRef("default", *op["set"].split(".", 1))
                entry |= {"set": op["set"], "value": op["value"]}
                entry |= oms_set(inst, cref, op["value"])
                entry["read_back"] = float(inst.getValue(cref))
            if "pause_s" in op:
                p0 = time.monotonic()
                time.sleep(op["pause_s"])  # paused: the worker simply does not call doStep
                entry["pause"] = hb.window(p0, time.monotonic())
                pause_info = entry["pause"]
            log.append(entry)
    terminate_status = str(Capi.terminate(inst.modelName)) if failed else None
    if not failed:
        inst.terminate()
    inst.delete()
    hb.halt.set()
    stepping_walls.sort()
    return {
        "doStep_statuses": statuses,
        "terminate_status_after_error": terminate_status,
        "rows": rows,
        "ops_log": log,
        "stalled_at": stalled_at,
        "end_time": rows[-1][0],
        "heartbeat": hb.overall(),
        "pause": pause_info,
        "doStep_ms_median": round(stepping_walls[len(stepping_walls) // 2] * 1000, 4),
    }


CHILDREN: dict[str, Callable[[dict], dict]] = {
    "fmpy_callback": child_fmpy_callback,
    "fmpy_own_cs": child_fmpy_own_cs,
    "fmpy_own_me": child_fmpy_own_me,
    "fmpy_terminal": child_fmpy_terminal,
    "oms": child_oms,
}


# ------------------------------------------------------------------------------------------
# parent: builds, children, references
# ------------------------------------------------------------------------------------------


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def build_pythonfmu(name: str, text: str) -> Path:
    SRC.mkdir(parents=True, exist_ok=True)
    source = SRC / name
    source.write_text(text)
    before = set(FMUS.glob("*.fmu"))
    done = run(
        [sys.executable, "-I", "-B", "-m", "pythonfmu", "build", "-f", str(source), "-d", str(FMUS)]
    )
    if done.returncode:
        raise RuntimeError(done.stderr[-2000:])
    (built,) = set(FMUS.glob("*.fmu")) - before
    return built


def build_msd() -> Path:
    scratch = OUT / "omc"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    model = scratch / MSD_SRC.name
    shutil.copyfile(MSD_SRC, model)
    (scratch / "build.mos").write_text(MSD_MOS.format(mo=model))
    run(["omc", "build.mos"], cwd=scratch)
    fmu = FMUS / "MSD.fmu"
    shutil.copyfile(scratch / "MSD.fmu", fmu)
    return fmu


def build_liveprobe() -> Path:
    spec = importlib.util.find_spec("fmpy")  # locates the headers without importing FMPy
    assert spec and spec.submodule_search_locations
    headers = Path(spec.submodule_search_locations[0]) / "c-code"
    work = SRC / "liveprobe"
    shutil.rmtree(work, ignore_errors=True)
    (work / "binaries" / "linux64").mkdir(parents=True)
    (work / "LiveProbe.c").write_text(LIVEPROBE_C)
    (work / "modelDescription.xml").write_text(LIVEPROBE_XML)
    lib = work / "binaries" / "linux64" / "LiveProbe.so"
    done = run(["gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", f"-I{headers}",
                str(work / "LiveProbe.c"), "-o", str(lib), "-lm"])  # fmt: skip
    if done.returncode:
        raise RuntimeError(done.stderr[-3000:])
    fmu = FMUS / "LiveProbe.fmu"
    with zipfile.ZipFile(fmu, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(work / "modelDescription.xml", "modelDescription.xml")
        archive.write(lib, "binaries/linux64/LiveProbe.so")
    return fmu


def run_child(name: str, tag: str, cfg: dict, timeout: float = CHILD_TIMEOUT) -> dict:
    RUNS.mkdir(parents=True, exist_ok=True)
    cfg_path = RUNS / f"{tag}.cfg.json"
    cfg_path.write_text(json.dumps(cfg))
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    env["TMPDIR"] = str(TMP / tag)
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    command = [sys.executable, "-I", "-B", __file__, "--child", name, str(cfg_path)]
    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        hung = False
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        hung = True
    (RUNS / f"{tag}.stdout").write_text(stdout)
    (RUNS / f"{tag}.stderr").write_text(stderr)
    lines = stdout.strip().splitlines()
    try:
        report = json.loads(next(line for line in reversed(lines) if line.startswith("{")))
    except (StopIteration, json.JSONDecodeError):
        report = {"error": "no report", "stderr_tail": stderr[-800:], "stdout_tail": stdout[-800:]}
    report |= {
        "exit": proc.returncode,
        "hung_killed": hung,
        "wall_s": round(time.perf_counter() - started, 2),
    }
    report["log_tail"] = [ln for ln in stdout.splitlines() if not ln.startswith("{")][-4:]
    return report


# ---- references ------------------------------------------------------------------------


def applied(log: list[dict]) -> list[tuple[float, str, float]]:
    return [(e["t"], e["name"], e["value"]) for e in log if e.get("cmd") == "set"]


def integrator_reference(rows: list, log: list[dict], h: float) -> list[float]:
    """x_{n+1} = x_n + k*u*h with k and u as set after the step that reached t_n."""
    sets = applied(log)
    k, u, x, xs = 1.0, 1.0, 0.0, [0.0]
    for i in range(1, len(rows)):
        t_prev = rows[i - 1][0]
        for t_set, name, value in sets:
            if math.isclose(t_set, t_prev, abs_tol=1e-9):
                k, u = (value, u) if name == "k" else (k, value)
        x += k * u * h
        xs.append(x)
    return xs


def liveprobe_reference(times: list[float], sets: list[tuple[float, str, float]]) -> list[float]:
    """Closed form of x' = -k*x + u with x(0) = 1 and piecewise-constant k, u, jumps."""
    k, u, x, t = 1.0, 0.0, 1.0, 0.0
    events = sorted(sets)
    out = []
    for target in times:
        while events and events[0][0] < target - 1e-12:
            t_set, name, value = events.pop(0)
            x = (x - u / k) * math.exp(-k * (t_set - t)) + u / k
            t = t_set
            if name == "k":
                k = value
            elif name == "u":
                u = value
            elif name == "jump":
                x += value
        out.append((x - u / k) * math.exp(-k * (target - t)) + u / k)
    return out


def apexes(rows: list, column: int = 1) -> list[float]:
    h = [r[column] for r in rows]
    return [
        round(h[i], 4) for i in range(1, len(h) - 1) if h[i - 1] < h[i] >= h[i + 1] and h[i] > 0.01
    ]


def on_grid(rows: list, h: float) -> dict[float, list]:
    return {
        round(r[0], 9): r for r in rows if math.isclose(r[0] / h, round(r[0] / h), abs_tol=1e-6)
    }


def max_diff(a: list, b: list, column: int = 1, h: float = H) -> float | None:
    ga, gb = on_grid(a, h), on_grid(b, h)
    common = sorted(set(ga) & set(gb))
    if not common:
        return None
    return max(abs(ga[t][column] - gb[t][column]) for t in common)


def pair_recurrence(n: int, overrides: dict[int, dict[str, float]]) -> list[list[float]]:
    """Jacobi recurrence of PI + exact plant. ``overrides[k]`` holds sets made after step k:
    ``Kp``, ``d``, ``K`` persist; ``F`` and ``y`` replace the connected input for the next
    step only (the one-step-override hypothesis)."""
    kp, d, gain = KP, 0.0, K
    a = math.exp(-H / TAU)
    u, x, integral = 0.0, 0.0, 0.0
    rows = [[0.0, u, x]]
    for step in range(n):
        y, f = x, u
        o = overrides.get(step, {})
        kp, d, gain = o.get("Kp", kp), o.get("d", d), o.get("K", gain)
        y, f = o.get("y", y), o.get("F", f)
        error = R - y
        integral += error * H
        u = kp * error + KI * integral
        x = a * x + gain * (1.0 - a) * (f + d)
        rows.append([(step + 1) * H, u, x])
    return rows


def pair_error(rows: list, ref: list) -> float:
    return max(abs(r[1] - q[1]) + abs(r[2] - q[2]) for r, q in zip(rows, ref, strict=False))


def strip(report: dict, keep_rows: bool = False) -> dict:
    out = dict(report)
    rows = out.pop("rows", None)
    if isinstance(rows, list):
        out["n_rows"] = len(rows)
        out["t_end"] = rows[-1][0] if rows else None
        if keep_rows:
            out["rows"] = rows
    return out


# ---- probe groups ----------------------------------------------------------------------


PAUSE = {"hold_s": 1.0, "steps": 3}


def cs_probes(fmus: dict[str, Path]) -> dict:
    report: dict = {}
    integ = str(fmus["integrator"])
    script = [
        {"at": 0.25, "cmd": "pause"},
        {"at": 0.5, "cmd": "set", "name": "k", "value": 2.0},
        {"at": 1.0, "cmd": "set", "name": "u", "value": 3.0},
        {"at": 1.5, "cmd": "stop"},
    ]
    base = {"fmu": integ, "interface": "CoSimulation", "stop": 2.0, "h": H, "outputs": ["x", "y"]}
    pause = PAUSE | {"while_paused": [{"cmd": "set", "name": "u", "value": 0.5}]}
    runs = {}
    for kind in ("fmpy_callback", "fmpy_own_cs"):
        runs[kind] = run_child(
            kind, f"cs_integrator_{kind}", base | {"script": script, "pause": pause}
        )
    ref = integrator_reference(runs["fmpy_callback"]["rows"], runs["fmpy_callback"]["gate_log"], H)
    cb, own = runs["fmpy_callback"]["rows"], runs["fmpy_own_cs"]["rows"]
    report["integrator"] = {
        "callback": strip(runs["fmpy_callback"]),
        "own_loop": strip(runs["fmpy_own_cs"]),
        "max_abs_dx_vs_reference_callback": max(
            abs(r[1] - q) for r, q in zip(cb, ref, strict=False)
        ),
        "rows_bit_identical_callback_vs_own": cb == own,
    }
    table = {"names": ["u"], "rows": [[0.0, 1.0], [2.0, 1.0]]}
    tab = run_child(
        "fmpy_callback",
        "cs_integrator_table",
        base
        | {"script": [{"at": 1.0, "cmd": "set", "name": "u", "value": 5.0}], "input_table": table},
    )
    rows = tab["rows"]
    slope = (
        (rows[150][1] - rows[110][1]) / (rows[150][0] - rows[110][0]) if len(rows) > 150 else None
    )
    report["integrator_input_table_overrides_live_set"] = strip(tab) | {
        "slope_after_set_t1.1_to_1.5": slope,
        "note": "u = 1 from the input table; a live set u = 5 at t = 1.0 (slope 5 if it held)",
    }
    at_t0 = base | {
        "stop": 0.5,
        "script": [
            {"at": 0.0, "cmd": "set", "name": "k", "value": 2.0},
            {"at": 0.0, "cmd": "pause"},
        ],
        "pause": {"hold_s": 0.5, "steps": 2},
    }
    entry: dict = {}
    for kind, extra in (
        ("fmpy_callback", {}),
        ("fmpy_callback", {"gate_at_start": True}),
        ("fmpy_own_cs", {"gate_at_start": True}),
    ):
        label = f"{kind}{'_gate_at_start' if extra else ''}"
        r = run_child(kind, f"cs_integrator_t0_{label}", at_t0 | extra)
        ref = integrator_reference(r["rows"], r["gate_log"], H)
        entry[label] = strip(r) | {
            "x_at_0.01": r["rows"][1][1] if len(r["rows"]) > 1 else None,
            "max_abs_dx_vs_reference": max(
                abs(row[1] - q) for row, q in zip(r["rows"], ref, strict=False)
            ),
        }
    report["integrator_set_and_pause_at_t0"] = entry
    for version in ("2.0", "3.0"):
        bb = {
            "fmu": str(REF / version / "BouncingBall.fmu"),
            "interface": "CoSimulation",
            "stop": 3.0,
            "h": H,
            "outputs": ["h", "v"],
            "script": [
                {"at": 0.5, "cmd": "pause"},
                {"at": 1.0, "cmd": "set", "name": "e", "value": 0.5},
                {"at": 2.5, "cmd": "stop"},
            ],
            "pause": {"hold_s": 1.0, "steps": 2},
        }
        a = run_child("fmpy_callback", f"cs_bb{version}_callback", bb)
        b = run_child("fmpy_own_cs", f"cs_bb{version}_own", bb)
        report[f"bouncingball_{version}"] = {
            "callback": strip(a),
            "own_loop": strip(b),
            "apexes_callback": apexes(a["rows"]),
            "rows_bit_identical_callback_vs_own": a["rows"] == b["rows"],
        }
    lp = {
        "fmu": str(fmus["liveprobe"]),
        "interface": "CoSimulation",
        "stop": 3.0,
        "h": H,
        "outputs": ["x"],
        "script": [
            {"at": 1.0, "cmd": "set", "name": "k", "value": 3.0},
            {"at": 1.5, "cmd": "set", "name": "u", "value": 2.0},
            {"at": 2.0, "cmd": "set", "name": "jump", "value": 0.5},
        ],
    }
    for kind in ("fmpy_callback", "fmpy_own_cs"):
        r = run_child(kind, f"cs_liveprobe_{kind}", lp)
        times = [row[0] for row in r["rows"]]
        ref = liveprobe_reference(times, applied(r["gate_log"]))
        report[f"liveprobe_cs_{kind}"] = strip(r) | {
            "max_abs_dx_vs_closed_form": max(
                abs(row[1] - q) for row, q in zip(r["rows"], ref, strict=True)
            ),
        }
    return report


def me_probes(fmus: dict[str, Path]) -> dict:
    report: dict = {}
    for version in ("2.0", "3.0"):
        bb = {
            "fmu": str(REF / version / "BouncingBall.fmu"),
            "interface": "ModelExchange",
            "stop": 3.0,
            "h": H,
            "outputs": ["h", "v"],
            "script": [
                {"at": 1.0, "cmd": "set", "name": "e", "value": 0.5},
                {"at": 2.5, "cmd": "stop"},
            ],
        }
        a = run_child("fmpy_callback", f"me_bb{version}_callback", bb)
        b = run_child("fmpy_own_me", f"me_bb{version}_own", bb)
        report[f"bouncingball_{version}"] = {
            "callback": strip(a),
            "own_loop": strip(b),
            "apexes_callback": apexes(a["rows"]),
            "apexes_own": apexes(b["rows"]),
            "max_abs_dh_callback_vs_own_on_grid": max_diff(a["rows"], b["rows"]),
        }
    vdp = {
        "fmu": str(REF / "3.0" / "VanDerPol.fmu"),
        "interface": "ModelExchange",
        "stop": 4.0,
        "h": H,
        "outputs": ["x0", "x1"],
    }
    plain = run_child("fmpy_callback", "me_vdp3_plain", vdp)
    paused = run_child(
        "fmpy_callback",
        "me_vdp3_paused",
        vdp | {"script": [{"at": 1.0, "cmd": "pause"}], "pause": PAUSE},
    )
    setmu = run_child(
        "fmpy_callback",
        "me_vdp3_set_mu",
        vdp | {"script": [{"at": 1.0, "cmd": "set", "name": "mu", "value": 2.0}]},
    )
    report["vanderpol_3.0"] = {
        "paused": strip(paused),
        "paused_rows_bit_identical_to_plain": paused["rows"] == plain["rows"],
        "set_fixed_mu_in_event_mode": strip(setmu),
        "set_mu_run_rows_equal_plain": setmu["rows"] == plain["rows"],
    }
    lp = {
        "fmu": str(fmus["liveprobe"]),
        "interface": "ModelExchange",
        "stop": 3.0,
        "h": H,
        "outputs": ["x"],
    }
    schedules = {
        "k_at_t0": [{"at": 0.0, "cmd": "set", "name": "k", "value": 3.0}],
        "jump_at_t0": [{"at": 0.0, "cmd": "set", "name": "jump", "value": 0.5}],
        "k": [{"at": 1.0, "cmd": "set", "name": "k", "value": 3.0}],
        "jump": [{"at": 1.0, "cmd": "set", "name": "jump", "value": 0.5}],
        "u": [{"at": 1.0, "cmd": "set", "name": "u", "value": 2.0}],
        "t_term": [{"at": 1.0, "cmd": "set", "name": "t_term", "value": 2.0}],
    }
    for label, script in schedules.items():
        for kind in ("fmpy_callback", "fmpy_own_me"):
            extra = {"gate_at_start": True} if label.endswith("_t0") else {}
            r = run_child(kind, f"me_liveprobe_{label}_{kind}", lp | {"script": script} | extra)
            rows = r.get("rows") or []
            sets = [s for s in applied(r.get("gate_log", [])) if s[1] != "t_term"]
            ref = liveprobe_reference([row[0] for row in rows], sets)
            errors = [abs(row[1] - q) for row, q in zip(rows, ref, strict=True)]
            report[f"liveprobe_me_{label}_{kind}"] = strip(r) | {
                "max_abs_dx_vs_closed_form": max(errors) if errors else None,
                "max_abs_dx_after_t1.05": max(
                    (e for row, e in zip(rows, errors, strict=True) if row[0] > 1.05), default=None
                ),
            }
    return report


def terminal_probes(fmus: dict[str, Path]) -> dict:
    report: dict = {}
    lp = str(fmus["liveprobe"])
    cs, me = {"interface": "CoSimulation"}, {"interface": "ModelExchange"}
    common = {"fmu": lp, "stop": 3.0, "h": 0.1}
    cases = {
        "liveprobe_cs_t_term_2.05": common | cs | {"start_values": {"t_term": 2.05}},
        "liveprobe_me_t_term_2.05": common | me | {"start_values": {"t_term": 2.05}},
        "liveprobe_cs_t_discard_1.05": common
        | cs
        | {"start_values": {"t_discard": 1.05}, "stall_limit": 1000},
        "pythonfmu_discard_cs": common
        | cs
        | {"fmu": str(fmus["discard"]), "stop": 1.0, "stall_limit": 1000},
    }
    for version in ("2.0", "3.0"):
        for interface in ("CoSimulation", "ModelExchange"):
            cases[f"stair_{version}_{interface}"] = {
                "fmu": str(REF / version / "Stair.fmu"),
                "interface": interface,
                "stop": 10.0,
                "h": 0.2,
            }
    for label, cfg in cases.items():
        report[label] = run_child("fmpy_terminal", f"term_{label}", cfg)
    for label in ("liveprobe_cs_t_discard_1.05", "pythonfmu_discard_cs"):
        cfg = cases[label] | {"no_gate": True}
        report[f"{label}_without_gate"] = run_child(
            "fmpy_terminal", f"term_{label}_nogate", cfg, timeout=15
        )
    return report


def oms_probes(fmus: dict[str, Path]) -> dict:
    report: dict = {}
    pair = {
        "components": {"PI": str(fmus["pi"]), "P": str(fmus["plant"])},
        "connections": [("PI.u", "P.F"), ("P.x", "PI.y")],
        "h": H,
        "stop": 2.0,
        "record": ["PI.u", "P.x", "P.F", "PI.y", "P.d", "PI.Kp"],
    }
    n = round(pair["stop"] / H)
    variants = {
        "base": ([], {}),
        "set_tunable_PI.Kp": ([{"step": 100, "set": "PI.Kp", "value": 40.0}], {100: {"Kp": 40.0}}),
        "set_tunable_P.K": ([{"step": 100, "set": "P.K", "value": 4.0}], {100: {"K": 4.0}}),
        "set_unconnected_input_P.d": (
            [{"step": 100, "set": "P.d", "value": 0.5}],
            {100: {"d": 0.5}},
        ),
        "set_connected_input_P.F": ([{"step": 100, "set": "P.F", "value": 5.0}], {100: {"F": 5.0}}),
        "set_connected_input_PI.y": (
            [{"step": 100, "set": "PI.y", "value": 0.0}],
            {100: {"y": 0.0}},
        ),
        "set_output_PI.u": ([{"step": 100, "set": "PI.u", "value": 5.0}], {100: {"F": 5.0}}),
        "pause_3s": ([{"step": 100, "pause_s": 3.0}], {}),
    }
    base_ref = pair_recurrence(n, {})
    for label, (ops, overrides) in variants.items():
        r = run_child("oms", f"oms_pair_{label}", pair | {"ops": ops})
        rows = r.get("rows") or []
        ref = pair_recurrence(n, overrides)
        entry = strip(r)
        if rows:
            entry["max_err_vs_hypothesis"] = pair_error(rows, ref)
            entry["max_err_vs_no_set"] = pair_error(rows, base_ref)
            entry["rows_99_to_103"] = [[round(v, 6) for v in row] for row in rows[99:104]]
        report[f"pair_{label}"] = entry
    msd = {
        "components": {"PI": str(fmus["pi"]), "MSD": str(fmus["msd"])},
        "connections": [("PI.u", "MSD.F"), ("MSD.x", "PI.y")],
        "h": H,
        "stop": 3.0,
        "record": ["PI.u", "MSD.x", "MSD.k", "PI.Kp"],
    }
    base_msd = run_child(
        "oms", "oms_msd_kp", msd | {"ops": [{"step": 100, "set": "PI.Kp", "value": 40.0}]}
    )
    fixed = run_child(
        "oms",
        "oms_msd_kp_and_fixed_k",
        msd
        | {
            "ops": [
                {"step": 100, "set": "PI.Kp", "value": 40.0},
                {"step": 150, "set": "MSD.k", "value": 20.0},
            ]
        },
    )
    report["msd_kp"] = strip(base_msd)
    report["msd_set_fixed_k_after_init"] = strip(fixed) | {
        "x_max_abs_diff_vs_without_it": max(
            abs(a[2] - b[2])
            for a, b in zip(fixed.get("rows", []), base_msd.get("rows", []), strict=False)
        )
        if fixed.get("rows") and base_msd.get("rows")
        else None,
    }
    for version in ("2.0", "3.0"):
        single = {
            "components": {"B": str(REF / version / "BouncingBall.fmu")},
            "h": H,
            "stop": 3.0,
            "record": ["B.h", "B.e", "B.g"],
            "ops": [
                {"step": 100, "set": "B.e", "value": 0.5},
                {"step": 150, "set": "B.g", "value": -5.0},
            ],
        }
        r = run_child("oms", f"oms_bb{version}", single)
        report[f"bouncingball_{version}"] = strip(r) | {"apexes": apexes(r.get("rows") or [])}
        stair = {
            "components": {"S": str(REF / version / "Stair.fmu")},
            "h": 0.2,
            "stop": 10.0,
            "record": ["S.counter"],
        }
        report[f"stair_{version}"] = strip(run_child("oms", f"oms_stair{version}", stair))
    probe = {
        "components": {"L": str(fmus["liveprobe"])},
        "h": H,
        "stop": 3.0,
        "record": ["L.x", "L.k"],
    }
    for label, extra in {
        "k_at_t1": {"ops": [{"step": 100, "set": "L.k", "value": 3.0}]},
        "t_term_2.05": {"init_set": {"L.t_term": 2.05}},
        "t_discard_1.05": {"init_set": {"L.t_discard": 1.05}},
    }.items():
        r = run_child("oms", f"oms_liveprobe_{label}", probe | extra)
        entry = strip(r)
        if label == "k_at_t1" and r.get("rows"):
            ref = liveprobe_reference([row[0] for row in r["rows"]], [(1.0, "k", 3.0)])
            entry["max_abs_dx_vs_closed_form"] = max(
                abs(row[1] - q) for row, q in zip(r["rows"], ref, strict=True)
            )
        report[f"liveprobe_{label}"] = entry
    report["pythonfmu_discard"] = strip(
        run_child(
            "oms",
            "oms_discard",
            {"components": {"D": str(fmus["discard"])}, "h": 0.1, "stop": 1.0, "record": ["D.y"]},
        )
    )
    return report


def complexity() -> dict:
    """Source lines of the approaches (the gate itself is shared)."""

    def lines(fn: Callable) -> int:
        return len(
            [
                ln
                for ln in inspect.getsource(fn).splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
        )

    return {
        "callback_runner_lines": lines(child_fmpy_callback),
        "own_cs_loop_lines": lines(child_fmpy_own_cs) + lines(instantiate_own),
        "own_me_loop_lines": lines(child_fmpy_own_me) + lines(instantiate_own),
        "me_event_mode_set_lines": lines(make_apply) + lines(event_iteration),
        "gate_lines": lines(Gate),
    }


def main() -> None:
    for sub in ("fmus", "src", "runs", "omc"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    FMUS.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)
    started = time.perf_counter()
    fmus = {
        "integrator": build_pythonfmu("integrator.py", INTEGRATOR_PY),
        "plant": build_pythonfmu("plant.py", PLANT_PY),
        "discard": build_pythonfmu("discard.py", DISCARD_PY),
        "pi": build_pythonfmu("pi_controller.py", PI_SRC.read_text()),
        "msd": build_msd(),
        "liveprobe": build_liveprobe(),
    }
    report: dict = {"fmus": {k: str(v.relative_to(REPO)) for k, v in fmus.items()}}
    report["build_s"] = round(time.perf_counter() - started, 1)
    report["cs"] = cs_probes(fmus)
    report["me"] = me_probes(fmus)
    report["terminal"] = terminal_probes(fmus)
    report["oms"] = oms_probes(fmus)
    report["complexity"] = complexity()
    report["total_s"] = round(time.perf_counter() - started, 1)
    report["parent_imported"] = sorted(
        m for m in ("fmpy.fmi2", "fmpy.fmi3", "OMSimulator") if m in sys.modules
    )
    (OUT / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str)[:4000])
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        result = CHILDREN[sys.argv[2]](json.loads(Path(sys.argv[3]).read_text()))
        sys.stdout.flush()
        print("\n" + json.dumps(result, default=str))
    else:
        main()
