"""Bouncing ball as a pythonfmu FMI 2.0 Co-Simulation slave: a ready-made example that builds in
about a second, offline and without OpenModelica.

The ball falls from h = 1 m and bounces with restitution e. Between bounces the motion is an
exact parabola, so do_step finds each impact analytically instead of with a solver. e is
tunable: a live run can change it in mid-flight, and it applies from the next bounce. The names
match the Modelica Reference FMU BouncingBall: h, v, g and e.
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

# initial=exact (start from the attribute set in __init__): pythonfmu writes no InitialUnknowns,
# so an output with the default initial fails fmu_validate.
OUTPUT = {"causality": Fmi2Causality.output, "initial": Fmi2Initial.exact}
REST_SPEED = 1e-3
"""A bounce slower than this (m/s) leaves the ball resting on the floor; without it the bounces
would get shorter forever."""


class BouncingBall(Fmi2Slave):
    author = "Agentic FMI"
    description = "Bouncing ball: falls under gravity g and bounces with restitution e"
    default_experiment = DefaultExperiment(start_time=0.0, stop_time=3.0, step_size=0.01)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.h, self.v = 1.0, 0.0
        self.g, self.e = -9.81, 0.7
        self.register_variable(Real("h", description="height [m]", **OUTPUT))
        self.register_variable(Real("v", description="velocity [m/s]", **OUTPUT))
        self.register_variable(
            Real(
                "g",
                causality=Fmi2Causality.parameter,
                variability=Fmi2Variability.fixed,
                description="gravity [m/s2], negative (down)",
            )
        )
        self.register_variable(
            Real(
                "e",
                causality=Fmi2Causality.parameter,
                variability=Fmi2Variability.tunable,
                description="coefficient of restitution, 0 to 1",
            )
        )

    def do_step(self, current_time, step_size):
        remaining = step_size
        while remaining > 0:
            if self.h <= 0 and self.v <= REST_SPEED:
                self.h, self.v = 0.0, 0.0  # at rest on the floor
                break
            # Time until the floor: the positive root of h + v*t + g*t^2/2 = 0 (g < 0).
            impact = (-self.v - math.sqrt(self.v**2 - 2 * self.g * self.h)) / self.g
            if impact >= remaining:
                self.h += self.v * remaining + self.g * remaining**2 / 2
                self.v += self.g * remaining
                break
            self.v = -self.e * (self.v + self.g * impact)
            self.h = 0.0
            remaining -= impact
        return True
