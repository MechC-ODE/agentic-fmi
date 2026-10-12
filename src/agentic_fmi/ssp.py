"""SSP and SSV as exchange formats: safe import of ``.ssp`` files, deterministic export of
system files, and the one SSV reader (ADR 0009).

Runs in the MCP server and never imports OMSimulator: SSD and SSV are XML, read with a hardened
lxml parser and written with lxml. The worker never sees an SSP; it builds the model from the
resolved ``SystemSpec`` (``engines/oms_engine.py``).

**Import (untrusted input), safe by construction:**
- The ``.ssp`` resolves inside the allowed roots and is at most ``Limits.ssp_max_bytes``. Before
  ``zipfile`` parses anything, the end-of-central-directory record is read and the archive is
  refused if it declares more than ``Limits.ssp_max_members`` entries or a central directory
  too large for them; after opening, the directory is refused if it holds more entries than
  that, duplicate names, encrypted entries, or a compression other than stored or deflated.
  Nothing is ever extracted by name: only members the SSD references are read, through
  ``ZipFile.open``, with caps per member (``ssp_xml_max_bytes`` for SSD and SSV,
  ``ssp_member_max_bytes`` for FMUs) and in total (``ssp_total_max_bytes``), checked against the
  declared size first and counted again on the decompressed bytes while streaming; a member
  whose stream doesn't match its declared size or CRC is refused.
- References (``source`` attributes) are refused if empty, absolute, containing ``..``, ``.``
  or empty segments, ``\\``, NUL or a URI scheme, or with ``sourceBase="component"``; they are
  checked before and after one percent-decoding and must then name a member exactly. Output
  file names come from our own grammar only (``imported/<name>/<name>.json`` and
  ``imported/<name>/<component>.fmu``), never from member paths.
- XML must be UTF-8; any DOCTYPE is refused (billion laughs, external entities) and the parser
  resolves no entities, loads no DTD and never touches the network (``resolve_entities=False,
  load_dtd=False, no_network=True, huge_tree=False``).
- Only flat systems that fit system file v1: Co-Simulation FMU components and connections
  between them. Refused by name: nested systems (``ssd:System`` elements or SSD/SSP
  components), SSM parameter mappings, signal dictionaries, system-level connectors (declared,
  or a connection end without an element), connection transformations and array-element
  connections, ModelExchange-only components (``implementation="ModelExchange"`` or an FMU
  without a Co-Simulation interface).
- Parameter bindings (inline or ``.ssv`` members, ``prefix`` prepended to every name) on the
  root system and on components become each component's ``parameters``. Precedence follows SSP
  1.0 section 5.2.3: within one level the later binding wins, and a higher level wins over a
  lower one (system over component); every override is reported as a warning. (OMSimulator
  applies the component level last; our exports never bind at two levels, so they are
  unaffected.) Root-level names are ``<component>.<variable>``; a name that names no component
  is ignored with a warning. An ``ssv:Enumeration`` item name becomes the item's integer value
  where the FMU declares the item; a unit that differs from the FMU's is kept with a warning.
- The experiment comes from ``DefaultExperiment`` and OMSimulator's annotation
  (``oms:SimulationInformation fixedStepSize`` on the system); a missing step becomes
  ``config.SYSTEM_DEFAULT_STEP_S`` and a missing stop time ``start + SYSTEM_DEFAULT_STOP_S``,
  each with a warning.
- Everything is written into a staging directory ``imported/.<name>.tmp-<pid>-<thread>``, the
  system file is loaded and checked there (``system.problems``; its errors and warnings become
  warnings of the result), and the directory is renamed to ``imported/<name>`` at the end; any
  failure removes the staging directory. An existing ``imported/<name>`` (any case) is never
  replaced. The source SSP is only ever opened for reading.

**Export, deterministic:** ``system_export`` writes a new ``.ssp`` (never overwriting) with
``SystemStructure.ssd``, ``resources/<component>.fmu`` (one per distinct FMU content, named
after the first component that uses it, stored uncompressed) and, if any value is set,
``resources/parameters.ssv`` bound once at the root system with names
``<component>.<variable>`` (system-file values < ``paramset``, converted to each variable's
type). Entries are sorted, timestamps fixed at 1980-01-01, mode 0644, no
``generationDateAndTime``, attribute order fixed, so repeated exports are byte-identical. The
SSD (version 1.0, or 2.0 when an FMI 3.0 connector type such as ``Float64`` or a connector kind
SSP 1.0.1 lacks, such as ``local`` for a state a value sets, is needed) lists
every scalar input, output and parameter connector plus every variable a value sets,
``DefaultExperiment`` start and stop, the step as OMSimulator's annotation, and each FMU's
sha256 in an ``org.agentic-fmi`` annotation. Enumerations travel as ``Integer`` (as OMSimulator
reads them) and no units are written. The archive is written next to ``dest``, the FMUs are
re-hashed (an FMU changed meanwhile is refused), and it is moved into place with ``os.link``
(never replacing a file that appeared meanwhile). Spike (h) confirmed this shape
validates against OMSimulator's bundled SSD and SSV XSDs and that OMSimulator 3 re-imports it
with the same result (``ParameterBindings`` must precede ``Elements``).

Collaborators: ``system.py`` (``SystemFile``, ``load_system``, ``problems``,
``resolve_parameters``, ``is_reserved_name``, ``Paramset``), ``fmu/inspect.py``
(``read_description`` of imported FMUs, ``sha256_file``), ``fmu/catalog.py``
(``relative_path``), ``config`` (limits, ``imported_dir``, ``SYSTEM_DEFAULT_STEP_S``,
``SYSTEM_DEFAULT_STOP_S``), ``budget.py`` (the import result is cut to the JSON budget),
``runs.py`` (``read_paramset`` for ``run_start``), ``mcp/server.py`` (``system_import``,
``system_export``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote

from lxml import etree
from pydantic import BaseModel, ValidationError

from agentic_fmi import __version__, budget
from agentic_fmi import system as systems
from agentic_fmi.config import SYSTEM_DEFAULT_STEP_S, SYSTEM_DEFAULT_STOP_S, Config, Limits
from agentic_fmi.errors import UserError
from agentic_fmi.fmu.catalog import relative_path
from agentic_fmi.fmu.inspect import ModelDescription, read_description, sha256_file, truncate
from agentic_fmi.store.protocol import (
    BUILD_NAME_PATTERN,
    COMPONENT_NAME_PATTERN,
    VARIABLE_REF_PATTERN,
    FileRef,
    ResolvedValue,
    StartValue,
)
from agentic_fmi.system import Paramset, SystemFile

SSD_MEMBER = "SystemStructure.ssd"
"""The default variant every SSP must hold at its root (SSP 1.0 section 3); other ``.ssd``
variants are ignored with a warning."""

PARAMETERS_MEMBER = "resources/parameters.ssv"
"""The one root-level parameter set an export writes."""

FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
"""The timestamp of every exported zip entry."""

NAMESPACES = {
    "ssd": "http://ssp-standard.org/SSP1/SystemStructureDescription",
    "ssc": "http://ssp-standard.org/SSP1/SystemStructureCommon",
    "ssv": "http://ssp-standard.org/SSP1/SystemStructureParameterValues",
    "ssm": "http://ssp-standard.org/SSP1/SystemStructureParameterMapping",
    "ssb": "http://ssp-standard.org/SSP1/SystemStructureSignalDictionary",
    "oms": "https://raw.githubusercontent.com/OpenModelica/OMSimulator/master/schema/oms.xsd",
    "afmi": "urn:agentic-fmi",
}
"""SSP 1.0 and 2.0 share these namespace URIs; ``oms`` is OpenModelica's annotation schema and
``afmi`` ours (the content of the ``org.agentic-fmi`` annotation)."""

ANNOTATION_TYPE = "org.agentic-fmi"
"""Our ``ssc:Annotation`` type (reverse domain notation, SSP 1.0 section 2.1.3)."""

OMS_ANNOTATION_TYPE: Final = "org.openmodelica"
FMU_TYPE: Final = "application/x-fmu-sharedlibrary"
PARAMETER_SET_TYPE: Final = "application/x-ssp-parameter-set"
NESTED_TYPES: Final = frozenset({"application/x-ssp-definition", "application/x-ssp-package"})
"""Component types that embed another system (an SSD or a whole SSP)."""
SOLVER_NAME: Final = "ma"
"""The one ``oms_ma`` solver an export declares and every component uses."""
XML_VERSIONS: Final = ("1.0", "2.0")
TRANSFORMATIONS: Final = frozenset(
    {
        "LinearTransformation",
        "BooleanMappingTransformation",
        "IntegerMappingTransformation",
        "EnumerationMappingTransformation",
    }
)
FLOAT_TYPES: Final = frozenset({"Real", "Float64", "Float32"})
INTEGER_TYPES: Final = frozenset(
    {"Integer", *(f"{sign}Int{bits}" for sign in ("", "U") for bits in (8, 16, 32, 64))}
)
CONNECTOR_TYPES: Final = {
    "Real": "Real",
    "Integer": "Integer",
    "Boolean": "Boolean",
    "String": "String",
    "Enumeration": "Integer",
    **{name: name for name in ("Float64", "Float32", *INTEGER_TYPES - {"Integer"})},
}
"""FMI variable type -> SSD connector type; Binary and Clock variables get no connector."""
SSP1_TYPES: Final = frozenset({"Real", "Integer", "Boolean", "String"})
SSP1_KINDS: Final = frozenset({"input", "output", "parameter", "calculatedParameter", "inout"})
"""The connector kinds SSP 1.0.1 allows; any other (``local`` for a state a value sets, ...) needs
SSD version 2.0."""
CONNECTOR_CAUSALITIES: Final = frozenset({"input", "output", "parameter"})

ZIP_MEMBER_MODE: Final = stat.S_IFREG | 0o644
CHUNK_BYTES: Final = 1 << 20
EOCD_SIGNATURE: Final = b"PK\x05\x06"
EOCD_BYTES: Final = 22
ZIP64_LOCATOR_SIGNATURE: Final = b"PK\x06\x07"
ZIP64_LOCATOR_BYTES: Final = 20
ZIP64_EOCD_SIGNATURE: Final = b"PK\x06\x06"
ZIP64_EOCD_BYTES: Final = 56
MAX_ZIP_COMMENT: Final = 0xFFFF
CENTRAL_DIRECTORY_ENTRY_MAX_BYTES: Final = 1024
"""Average central-directory bytes allowed per permitted entry (a typical entry takes 50 to
150): bounds what ``zipfile`` parses into memory before the entry count can be checked."""
ENCRYPTED_FLAG: Final = 0x1
MAX_WARNINGS: Final = 5
MAX_NAMED: Final = 3
REF_SHOWN_CHARS: Final = 120

DOCTYPE: Final = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
URI_SCHEME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
DOUBLE: Final = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
INTEGER: Final = re.compile(r"^[+-]?\d+$")
BOOLEANS: Final = {"true": True, "1": True, "false": False, "0": False}
IMPORT_NAME: Final = re.compile(BUILD_NAME_PATTERN)
COMPONENT_NAME: Final = re.compile(COMPONENT_NAME_PATTERN)
VARIABLE_REF: Final = re.compile(VARIABLE_REF_PATTERN)


class SystemImport(BaseModel):
    """The ``system_import`` result."""

    system: str
    """Workspace-relative path of the new system file, ``imported/<name>/<name>.json``; each
    component's FMU copy is ``<component>.fmu`` next to it."""
    components: dict[str, str]
    """Component -> sha256 of its FMU copy."""
    connections: int
    parameters: int
    """Parameter values taken from the SSP's bindings."""
    warnings: list[str] = []
    """At most five: the imported system's errors (``error: ...``), then the import's own
    warnings (overrides, defaults, ignored names), then the system's warnings."""
    hint: str | None = None


class SystemExport(BaseModel):
    """The ``system_export`` result."""

    path: str
    """Workspace-relative path of the new ``.ssp``."""
    sha256: str
    size_bytes: int
    fmus: dict[str, str]
    """Component -> sha256 of the FMU embedded for it."""
    parameters: int


# --- XML ----------------------------------------------------------------------------------------


def _tag(prefix: str, name: str) -> str:
    return f"{{{NAMESPACES[prefix]}}}{name}"


def _local(element: Any) -> str:
    """The element's tag without its namespace."""
    return str(etree.QName(element).localname)


def _children(element: Any) -> list[Any]:
    """The element children (no comments or processing instructions)."""
    return [child for child in element if isinstance(child.tag, str)]


def _xml_root(data: bytes, where: str) -> Any:
    """Parse untrusted XML: UTF-8 only, no DOCTYPE, no entities, no DTD, no network.

    Raises:
        UserError: not UTF-8, a DOCTYPE, or not well-formed.
    """
    if b"\x00" in data:
        raise UserError(
            f"{where} is not UTF-8 XML (it holds NUL bytes, as UTF-16 text does). Save it as UTF-8."
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UserError(f"{where} is not UTF-8 text. Save it as UTF-8 XML.") from exc
    if DOCTYPE.search(data):
        raise UserError(_doctype_message(where))
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        dtd_validation=False,
        encoding="utf-8",
    )
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise UserError(
            f"{where} is not well-formed XML ({truncate(str(exc), 200)}). Fix it, or export it "
            "again from its tool."
        ) from exc
    if root.getroottree().docinfo.doctype:
        raise UserError(_doctype_message(where))
    return root


def _doctype_message(where: str) -> str:
    return (
        f"{where} has a DOCTYPE declaration, which Agentic FMI refuses (DTDs and entities can "
        "expand without bound or read other files). SSP files need none; remove it."
    )


# --- SSV ----------------------------------------------------------------------------------------


def parse_ssv(data: bytes, where: str) -> tuple[dict[str, StartValue], dict[str, str]]:
    """Parse an SSV ``ssv:ParameterSet`` (version 1.0 or 2.0, UTF-8) into name -> value and
    name -> declared unit.

    Accepts ``ssv:Real``, ``Integer``, ``Boolean``, ``String``, ``Enumeration`` (its integer
    value, or the item name as a string) and the SSP 2.0 numeric types (``Float64``,
    ``Int32``, ...); refuses ``Binary``, arrays, non-finite numbers, duplicate names and a
    DOCTYPE. ``where`` names the file or member in messages. The caller bounds ``data``
    (``Limits.system_file_max_bytes`` for a workspace file, ``ssp_xml_max_bytes`` in an SSP).

    Raises:
        UserError: what is wrong, where, and the fix.
    """
    root = _xml_root(data, where)
    if root.tag != _tag("ssv", "ParameterSet"):
        raise UserError(
            f"{where} is not an SSV parameter set: its root element is <{_local(root)}>, not "
            f"ssv:ParameterSet in {NAMESPACES['ssv']}. Write an SSP parameter values file."
        )
    return _parameter_values(root, where)


def _parameter_values(
    parameter_set: Any, where: str
) -> tuple[dict[str, StartValue], dict[str, str]]:
    """The values and units of an ``ssv:ParameterSet`` element (a file's root, or inline)."""
    version = parameter_set.get("version")
    if version not in XML_VERSIONS:
        raise UserError(
            f'{where}: SSV version {version!r} is not supported; use version="1.0" or "2.0".'
        )
    values: dict[str, StartValue] = {}
    units: dict[str, str] = {}
    parameters = parameter_set.find(_tag("ssv", "Parameters"))
    for parameter in [] if parameters is None else parameters.iterfind(_tag("ssv", "Parameter")):
        name = (parameter.get("name") or "").strip()
        if not name:
            raise UserError(f"{where}: an ssv:Parameter has no name. Give every parameter one.")
        if name in values:
            raise UserError(f"{where}: parameter {name!r} appears twice. Keep one value per name.")
        values[name], unit = _parameter_value(parameter, f"{where}: parameter {name!r}")
        if unit:
            units[name] = unit
    return values, units


def _parameter_value(parameter: Any, where: str) -> tuple[StartValue, str | None]:
    """One ``ssv:Parameter``'s value and unit."""
    typed = [
        child
        for child in _children(parameter)
        if etree.QName(child).namespace == NAMESPACES["ssv"] and _local(child) != "Annotations"
    ]
    if parameter.find(_tag("ssc", "Dimensions")) is not None:
        raise UserError(f"{where} is an array; systems set scalar values only. Remove it.")
    if len(typed) != 1:
        raise UserError(f'{where} needs exactly one value element such as <ssv:Real value="1.0"/>.')
    element = typed[0]
    kind = _local(element)
    if kind == "Binary":
        raise UserError(f"{where} is Binary, which systems can't set. Remove it.")
    text = element.get("value")
    if element.find(_tag("ssv", "Value")) is not None:
        raise UserError(f"{where} has several values (an array); give one value.")
    if text is None:
        raise UserError(f'{where}: <ssv:{kind}> has no value attribute. Add value="...".')
    if kind != "String" and len(text.split()) > 1:
        raise UserError(f"{where} has several values (an array); give one value.")
    unit = element.get("unit") if kind in FLOAT_TYPES else None
    return _typed_value(kind, text, where), unit


def _typed_value(kind: str, text: str, where: str) -> StartValue:
    """The text of an ``ssv:<kind>`` value as a Python value."""
    token = text.strip()
    if kind in FLOAT_TYPES:
        return _double(token, where)
    if kind in INTEGER_TYPES:
        if not INTEGER.fullmatch(token):
            raise UserError(f"{where}: {text!r} is not a whole number. Write one, e.g. 3.")
        return int(token)
    if kind == "Boolean":
        if token not in BOOLEANS:
            raise UserError(f"{where}: {text!r} is not a Boolean. Write true or false.")
        return BOOLEANS[token]
    if kind == "String":
        return text
    if kind == "Enumeration":
        return int(token) if INTEGER.fullmatch(token) else token
    raise UserError(
        f"{where} has the unknown type <ssv:{kind}>. Use Real, Integer, Boolean, String or "
        "Enumeration (or an SSP 2.0 numeric type such as Float64)."
    )


def _double(text: str, where: str) -> float:
    """``text`` as a finite ``xs:double``."""
    token = text.strip()
    if not DOUBLE.fullmatch(token):
        raise UserError(
            f"{where}: {text!r} is not a finite number. Write a decimal number such as 0.01 "
            "(INF and NaN are not accepted)."
        )
    value = float(token)
    if value != value or value in (float("inf"), float("-inf")):
        raise UserError(f"{where}: {text!r} is not a finite number. Write a finite value.")
    return value


def read_paramset(config: Config, paramset: str) -> Paramset:
    """Read a workspace ``.ssv`` for ``run_start(paramset=...)`` or ``system_export``: resolve
    it inside the allowed roots, read it once (at most ``Limits.system_file_max_bytes``), hash
    those bytes and :func:`parse_ssv` them. Names must be ``<component>.<variable>``.

    Raises:
        UserError: not found, outside the roots, not ``.ssv``, too large or invalid.
    """
    path = _resolve_input(config, paramset, ".ssv", "Parameter set", "paramset")
    shown = relative_path(config, path)
    data = _read_bounded(path, config.limits.system_file_max_bytes, shown)
    values, units = parse_ssv(data, shown)
    bad = [name for name in values if not VARIABLE_REF.fullmatch(name)]
    if bad:
        listed = ", ".join(repr(name) for name in bad[:MAX_NAMED])
        raise UserError(
            f"{shown}: {listed} {'is' if len(bad) == 1 else 'are'} not '<component>.<variable>'. "
            "A parameter set applies to the whole system, so name each value after its "
            "component, e.g. 'PI.Kp'."
        )
    return Paramset(
        ref=FileRef(path=str(path), sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)),
        values=values,
        units=units,
    )


def _resolve_input(config: Config, given: str, suffix: str, label: str, argument: str) -> Path:
    """The absolute, symlink-resolved path of an existing ``suffix`` file inside the roots
    (containment checked before existence)."""
    if not given or not given.strip():
        raise UserError(
            f"The {argument} argument is empty. Pass the workspace-relative path of a "
            f"{suffix} file."
        )
    shown = repr(given[:REF_SHOWN_CHARS])
    candidate = Path(given)
    if not candidate.is_absolute():
        candidate = config.workspace / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserError(f"Can't resolve the {argument} path {shown}: {exc}.") from exc
    if not any(resolved.is_relative_to(root) for root in config.allowed_roots):
        raise UserError(
            f"{label} {shown} is outside the workspace ({config.workspace}), possibly through a "
            "symlink. Copy it into the workspace and pass its workspace-relative path."
        )
    if candidate.suffix.lower() != suffix:
        raise UserError(f"{shown} is not a {suffix} file. Pass a {suffix} file.")
    if resolved.is_dir():
        raise UserError(f"{shown} is a directory. Pass a {suffix} file.")
    if not resolved.is_file():
        raise UserError(
            f"{label} {shown} was not found in the workspace. Paths are relative to the "
            "workspace; check the name."
        )
    return resolved


def _read_bounded(path: Path, limit: int, shown: str) -> bytes:
    """The file's bytes, read once; at most ``limit`` of them."""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise UserError(
            f"{shown} is larger than {limit} bytes ({limit // 1024} KiB), the most a parameter "
            "set may be. Keep only the values the system needs."
        )
    return data


# --- the archive --------------------------------------------------------------------------------


def _check_central_directory(path: Path, limits: Limits, shown: str) -> None:
    """Refuse an archive whose end-of-central-directory record declares too many entries or a
    central directory too large for them, before ``zipfile`` parses it into memory. Finds the
    record as ``zipfile`` does (the last signature in the final 64 KiB)."""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        span = min(size, EOCD_BYTES + MAX_ZIP_COMMENT + ZIP64_LOCATOR_BYTES + ZIP64_EOCD_BYTES)
        handle.seek(size - span)
        tail = handle.read(span)
    at = tail.rfind(EOCD_SIGNATURE)
    if at < 0 or len(tail) - at < EOCD_BYTES:
        raise UserError(f"{shown} is not a zip archive. An SSP is a zip file; export it again.")
    entries = int.from_bytes(tail[at + 10 : at + 12], "little")
    directory = int.from_bytes(tail[at + 12 : at + 16], "little")
    locator = tail[at - ZIP64_LOCATOR_BYTES : at] if at >= ZIP64_LOCATOR_BYTES else b""
    start = at - ZIP64_LOCATOR_BYTES - ZIP64_EOCD_BYTES
    if locator[:4] == ZIP64_LOCATOR_SIGNATURE and start >= 0:
        record = tail[start : start + ZIP64_EOCD_BYTES]
        if record[:4] == ZIP64_EOCD_SIGNATURE:
            entries = int.from_bytes(record[32:40], "little")
            directory = int.from_bytes(record[40:48], "little")
    _check_entry_count(entries, limits, shown)
    if directory > limits.ssp_max_members * CENTRAL_DIRECTORY_ENTRY_MAX_BYTES:
        raise UserError(
            f"{shown} has a {directory}-byte zip directory, more than "
            f"{limits.ssp_max_members} entries need. It is refused unread; export the SSP again."
        )


def _check_entry_count(entries: int, limits: Limits, shown: str) -> None:
    if entries > limits.ssp_max_members:
        raise UserError(
            f"{shown} has {entries} zip entries; the limit is {limits.ssp_max_members}. An SSP "
            "needs one SSD, its FMUs and a few parameter files; remove what isn't needed."
        )


def _checked_directory(
    archive: zipfile.ZipFile, limits: Limits, shown: str
) -> dict[str, zipfile.ZipInfo]:
    """The archive's entries by name, after the directory checks."""
    infos = archive.infolist()
    _check_entry_count(len(infos), limits, shown)
    duplicates = sorted(name for name, n in Counter(i.filename for i in infos).items() if n > 1)
    if duplicates:
        raise UserError(
            f"{shown} holds several entries named {', '.join(map(repr, duplicates[:MAX_NAMED]))}, "
            "so which one a tool reads is ambiguous. Export the SSP again."
        )
    for info in infos:
        if info.flag_bits & ENCRYPTED_FLAG:
            raise UserError(
                f"{shown}: entry {info.filename!r} is encrypted. Export the SSP without encryption."
            )
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise UserError(
                f"{shown}: entry {info.filename!r} uses compression method "
                f"{info.compress_type}; only stored and deflated entries are read. Export the "
                "SSP again with standard zip compression."
            )
    return {info.filename: info for info in infos}


@dataclass
class _Archive:
    """An opened SSP: its checked directory, and the decompressed bytes read so far."""

    zip: zipfile.ZipFile
    members: dict[str, zipfile.ZipInfo]
    limits: Limits
    shown: str
    read_total: int = 0

    def xml(self, name: str) -> bytes:
        """A referenced SSD or SSV member, at most ``Limits.ssp_xml_max_bytes``."""
        return b"".join(self._chunks(name, self.limits.ssp_xml_max_bytes))

    def copy(self, name: str, target: Path) -> str:
        """Stream a referenced FMU member into the new file ``target``; return its sha256."""
        digest = hashlib.sha256()
        with target.open("xb") as out:
            for chunk in self._chunks(name, self.limits.ssp_member_max_bytes):
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest()

    def _chunks(self, name: str, cap: int) -> Iterator[bytes]:
        info = self.members[name]
        where = f"{self.shown}: entry {name!r}"
        if info.file_size > cap:
            raise UserError(
                f"{where} declares {info.file_size} bytes, above the {cap}-byte cap for this "
                "kind of entry. It was not read."
            )
        total = self.limits.ssp_total_max_bytes
        if self.read_total + info.file_size > total:
            raise UserError(
                f"{where} would take the bytes read from the SSP above the {total}-byte total "
                "cap. It was not read."
            )
        size = 0
        try:
            with self.zip.open(info) as stream:
                while chunk := stream.read(CHUNK_BYTES):
                    size += len(chunk)
                    self.read_total += len(chunk)
                    if size > info.file_size or size > cap or self.read_total > total:
                        raise UserError(
                            f"{where} decompresses to more than its declared {info.file_size} "
                            "bytes (or a cap). Reading stopped; the SSP is refused."
                        )
                    yield chunk
            if size != info.file_size:
                raise zipfile.BadZipFile(f"{size} bytes instead of {info.file_size}")
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise UserError(
                f"{where} is corrupt or larger than its declared size ({truncate(str(exc), 120)})."
                " Export the SSP again."
            ) from exc


def _member(archive: _Archive, ref: str | None, where: str, source_base: str | None = None) -> str:
    """The member a ``source`` reference names, after the reference checks."""
    if source_base not in (None, "SSD"):
        raise UserError(
            f'{where}: sourceBase="{source_base}" (a file inside a component\'s FMU) is not '
            "supported. Put the file in the SSP and reference it from the SSD."
        )
    raw = ref or ""
    name = unquote(raw)
    problem = _reference_problem(raw) or _reference_problem(name)
    if problem is not None:
        raise UserError(
            f"{where}: the reference {raw[:REF_SHOWN_CHARS]!r} {problem}. Only plain relative "
            "paths of files inside the SSP are read; fix the SSD."
        )
    if name not in archive.members:
        raise UserError(
            f"{where} references {name[:REF_SHOWN_CHARS]!r}, which the SSP doesn't hold. "
            "Export the SSP again with all its resources."
        )
    return name


def _reference_problem(ref: str) -> str | None:
    """Why ``ref`` is not a plain relative path inside the archive, or ``None``."""
    if not ref:
        return "is empty"
    if "\\" in ref:
        return "contains a backslash"
    if "\x00" in ref:
        return "contains a NUL character"
    if URI_SCHEME.match(ref):
        return "has a URI scheme"
    if ref.startswith("/"):
        return "is absolute"
    if any(part in ("", ".", "..") for part in ref.split("/")):
        return "has a '..', '.' or empty path segment"
    return None


# --- reading the SSD ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Binding:
    """One parameter binding: an SSV member or inline values, with its prefix."""

    where: str
    member: str | None
    inline: Any
    prefix: str


@dataclass(frozen=True)
class _Component:
    name: str
    member: str
    bindings: tuple[_Binding, ...]


@dataclass(frozen=True)
class _Ssd:
    """What the import takes from a flat SSD."""

    components: tuple[_Component, ...]
    connections: tuple[tuple[str, str], ...]
    bindings: tuple[_Binding, ...]
    start: float
    stop: float | None
    step: float | None


@dataclass(frozen=True)
class _Value:
    value: StartValue
    unit: str | None
    where: str


def _read_ssd(archive: _Archive) -> _Ssd:
    """The SSD of a flat system, with every unsupported feature refused by name."""
    where = f"{archive.shown}: {SSD_MEMBER}"
    if SSD_MEMBER not in archive.members:
        raise UserError(
            f"{archive.shown} has no {SSD_MEMBER} at its root, so it is not an SSP. Export it "
            "again as an SSP."
        )
    root = _xml_root(archive.xml(SSD_MEMBER), where)
    if root.tag != _tag("ssd", "SystemStructureDescription"):
        raise UserError(f"{where}: the root element is <{_local(root)}>, not an SSD.")
    if root.get("version") not in XML_VERSIONS:
        raise UserError(
            f"{where}: SSD version {root.get('version')!r} is not supported (1.0 or 2.0)."
        )
    system = root.find(_tag("ssd", "System"))
    if system is None:
        raise UserError(f"{where} has no ssd:System. Export the system again.")
    _refuse_structure(system, archive.limits)
    components = _components(archive, system, archive.limits)
    start, stop = _default_experiment(root)
    return _Ssd(
        components=components,
        connections=_connections(system, {c.name for c in components}, archive.limits),
        bindings=_bindings(archive, system, "the system"),
        start=start,
        stop=stop,
        step=_fixed_step(system),
    )


def _refuse_structure(system: Any, limits: Limits) -> None:
    """Refuse system-level connectors, signal dictionaries and nested systems."""
    connectors = system.find(_tag("ssd", "Connectors"))
    if connectors is not None and connectors.find(_tag("ssd", "Connector")) is not None:
        names = [c.get("name") for c in connectors.iterfind(_tag("ssd", "Connector"))]
        raise UserError(
            f"The system declares system-level connectors ({', '.join(map(str, names[:3]))}), "
            "which system files don't have. Connect the components directly and export a flat "
            "system."
        )
    if system.find(_tag("ssd", "SignalDictionaries")) is not None:
        raise UserError(_dictionary_message())
    elements = system.find(_tag("ssd", "Elements"))
    for element in [] if elements is None else _children(elements):
        kind = _local(element)
        if kind == "System":
            raise UserError(_nested_message(element.get("name")))
        if kind == "SignalDictionaryReference":
            raise UserError(_dictionary_message())


def _nested_message(name: str | None) -> str:
    return (
        f"The SSP has a nested system {name!r}; system_import takes flat systems only (FMU "
        "components connected directly). Flatten it in its tool and export again."
    )


def _dictionary_message() -> str:
    return (
        "The SSP uses signal dictionaries, which system files don't have. Connect the "
        "components directly in its tool and export again."
    )


def _components(archive: _Archive, system: Any, limits: Limits) -> tuple[_Component, ...]:
    elements = system.find(_tag("ssd", "Elements"))
    found = [] if elements is None else list(elements.iterfind(_tag("ssd", "Component")))
    if not found:
        raise UserError("The SSP's system has no components. Export a system with FMUs.")
    if len(found) > limits.system_max_components:
        raise UserError(
            f"The SSP has {len(found)} components; the limit is {limits.system_max_components}."
        )
    taken: set[str] = set()
    return tuple(_component(archive, element, taken) for element in found)


def _component(archive: _Archive, element: Any, taken: set[str]) -> _Component:
    name = element.get("name") or ""
    _check_component_name(name, taken)
    kind = element.get("type", FMU_TYPE)
    if kind in NESTED_TYPES:
        raise UserError(_nested_message(name))
    if kind != FMU_TYPE:
        raise UserError(
            f"Component {name} has type {kind!r}; system_import takes FMU components only "
            f"({FMU_TYPE}). Replace it with an FMU in its tool."
        )
    implementation = element.get("implementation", "any")
    if implementation == "ModelExchange":
        raise UserError(_model_exchange_message(name))
    if implementation not in ("any", "CoSimulation"):
        raise UserError(
            f"Component {name} uses implementation {implementation!r}; systems couple "
            "Co-Simulation FMUs. Use the FMU's Co-Simulation interface."
        )
    where = f"component {name}"
    return _Component(
        name=name,
        member=_member(archive, element.get("source"), where),
        bindings=_bindings(archive, element, where),
    )


def _model_exchange_message(name: str) -> str:
    return (
        f"Component {name} is used as ModelExchange; systems couple Co-Simulation FMUs only "
        "(OMSimulator's oms_ma master). Export the FMU with a Co-Simulation interface."
    )


def _check_component_name(name: str, taken: set[str]) -> None:
    if not COMPONENT_NAME.fullmatch(name) or systems.is_reserved_name(name):
        raise UserError(
            f"Component name {name[:80]!r} can't be a system component: names are letters, "
            "digits and underscores, not starting with a digit, at most 64 characters, and not "
            "a Windows reserved name. Rename the component in its tool and export again."
        )
    if name.lower() in taken:
        raise UserError(
            f"Two components are named {name!r} (ignoring case); each becomes <name>.fmu. "
            "Rename one in its tool and export again."
        )
    taken.add(name.lower())


def _bindings(archive: _Archive, element: Any, level: str) -> tuple[_Binding, ...]:
    """The parameter bindings of a system or component, in document order."""
    container = element.find(_tag("ssd", "ParameterBindings"))
    found = [] if container is None else list(container.iterfind(_tag("ssd", "ParameterBinding")))
    bindings: list[_Binding] = []
    for i, binding in enumerate(found, start=1):
        where = f"{level}, parameter binding {i}"
        if binding.find(_tag("ssd", "ParameterMapping")) is not None:
            raise UserError(
                f"{where} uses a parameter mapping (SSM), which system_import doesn't apply. "
                "Apply the mapping in its tool (or remove it) and export again."
            )
        kind = binding.get("type", PARAMETER_SET_TYPE)
        if kind != PARAMETER_SET_TYPE:
            raise UserError(f"{where} has type {kind!r}; only SSV parameter sets are read.")
        prefix = binding.get("prefix", "")
        source = binding.get("source")
        if source is not None:
            member = _member(archive, source, where, binding.get("sourceBase"))
            bindings.append(_Binding(f"{member} (bound to {level})", member, None, prefix))
            continue
        values = binding.find(_tag("ssd", "ParameterValues"))
        inline = None if values is None else values.find(_tag("ssv", "ParameterSet"))
        if inline is not None:
            bindings.append(_Binding(f"the inline values of {level}", None, inline, prefix))
    return tuple(bindings)


def _connections(system: Any, components: set[str], limits: Limits) -> tuple[tuple[str, str], ...]:
    container = system.find(_tag("ssd", "Connections"))
    found = [] if container is None else list(container.iterfind(_tag("ssd", "Connection")))
    if len(found) > limits.system_max_connections:
        raise UserError(
            f"The SSP has {len(found)} connections; the limit is {limits.system_max_connections}."
        )
    return tuple(_connection(i, c, components) for i, c in enumerate(found, start=1))


def _connection(i: int, connection: Any, components: set[str]) -> tuple[str, str]:
    start, end = connection.get("startElement"), connection.get("endElement")
    first, second = connection.get("startConnector") or "", connection.get("endConnector") or ""
    where = f"Connection {i} ({start or ''}.{first} -> {end or ''}.{second})"
    if not start or not end:
        raise UserError(
            f"{where} ends at a system-level connector, which system files don't have. Connect "
            "the components directly in its tool and export again."
        )
    unknown = [name for name in (start, end) if name not in components]
    if unknown:
        raise UserError(f"{where} names {unknown[0]!r}, which is not a component of the system.")
    if connection.get("startIndices") or connection.get("endIndices"):
        raise UserError(f"{where} connects array elements; systems connect scalar variables.")
    transformed = [_local(c) for c in _children(connection) if _local(c) in TRANSFORMATIONS]
    if transformed:
        raise UserError(
            f"{where} has a {transformed[0]} (a connection transformation), which system "
            "files don't support. Remove it, or put a gain component in the connection."
        )
    if not first or not second:
        raise UserError(f"{where} lacks a connector name. Fix the SSD.")
    return f"{start}.{first}", f"{end}.{second}"


def _default_experiment(root: Any) -> tuple[float, float | None]:
    experiment = root.find(_tag("ssd", "DefaultExperiment"))
    if experiment is None:
        return 0.0, None
    start, stop = experiment.get("startTime"), experiment.get("stopTime")
    return (
        0.0 if start is None else _double(start, "DefaultExperiment startTime"),
        None if stop is None else _double(stop, "DefaultExperiment stopTime"),
    )


def _fixed_step(system: Any) -> float | None:
    """The step of OMSimulator's annotation on the system: ``oms:SimulationInformation
    fixedStepSize`` (OMSimulator 3) or ``oms:FixedStepMaster stepSize`` (older exports)."""
    annotations = system.find(_tag("ssd", "Annotations"))
    for annotation in (
        [] if annotations is None else annotations.iterfind(_tag("ssc", "Annotation"))
    ):
        if annotation.get("type") != OMS_ANNOTATION_TYPE:
            continue
        for element in annotation.iter():
            if not isinstance(element.tag, str):
                continue
            kind = _local(element)
            text = element.get("fixedStepSize") if kind == "SimulationInformation" else None
            text = element.get("stepSize") if kind == "FixedStepMaster" else text
            if text is not None:
                return _double(text, "the OMSimulator step annotation")
    return None


# --- parameter values ---------------------------------------------------------------------------


@dataclass
class _Values:
    """Values by (component, variable) in binding order, with the import's warnings."""

    assigned: dict[tuple[str, str], _Value] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def assign(self, component: str, variable: str, new: _Value) -> None:
        """Set a value; a value set before is overridden (SSP 1.0 section 5.2.3)."""
        old = self.assigned.get((component, variable))
        if old is not None:
            self.warnings.append(
                f"{component}.{variable}: {new.value!r} from {new.where} overrides {old.value!r} "
                f"from {old.where} (SSP 1.0 section 5.2.3: a later binding and a higher level "
                "win)."
            )
        self.assigned[(component, variable)] = new


def _bound_values(archive: _Archive, ssd: _Ssd) -> _Values:
    """Every binding's values: components first, then the system, each in document order."""
    cache: dict[str, tuple[dict[str, StartValue], dict[str, str]]] = {}

    def load(binding: _Binding) -> tuple[dict[str, StartValue], dict[str, str]]:
        if binding.member is None:
            return _parameter_values(binding.inline, binding.where)
        if binding.member not in cache:
            cache[binding.member] = parse_ssv(archive.xml(binding.member), binding.member)
        return cache[binding.member]

    found = _Values()
    for component in ssd.components:
        for binding in component.bindings:
            values, units = load(binding)
            for name, value in values.items():
                found.assign(
                    component.name,
                    binding.prefix + name,
                    _Value(value, units.get(name), binding.where),
                )
    names = {component.name for component in ssd.components}
    for binding in ssd.bindings:
        values, units = load(binding)
        for name, value in values.items():
            owner, dot, variable = (binding.prefix + name).partition(".")
            if not dot or owner not in names or not variable:
                found.ignored.append(binding.prefix + name)
                continue
            found.assign(owner, variable, _Value(value, units.get(name), binding.where))
    if found.ignored:
        listed = ", ".join(repr(name) for name in found.ignored[:MAX_NAMED])
        found.warnings.append(
            f"{len(found.ignored)} system-level value(s) name no component and were ignored: "
            f"{listed}. System-level names are '<component>.<variable>'."
        )
    return found


def _component_parameters(
    found: _Values, descriptions: Mapping[str, ModelDescription]
) -> tuple[dict[str, dict[str, StartValue]], list[str]]:
    """Each component's parameters: enumeration item names as their values; differing units
    reported."""
    parameters: dict[str, dict[str, StartValue]] = {}
    warnings: list[str] = []
    variables = {
        name: {variable.name: variable for variable in md.modelVariables}
        for name, md in descriptions.items()
    }
    for (component, name), bound in found.assigned.items():
        variable = variables[component].get(name)
        value = bound.value if variable is None else _enumeration_value(variable, bound.value)
        declared = None if variable is None else _declared_unit(variable)
        if bound.unit and declared and bound.unit != declared:
            warnings.append(
                f"{component}.{name}: {bound.where} gives the value in '{bound.unit}' but the "
                f"FMU declares '{declared}'; it was imported unchanged (no unit conversion). "
                "Convert it in the system file."
            )
        parameters.setdefault(component, {})[name] = value
    return parameters, warnings


def _enumeration_value(variable: Any, value: StartValue) -> StartValue:
    """An enumeration item name as the item's integer value, if the FMU declares it."""
    if variable.type != "Enumeration" or not isinstance(value, str):
        return value
    for item in getattr(variable.declaredType, "items", None) or []:
        if item.name == value and item.value is not None and INTEGER.fullmatch(item.value):
            return int(item.value)
    return value


def _declared_unit(variable: Any) -> str | None:
    """The variable's declared unit: its own, else its declared type's."""
    own = getattr(variable, "unit", None)
    inherited = getattr(getattr(variable, "declaredType", None), "unit", None)
    unit = own or inherited
    return str(unit) if unit else None


# --- import -------------------------------------------------------------------------------------


def import_ssp(config: Config, source: str, name: str | None = None) -> SystemImport:
    """Import a flat ``.ssp`` as ``imported/<name>/`` (see the module docstring), then run
    ``system.problems`` on the result and pass its errors and warnings on as warnings.

    ``name`` (default: the SSP's stem) matches ``protocol.BUILD_NAME_PATTERN``, isn't a
    Windows reserved name and doesn't exist under ``Config.imported_dir`` (case-insensitively).

    Raises:
        UserError: any refusal of the archive or its content, naming the member or element and
            the reason; nothing is left behind (the staging directory is removed).
    """
    path = _resolve_input(config, source, ".ssp", "SSP", "source")
    shown = relative_path(config, path)
    size = path.stat().st_size
    if size > config.limits.ssp_max_bytes:
        raise UserError(
            f"{shown} is {size} bytes, above the {config.limits.ssp_max_bytes}-byte limit for an "
            "SSP. It was not opened."
        )
    chosen = _import_name(config, path, name)
    _check_central_directory(path, config.limits, shown)
    try:
        with zipfile.ZipFile(path) as opened:
            members = _checked_directory(opened, config.limits, shown)
            return _import(config, _Archive(opened, members, config.limits, shown), chosen)
    except zipfile.BadZipFile as exc:
        raise UserError(
            f"{shown} is not a valid zip archive ({truncate(str(exc), 120)}). Export the SSP again."
        ) from exc


def _import_name(config: Config, source: Path, name: str | None) -> str:
    """The output name: valid, not reserved, and free under ``imported/`` (any case)."""
    chosen = source.stem if name is None else name
    if not IMPORT_NAME.fullmatch(chosen):
        origin = "The SSP's file name" if name is None else "The name"
        raise UserError(
            f"{origin} {chosen[:80]!r} can't name imported/<name>/: use 1 to 64 letters, digits "
            "and underscores, starting with a letter. Pass name='...'."
        )
    if systems.is_reserved_name(chosen):
        raise UserError(f"{chosen!r} is a reserved file name on Windows. Pass another name='...'.")
    _check_free(config, chosen)
    return chosen


def _check_free(config: Config, name: str) -> None:
    """``imported/<name>`` (any case) must not exist; ``imported/`` must stay in the workspace."""
    imported = config.imported_dir
    if not (imported.exists() or imported.is_symlink()):
        return
    resolved = imported.resolve()
    if not any(resolved.is_relative_to(root) for root in config.allowed_roots):
        raise UserError("imported/ points outside the workspace. Remove that symlink first.")
    if not resolved.is_dir():
        raise UserError("imported/ in the workspace is not a directory. Move it away first.")
    clash = next((e for e in os.listdir(resolved) if e.lower() == name.lower()), None)
    if clash is not None:
        raise UserError(
            f"imported/{clash} already exists, and system_import never replaces anything. Pass "
            "another name='...', or delete that directory first."
        )


def _import(config: Config, archive: _Archive, name: str) -> SystemImport:
    """Read the SSD and its values, then build ``imported/<name>`` through a staging directory."""
    ssd = _read_ssd(archive)
    found = _bound_values(archive, ssd)
    warnings = _variant_warnings(archive) + found.warnings
    staging = _staging(config, name)
    try:
        digests, descriptions = _copy_fmus(archive, ssd, staging)
        parameters, unit_warnings = _component_parameters(found, descriptions)
        experiment, experiment_warnings = _experiment(ssd)
        document = _system_document(ssd, parameters, experiment)
        system_path = staging / f"{name}.json"
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        system_path.write_text(text, encoding="utf-8", newline="\n")
        loaded = systems.load_system(config, str(system_path))
        errors, system_warnings = systems.problems(loaded, config.limits)
        final = _publish_directory(config, staging, name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    result = SystemImport(
        system=relative_path(config, final / f"{name}.json"),
        components=digests,
        connections=len(ssd.connections),
        parameters=len(found.assigned),
        warnings=_capped(
            [f"error: {e}" for e in errors]
            + experiment_warnings
            + warnings
            + unit_warnings
            + system_warnings,
            config.limits,
        ),
        hint=_import_hint(relative_path(config, final / f"{name}.json"), len(errors)),
    )
    return budget.fit_to_budget(result, config.limits.json_budget_bytes, _fewer_warnings)


def _variant_warnings(archive: _Archive) -> list[str]:
    others = sorted(n for n in archive.members if n.endswith(".ssd") and n != SSD_MEMBER)
    if not others:
        return []
    return [
        f"Only {SSD_MEMBER} was imported; the other variants were ignored: "
        f"{', '.join(others[:MAX_NAMED])}."
    ]


def _staging(config: Config, name: str) -> Path:
    """A fresh, private staging directory under ``imported/``."""
    config.imported_dir.mkdir(parents=True, exist_ok=True)
    staging = config.imported_dir / f".{name}.tmp-{os.getpid()}-{threading.get_ident()}"
    if staging.exists():  # left by a dead process with the same pid
        shutil.rmtree(staging)
    staging.mkdir()
    return staging


def _copy_fmus(
    archive: _Archive, ssd: _Ssd, staging: Path
) -> tuple[dict[str, str], dict[str, ModelDescription]]:
    """Stream each component's FMU to ``<component>.fmu`` (a member used twice is read once),
    hash it and parse its ``modelDescription.xml``; refuse FMUs without Co-Simulation."""
    copied: dict[str, tuple[Path, str]] = {}
    digests: dict[str, str] = {}
    descriptions: dict[str, ModelDescription] = {}
    for component in ssd.components:
        target = staging / f"{component.name}.fmu"
        if component.member in copied:
            first, digest = copied[component.member]
            shutil.copyfile(first, target)
        else:
            digest = archive.copy(component.member, target)
            copied[component.member] = (target, digest)
        digests[component.name] = digest
        descriptions[component.name] = _co_simulation(target, component)
    return digests, descriptions


def _co_simulation(path: Path, component: _Component) -> ModelDescription:
    try:
        md = read_description(path)
    except UserError as exc:
        raise UserError(f"Component {component.name} ({component.member}): {exc}") from exc
    if md.coSimulation is None:
        if md.modelExchange is not None:
            raise UserError(_model_exchange_message(component.name))
        raise UserError(
            f"Component {component.name} ({component.member}) has no Co-Simulation interface "
            "(ScheduledExecution only). Systems couple Co-Simulation FMUs."
        )
    return md


def _experiment(ssd: _Ssd) -> tuple[dict[str, float], list[str]]:
    """The experiment of the system file, with defaults (and warnings) for what is missing."""
    warnings: list[str] = []
    stop = ssd.stop
    if stop is None:
        stop = ssd.start + SYSTEM_DEFAULT_STOP_S
        warnings.append(
            f"The SSP states no stop time; stop_time is {stop:g}. Set experiment.stop_time in "
            "the system file."
        )
    step = ssd.step
    if step is None:
        step = SYSTEM_DEFAULT_STEP_S
        warnings.append(
            f"The SSP states no communication step; step_size is {step:g} s (OMSimulator's "
            "default). The step changes a closed loop's dynamics: choose it in the system file."
        )
    if stop <= ssd.start:
        raise UserError(
            f"The SSP's DefaultExperiment stops at {stop:g}, not after its start {ssd.start:g}. "
            "Fix the experiment in its tool."
        )
    if step <= 0:
        raise UserError(f"The SSP's step size {step:g} is not positive. Fix it in its tool.")
    return {"start_time": ssd.start, "stop_time": stop, "step_size": step}, warnings


def _system_document(
    ssd: _Ssd, parameters: Mapping[str, Mapping[str, StartValue]], experiment: dict[str, float]
) -> dict[str, Any]:
    """The version 1 system file, validated."""
    components: dict[str, Any] = {}
    for component in ssd.components:
        entry: dict[str, Any] = {"fmu": f"{component.name}.fmu"}
        if parameters.get(component.name):
            entry["parameters"] = dict(parameters[component.name])
        components[component.name] = entry
    document = {
        "version": 1,
        "components": components,
        "connections": [{"from": a, "to": b} for a, b in ssd.connections],
        "experiment": experiment,
    }
    try:
        SystemFile.model_validate(document)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        raise UserError(
            f"The SSP doesn't fit a system file: {where}: {first['msg']}. Fix it in its tool "
            "and export again."
        ) from exc
    return document


def _publish_directory(config: Config, staging: Path, name: str) -> Path:
    """Rename the staging directory to ``imported/<name>``, never over an existing one."""
    _check_free(config, name)
    final = config.imported_dir / name
    try:
        os.rename(staging, final)
    except OSError as exc:
        raise UserError(
            f"imported/{name} appeared while the SSP was imported; nothing was replaced. Pass "
            "another name='...'."
        ) from exc
    return final


def _capped(messages: Sequence[str], limits: Limits) -> list[str]:
    """At most :data:`MAX_WARNINGS` messages, each cut to ``Limits.message_max_chars``."""
    shown = [truncate(m, limits.message_max_chars) for m in messages]
    if len(shown) <= MAX_WARNINGS:
        return shown
    keep = MAX_WARNINGS - 1
    return [*shown[:keep], f"... and {len(shown) - keep} more (system_check lists the problems)."]


def _fewer_warnings(result: SystemImport) -> SystemImport | None:
    if not result.warnings:
        return None
    return result.model_copy(update={"warnings": result.warnings[:-1]})


def _import_hint(system: str, errors: int) -> str:
    if errors:
        return (
            f"The imported system has {errors} error(s). Call system_check(system='{system}'), "
            "fix the system file, then run it with run_start(system=...)."
        )
    return f"Run it with run_start(system='{system}'), or review it with system_check."


# --- export -------------------------------------------------------------------------------------


def export_ssp(config: Config, system: str, dest: str, paramset: str | None = None) -> SystemExport:
    """Export a system file (plus an optional ``.ssv``) as a new, byte-reproducible ``.ssp``.

    ``system`` must pass ``system.problems`` without errors; ``dest`` is a workspace-relative
    ``.ssp`` path outside the run store, ``generated/`` and ``imported/`` that doesn't exist yet
    (its parent directories are created inside the workspace). The archive is written to a
    temporary file in the same directory and moved into place with ``os.link`` semantics (never
    replacing a file that appeared meanwhile).

    Raises:
        UserError: an invalid system or parameter set, a bad or existing ``dest``, an FMU that
            changed during the export.
    """
    loaded = systems.load_system(config, system)
    errors, _ = systems.problems(loaded, config.limits)
    if errors:
        raise UserError(_export_refusal(loaded.name, errors))
    values = systems.resolve_parameters(
        loaded, None if paramset is None else read_paramset(config, paramset), None
    )
    target = _new_ssp_path(config, dest)
    fmus = _distinct_fmus(loaded)
    members: dict[str, bytes | Path] = {SSD_MEMBER: _ssd_bytes(loaded, fmus, values)}
    if values:
        members[PARAMETERS_MEMBER] = _ssv_bytes(values)
    for component, (member, _) in fmus.items():
        members.setdefault(member, loaded.fmus[component])
    digest, size = _write_new(config, target, members, lambda: _verify_fmus(loaded, fmus))
    return SystemExport(
        path=relative_path(config, target),
        sha256=digest,
        size_bytes=size,
        fmus={component: sha for component, (_, sha) in fmus.items()},
        parameters=len(values),
    )


def _export_refusal(name: str, errors: Sequence[str]) -> str:
    quoted = " ".join(
        f"({i}) {truncate(error, systems.MAX_REFUSAL_CHARS)}"
        for i, error in enumerate(errors[: systems.MAX_REFUSAL_ERRORS], start=1)
    )
    return (
        f"Can't export {name}: it has {len(errors)} error(s). {quoted} Call system_check for "
        "all of them, fix the system file, then export again."
    )


def _new_ssp_path(config: Config, dest: str) -> Path:
    """The absolute path of a new ``.ssp`` inside the workspace; its directory is created."""
    if not dest or not dest.strip():
        raise UserError("The dest argument is empty. Pass a new workspace-relative .ssp path.")
    shown = repr(dest[:REF_SHOWN_CHARS])
    candidate = Path(dest)
    if not candidate.is_absolute():
        candidate = config.workspace / candidate
    if candidate.suffix.lower() != ".ssp":
        raise UserError(f"dest {shown} must end in .ssp, e.g. 'exports/loop.ssp'.")
    try:
        parent = candidate.parent.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise UserError(f"Can't resolve dest {shown}: {exc}.") from exc
    if not any(parent.is_relative_to(root) for root in config.allowed_roots):
        raise UserError(
            f"dest {shown} is outside the workspace ({config.workspace}), possibly through a "
            "symlink. Pass a workspace-relative path."
        )
    owned = (
        (config.store_dir, "the run store"),
        (config.generated_dir, "generated/"),
        (config.imported_dir, "imported/"),
    )
    for directory, label in owned:
        if parent.is_relative_to(directory.resolve()):
            raise UserError(
                f"dest {shown} is inside {label}, which Agentic FMI manages itself. Pass a path "
                "elsewhere, e.g. 'exports/<name>.ssp'."
            )
    target = parent / candidate.name
    if os.path.lexists(target):
        raise UserError(
            f"{relative_path(config, target)} already exists, and system_export never "
            "overwrites. Pass a new dest, or delete the old file first."
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise UserError(f"dest {shown}: a parent of it is a file, not a directory.") from exc
    return target


def _distinct_fmus(loaded: systems.LoadedSystem) -> dict[str, tuple[str, str]]:
    """Component -> (member, sha256): one member per distinct FMU content, named after the
    first component that uses it."""
    digests: dict[Path, str] = {}
    members: dict[str, str] = {}
    result: dict[str, tuple[str, str]] = {}
    for component, path in loaded.fmus.items():
        if path not in digests:
            digests[path] = _sha256(component, path)
        digest = digests[path]
        member = members.setdefault(digest, f"resources/{component}.fmu")
        result[component] = (member, digest)
    return result


def _sha256(component: str, path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise UserError(
            f"components.{component}: can't read {path.name}: {exc.strerror or exc}. Check that "
            "the file is still there."
        ) from exc


def _verify_fmus(loaded: systems.LoadedSystem, fmus: Mapping[str, tuple[str, str]]) -> None:
    """Refuse the export if an FMU changed while it was being written."""
    checked: dict[Path, str] = {}
    for component, (_, expected) in fmus.items():
        path = loaded.fmus[component]
        if path not in checked:
            checked[path] = _sha256(component, path)
        if checked[path] != expected:
            raise UserError(
                f"The FMU of component {component} ({path.name}) changed during the export. "
                "Export again once it is final."
            )


def _xml_name(stem: str) -> str:
    """``stem`` as an SSD element name: letters, digits and underscores (no dots, which
    OMSimulator reads as hierarchy)."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", stem) or "system"
    return name if re.match(r"[A-Za-z_]", name) else f"_{name}"


def _connectors(md: ModelDescription, valued: set[str]) -> list[tuple[str, str, str]]:
    """(name, kind, connector type) of the scalar inputs, outputs and parameters, and of every
    variable a value sets, in the FMU's order."""
    found: list[tuple[str, str, str]] = []
    for variable in md.modelVariables:
        type_name = CONNECTOR_TYPES.get(variable.type)
        if variable.dimensions or type_name is None:
            continue
        if variable.causality in CONNECTOR_CAUSALITIES or variable.name in valued:
            found.append((variable.name, variable.causality, type_name))
    return found


def _ssd_bytes(
    loaded: systems.LoadedSystem,
    fmus: Mapping[str, tuple[str, str]],
    values: Mapping[str, ResolvedValue],
) -> bytes:
    """The SSD of the system: components with their connectors and annotations, connections,
    the step and the experiment."""
    valued: dict[str, set[str]] = {}
    for ref in values:
        component, _, variable = ref.partition(".")
        valued.setdefault(component, set()).add(variable)
    connectors = {
        name: _connectors(loaded.descriptions[name], valued.get(name, set()))
        for name in loaded.file.components
    }
    kinds = {kind for found in connectors.values() for _, kind, _ in found}
    types = {type_name for found in connectors.values() for _, _, type_name in found}
    name = _xml_name(loaded.name)
    root = etree.Element(
        _tag("ssd", "SystemStructureDescription"),
        nsmap={prefix: NAMESPACES[prefix] for prefix in ("ssc", "ssd", "oms", "afmi")},
    )
    root.set("name", name)
    older = types <= SSP1_TYPES and kinds <= SSP1_KINDS
    root.set("version", "1.0" if older else "2.0")
    root.set("generationTool", f"Agentic FMI {__version__}")
    system = etree.SubElement(root, _tag("ssd", "System"))
    system.set("name", name)
    if values:
        bindings = etree.SubElement(system, _tag("ssd", "ParameterBindings"))
        etree.SubElement(bindings, _tag("ssd", "ParameterBinding")).set("source", PARAMETERS_MEMBER)
    elements = etree.SubElement(system, _tag("ssd", "Elements"))
    for component in loaded.file.components:
        member, digest = fmus[component]
        _ssd_component(elements, component, member, digest, connectors[component])
    _ssd_connections(system, loaded)
    experiment = loaded.file.experiment
    annotations = etree.SubElement(system, _tag("ssd", "Annotations"))
    _oms_annotation(
        annotations,
        {"name": SOLVER_NAME, "method": "oms_ma", "fixedStepSize": repr(experiment.step_size)},
    )
    default = etree.SubElement(root, _tag("ssd", "DefaultExperiment"))
    default.set("startTime", repr(float(experiment.start_time)))
    default.set("stopTime", repr(float(experiment.stop_time)))
    return bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True))


def _ssd_component(
    elements: Any, name: str, member: str, digest: str, connectors: Sequence[tuple[str, str, str]]
) -> None:
    element = etree.SubElement(elements, _tag("ssd", "Component"))
    for key, value in (
        ("name", name),
        ("type", FMU_TYPE),
        ("source", member),
        ("implementation", "CoSimulation"),
    ):
        element.set(key, value)
    if connectors:
        container = etree.SubElement(element, _tag("ssd", "Connectors"))
        for variable, kind, type_name in connectors:
            connector = etree.SubElement(container, _tag("ssd", "Connector"))
            connector.set("name", variable)
            connector.set("kind", kind)
            etree.SubElement(connector, _tag("ssc", type_name))
    annotations = etree.SubElement(element, _tag("ssd", "Annotations"))
    _oms_annotation(annotations, {"name": SOLVER_NAME})
    ours = etree.SubElement(annotations, _tag("ssc", "Annotation"))
    ours.set("type", ANNOTATION_TYPE)
    etree.SubElement(ours, _tag("afmi", "Fmu")).set("sha256", digest)


def _ssd_connections(system: Any, loaded: systems.LoadedSystem) -> None:
    if not loaded.file.connections:
        return
    container = etree.SubElement(system, _tag("ssd", "Connections"))
    for connection in loaded.file.connections:
        start, _, first = connection.from_.partition(".")
        end, _, second = connection.to.partition(".")
        element = etree.SubElement(container, _tag("ssd", "Connection"))
        for key, value in (
            ("startElement", start),
            ("startConnector", first),
            ("endElement", end),
            ("endConnector", second),
        ):
            element.set(key, value)


def _oms_annotation(annotations: Any, information: Mapping[str, str]) -> None:
    """OMSimulator's ``oms:SimulationInformation`` inside an ``org.openmodelica`` annotation."""
    annotation = etree.SubElement(annotations, _tag("ssc", "Annotation"))
    annotation.set("type", OMS_ANNOTATION_TYPE)
    container = etree.SubElement(annotation, _tag("oms", "Annotations"))
    element = etree.SubElement(container, _tag("oms", "SimulationInformation"))
    for key, value in information.items():
        element.set(key, value)


def _ssv_bytes(values: Mapping[str, ResolvedValue]) -> bytes:
    """The root-level SSV (version 1.0): one typed value per ``<component>.<variable>``."""
    root = etree.Element(
        _tag("ssv", "ParameterSet"),
        nsmap={prefix: NAMESPACES[prefix] for prefix in ("ssc", "ssv")},
    )
    root.set("version", "1.0")
    root.set("name", "parameters")
    parameters = etree.SubElement(root, _tag("ssv", "Parameters"))
    for ref, resolved in values.items():
        parameter = etree.SubElement(parameters, _tag("ssv", "Parameter"))
        parameter.set("name", ref)
        kind, text = _ssv_value(resolved.value)
        try:
            etree.SubElement(parameter, _tag("ssv", kind)).set("value", text)
        except ValueError as exc:
            raise UserError(
                f"The value of {ref} can't be written as XML ({exc}). Remove control characters "
                "from it."
            ) from exc
    return bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True))


def _ssv_value(value: StartValue) -> tuple[str, str]:
    """The SSV 1.0 element and text of a value already converted to its variable's type
    (enumerations travel as ``Integer``, as OMSimulator reads them)."""
    if isinstance(value, bool):
        return "Boolean", "true" if value else "false"
    if isinstance(value, int):
        return "Integer", str(value)
    if isinstance(value, float):
        return "Real", repr(value)
    return "String", value


def _write_new(
    config: Config,
    target: Path,
    members: Mapping[str, bytes | Path],
    verify: Callable[[], None],
) -> tuple[str, int]:
    """Write the archive next to ``target``, call ``verify``, then move it into place without
    replacing anything; return its sha256 and size."""
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        write_deterministic_zip(temporary, members)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        verify()
        digest, size = sha256_file(temporary), temporary.stat().st_size
        _publish_file(temporary, target, relative_path(config, target))
    finally:
        temporary.unlink(missing_ok=True)
    return digest, size


def _publish_file(temporary: Path, target: Path, shown: str) -> None:
    """Give ``temporary``'s content the name ``target``, failing if ``target`` exists."""
    try:
        os.link(temporary, target)
        return
    except FileExistsError as exc:
        raise UserError(_appeared_message(shown)) from exc
    except OSError:  # no hard links on this file system: claim the name, then replace it
        pass
    try:
        os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError as exc:
        raise UserError(_appeared_message(shown)) from exc
    os.replace(temporary, target)


def _appeared_message(shown: str) -> str:
    return f"{shown} appeared while the export was written; it was left alone. Pass another dest."


def write_deterministic_zip(path: Path, members: Mapping[str, bytes | Path]) -> None:
    """Write ``members`` (name -> bytes, or a file to stream) as a zip whose bytes depend on
    the content only: sorted names, :data:`FIXED_ZIP_TIME`, ``create_system`` 3, mode 0644,
    ``.fmu`` stored and XML deflated. Writes ``path`` directly; the caller owns atomicity."""
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = ZIP_MEMBER_MODE << 16
            stored = name.lower().endswith(".fmu")
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            content = members[name]
            if isinstance(content, bytes):
                archive.writestr(info, content)
                continue
            info.file_size = content.stat().st_size
            with content.open("rb") as source, archive.open(info, "w") as out:
                shutil.copyfileobj(source, out, CHUNK_BYTES)
