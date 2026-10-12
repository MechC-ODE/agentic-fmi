"""Helpers for the SSP tests: SSD and SSV text, raw SSP archives (malformed ones included), the
check against OMSimulator's bundled XSDs, and the OMSimulator child process.

Collaborators: ``tests/ssp/conftest.py`` and the test modules (pytest puts this directory on
``sys.path``), ``agentic_fmi.ssp`` (only its constants). OMSimulator runs only in the child
process ``oms_child.py``; here its package is only located with ``importlib.util.find_spec``,
which doesn't import it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import warnings
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agentic_fmi.ssp import NAMESPACES

MakeSsp = Callable[..., Path]
"""The ``make_ssp`` fixture: write an SSP into the workspace, return its path."""
Pythonfmu = Callable[[str], Path]
"""The ``pythonfmu`` fixture: copy a built pythonfmu FMU into the workspace, return the copy."""

HERE = Path(__file__).resolve().parent
OMS_CHILD = HERE / "oms_child.py"

ROOT_NAMESPACES = " ".join(
    f'xmlns:{prefix}="{NAMESPACES[prefix]}"' for prefix in ("ssd", "ssc", "ssv", "oms")
)

BILLION_LAUGHS = (
    '<!DOCTYPE lolz [<!ENTITY lol "lol">'
    + "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
        for i in range(1, 10)
    )
    + "]>\n"
)
"""A DOCTYPE whose entities expand to a billion ``lol`` strings."""


def component_xml(
    name: str, source: str, *, attributes: str = "", bindings: str = "", extra: str = ""
) -> str:
    """An ``ssd:Component``; ``bindings`` are ``ssd:ParameterBinding`` elements."""
    inner = f"<ssd:ParameterBindings>{bindings}</ssd:ParameterBindings>" if bindings else ""
    return (
        f'<ssd:Component name="{name}" source="{source}"{attributes}>{inner}{extra}</ssd:Component>'
    )


def connection_xml(start: str, first: str, end: str, second: str, inner: str = "") -> str:
    return (
        f'<ssd:Connection startElement="{start}" startConnector="{first}" endElement="{end}" '
        f'endConnector="{second}">{inner}</ssd:Connection>'
    )


DEFAULT_COMPONENTS = component_xml("PI", "resources/pi.fmu") + component_xml(
    "MSD", "resources/plant.fmu"
)
DEFAULT_CONNECTIONS = connection_xml("PI", "u", "MSD", "F") + connection_xml("MSD", "x", "PI", "y")


def binding_xml(
    source: str | None = None,
    *,
    prefix: str | None = None,
    inline: str | None = None,
    attributes: str = "",
    children: str = "",
) -> str:
    """An ``ssd:ParameterBinding`` of an SSV member or of inline values."""
    attrs = f' source="{source}"' if source is not None else ""
    attrs += f' prefix="{prefix}"' if prefix is not None else ""
    inner = f"<ssd:ParameterValues>{inline}</ssd:ParameterValues>" if inline else ""
    return f"<ssd:ParameterBinding{attrs}{attributes}>{inner}{children}</ssd:ParameterBinding>"


def ssd_text(
    *,
    components: str = DEFAULT_COMPONENTS,
    connections: str = DEFAULT_CONNECTIONS,
    bindings: str = "",
    system_head: str = "",
    system_tail: str = "",
    experiment: str = '<ssd:DefaultExperiment startTime="0" stopTime="2"/>',
    step: float | None = 0.01,
    prolog: str = "",
    version: str = "1.0",
) -> bytes:
    """An SSD of a flat system named ``loop``. ``system_head`` goes before the parameter
    bindings (system connectors), ``system_tail`` after the connections (signal dictionaries);
    ``step`` becomes OMSimulator's annotation; ``prolog`` sits before the root element."""
    annotation = (
        ""
        if step is None
        else '<ssd:Annotations><ssc:Annotation type="org.openmodelica"><oms:Annotations>'
        f'<oms:SimulationInformation name="ma" method="oms_ma" fixedStepSize="{step}"/>'
        "</oms:Annotations></ssc:Annotation></ssd:Annotations>"
    )
    connected = f"<ssd:Connections>{connections}</ssd:Connections>" if connections else ""
    bound = f"<ssd:ParameterBindings>{bindings}</ssd:ParameterBindings>" if bindings else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n{prolog}'
        f'<ssd:SystemStructureDescription {ROOT_NAMESPACES} name="loop" version="{version}">'
        f'<ssd:System name="loop">{system_head}{bound}<ssd:Elements>{components}</ssd:Elements>'
        f"{connected}{system_tail}{annotation}</ssd:System>{experiment}"
        "</ssd:SystemStructureDescription>\n"
    ).encode()


def ssv_text(
    parameters: Mapping[str, tuple[str, str]],
    *,
    version: str = "1.0",
    prolog: str = "",
    inline: bool = False,
    units: Mapping[str, str] | None = None,
) -> str:
    """An ``ssv:ParameterSet`` with ``name -> (type, value)``; ``inline`` leaves out the XML
    declaration and the namespace declarations (the SSD's root declares them)."""
    rows = "".join(
        f'<ssv:Parameter name="{name}"><ssv:{kind} value="{value}"'
        f"{_unit(units, name)}/></ssv:Parameter>"
        for name, (kind, value) in parameters.items()
    )
    namespaces = "" if inline else f' xmlns:ssv="{NAMESPACES["ssv"]}"'
    body = (
        f'<ssv:ParameterSet{namespaces} version="{version}" name="p">'
        f"<ssv:Parameters>{rows}</ssv:Parameters></ssv:ParameterSet>"
    )
    return body if inline else f'<?xml version="1.0" encoding="UTF-8"?>\n{prolog}{body}\n'


def _unit(units: Mapping[str, str] | None, name: str) -> str:
    unit = (units or {}).get(name)
    return f' unit="{unit}"' if unit else ""


def write_zip(
    path: Path,
    members: Mapping[str, bytes] | Sequence[tuple[str, bytes]],
    *,
    compression: Mapping[str, int] | None = None,
) -> Path:
    """Write ``members`` in order (a sequence may repeat a name); ``compression`` picks a
    method per name (default deflated)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    items = list(members.items()) if isinstance(members, Mapping) else list(members)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "Duplicate name"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in items:
                method = (compression or {}).get(name, zipfile.ZIP_DEFLATED)
                archive.writestr(zipfile.ZipInfo(name, (2024, 1, 1, 0, 0, 0)), data, method)
    return path


def central_entry(data: bytes, name: str) -> int:
    """The offset of ``name``'s central-directory header in a zip's bytes."""
    at = data.find(b"PK\x01\x02")
    while at >= 0:
        length = int.from_bytes(data[at + 28 : at + 30], "little")
        if data[at + 46 : at + 46 + length] == name.encode():
            return at
        at = data.find(b"PK\x01\x02", at + 4)
    raise AssertionError(f"no central entry {name!r}")


def patch(path: Path, offset: int, value: bytes) -> None:
    data = bytearray(path.read_bytes())
    data[offset : offset + len(value)] = value
    path.write_bytes(bytes(data))


def set_flag_bits(path: Path, name: str, bits: int) -> None:
    """OR ``bits`` into the general-purpose flags of ``name``'s central entry."""
    at = central_entry(path.read_bytes(), name) + 8
    flags = int.from_bytes(path.read_bytes()[at : at + 2], "little") | bits
    patch(path, at, flags.to_bytes(2, "little"))


def set_declared_size(path: Path, name: str, size: int) -> None:
    """Make ``name``'s central entry declare ``size`` uncompressed bytes."""
    patch(path, central_entry(path.read_bytes(), name) + 24, size.to_bytes(4, "little"))


def set_declared_entries(path: Path, count: int) -> None:
    """Make the end-of-central-directory record declare ``count`` entries."""
    at = path.read_bytes().rfind(b"PK\x05\x06")
    patch(path, at + 8, count.to_bytes(2, "little") * 2)


def oms_schema_dir() -> Path | None:
    """OMSimulator's bundled SSP schemas, located without importing the package."""
    spec = importlib.util.find_spec("OMSimulator")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(next(iter(spec.submodule_search_locations))) / "schema" / "ssp"
    return path if path.is_dir() else None


def xsd_errors(xml: bytes, schema_file: str) -> list[str]:
    """The XSD validation errors of ``xml`` against one of OMSimulator's bundled schemas."""
    from lxml import etree

    directory = oms_schema_dir()
    assert directory is not None
    schema = etree.XMLSchema(etree.parse(str(directory / schema_file)))
    return [] if schema.validate(etree.fromstring(xml)) else [str(e) for e in schema.error_log]


def run_oms_child(
    ssp: Path, system: str, stop: float, step: float, names: Iterable[str], scratch: Path
) -> dict[str, Any]:
    """Run ``oms_child.py`` on ``ssp`` in a fresh interpreter (``TMPDIR`` and the working
    directory in ``scratch``, ``LD_PRELOAD`` removed); return its report."""
    scratch.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key != "LD_PRELOAD"}
    env["TMPDIR"] = str(scratch)
    command = [sys.executable, "-I", "-B", str(OMS_CHILD), str(ssp), system, repr(stop)]
    done = subprocess.run(
        [*command, repr(step), *names],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    assert done.returncode == 0 and lines, (done.returncode, done.stdout[-800:], done.stderr[-800:])
    report: dict[str, Any] = json.loads(lines[-1])
    return report
