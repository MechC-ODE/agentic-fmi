"""PI controller as a pythonfmu FMI 2.0 Co-Simulation slave: u = Kp*e + Ki*integral(e), e = r - y.

Wire it to the MSD plant: controller u -> plant F, plant x -> controller y. The output is
computed in do_step, so it lags the input by one communication step.
"""

from pythonfmu import (
    DefaultExperiment,
    Fmi2Causality,
    Fmi2Initial,
    Fmi2Slave,
    Fmi2Variability,
    Real,
)

TUNABLE = {"causality": Fmi2Causality.parameter, "variability": Fmi2Variability.tunable}
# initial=exact (start 0.0, from self.u): pythonfmu writes no InitialUnknowns, so an output with
# the default initial fails fmu_validate.
OUTPUT = {"causality": Fmi2Causality.output, "initial": Fmi2Initial.exact}


class PIController(Fmi2Slave):
    author = "Agentic FMI"
    description = "PI controller: u = Kp*(r - y) + Ki*integral(r - y) dt"
    default_experiment = DefaultExperiment(start_time=0.0, stop_time=10.0, step_size=0.01)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.Kp, self.Ki, self.r = 10.0, 15.0, 1.0
        self.y, self.u, self.integral = 0.0, 0.0, 0.0
        self.register_variable(Real("Kp", description="proportional gain", **TUNABLE))
        self.register_variable(Real("Ki", description="integral gain", **TUNABLE))
        self.register_variable(Real("r", description="setpoint", **TUNABLE))
        self.register_variable(Real("y", causality=Fmi2Causality.input, description="measurement"))
        self.register_variable(Real("u", description="control", **OUTPUT))

    def do_step(self, current_time, step_size):
        error = self.r - self.y
        self.integral += error * step_size
        self.u = self.Kp * error + self.Ki * self.integral
        return True
