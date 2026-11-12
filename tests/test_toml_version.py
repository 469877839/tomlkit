"""Capability matrix for the TOML 1.0 / TOML 1.1 version selection.

The expectations below are written directly from the published grammars,
not inferred from any particular parser's behaviour:

* TOML 1.0.0: https://toml.io/en/v1.0.0
* TOML 1.1.0: https://toml.io/en/v1.1.0

The TOML 1.1 extensions exercised here are, per the 1.1 grammar:

* basic strings gain the ``\\e`` (escape) and ``\\xHH`` (hex) escapes;
* the time grammar makes the seconds field optional and adds an offset to
  the date-less time (offset-time);
* inline tables may span several lines, contain comments and carry a
  trailing comma.

The rule under test is that an old parser rejecting a token is not evidence
that the token is malformed: the 1.1 column asserts acceptance from the
grammar, while the 1.0 column asserts the explicit, positioned diagnostic.
Every TOML 1.0 output produced by these tests is re-checked with the
standard-library ``tomllib`` parser.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from tomlkit import TOMLVersion
from tomlkit import document
from tomlkit import dumps
from tomlkit import loads
from tomlkit import parse
from tomlkit.exceptions import ParseError
from tomlkit.toml_version import InputVersionError
from tomlkit.toml_version import OutputVersionError
from tomlkit.toml_version import downgrade_v11_escapes
from tomlkit.toml_version import downgrade_v11_key_path
from tomlkit.toml_version import normalize_version

# tomllib only exists from Python 3.11; the whole module is a cross-check
# against it, so skip it entirely on older interpreters.
tomllib = pytest.importorskip("tomllib")


# Each case is (source, expected decoded value, fragment of the 1.0 diagnostic).
V11_ONLY_CASES = [
    pytest.param(
        's = "a\\eb"\n',
        {"s": "a\x1bb"},
        "\\e",
        id="escape-e",
    ),
    pytest.param(
        's = "\\x41\\x42"\n',
        {"s": "AB"},
        "\\xXX",
        id="escape-x",
    ),
    pytest.param(
        's = """line1\\eline2\\x09end"""\n',
        {"s": "line1\x1bline2\tend"},
        "\\e",
        id="escape-e-in-multiline",
    ),
    pytest.param(
        't = 07:32\n',
        {"t": time(7, 32)},
        "without seconds",
        id="local-time-without-seconds",
    ),
    pytest.param(
        't = 07:32Z\n',
        {"t": time(7, 32, tzinfo=timezone.utc)},
        "without seconds",
        id="offset-time-without-seconds",
    ),
    pytest.param(
        "t = 07:32:00-05:00\n",
        {"t": time(7, 32, tzinfo=timezone(timedelta(hours=-5)))},
        "offset on a local time",
        id="offset-time",
    ),
    pytest.param(
        'dt = 1979-05-27T07:32\n',
        {"dt": datetime(1979, 5, 27, 7, 32)},
        "without seconds",
        id="local-datetime-without-seconds",
    ),
    pytest.param(
        "dt = 1979-05-27T07:32Z\n",
        {"dt": datetime(1979, 5, 27, 7, 32, tzinfo=timezone.utc)},
        "without seconds",
        id="offset-datetime-without-seconds",
    ),
    pytest.param(
        "t = {\na = 1,\nb = 2,\n}\n",
        {"t": {"a": 1, "b": 2}},
        "line break inside an inline table",
        id="multiline-inline-table",
    ),
    pytest.param(
        "t = {a = 1,}\n",
        {"t": {"a": 1}},
        "trailing comma in an inline table",
        id="inline-table-trailing-comma",
    ),
    pytest.param(
        "t = {\n a = 1, # comment\n b = 2\n}\n",
        {"t": {"a": 1, "b": 2}},
        "line break inside an inline table",
        id="multiline-inline-table-with-comment",
    ),
    pytest.param(
        "t = {\r\n a = 1\r\n}\n",
        {"t": {"a": 1}},
        "line break inside an inline table",
        id="multiline-inline-table-crlf",
    ),
    pytest.param(
        "dt = 1979-05-27 07:32 # tail comment\n",
        {"dt": datetime(1979, 5, 27, 7, 32)},
        "without seconds",
        id="space-separated-datetime-without-seconds",
    ),
]


@pytest.mark.parametrize("source,expected,fragment", V11_ONLY_CASES)
def test_v11_only_syntax_accepted_in_1_1(
    source: str, expected: dict[str, Any], fragment: str
) -> None:
    doc = parse(source, version="1.1")
    assert doc.version is TOMLVersion.V1_1
    assert doc.unwrap() == expected
    # Round-tripping through 1.1 keeps the original text verbatim.
    assert dumps(doc, version="1.1") == source


@pytest.mark.parametrize("source,expected,fragment", V11_ONLY_CASES)
def test_v11_only_syntax_diagnosed_in_1_0(
    source: str, expected: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(InputVersionError) as exc_info:
        parse(source, version="1.0")
    err = exc_info.value
    # The diagnostic names the 1.1 feature and points at its position.
    assert fragment in str(err)
    assert err.line >= 1
    assert err.col >= 1
    assert isinstance(err, ParseError)


# Syntax legal in both versions; decoded values must agree with tomllib.
COMMON_CASES = [
    's = "plain\\nstring"\n',
    "s = 'literal\\e-untouched'\n",
    's = """multi\\u001b line"""\n',
    "t = 07:32:00\n",
    "t = 07:32:00.999999\n",
    "dt = 1979-05-27 07:32:00.5-05:00\n",
    "dt = 1979-05-27T07:32:00Z\n",
    "t = {a = 1, b = 2}\n",
    "t = { a = 1 }\n",
    "t = {}\n",
    "a = [1, 2, 3]\n",
    "a = [\n 1,\n 2,\n]\n",
]


@pytest.mark.parametrize("source", COMMON_CASES)
def test_common_syntax_accepted_in_both_and_matches_tomllib(source: str) -> None:
    expected = tomllib.loads(source)
    assert parse(source, version="1.0").unwrap() == expected
    assert parse(source, version="1.1").unwrap() == expected
    # A 1.0 document written without a target version stays byte-identical.
    assert dumps(parse(source, version="1.0")) == source


# Constructs that are not valid in either version. A 1.1 parser must reject
# these just like a 1.0 parser; relaxation is not the same as acceptance.
INVALID_IN_BOTH = [
    't = {,}\n',
    "t = {a = 1 b = 2}\n",
    "t = {a = 1,, b = 2}\n",
    's = "\\xG1"\n',
    's = "\\x4"\n',
    's = "\\E"\n',
    "t = 07:32.5\n",
    "t = 99:99\n",
    "dt = 1979-13-40T07:32\n",
]


@pytest.mark.parametrize("source", INVALID_IN_BOTH)
def test_invalid_syntax_rejected_in_both_versions(source: str) -> None:
    for version in ("1.0", "1.1"):
        with pytest.raises(ParseError):
            parse(source, version=version)


# Lossless 1.1 -> 1.0 rewrites: the exact 1.0 spelling and, independently,
# that tomllib reads the same scalar/structure as the 1.1 decode.
DOWNGRADE_CASES = [
    pytest.param(
        's = "a\\eb\\x41"\n',
        's = "a\\u001bb\\u0041"\n',
        id="escapes",
    ),
    pytest.param(
        's = """\\e\\x7f"""\n',
        's = """\\u001b\\u007f"""\n',
        id="escapes-multiline",
    ),
    pytest.param(
        "t = 07:32\n",
        "t = 07:32:00\n",
        id="time-pad-seconds",
    ),
    pytest.param(
        "dt = 1979-05-27 07:32\n",
        "dt = 1979-05-27T07:32:00\n",
        id="datetime-pad-and-separator",
    ),
    pytest.param(
        "dt = 1979-05-27T07:32Z\n",
        "dt = 1979-05-27T07:32:00Z\n",
        id="datetime-offset-pad-seconds",
    ),
    pytest.param(
        "t = {\n a = 1,\n b = 2,\n}\n",
        "t = {a = 1, b = 2}\n",
        id="inline-table-flatten",
    ),
    pytest.param(
        "t = {\n a = 1, # c\n b = 2,\n}\n",
        "t = {a = 1, b = 2}\n",
        id="inline-table-flatten-drops-comment",
    ),
    pytest.param(
        "t = { p = {\n k = 1\n} }\n",
        "t = { p = {k = 1} }\n",
        id="nested-inline-table-flatten",
    ),
    pytest.param(
        "t = { m = [\n 1,\n 2,\n] }\n",
        "t = { m = [1, 2] }\n",
        id="array-inside-inline-table-collapse",
    ),
    pytest.param(
        "t = {\n a = {b = 1,},\n}\n",
        "t = {a = {b = 1}}\n",
        id="nested-trailing-comma-flatten",
    ),
    pytest.param(
        'k."\\e" = 1\n',
        'k."\\u001b" = 1\n',
        id="quoted-dotted-key",
    ),
    pytest.param(
        '["\\x41"]\nv = 1\n',
        '["\\u0041"]\nv = 1\n',
        id="quoted-table-header",
    ),
]


@pytest.mark.parametrize("source,expected_1_0", DOWNGRADE_CASES)
def test_v11_document_downgraded_losslessly_to_1_0(
    source: str, expected_1_0: str
) -> None:
    doc = parse(source, version="1.1")
    out = dumps(doc, version="1.0")
    assert out == expected_1_0
    # Cross-check with the standard library and with the 1.1 decode.
    assert tomllib.loads(out) == doc.unwrap()


def test_offset_time_cannot_be_written_as_1_0_and_reports_location() -> None:
    source = "first = 1\not = 07:32:00-05:00\n"
    doc = parse(source, version="1.1")
    with pytest.raises(OutputVersionError) as exc_info:
        dumps(doc, version="1.0")
    err = exc_info.value
    assert "offset-time" in err.feature
    assert err.line == 2
    assert err.col == 1
    # The same document still serializes fine as 1.1.
    assert dumps(doc, version="1.1") == source


def test_default_output_follows_document_version() -> None:
    source_11 = 's = "a\\eb"\nt = {\n a = 1,\n}\n'
    doc_11 = parse(source_11, version="1.1")
    assert doc_11.version is TOMLVersion.V1_1
    assert dumps(doc_11) == source_11  # no target: 1.1 text preserved

    source_10 = "s = \"plain\"\nt = {a = 1}\n"
    doc_10 = parse(source_10)
    assert doc_10.version is TOMLVersion.V1_0
    assert dumps(doc_10) == source_10


def test_untouched_text_conflicts_resolve_in_favour_of_explicit_target() -> None:
    # Rule: preserving original text applies when the target version can hold
    # it. An explicit target that cannot hold the original spelling triggers
    # the documented rewrite instead, never a silent label change.
    source = 's = "a\\eb"\nt = 07:32\n'
    doc = parse(source, version="1.1")

    assert dumps(doc, version="1.1") == source
    rewritten = dumps(doc, version="1.0")
    assert rewritten == 's = "a\\u001bb"\nt = 07:32:00\n'
    assert tomllib.loads(rewritten) == doc.unwrap()

    # A 1.0 document explicitly written as 1.1 needs no changes at all, since
    # 1.1 is a strict superset; its text stays byte-identical.
    plain = "a = 1\nb = {c = 2}\n"
    assert dumps(parse(plain), version="1.1") == plain


def test_version_arguments_are_normalized() -> None:
    for spelling, expected in [
        ("1", TOMLVersion.V1_0),
        ("1.0", TOMLVersion.V1_0),
        ("1.0.0", TOMLVersion.V1_0),
        ("1.1", TOMLVersion.V1_1),
        ("1.1.0", TOMLVersion.V1_1),
        (TOMLVersion.V1_1, TOMLVersion.V1_1),
    ]:
        assert normalize_version(spelling) is expected

    bad_versions: list[Any] = ["2.0", "1.2", "0.9", 1.1]
    for bad in bad_versions:
        with pytest.raises((ValueError, TypeError)):
            parse("a = 1\n", version=bad)


def test_document_constructor_records_version() -> None:
    doc = document(version="1.1")
    assert doc.version is TOMLVersion.V1_1
    assert document().version is TOMLVersion.V1_0


def test_escape_downgrade_does_not_touch_escaped_backslash() -> None:
    # "\\" + "e" is a literal backslash followed by the letter e, not \e.
    assert downgrade_v11_escapes(r"\\e") == r"\\e"
    assert downgrade_v11_escapes(r"\e\\x41") == r"\u001b\\x41"
    assert downgrade_v11_escapes(r"\x4A") == r"\u004a"

    assert downgrade_v11_key_path('a."b\\e".\'c\'') == 'a."b\\u001b".\'c\''


def test_plain_mapping_dumps_defaults_to_1_0() -> None:
    out = dumps({"a": "\x1b"})
    assert out == 'a = "\\u001b"\n'
    assert tomllib.loads(out) == {"a": "\x1b"}


def test_offset_time_built_from_python_time_round_trips_in_1_1() -> None:
    from datetime import timedelta

    from tomlkit import item

    clock = time(7, 32, tzinfo=timezone(timedelta(hours=2)))
    offset_time = item(clock)
    assert offset_time.as_string(version="1.1") == "07:32:00+02:00"
    with pytest.raises(OutputVersionError):
        offset_time.as_string(version="1.0")


def test_nested_offset_time_is_rejected_with_a_precise_location() -> None:
    doc = parse("t = {\n ot = 07:32:00-05:00,\n a = 1\n}\n", version="1.1")
    with pytest.raises(OutputVersionError) as exc_info:
        dumps(doc, version="1.0")
    assert exc_info.value.line == 1
    assert exc_info.value.col == 2

    array_doc = parse("a = [1, 07:32:00Z]\n", version="1.1")
    with pytest.raises(OutputVersionError) as exc_info:
        dumps(array_doc, version="1.0")
    assert exc_info.value.line == 1
    assert exc_info.value.col == 5


def test_value_and_key_value_helpers_take_a_version() -> None:
    from tomlkit import key_value
    from tomlkit import value

    assert value('"a\\eb"', version="1.1") == "a\x1bb"
    with pytest.raises(InputVersionError):
        value('"a\\eb"', version="1.0")

    key, item = key_value('k = 07:32\n', version="1.1")
    assert key.key == "k"
    assert item == time(7, 32)
    with pytest.raises(InputVersionError):
        key_value("k = 07:32\n", version="1.0")


def test_toml_file_read_takes_a_version(tmp_path: Path) -> None:
    from tomlkit.toml_file import TOMLFile

    path = tmp_path / "doc.toml"
    path.write_text('s = "a\\eb"\n', encoding="utf-8")

    with pytest.raises(InputVersionError):
        TOMLFile(path).read()

    doc = TOMLFile(path).read(version="1.1")
    assert doc.version is TOMLVersion.V1_1
    assert doc.unwrap() == {"s": "a\x1bb"}


def test_time_constructor_is_strict_in_1_0_and_keeps_1_1_raw() -> None:
    from tomlkit import datetime as make_datetime
    from tomlkit import time as make_time

    assert make_time("07:32:00").as_string() == "07:32:00"
    for token in ("07:32", "07:32Z", "07:32:00Z"):
        with pytest.raises(ValueError):
            make_time(token)

    value = make_time("07:32", version="1.1")
    assert value.as_string(version="1.1") == "07:32"
    assert value.as_string(version="1.0") == "07:32:00"
    assert isinstance(value, time)

    instant = make_datetime("1979-05-27 07:32", version="1.1")
    assert instant.as_string(version="1.1") == "1979-05-27 07:32"
    assert instant.as_string(version="1.0") == "1979-05-27T07:32:00"
