"""The one SSV reader: ``parse_ssv`` (types, units, refusals) and ``read_paramset`` (the workspace
``.ssv`` that ``run_start(paramset=...)`` and ``system_export`` read).

Collaborators: ``agentic_fmi.ssp`` (under test), ``ssp_support`` (SSV text), the ``config`` and
``workspace`` fixtures. Pure parsing; nothing is run.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from ssp_support import BILLION_LAUGHS, ssv_text

from agentic_fmi.config import Config
from agentic_fmi.errors import UserError
from agentic_fmi.ssp import NAMESPACES, parse_ssv, read_paramset
from agentic_fmi.store.protocol import FileRef


def parameter(name: str, body: str) -> bytes:
    return (
        f'<ssv:ParameterSet xmlns:ssv="{NAMESPACES["ssv"]}" xmlns:ssc="{NAMESPACES["ssc"]}" '
        f'version="1.0" name="p"><ssv:Parameters><ssv:Parameter name="{name}">{body}'
        "</ssv:Parameter></ssv:Parameters></ssv:ParameterSet>"
    ).encode()


def test_every_supported_type_is_read_with_its_unit() -> None:
    text = ssv_text(
        {
            "PI.Kp": ("Real", "10"),
            "PI.Ki": ("Float64", "-1.5e-3"),
            "PI.f": ("Float32", ".5"),
            "PI.n": ("Integer", "+3"),
            "PI.m": ("Int32", "-7"),
            "PI.b": ("UInt8", "255"),
            "PI.on": ("Boolean", "true"),
            "PI.off": ("Boolean", "0"),
            "PI.label": ("String", "two words"),
            "PI.mode": ("Enumeration", "2"),
            "PI.kind": ("Enumeration", "Fast"),
        },
        version="2.0",
        units={"PI.Kp": "1", "PI.Ki": "s", "PI.n": "ignored"},
    )
    values, units = parse_ssv(text.encode(), "p.ssv")
    assert values == {
        "PI.Kp": 10.0,
        "PI.Ki": -1.5e-3,
        "PI.f": 0.5,
        "PI.n": 3,
        "PI.m": -7,
        "PI.b": 255,
        "PI.on": True,
        "PI.off": False,
        "PI.label": "two words",
        "PI.mode": 2,
        "PI.kind": "Fast",
    }
    assert [type(values[k]) for k in ("PI.Kp", "PI.n", "PI.on")] == [float, int, bool]
    assert units == {"PI.Kp": "1", "PI.Ki": "s"}  # units of numbers with a fraction only


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (parameter("a", '<ssv:Binary value="00"/>'), r"parameter 'a' is Binary"),
        (parameter("a", '<ssv:Real value="NaN"/>'), r"'NaN' is not a finite number"),
        (parameter("a", '<ssv:Real value="INF"/>'), r"'INF' is not a finite number"),
        (parameter("a", '<ssv:Real value="1e999"/>'), r"'1e999' is not a finite number"),
        (parameter("a", '<ssv:Real value="1_0"/>'), r"'1_0' is not a finite number"),
        (parameter("a", '<ssv:Real value="1 2"/>'), r"has several values \(an array\)"),
        (parameter("a", '<ssv:Integer value="1.5"/>'), r"'1\.5' is not a whole number"),
        (parameter("a", '<ssv:Boolean value="yes"/>'), r"'yes' is not a Boolean"),
        (parameter("a", "<ssv:Real/>"), r"<ssv:Real> has no value attribute"),
        (parameter("a", '<ssv:Complex value="1"/>'), r"unknown type <ssv:Complex>"),
        (parameter("a", ""), r"needs exactly one value element"),
        (
            parameter("a", '<ssv:Real value="1"/><ssv:Integer value="1"/>'),
            r"needs exactly one value element",
        ),
        (
            parameter(
                "a",
                '<ssv:Real value="1"/><ssc:Dimensions><ssc:Dimension size="2"/></ssc:Dimensions>',
            ),
            r"parameter 'a' is an array",
        ),
        (parameter("", '<ssv:Real value="1"/>'), r"an ssv:Parameter has no name"),
        (ssv_text({}, version="3.0").encode(), r"SSV version '3\.0' is not supported"),
        (ssv_text({}, prolog=BILLION_LAUGHS).encode(), r"has a DOCTYPE declaration"),
        (b'<?xml version="1.0"?><other/>', r"its root element is <other>, not ssv:ParameterSet"),
        (b"<unclosed>", r"is not well-formed XML"),
        ("<ssv:ParameterSet/>".encode("utf-16"), r"is not UTF-8 XML"),
        (b"\xff\xfe<x/>", r"is not UTF-8 text"),
    ],
)
def test_bad_parameter_sets_are_refused_with_the_place(data: bytes, match: str) -> None:
    with pytest.raises(UserError, match=match) as info:
        parse_ssv(data, "p.ssv")
    assert str(info.value).startswith("p.ssv")


def test_duplicate_names_are_refused() -> None:
    rows = '<ssv:Parameter name="a"><ssv:Real value="1"/></ssv:Parameter>' * 2
    data = (
        f'<ssv:ParameterSet xmlns:ssv="{NAMESPACES["ssv"]}" version="1.0" name="p">'
        f"<ssv:Parameters>{rows}</ssv:Parameters></ssv:ParameterSet>"
    ).encode()
    with pytest.raises(UserError, match=r"parameter 'a' appears twice"):
        parse_ssv(data, "p.ssv")


def test_read_paramset_pins_the_bytes_it_parsed(config: Config, workspace: Path) -> None:
    path = workspace / "sets" / "p.ssv"
    path.parent.mkdir()
    path.write_text(
        ssv_text({"PI.Kp": ("Real", "12"), "MSD.k": ("Integer", "4")}, units={"PI.Kp": "1"}),
        encoding="utf-8",
    )
    found = read_paramset(config, "sets/p.ssv")
    data = path.read_bytes()
    assert found.ref == FileRef(
        path=str(path), sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    )
    assert dict(found.values) == {"PI.Kp": 12.0, "MSD.k": 4}
    assert dict(found.units) == {"PI.Kp": "1"}


@pytest.mark.parametrize(
    ("argument", "match"),
    [
        ("", r"The paramset argument is empty"),
        ("../p.ssv", r"Parameter set '\.\./p\.ssv' is outside the workspace"),
        ("missing.ssv", r"Parameter set 'missing\.ssv' was not found"),
        ("p.json", r"'p\.json' is not a \.ssv file"),
        ("dir.ssv", r"'dir\.ssv' is a directory"),
        ("bare.ssv", r"bare\.ssv: 'Kp' is not '<component>\.<variable>'"),
        ("big.ssv", r"big\.ssv is larger than 400 bytes"),
    ],
)
def test_read_paramset_refusals(config: Config, workspace: Path, argument: str, match: str) -> None:
    (workspace.parent / "p.ssv").write_text(ssv_text({}), encoding="utf-8")
    (workspace / "p.json").write_text("{}", encoding="utf-8")
    (workspace / "dir.ssv").mkdir()
    (workspace / "bare.ssv").write_text(ssv_text({"Kp": ("Real", "1")}), encoding="utf-8")
    many = {f"PI.p{i}": ("Real", "1") for i in range(9)}
    (workspace / "big.ssv").write_text(ssv_text(many), encoding="utf-8")
    small = replace(config, limits=replace(config.limits, system_file_max_bytes=400))
    with pytest.raises(UserError, match=match):
        read_paramset(small, argument)
