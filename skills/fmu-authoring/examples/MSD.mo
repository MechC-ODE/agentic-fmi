model MSD "Mass-spring-damper plant with a force input"
  parameter Real m(unit="kg", min=0.001) = 1.0 "mass";
  parameter Real k(unit="N/m") = 10.0 "spring stiffness";
  parameter Real d(unit="N.s/m") = 2.0 "damping";
  input Real F(unit="N") = 0 "external force";
  output Real x(start=0, fixed=true, unit="m") "position";
  output Real v(start=0, fixed=true, unit="m/s") "velocity";
equation
  der(x) = v;
  m*der(v) = F - k*x - d*v;
  annotation(experiment(StartTime=0, StopTime=10, Tolerance=1e-6));
end MSD;
