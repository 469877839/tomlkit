import sys

import pytest

import tomlkit

from tomlkit import parse
from tomlkit.exceptions import TableMoveError

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="tomllib requires 3.11+"
)


def _std_loads(content: str) -> dict[str, object]:
    """Parse with the standard library (guarded by the module skip)."""
    import tomllib

    return tomllib.loads(content)


def _check(content: str) -> tomlkit.TOMLDocument:
    """Parse, render and cross-check with the standard library."""
    doc = parse(content)
    rendered = doc.as_string()
    assert _std_loads(rendered) == _std_loads(content)
    return doc


def test_move_table_keeps_body_style_and_comments() -> None:
    content = """\
title = "T"

# about x
[x] # eol
value   =   1
"key.with.dot" = "v"

[y]
z = 2
"""
    doc = _check(content)
    doc.move_table("x", "m.n")

    expected = """\
title = "T"

[y]
z = 2
# about x

[m.n] # eol
value   =   1
"key.with.dot" = "v"

"""
    assert doc.as_string() == expected
    # The semantic value is what the stdlib sees.
    assert _std_loads(doc.as_string()) == {
        "title": "T",
        "y": {"z": 2},
        "m": {"n": {"value": 1, "key.with.dot": "v"}},
    }


def test_move_table_does_not_touch_unrelated_regions() -> None:
    content = """\
before = 1

# header for x
[x]
a = 1

[y]
b = 2
after = 3
"""
    doc = _check(content)
    doc.move_table("x", "z")

    rendered = doc.as_string()
    # The key styles and the whole y table are reproduced verbatim.
    assert rendered.startswith("before = 1\n")
    assert "[y]\nb = 2\nafter = 3\n" in rendered
    assert _std_loads(rendered) == {
        "before": 1,
        "y": {"b": 2, "after": 3},
        "z": {"a": 1},
    }


def test_move_table_preserves_item_identity() -> None:
    doc = parse("[x]\na = 1\n[b]\nz = 2\n")
    table = doc.item("x")
    inner = doc["x"]["a"]

    doc.move_table("x", "c.d")

    assert doc["c"]["d"] is table
    assert doc["c"]["d"]["a"] is inner
    assert table.unwrap() == {"a": 1}


def test_move_table_on_a_table_resyncs_its_dict_view() -> None:
    doc = parse("[a.b]\nx = 1\n[a.c]\ny = 2\n")
    table = doc["a"]

    table.move_table("b", "d")

    assert list(table.keys()) == ["c", "d"]
    assert "b" not in table and "d" in table
    assert table.unwrap() == {"c": {"y": 2}, "d": {"x": 1}}
    assert _std_loads(doc.as_string()) == {
        "a": {"c": {"y": 2}, "d": {"x": 1}}
    }


def test_move_table_into_existing_table() -> None:
    content = """\
[a.b]
x = 1
[c]
y = 2
"""
    doc = _check(content)
    doc.move_table("a.b", "c.m")

    assert doc.as_string() == """\
[c]
y = 2

[c.m]
x = 1
"""
    # The emptied implicit super table `a` renders no header and is gone.
    assert _std_loads(doc.as_string()) == {
        "c": {"y": 2, "m": {"x": 1}},
    }


def test_move_table_creates_implicit_super_tables() -> None:
    doc = _check("[x]\na = 1\n")
    doc.move_table("x", "n.m.o")

    rendered = doc.as_string()
    assert rendered == "[n.m.o]\na = 1\n"
    assert _std_loads(rendered) == {"n": {"m": {"o": {"a": 1}}}}


def test_move_table_accepts_quoted_segments_with_dots() -> None:
    content = '[a."b.c"]\nx = 1\n'
    doc = _check(content)
    doc.move_table('a."b.c"', '"p.q".r')

    rendered = doc.as_string()
    assert rendered == '\n["p.q".r]\nx = 1\n'
    assert _std_loads(rendered) == {"p.q": {"r": {"x": 1}}}


def test_move_table_accepts_segment_iterable() -> None:
    doc = _check('["a.b"]\nx = 1\n')
    doc.move_table(["a.b"], ["n", "m"])
    assert _std_loads(doc.as_string()) == {"n": {"m": {"x": 1}}}


def test_move_table_handles_dotted_key_super_tables() -> None:
    content = """\
[a]
b.c = 1
[z]
q = 2
"""
    doc = _check(content)
    doc.move_table("z", "a.b.d")

    rendered = doc.as_string()
    assert rendered == """\
[a]
b.c = 1

[a.b.d]
q = 2
"""
    assert _std_loads(rendered) == {
        "a": {"b": {"c": 1, "d": {"q": 2}}}
    }


def test_move_table_moves_dotted_key_materialized_table() -> None:
    doc = _check("a.b.c = 1\n[z]\nq = 2\n")
    doc.move_table("a.b", "m")
    assert _std_loads(doc.as_string()) == {
        "m": {"c": 1},
        "z": {"q": 2},
    }


def test_move_table_keeps_scope_of_other_keys() -> None:
    content = """\
a.b = 1
c.d = 2

[t]
x = 1

[t.sub]
y = 2
"""
    doc = _check(content)
    doc.move_table("t.sub", "m")

    rendered = doc.as_string()
    assert _std_loads(rendered) == {
        "a": {"b": 1},
        "c": {"d": 2},
        "t": {"x": 1},
        "m": {"y": 2},
    }
    # Root-level dotted keys still render inline at the top.
    assert rendered.startswith("a.b = 1\nc.d = 2\n")


def test_move_table_inside_an_aot_element() -> None:
    content = """\
[[p]]
x = 1

# q header
[p.q]
y = 2

[other]
z = 3
"""
    doc = _check(content)
    # A table inside an AoT element is moved through its own container.
    doc["p"][0].move_table("q", "r")

    rendered = doc.as_string()
    assert "[p.r]\ny = 2" in rendered
    assert "# q header" in rendered
    assert _std_loads(rendered) == {
        "p": [{"x": 1, "r": {"y": 2}}],
        "other": {"z": 3},
    }


def test_move_table_round_trip_restores_semantics() -> None:
    content = """\
title = "T"

# about x
[x] # eol
value   =   1
"key.with.dot" = "v"

[y]
z = 2
"""
    original_semantics = _std_loads(content)
    doc = parse(content)

    doc.move_table("x", "m.n")
    assert _std_loads(doc.as_string()) != original_semantics
    assert doc["m"]["n"].unwrap() == {"value": 1, "key.with.dot": "v"}

    doc.move_table("m.n", "x")
    assert _std_loads(doc.as_string()) == original_semantics
    # The moved table kept its inner key styles and header trivia.
    assert "value   =   1\n" in doc.as_string()
    assert "[x] # eol\n" in doc.as_string()


def test_move_table_failures_are_atomic() -> None:
    content = """\
root = 1
[a]
b.c = 1
[z]
q = 2
"""
    cases = [
        ("missing", "nope", "m"),
        ("value source", "root", "m"),
        ("aot source", "p", "m"),
        ("existing target", "z", "a"),
        ("target under source", "z", "z.inside"),
        ("same path", "z", "z"),
        ("dotted collision", "z", "a.b.c"),
        ("target parent value", "z", "root.inside"),
        ("bad path", "z", ""),
    ]
    for name, source, target in cases:
        doc = parse(content)
        before = doc.as_string()
        with pytest.raises(TableMoveError):
            doc.move_table(source, target)
        assert doc.as_string() == before, name
        assert _std_loads(doc.as_string()) == _std_loads(content), name


def test_move_table_rejects_aot_and_aot_elements() -> None:
    doc = parse("[[p]]\nx = 1\n[p.q]\ny = 2\n")
    before = doc.as_string()

    with pytest.raises(TableMoveError):
        doc.move_table("p", "m")
    with pytest.raises(TableMoveError):
        doc.move_table("p.q", "m")
    assert doc.as_string() == before


def test_move_table_rejects_out_of_order_table() -> None:
    content = "[a.b]\nx = 1\n[z]\nw = 2\n[a.c]\ny = 3\n"
    doc = parse(content)
    before = doc.as_string()
    with pytest.raises(TableMoveError):
        doc.move_table("a", "m")
    assert doc.as_string() == before


def test_move_table_rejects_target_into_out_of_order_table() -> None:
    content = "[a.b]\nx = 1\n[z]\nw = 2\n[a.c]\ny = 3\n"
    doc = parse(content)
    before = doc.as_string()
    with pytest.raises(TableMoveError):
        doc.move_table("z", "a.m")
    assert doc.as_string() == before
