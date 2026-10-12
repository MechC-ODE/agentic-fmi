---
name: control-engineer
description: Control engineering judgment for designing, building and verifying controllers, estimators and plant models with the Agentic FMI tools. Use whenever the user asks to design or tune a controller (P, PI, PID, lead-lag, feedforward, state feedback), an observer or Kalman filter, to identify a plant model from a step test or measured data, to meet closed-loop requirements (overshoot, settling time, steady-state error, disturbance rejection, noise, actuator limits), or to judge the stability and robustness of a loop.
---

# Control engineering

Work like a control engineer: requirements first, then the plant, then the simplest structure that meets the requirements, then a careful discrete implementation, then verification of every requirement by simulation. Write and build FMUs with the [fmu-authoring skill](../fmu-authoring/SKILL.md), connect and run loops with the [system-builder skill](../system-builder/SKILL.md), and use the run tools as in the [fmi-expert skill](../fmi-expert/SKILL.md). Do the design math by hand with the formulas below; there is no control toolbox here, so every claim is checked by a run.

## 1. Requirements

Before any code, pin down and write down:

- **Tracking:** overshoot, rise time (10 to 90 %), settling time (with its band, e.g. ±2 %), steady-state error.
- **Disturbances:** what enters where (load force, flow, torque), how large, and the allowed peak deviation and recovery time.
- **Measurement:** sensor noise level (σ), resolution, delay; which signals are measured at all.
- **Actuator:** limits (`u_min`, `u_max`), rate limits, and what happens at the limit.
- **Timing:** the controller's sample time if the target hardware fixes it; otherwise you choose it (section 4).
- **Robustness:** how much the plant parameters may differ from the model (default assumption: ±20 %).

Ask for what is missing when it changes the design; otherwise state an assumption and continue. Translate tracking specs into targets with the second-order rules: ζ = −ln(OS) / √(π² + ln²(OS)) for an overshoot OS (0.05 → ζ ≈ 0.69), settling time (2 %) t_s ≈ 4 / (ζ ω_n), rise time t_r ≈ 1.8 / ω_n.

## 2. Know or identify the plant

Write down a simple model before tuning: gain, time constants, dead time, poles, nonlinearities, saturation.

- **From an FMU:** `fmu_inspect` for inputs, outputs, parameters and units; read the source if Claude wrote it. Then an open-loop step: `run_start` with `inputs` such as `{"F": {"kind": "step", "time": 1, "before": 0, "after": 1}}`, and `run_results` (one or two variables, `max_points` up to 500) to read the response.
- **From measured data:** read the user's CSV with the Read tool (the header and a subsample for a large file). Note the step size Δu, the noise level at rest, and the sampling interval.

Read the model from the step response y(t) after a step Δu at t₀ (Δy = final change):

| Response | Model | Parameters |
|---|---|---|
| Monotonic, S-shaped | FOPDT K·e^(−θs)/(τs + 1) | K = Δy/Δu. With t₂₈ and t₆₃ the times (after t₀) to 28.3 % and 63.2 % of Δy: τ = 1.5·(t₆₃ − t₂₈), θ = t₆₃ − τ |
| Ramps without settling | Integrating K_v·e^(−θs)/s | K_v = steady slope / Δu; θ = where the tangent of the ramp crosses the initial value, minus t₀ |
| Overshoots and rings | Second order K·ω_n²/(s² + 2ζω_n s + ω_n²) | K = Δy/Δu; OS = (y_max − y_final)/Δy gives ζ (formula above); first peak at T_p after t₀: ω_n = π / (T_p·√(1 − ζ²)) |

Check the identified model: compare its gain, t₆₃ or peak and period with the data, and say how well they match. The mismatch is model uncertainty: test robustness against at least that much (section 5).

## 3. Choose the structure

Use the simplest one that meets the requirements, and say why the simpler ones don't.

- **P:** a steady-state error remains on a non-integrating plant. Fine when the plant integrates and no constant disturbance acts.
- **PI:** the default. Zero steady-state error for steps; the integral is what needs anti-windup.
- **PID:** when a lightly damped or second-order plant needs phase lead. Always with a filtered derivative on the measurement, never on the error.
- **Lead-lag:** a lead adds phase like D with bounded high-frequency gain; a lag raises low-frequency gain like I without a pure integrator.
- **Feedforward:** add it when the setpoint moves a lot (u_ff from the inverse plant gain, u_ff = r/K for a stable plant) or a disturbance is measured. Feedback still removes the error.
- **State feedback + observer:** when several states matter (position and velocity) or the plant is unstable; add integral action if the model is uncertain.
- **Kalman filter:** when the measurement is noisy and a model exists; it estimates states the sensor does not give (velocity from position).

## 4. Design formulas

Forms: parallel PID u = K_p·e + K_i·∫e dt + K_d·de/dt; standard form K_c·(1 + 1/(τ_I s) + τ_D s), so K_p = K_c, K_i = K_c/τ_I, K_d = K_c·τ_D. Continuous time unless stated.

- **SIMC PI for FOPDT** (Skogestad): K_c = τ / (K·(τ_c + θ)), τ_I = min(τ, 4·(τ_c + θ)). τ_c is the desired closed-loop time constant; τ_c = θ is a good robust default, larger is smoother and more robust, smaller is faster and more aggressive.
- **SIMC PI for an integrating plant:** K_c = 1 / (K_v·(τ_c + θ)), τ_I = 4·(τ_c + θ).
- **Pole placement, PI on a first-order plant K/(τs + 1):** K_p = (2ζω_n τ − 1)/K, K_i = ω_n² τ / K. The PI zero adds overshoot beyond the second-order prediction: verify, and use setpoint weighting (below) if it is too much.
- **Pole placement, PID on m·ẍ + d·ẋ + k·x = u:** place (s + αω_n)(s² + 2ζω_n s + ω_n²) with α ≈ 3 to 5: K_d = m·(2ζ + α)·ω_n − d, K_p = m·(1 + 2ζα)·ω_n² − k, K_i = m·α·ω_n³.
- **State feedback on the same plant** (states x, v): u = −k₁x − k₂v + N·r with k₁ = m·ω_n² − k, k₂ = 2ζω_n·m − d, N = m·ω_n² (unity steady-state gain, exact only if the model is).

**Sample time.** h ≈ closed-loop rise time / 10 to / 20, or ω_c·h ≈ 0.1 to 0.3 rad at the crossover frequency ω_c. A sampled controller acts like an extra delay of about h/2. In a system, the loop gains about one more communication step (OMSimulator's Jacobi coupling; a Python FMU's outputs also appear after its step). Count the total delay Δ in the design: it costs ω_c·Δ radians of phase margin. Check by halving h (system-builder, "Choosing the step h").

**Discrete implementation** (in `do_step`, with h = `step_size`):

- Integral: I ← I + h·K_i·e (forward Euler; fine when h is small against τ_I).
- Filtered derivative on the measurement, filter T_f = τ_D/N with N ≈ 5 to 20: D ← (T_f·D − K_d·(y − y_prev)) / (T_f + h).
- Setpoint weighting: P = K_p·(b·r − y) with b ≈ 0.5 to 1 cuts setpoint overshoot without changing disturbance rejection.
- Anti-windup by back-calculation: v = P + I + D, u = clamp(v, u_min, u_max), then I ← I + h·(K_i·e + (u − v)/T_t), with T_t ≈ τ_I for PI and T_t ≈ √(τ_I·τ_D) for PID. Simpler alternative (clamping): skip the integral update while u is saturated and e pushes further into the limit.
- Initialization: set the integral so that u at t = 0 equals the actuator's expected value (bumpless start); set y_prev to the first measurement.
- Expose the gains, limits, b and the setpoint as **tunable parameters** so a live run can change them with `run_control`; give them units and sensible `min`/`max` where the FMU format allows.

**Estimators.**

- **Luenberger observer:** x̂' = A x̂ + B u + L(y − C x̂), with the poles of A − LC 2 to 6 times faster than the closed loop. Faster makes the estimate noisier.
- **Kalman filter** (discrete, inside a Python FMU; standard library only, so write the 2×2 algebra out by hand). With F ≈ I + A·h and G ≈ B·h for small h:
  - predict: x̂ ← F x̂ + G u, P ← F P Fᵀ + Q
  - update: ν = y − C x̂, S = C P Cᵀ + R, K = P Cᵀ S⁻¹, x̂ ← x̂ + K ν, P ← (I − K C) P
  - R = σ² of the sensor noise (spec, or the variance of the signal at rest). For a position/velocity state with an unknown acceleration of spectral density q: Q = q·[[h³/3, h²/2], [h²/2, h]]. Tune q (and R if unsure) on a log scale.
  - Output the innovation ν and NIS = ν²/S (one measurement). For a consistent filter the mean NIS is about 1: well above means Q or R is too small (overconfident), well below means too large.
- **Noise for testing:** a small Python source FMU with parameters `seed` and `sigma` that draws from `random.Random(seed).gauss(0, sigma)`, added to the measurement. Create the generator in the first `do_step`, not in `__init__`: parameter values are applied after `__init__`, so a generator made there ignores `seed`. Don't name the file `random.py`. The same seed reproduces a run; judge a filter over several seeds, not one.

## 5. Verify every requirement

Simulate each case and report pass or fail with numbers and run IDs. Keep to four runs at a time.

| Check | How | Measure |
|---|---|---|
| Nominal step | Setpoint step on the closed loop | Overshoot (y_max − r)/r from the summary; steady-state error r − final; settling time = last time outside the band, from `run_results` (resolution = horizon / `max_points`, so shorten `stop_time` around the settling region if needed) |
| Disturbance | A step on an unconnected plant input or load parameter: `inputs` for a single FMU, a source component in a system, or `run_control` `set` at a known time in a live run | Peak deviation, recovery time |
| Actuator limits | A setpoint step large enough to saturate | u stays within limits; overshoot after saturation close to nominal (no windup) |
| Noise | The noise source on the measurement | Spread of u (e.g. max − min at steady state) and of the error; a noisy u means less derivative or a slower filter |
| Robustness | Plant parameters ±20 % (or the identified mismatch, up to ±50 %) through `start_values` (`plant.m`, `plant.k`, ...); loop gain ×2 | Still stable; requirements degrade gracefully. Stable at twice the gain is a rough gain margin above 6 dB |
| Step size | Halve h and rerun | Unchanged answers; otherwise h is part of the design and you say which h the numbers are for |
| Estimator | Noisy runs, several seeds | RMS of the estimation error after convergence, well below σ for a useful filter; mean NIS near 1 |

Use `run_compare` against the nominal run and `run_plot` for the controlled output and u together. Fix the design and re-verify; don't stretch a requirement to fit.

## 6. Report a design note

End with a short note:

1. Requirements and assumptions (marked as such).
2. Plant model and how it was obtained (data or FMU, the identified numbers, the fit).
3. Structure and why; why not simpler.
4. Parameters and how each was derived (formula and inputs), sample time and the delay counted.
5. Verification table: requirement, target, result, run ID, pass or fail.
6. Residual risks: untested conditions, model mismatch, noise, saturation, numerical step effects.
