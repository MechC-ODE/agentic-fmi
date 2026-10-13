"""Mass-spring-damper plant as a pythonfmu FMI 2.0 Co-Simulation slave: the Python twin of
MSD.mo, for when OpenModelica isn't installed. Same model name, variables and defaults, so it
builds to generated/MSD/MSD.fmu and the system-builder example loop.json runs with either.

m*x'' = F - k*x - d*v. The force F holds over each communication step, as a co-simulation
master delivers it, and do_step integrates with classic Runge-Kutta in sub-steps of at most
1 ms. Units are in the descriptions (pythonfmu has no unit attribute).
"""

import math

from pythonfmu import (
    DefaultExperiment,
    Fmi2Causality,
    Fmi2Initial,
    Fmi2Slave,
    Fmi2Variability,
    Real,
)

FIXED = {"causality": Fmi2Causality.parameter, "variability": Fmi2Variability.fixed}
OUTPUT = {"causality": Fmi2Causality.output, "initial": Fmi2Initial.exact}
MAX_SUBSTEP = 1e-3
"""The longest Runge-Kutta sub-step (s)."""


class MSD(Fmi2Slave):
    author = "Agentic FMI"
    description = "Mass-spring-damper plant with a force input"
    default_experiment = DefaultExperiment(start_time=0.0, stop_time=10.0, step_size=0.01)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.m, self.k, self.d = 1.0, 10.0, 2.0
        self.F, self.x, self.v = 0.0, 0.0, 0.0
        self.register_variable(Real("m", description="mass [kg]", **FIXED))
        self.register_variable(Real("k", description="spring stiffness [N/m]", **FIXED))
        self.register_variable(Real("d", description="damping [N.s/m]", **FIXED))
        self.register_variable(
            Real("F", causality=Fmi2Causality.input, description="external force [N]")
        )
        self.register_variable(Real("x", description="position [m]", **OUTPUT))
        self.register_variable(Real("v", description="velocity [m/s]", **OUTPUT))

    def _acceleration(self, x, v):
        return (self.F - self.k * x - self.d * v) / self.m

    def do_step(self, current_time, step_size):
        n = max(1, math.ceil(step_size / MAX_SUBSTEP - 1e-9))
        h = step_size / n
        x, v = self.x, self.v
        for _ in range(n):
            a1 = self._acceleration(x, v)
            x2, v2 = x + h / 2 * v, v + h / 2 * a1
            a2 = self._acceleration(x2, v2)
            x3, v3 = x + h / 2 * v2, v + h / 2 * a2
            a3 = self._acceleration(x3, v3)
            x4, v4 = x + h * v3, v + h * a3
            a4 = self._acceleration(x4, v4)
            x += h / 6 * (v + 2 * v2 + 2 * v3 + v4)
            v += h / 6 * (a1 + 2 * a2 + 2 * a3 + a4)
        self.x, self.v = x, v
        return True
