"""Simulation engines. Only worker processes run them (ADR 0002).

Two engines, one function each with the signature of ``engines/base.py`` (ADR 0009):
``fmpy_engine.simulate`` runs one FMU with FMPy, ``oms_engine.simulate`` runs a multi-FMU
system with OMSimulator (which it imports only inside ``simulate``).
"""
