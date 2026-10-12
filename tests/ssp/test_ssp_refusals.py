"""``system_import`` refusals of untrusted SSPs: unsafe references, archive tricks, XML bombs,
size bombs and unsupported SSP features. Every refusal names the cause, leaves nothing under
``imported/`` (the staging directory is removed) and leaves the source SSP untouched.

Collaborators: ``agentic_fmi.ssp.import_ssp`` (under test), ``make_ssp`` and ``config``
(``tests/ssp/conftest.py``), ``ssp_support`` (SSD/SSV text and the byte patches that fake
encrypted entries, lying sizes and counts) and ``system_fmus`` (a ModelExchange-only FMU).
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from ssp_support import (
    BILLION_LAUGHS,
    DEFAULT_COMPONENTS,
    MakeSsp,
    binding_xml,
    component_xml,
    connection_xml,
    set_declared_entries,
    set_declared_size,
    set_flag_bits,
    ssd_text,
    ssv_text,
    write_zip,
)
from system_fmus import PI_VARIABLES, write_fmu

from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.ssp import import_ssp


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refused(config: Config, source: Path, match: str) -> str:
    """Import ``source`` expecting a refusal matching ``match``; check nothing was left."""
    before = sha256(source)
    with pytest.raises(UserError, match=match) as info:
        import_ssp(config, source.relative_to(config.workspace).as_posix())
    imported = config.workspace / "imported"
    assert not imported.exists() or list(imported.iterdir()) == []
    assert sha256(source) == before
    return str(info.value)


def with_pi_source(source: str) -> dict[str, Any]:
    return {"components": component_xml("PI", source) + component_xml("MSD", "resources/plant.fmu")}


@pytest.mark.parametrize(
    ("options", "match"),
    [
        (with_pi_source("../evil.fmu"), r"'\.\./evil\.fmu' has a '\.\.', '\.' or empty path"),
        (with_pi_source("resources/../../x.fmu"), r"has a '\.\.', '\.' or empty path segment"),
        (with_pi_source("%2e%2e/evil.fmu"), r"'%2e%2e/evil\.fmu' has a '\.\.'"),
        (with_pi_source("/etc/passwd"), r"'/etc/passwd' is absolute"),
        (with_pi_source("file:///etc/passwd"), r"has a URI scheme"),
        (with_pi_source("https://example.com/pi.fmu"), r"has a URI scheme"),
        (with_pi_source("C:/pi.fmu"), r"has a URI scheme"),
        (with_pi_source("resources\\pi.fmu"), r"contains a backslash"),
        (with_pi_source("resources%5Cpi.fmu"), r"contains a backslash"),
        (with_pi_source(""), r"component PI: the reference '' is empty"),
        (with_pi_source("resources/none.fmu"), r"references 'resources/none\.fmu', which the SSP"),
    ],
)
def test_unsafe_or_dangling_references_are_refused(
    config: Config, make_ssp: MakeSsp, options: dict[str, Any], match: str
) -> None:
    message = refused(config, make_ssp(members={"../evil.fmu": b"x"}, **options), match)
    assert "component PI" in message


def test_source_base_component_is_refused(config: Config, make_ssp: MakeSsp) -> None:
    binding = binding_xml("resources/p.ssv", attributes=' sourceBase="component"')
    source = make_ssp(bindings=binding, members={"resources/p.ssv": ssv_text({}).encode()})
    refused(config, source, r'the system, parameter binding 1: sourceBase="component"')


def test_duplicate_member_names_are_refused(
    config: Config, workspace: Path, fmus: dict[str, Path]
) -> None:
    members = [
        ("SystemStructure.ssd", ssd_text()),
        ("resources/pi.fmu", fmus["pi"].read_bytes()),
        ("resources/plant.fmu", fmus["plant"].read_bytes()),
        ("resources/pi.fmu", b"another"),
    ]
    source = write_zip(workspace / "loop.ssp", members)
    refused(config, source, r"several entries named 'resources/pi\.fmu'")


def test_too_many_entries_are_refused_before_reading(config: Config, make_ssp: MakeSsp) -> None:
    few = replace(config, limits=replace(config.limits, ssp_max_members=3))
    source = make_ssp(name="four.ssp", members={"extra.txt": b"x"})
    refused(few, source, r"four\.ssp has 4 zip entries; the limit is 3")


def test_a_lying_entry_count_is_caught_after_opening(config: Config, make_ssp: MakeSsp) -> None:
    few = replace(config, limits=replace(config.limits, ssp_max_members=3))
    source = make_ssp(members={"extra.txt": b"x"})
    set_declared_entries(source, 1)
    refused(few, source, r"has 4 zip entries; the limit is 3")


def test_an_oversized_zip_directory_is_refused_unread(config: Config, workspace: Path) -> None:
    few = replace(config, limits=replace(config.limits, ssp_max_members=2))
    source = write_zip(workspace / "long.ssp", {"a" * 1500: b"", "b" * 1500: b""})
    refused(few, source, r"long\.ssp has a \d+-byte zip directory, more than 2 entries need")


def test_encrypted_entries_are_refused(config: Config, make_ssp: MakeSsp) -> None:
    source = make_ssp()
    set_flag_bits(source, "resources/pi.fmu", 0x1)
    refused(config, source, r"entry 'resources/pi\.fmu' is encrypted")


def test_other_compression_methods_are_refused(
    config: Config, workspace: Path, fmus: dict[str, Path]
) -> None:
    members = {
        "SystemStructure.ssd": ssd_text(),
        "resources/pi.fmu": fmus["pi"].read_bytes(),
        "resources/plant.fmu": fmus["plant"].read_bytes(),
    }
    source = write_zip(
        workspace / "loop.ssp", members, compression={"resources/plant.fmu": zipfile.ZIP_BZIP2}
    )
    refused(config, source, r"'resources/plant\.fmu' uses compression method 12")


def test_a_doctype_in_the_ssd_is_refused(config: Config, make_ssp: MakeSsp) -> None:
    source = make_ssp(prolog=BILLION_LAUGHS)
    refused(config, source, r"SystemStructure\.ssd has a DOCTYPE declaration")


def test_a_doctype_in_an_ssv_is_refused(config: Config, make_ssp: MakeSsp) -> None:
    laughs = ssv_text({"PI.Kp": ("Real", "&lol9;")}, prolog=BILLION_LAUGHS)
    source = make_ssp(
        bindings=binding_xml("resources/p.ssv"), members={"resources/p.ssv": laughs.encode()}
    )
    refused(config, source, r"resources/p\.ssv has a DOCTYPE declaration")


def test_an_undeclared_entity_is_not_well_formed(config: Config, make_ssp: MakeSsp) -> None:
    ssd = ssd_text().replace(b'name="loop"', b'name="&xxe;"', 1)
    refused(config, make_ssp(ssd=ssd), r"is not well-formed XML")


def test_xml_that_is_not_utf8_is_refused(config: Config, make_ssp: MakeSsp) -> None:
    ssd = ssd_text().decode().replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    refused(config, make_ssp(ssd=ssd), r"is not UTF-8 XML")


def test_a_member_larger_than_its_declared_size_is_refused(
    config: Config, make_ssp: MakeSsp
) -> None:
    source = make_ssp(ssd=ssd_text() + b" " * 200_000)
    set_declared_size(source, "SystemStructure.ssd", 100)
    refused(config, source, r"'SystemStructure\.ssd' is corrupt or larger than its declared size")


def test_an_fmu_larger_than_its_declared_size_is_refused_and_staging_removed(
    config: Config, make_ssp: MakeSsp
) -> None:
    source = make_ssp()
    set_declared_size(source, "resources/plant.fmu", 10)
    refused(config, source, r"'resources/plant\.fmu' is corrupt or larger than its declared size")


@pytest.mark.parametrize(
    ("limit", "match"),
    [
        ({"ssp_xml_max_bytes": 200}, r"'SystemStructure\.ssd' declares \d+ bytes, above the 200"),
        ({"ssp_member_max_bytes": 100}, r"'resources/pi\.fmu' declares \d+ bytes, above the 100"),
        ({"ssp_total_max_bytes": 2_000}, r"above the 2000-byte total cap"),
        ({"ssp_max_bytes": 100}, r"above the 100-byte limit for an SSP\. It was not opened"),
    ],
)
def test_size_caps_refuse_before_reading(
    config: Config, make_ssp: MakeSsp, limit: dict[str, int], match: str
) -> None:
    capped = replace(config, limits=replace(config.limits, **limit))
    refused(capped, make_ssp(), match)


NESTED = '<ssd:System name="sub"><ssd:Elements/></ssd:System>'
SSM = '<ssd:ParameterMapping source="resources/m.ssm"/>'
DICTIONARY = '<ssd:SignalDictionaries><ssd:SignalDictionary name="d" source="resources/d.ssb"/></ssd:SignalDictionaries>'  # noqa: E501
SYSTEM_CONNECTOR = '<ssd:Connectors><ssd:Connector name="out" kind="output"><ssc:Real/></ssd:Connector></ssd:Connectors>'  # noqa: E501
EMPTY_SSV = ssv_text({}).encode()


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"components": DEFAULT_COMPONENTS + NESTED}, r"nested system 'sub'"),
        (
            with_pi_source("resources/pi.fmu")
            | {
                "components": component_xml(
                    "Sub", "resources/pi.fmu", attributes=' type="application/x-ssp-package"'
                )
            },
            r"nested system 'Sub'",
        ),
        (
            {"bindings": binding_xml("resources/p.ssv", children=SSM)},
            r"parameter binding 1 uses a parameter mapping \(SSM\)",
        ),
        (
            {
                "components": component_xml(
                    "PI", "resources/pi.fmu", bindings=binding_xml("resources/p.ssv", children=SSM)
                )
                + component_xml("MSD", "resources/plant.fmu")
            },
            r"component PI, parameter binding 1 uses a parameter mapping \(SSM\)",
        ),
        ({"system_tail": DICTIONARY}, r"uses signal dictionaries"),
        (
            {
                "components": DEFAULT_COMPONENTS
                + '<ssd:SignalDictionaryReference name="r" dictionary="d"/>'
            },
            r"uses signal dictionaries",
        ),
        ({"system_head": SYSTEM_CONNECTOR}, r"declares system-level connectors \(out\)"),
        (
            {
                "connections": '<ssd:Connection startConnector="u" endElement="MSD" '
                'endConnector="F"/>'
            },
            r"Connection 1 \(\.u -> MSD\.F\) ends at a system-level connector",
        ),
        (
            {
                "connections": connection_xml(
                    "PI", "u", "MSD", "F", '<ssc:LinearTransformation factor="2"/>'
                )
            },
            r"has a LinearTransformation \(a connection transformation\)",
        ),
        (
            {
                "connections": '<ssd:Connection startElement="PI" startConnector="u" '
                'endElement="MSD" endConnector="F" endIndices="1"/>'
            },
            r"connects array elements",
        ),
        (
            {
                "components": component_xml(
                    "PI", "resources/pi.fmu", attributes=' implementation="ModelExchange"'
                )
                + component_xml("MSD", "resources/plant.fmu")
            },
            r"Component PI is used as ModelExchange",
        ),
        (
            {
                "components": component_xml("1PI", "resources/pi.fmu")
                + component_xml("MSD", "resources/plant.fmu")
            },
            r"Component name '1PI' can't be a system component",
        ),
        (
            {
                "components": component_xml("PI", "resources/pi.fmu")
                + component_xml("pi", "resources/plant.fmu")
            },
            r"Two components are named 'pi' \(ignoring case\)",
        ),
        (
            {"components": component_xml("PI", "resources/pi.fmu", attributes=' type="text/csv"')},
            r"Component PI has type 'text/csv'; system_import takes FMU components only",
        ),
        ({"components": ""}, r"has no components"),
        ({"version": "3.0"}, r"SSD version '3\.0' is not supported"),
    ],
)
def test_unsupported_ssp_features_are_refused_by_name(
    config: Config, make_ssp: MakeSsp, options: dict[str, Any], match: str
) -> None:
    members: dict[str, bytes | None] = {"resources/p.ssv": EMPTY_SSV}
    refused(config, make_ssp(members=members, **options), match)


def test_a_model_exchange_only_fmu_is_refused_and_staging_removed(
    config: Config, make_ssp: MakeSsp, workspace: Path
) -> None:
    only_me = write_fmu(
        workspace / "src" / "me.fmu", PI_VARIABLES, model="PI", interfaces=("ModelExchange",)
    )
    source = make_ssp(members={"resources/pi.fmu": only_me.read_bytes()})
    refused(config, source, r"Component PI is used as ModelExchange; systems couple Co-Simulation")


def test_an_invalid_fmu_is_refused_with_its_component(config: Config, make_ssp: MakeSsp) -> None:
    source = make_ssp(members={"resources/pi.fmu": b"not a zip"})
    refused(config, source, r"Component PI \(resources/pi\.fmu\): pi\.fmu|Component PI \(resources")


def test_archives_that_are_not_ssps_are_refused(
    config: Config, workspace: Path, make_ssp: MakeSsp
) -> None:
    (workspace / "plain.ssp").write_bytes(b"just text")
    refused(config, workspace / "plain.ssp", r"plain\.ssp is not a zip archive")
    no_ssd = make_ssp(name="nossd.ssp", members={"SystemStructure.ssd": None})
    refused(config, no_ssd, r"nossd\.ssp has no SystemStructure\.ssd at its root")
    other_root = make_ssp(name="root.ssp", ssd=b'<?xml version="1.0"?><other/>')
    refused(config, other_root, r"the root element is <other>, not an SSD")


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ("", r"The source argument is empty"),
        ("../outside.ssp", r"is outside the workspace"),
        ("missing.ssp", r"'missing\.ssp' was not found in the workspace"),
        ("loop.json", r"'loop\.json' is not a \.ssp file"),
    ],
)
def test_bad_source_arguments_are_refused(
    config: Config, workspace: Path, source: str, match: str
) -> None:
    (workspace / "loop.json").write_text("{}", encoding="utf-8")
    (workspace.parent / "outside.ssp").write_bytes(b"PK")
    with pytest.raises(UserError, match=match):
        import_ssp(config, source)
    assert not (workspace / "imported").exists()


def test_a_symlink_out_of_the_workspace_is_refused(config: Config, workspace: Path) -> None:
    outside = workspace.parent / "real.ssp"
    outside.write_bytes(b"PK")
    (workspace / "link.ssp").symlink_to(outside)
    with pytest.raises(
        UserError, match=r"is outside the workspace \(.*\), possibly through a symlink"
    ):
        import_ssp(config, "link.ssp")
