"""Tests for the incremental (streaming) renderer behind ``iter_dumps``.

The renderer is expected to:

* produce output that is character-identical to ``dumps`` (and to the
  original input where the round trip is identity preserving), including
  comments, emptied arrays of tables, out-of-order tables, dotted keys and
  different line endings;
* hand fragments to a writer one at a time, with a fragment size bounded by a
  single scalar rather than the whole document;
* leave the document untouched when the consumer aborts or the writer raises,
  so the same document can be rendered again immediately.
"""

from __future__ import annotations

import gc
import io
import tracemalloc
from typing import Any

import pytest

from tomlkit import dump
from tomlkit import dumps
from tomlkit import iter_dumps
from tomlkit import parse


# Short documents covering every required formatting corner.
SHORTS: dict[str, str] = {
    "comments": (
        "# top comment\n"
        'title = "x"  # inline\n'
        "\n"
        "# before table\n"
        "[a]  # header comment\n"
        "x = 1 # trailing\n"
        "# after\n"
        "\n"
        "[b]\n"
        "y = 2\n"
    ),
    "empty_aot": (
        "[[items]]\n"
        'name = "one"\n'
        "\n"
        "[[items]]\n"
        'name = "two"\n'
        "\n"
        "[other]\n"
        "z = 1\n"
    ),
    "out_of_order": (
        "[[fruit]]\n"
        'apple.color = "red"\n'
        "\n"
        "[potato]\n"
        "\n"
        "[fruit.apple.texture]\n"
        "smooth = true\n"
    ),
    "dotted": (
        "a.b.c = 1\n"
        "a.b.d = 2\n"
        'x.y = "z"\n'
        "\n"
        "[t]\n"
        "p.q = 3\n"
    ),
    "dotted_inline": 'a = {x = 1, b.c = 1, y = 2}\nb = {b.c.d = 1}\n',
    "crlf": "a = 1\r\n[b]\r\nc = 2\r\n# hi\r\n",
    "mixed_newlines": "a = 1\r\nb = 2\nc = 3\r\n[d]\ne = 4\n",
    "only_comments": "# one\n# two\n",
    "arrays": (
        "a = [\n"
        "  1, # one\n"
        "  2,\n"
        "  3,\n"
        "]\n"
        "b = [1, 2, 3]\n"
        'c = [ [1, 2], ["x", "y"] ]\n'
    ),
    "aot_nested": (
        "[[a]]\n"
        "x = 1\n"
        "[[a.b]]\n"
        "y = 2\n"
        "[[a]]\n"
        "x = 3\n"
        "[[a.b]]\n"
        "y = 4\n"
    ),
    "super_tables": "[a.b.c]\nx = 1\n[a.b.d]\ny = 2\n[a]\nz = 3\n",
    "empty_tables": "[a]\n[b]\n[c]\n",
    "multiline_string": 'text = """\nline one\nline two\n"""\n',
}


def _build_doc(scale: int) -> Any:
    parts = ["# synthetic document\n"]
    for i in range(scale):
        parts.append(f"[table_{i}]\n")
        for j in range(8):
            parts.append(f'key_{j} = "value-{i}-{j}"\n')
        parts.append("\n")
    return parse("".join(parts))


class CountingWriter:
    """Records every fragment the renderer emits."""

    def __init__(self) -> None:
        self.calls = 0
        self.bytes = 0
        self.max_fragment = 0
        self.fragments: list[str] = []

    def write(self, fragment: str) -> None:
        self.calls += 1
        self.bytes += len(fragment)
        self.max_fragment = max(self.max_fragment, len(fragment))
        self.fragments.append(fragment)


class FailingWriter:
    """Raises on the ``fail_after``-th write call."""

    def __init__(self, fail_after: int) -> None:
        self.fail_after = fail_after
        self.calls = 0

    def write(self, fragment: str) -> None:
        self.calls += 1
        if self.calls >= self.fail_after:
            raise OSError("simulated write failure")


@pytest.mark.parametrize("name", sorted(SHORTS))
def test_iter_dumps_matches_dumps_character_for_character(name: str) -> None:
    doc = parse(SHORTS[name])
    expected = dumps(doc)

    assert "".join(iter_dumps(doc)) == expected


@pytest.mark.parametrize("name", sorted(SHORTS))
def test_iter_dumps_with_chunk_size_matches_dumps(name: str) -> None:
    doc = parse(SHORTS[name])
    expected = dumps(doc)

    for chunk_size in (1, 2, 3, 7, 64, 10_000):
        chunks = list(iter_dumps(doc, chunk_size=chunk_size))
        assert "".join(chunks) == expected
        assert all(len(chunk) <= chunk_size for chunk in chunks)


@pytest.mark.parametrize("name", sorted(SHORTS))
def test_dump_to_stream_matches_dumps(name: str) -> None:
    doc = parse(SHORTS[name])
    expected = dumps(doc)

    fp = io.StringIO()
    dump(doc, fp)
    assert fp.getvalue() == expected


def test_shorts_round_trip_unchanged() -> None:
    # These inputs are already in canonical tomlkit layout, so dumping keeps
    # them byte-identical -- including CRLF and mixed line endings.
    for name in ("comments", "crlf", "mixed_newlines", "dotted"):
        assert dumps(parse(SHORTS[name])) == SHORTS[name]


def test_all_example_fixtures_stream_identically(example: Any) -> None:
    for name in (
        "example",
        "fruit",
        "hard",
        "sections_with_same_start",
        "pyproject",
        "0.5.0",
        "test",
        "table_names",
        "newline_in_strings",
        "preserve_quotes_in_string",
        "string_slash_whitespace_newline",
    ):
        content = example(name)
        doc = parse(content)
        expected = dumps(doc)

        assert "".join(iter_dumps(doc)) == expected
        fp = io.StringIO()
        dump(doc, fp)
        assert fp.getvalue() == expected


def test_iter_dumps_never_emits_a_whole_document_sized_fragment() -> None:
    doc = _build_doc(scale=40)
    expected = dumps(doc)
    total = len(expected)
    writer = CountingWriter()

    dump(doc, writer)

    assert writer.bytes == total
    # The largest fragment is a single scalar/trivia piece -- orders of
    # magnitude smaller than the document, and independent of its size.
    assert writer.max_fragment < 100
    assert writer.calls > total // writer.max_fragment // 2
    assert "".join(writer.fragments) == expected


def test_streaming_peak_memory_does_not_grow_with_document_size() -> None:
    # Allocation evidence: multiplying the document size must not multiply the
    # memory used to stream it. A whole-document buffer would grow linearly.
    def measure(scale: int) -> tuple[int, int]:
        doc = _build_doc(scale)
        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        written = 0
        for fragment in iter_dumps(doc):
            written += len(fragment)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return written, peak - baseline

    # Pay the one-time cost of the rendering machinery (thread, queue) before
    # measuring so the comparison reflects document-driven allocations only.
    list(iter_dumps(_build_doc(scale=2)))

    small_bytes, small_peak = measure(scale=100)
    large_bytes, large_peak = measure(scale=400)

    assert large_bytes > small_bytes * 3
    # The peaks stay flat and well below even the smaller document's size; a
    # non-incremental renderer would need ~large_bytes of heap instead.
    assert small_peak < small_bytes
    assert large_peak < small_bytes


def test_render_is_lazy_from_the_first_fragment() -> None:
    doc = _build_doc(scale=40)
    total = len(dumps(doc))

    generator = iter_dumps(doc)
    first = next(generator)
    generator.close()

    # The consumer receives a small fragment without pulling the rest.
    assert first
    assert len(first) < 100
    assert len(first) < total
    # Closing early must not break subsequent full renders.
    assert "".join(iter_dumps(doc)) == dumps(doc)


@pytest.mark.parametrize("abort_at", range(1, 12))
def test_aborting_at_every_position_leaves_document_usable(abort_at: int) -> None:
    doc = parse(SHORTS["comments"])
    expected = dumps(doc)
    seen: list[str] = []

    generator = iter_dumps(doc)
    for index, fragment in enumerate(generator):
        seen.append(fragment)
        if index + 1 == abort_at:
            generator.close()
            break
    else:
        # The document is shorter than some abort positions; that is fine --
        # it simply ran to completion.
        assert "".join(seen) == expected
        return

    # Only a prefix was consumed, never the full document.
    assert expected.startswith("".join(seen))
    assert "".join(seen) != expected
    # The document renders fully again after the aborted run.
    assert dumps(doc) == expected
    assert "".join(iter_dumps(doc)) == expected


def test_aborting_empty_and_tiny_documents_still_re_renders() -> None:
    from tomlkit import document

    tiny = parse("a = 1\n")
    for doc in (document(), tiny):
        expected = dumps(doc)
        generator = iter_dumps(doc)
        for fragment in generator:
            generator.close()
            break
        else:
            generator.close()
        # Aborting the (possibly empty) stream leaves the document usable.
        assert dumps(doc) == expected
        assert "".join(iter_dumps(doc)) == expected


def test_writer_error_propagates_and_document_renders_again() -> None:
    doc = _build_doc(scale=4)
    expected = dumps(doc)
    fragment_count = len(list(iter_dumps(doc)))

    for fail_after in range(1, fragment_count + 1):
        writer = FailingWriter(fail_after)
        with pytest.raises(OSError, match="simulated write failure"):
            dump(doc, writer)
        assert writer.calls == fail_after

    # A failed render did not mutate the document.
    assert dumps(doc) == expected
    fp = io.StringIO()
    dump(doc, fp)
    assert fp.getvalue() == expected


def test_iter_dumps_propagates_render_errors(monkeypatch: Any) -> None:
    doc = parse('a = 1\nb = 2\n[c]\nx = "hello"\n')
    expected = dumps(doc)

    from tomlkit.items import String

    original = String._render

    def explode(self: Any, write: Any) -> None:
        raise ValueError("nope")

    # Fail on the first string fragment, which is reached early in the walk,
    # and make sure the failure surfaces through the iterator.
    monkeypatch.setattr(String, "_render", explode)
    with pytest.raises(ValueError, match="nope"):
        list(iter_dumps(doc))

    monkeypatch.setattr(String, "_render", original)
    # The document is still intact after the failed run.
    assert dumps(doc) == expected
    assert "".join(iter_dumps(doc)) == expected


def test_dumps_still_returns_full_string() -> None:
    doc = parse(SHORTS["dotted"])
    assert isinstance(dumps(doc), str)
    assert dumps(doc) == "a.b.c = 1\na.b.d = 2\nx.y = \"z\"\n\n[t]\np.q = 3\n"


def test_iter_dumps_rejects_invalid_chunk_size() -> None:
    doc = parse("a = 1\n")
    with pytest.raises(ValueError):
        list(iter_dumps(doc, chunk_size=0))
    with pytest.raises(ValueError):
        list(iter_dumps(doc, chunk_size=-3))


def test_iter_dumps_works_for_plain_dict_and_sort_keys() -> None:
    data = {"z": 1, "a": {"y": 2, "b": 3}}
    expected = dumps(data, sort_keys=True)

    assert "".join(iter_dumps(data, sort_keys=True)) == expected
    assert "".join(iter_dumps(data)) == dumps(data)
