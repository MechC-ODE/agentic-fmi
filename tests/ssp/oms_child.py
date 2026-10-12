"""Child process of the SSP tests: load an exported SSP with OMSimulator's own importer
(``SSP(path)``), step it with ``doStep`` and read the given variables after each step, as the
worker's engine does, then print the rows and any Python warning (OMSimulator reports an SSD
or SSV that fails its bundled XSDs as a ``UserWarning``) as one JSON line.

Run as ``python -I -B oms_child.py SSP SYSTEM STOP STEP COMPONENT.VARIABLE...`` with ``TMPDIR``
and the working directory in a scratch directory. Collaborators: OMSimulator (imported only
here, never in the test process), ``ssp_support.run_oms_child``.
"""

import json
import sys
import warnings


def main(argv: list[str]) -> None:
    path, system, stop_text, step_text, *names = argv
    stop, step = float(stop_text), float(step_text)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from OMSimulator import SSP, CRef

        model = SSP(path, temp_dir="ssp")
        instance = model.instantiate()
        instance.setResultFile("")
        instance.initialize()
        refs = [CRef(system, *name.split(".", 1)) for name in names]
        rows = [[instance.getTime(), *[instance.getValue(ref) for ref in refs]]]
        while instance.getTime() < stop - step / 2:
            instance.doStep()
            rows.append([instance.getTime(), *[instance.getValue(ref) for ref in refs]])
        instance.terminate()
        instance.delete()
    messages = [str(warning.message)[:300] for warning in caught]
    print(json.dumps({"rows": rows, "warnings": messages}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
