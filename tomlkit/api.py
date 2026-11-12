from __future__ import annotations

import contextlib
import datetime as _datetime
import re

from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from typing import IO
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

from tomlkit._stream import StringWriter
from tomlkit._stream import Writer
from tomlkit._utils import parse_rfc3339
from tomlkit._utils import validate_comment
from tomlkit.container import Container
from tomlkit.exceptions import UnexpectedCharError
from tomlkit.items import CUSTOM_ENCODERS
from tomlkit.items import AoT
from tomlkit.items import Array
from tomlkit.items import Bool
from tomlkit.items import Comment
from tomlkit.items import Date
from tomlkit.items import DateTime
from tomlkit.items import DottedKey
from tomlkit.items import Float
from tomlkit.items import InlineTable
from tomlkit.items import Integer
from tomlkit.items import Item as _Item
from tomlkit.items import Key
from tomlkit.items import SingleKey
from tomlkit.items import String
from tomlkit.items import StringType as _StringType
from tomlkit.items import Table
from tomlkit.items import Time
from tomlkit.items import Trivia
from tomlkit.items import Whitespace
from tomlkit.items import item as item
from tomlkit.parser import Parser
from tomlkit.toml_document import TOMLDocument as TOMLDocument


if TYPE_CHECKING:
    from tomlkit.items import Encoder

    E = TypeVar("E", bound=Encoder)


def loads(string: str | bytes) -> TOMLDocument:
    """
    Parses a string into a TOMLDocument.

    Alias for parse().
    """
    return parse(string)


def _as_renderable(
    data: Mapping[str, Any], sort_keys: bool = False
) -> _Item | Container | _PlainString:
    """Return the item/container that renders ``data``.

    Raises ``TypeError`` for inputs that cannot be rendered as a TOML table.
    """
    if isinstance(data, (Table, InlineTable, Container)):
        if not sort_keys:
            return data

        return item(data, _sort_keys=True)

    if isinstance(data, Mapping):
        return item(dict(data), _sort_keys=sort_keys)

    # Mapping-like wrappers (e.g. dotty_dict's Dotty) delegate ``as_string``
    # to the document they wrap; rendering it directly preserves the original
    # layout, which re-encoding through a plain dict would lose.
    render = getattr(data, "_render", None)
    if render is not None:
        return data  # type: ignore[return-value]

    as_string = getattr(data, "as_string", None)
    if as_string is None:
        msg = f"Expecting Mapping or TOML Table or Container, {type(data)} given"
        raise TypeError(msg)

    # A wrapper without the streaming ``_render`` hook can only be rendered
    # through the string it already knows how to produce.
    return _PlainString(as_string)


class _PlainString:
    """Adapter for a wrapper that only offers ``as_string``.

    The wrapper's own ``as_string`` is called on every render -- never cached
    -- so repeated dumps see edits made in between, as they did before.
    """

    def __init__(self, as_string: Any) -> None:
        self._as_string = as_string

    def _render(self, write: Writer) -> None:
        write(self._as_string())


def dumps(data: Mapping[str, Any], sort_keys: bool = False) -> str:
    """
    Dumps a TOMLDocument into a string.
    """
    renderable = _as_renderable(data, sort_keys)
    writer = StringWriter()
    renderable._render(writer.write)
    return writer.getvalue()


_DONE = object()


def _iter_fragments(
    renderable: _Item | Container | _PlainString,
) -> Iterator[str]:
    """Yield each render fragment as soon as it is produced.

    The renderer takes a plain ``write(fragment)`` callback, which cannot by
    itself suspend its call stack. A daemon thread therefore drives the render
    walk and hands each fragment over through a one-slot bounded queue: the
    walk blocks as soon as a fragment is waiting, so it cannot run ahead of the
    consumer and at most one fragment (plus the current scalar and the scope
    stack held by the renderer) is resident at a time.

    If the consumer stops early -- by closing the iterator, breaking out of a
    loop, or raising out of it -- the queue is released and the render thread
    is told to stop. Rendering never mutates the document, so it can be
    restarted afterwards. Exceptions raised while rendering are re-raised in
    the consumer.
    """
    import queue
    import threading

    slots: queue.Queue[object] = queue.Queue(maxsize=1)
    stopped = threading.Event()
    finished = threading.Event()

    def put(item: object) -> bool:
        """Hand ``item`` over unless the consumer has gone away."""
        while not stopped.is_set():
            try:
                slots.put(item, timeout=0.05)
            except queue.Full:
                continue
            return True
        return False

    def worker() -> None:
        try:
            def write(fragment: str) -> None:
                if not put(fragment):
                    # The consumer abandoned the iteration; unwind the render.
                    raise _AbandonedRender

            renderable._render(write)
        except _AbandonedRender:
            pass
        except BaseException as exc:  # deliver the failure to the consumer
            put(exc)
        finally:
            put(_DONE)
            finished.set()

    thread = threading.Thread(target=worker, name="tomlkit-iter-dumps", daemon=True)
    thread.start()
    try:
        while True:
            item = slots.get()
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            fragment = item
            assert isinstance(fragment, str)
            yield fragment
    finally:
        stopped.set()
        # Unblock a worker waiting on a full queue.
        try:
            while True:
                slots.get_nowait()
        except queue.Empty:
            pass
        finished.wait(timeout=1.0)


class _AbandonedRender(Exception):
    """Internal signal: the consumer stopped iterating."""


def iter_dumps(
    data: Mapping[str, Any],
    sort_keys: bool = False,
    chunk_size: int | None = None,
) -> Iterator[str]:
    """
    Iteratively dumps a TOMLDocument into small string chunks.

    Unlike :func:`dumps`, the full rendered document is never assembled in
    memory: rendering only resumes when the caller asks for the next chunk,
    so fragments are produced on demand while the document tree is walked.
    Joining the chunks yields exactly the string :func:`dumps` would return
    for the same input.

    :param data: a dict-like object to dump
    :param sort_keys: if true, sort the keys in alphabetic order
    :param chunk_size: if given, coalesce fragments into chunks of at most
        this many characters; otherwise every render fragment is yielded as
        soon as it is produced
    """
    if chunk_size is not None and chunk_size <= 0:
        msg = "chunk_size must be a positive integer or None"
        raise ValueError(msg)

    renderable = _as_renderable(data, sort_keys)
    if chunk_size is None:
        yield from _iter_fragments(renderable)
        return

    pending = ""
    for fragment in _iter_fragments(renderable):
        pending += fragment
        while len(pending) >= chunk_size:
            yield pending[:chunk_size]
            pending = pending[chunk_size:]
    if pending:
        yield pending


def load(fp: IO[str] | IO[bytes]) -> TOMLDocument:
    """
    Load toml document from a file-like object.
    """
    return parse(fp.read())


def dump(data: Mapping[str, Any], fp: IO[str], *, sort_keys: bool = False) -> None:
    """
    Dump a TOMLDocument into a writable file stream.

    The document is streamed fragment by fragment straight to ``fp`` instead
    of being assembled into one string first, so writing a large document does
    not need memory proportional to its rendered size.

    :param data: a dict-like object to dump
    :param sort_keys: if true, sort the keys in alphabetic order

    :Example:

    >>> with open("output.toml", "w") as fp:
    ...     tomlkit.dump(data, fp)
    """
    renderable = _as_renderable(data, sort_keys)
    renderable._render(fp.write)


def parse(string: str | bytes) -> TOMLDocument:
    """
    Parses a string or bytes into a TOMLDocument.
    """
    return Parser(string).parse()


def document() -> TOMLDocument:
    """
    Returns a new TOMLDocument instance.
    """
    return TOMLDocument()


# Items
_INTEGER_RE = re.compile(
    r"(?:"
    r"0x[0-9a-fA-F](?:_?[0-9a-fA-F])*"  # hex: lowercase prefix only, no sign
    r"|0o[0-7](?:_?[0-7])*"  # octal: lowercase prefix only, no sign
    r"|0b[01](?:_?[01])*"  # binary: lowercase prefix only, no sign
    r"|[+-]?(?:0|[1-9](?:_?[0-9])*)"  # decimal: optional sign, no leading zeros
    r")"
)
"""Valid TOML v1.0.0 integer literals (https://toml.io/en/v1.0.0#integer).

Underscores may only appear between digits; hex/octal/binary use a
lowercase prefix and take no sign, mirroring ``Parser._parse_number``.
"""


def integer(raw: str | int) -> Integer:
    """Create an integer item from a number or string.

    When ``raw`` is a string it must be a valid TOML v1.0.0 integer
    literal; the original text is then preserved verbatim so grouping
    underscores (e.g. ``"1_000"``) survive ``dumps()``. Anything else
    raises ``ValueError``.
    """
    if isinstance(raw, str):
        if not _INTEGER_RE.fullmatch(raw):
            raise ValueError(f"Invalid TOML integer: {raw!r}")
        return Integer(int(raw, 0), Trivia(), raw)
    return item(int(raw))


def float_(raw: str | float) -> Float:
    """Create an float item from a number or string."""
    return item(float(raw))


def boolean(raw: str | bool) -> Bool:
    """Turn `true` or `false` into a boolean item."""
    return item(raw == "true" if isinstance(raw, str) else raw)


def string(
    raw: str,
    *,
    literal: bool = False,
    multiline: bool = False,
    escape: bool = True,
) -> String:
    """Create a string item.

    By default, this function will create *single line basic* strings, but
    boolean flags (e.g. ``literal=True`` and/or ``multiline=True``)
    can be used for personalization.

    For more information, please check the spec: `<https://toml.io/en/v1.0.0#string>`__.

    Dumping produces a TOML string that faithfully represents the given string.
    This includes character escaping for basic strings.
    This behaviour can be disabled by setting ``escape=False``.
    In that case, the given string is used verbatim for the content of the TOML string.
    Please note that this makes you responsible for ensuring the given string does not
    contain any forbidden character or sequence.
    """
    type_ = _StringType.select(literal, multiline)
    return String.from_raw(raw, type_, escape)


def date(raw: str) -> Date:
    """Create a TOML date."""
    value = parse_rfc3339(raw)
    if isinstance(value, _datetime.datetime) or not isinstance(value, _datetime.date):
        raise ValueError("date() only accepts date strings.")

    return item(value)


def time(raw: str) -> Time:
    """Create a TOML time."""
    value = parse_rfc3339(raw)
    if not isinstance(value, _datetime.time):
        raise ValueError("time() only accepts time strings.")

    return item(value)


def datetime(raw: str) -> DateTime:
    """Create a TOML datetime."""
    value = parse_rfc3339(raw)
    if not isinstance(value, _datetime.datetime):
        raise ValueError("datetime() only accepts datetime strings.")

    return item(value)


def array(raw: str = "[]") -> Array:
    """Create an array item for its string representation.

    :Example:

    >>> array("[1, 2, 3]")  # Create from a string
    [1, 2, 3]
    >>> a = array()
    >>> a.extend([1, 2, 3])  # Create from a list
    >>> a
    [1, 2, 3]
    """
    v = value(raw)
    if not isinstance(v, Array):
        raise ValueError(f"Expected an array, got {type(v)}")
    return v


def table(is_super_table: bool | None = None) -> Table:
    """Create an empty table.

    :param is_super_table: if true, the table is a super table

    :Example:

    >>> doc = document()
    >>> foo = table(True)
    >>> bar = table()
    >>> bar.update({'x': 1})
    >>> foo.append('bar', bar)
    >>> doc.append('foo', foo)
    >>> print(doc.as_string())
    [foo.bar]
    x = 1
    """
    return Table(Container(), Trivia(), False, is_super_table)


def inline_table() -> InlineTable:
    """Create an inline table.

    :Example:

    >>> table = inline_table()
    >>> table.update({'x': 1, 'y': 2})
    >>> print(table.as_string())
    {x = 1, y = 2}
    """
    return InlineTable(Container(), Trivia(), new=True)


def aot() -> AoT:
    """Create an array of table.

    :Example:

    >>> doc = document()
    >>> aot = aot()
    >>> aot.append(item({'x': 1}))
    >>> doc.append('foo', aot)
    >>> print(doc.as_string())
    [[foo]]
    x = 1
    """
    return AoT([])


def key(k: str | Iterable[str]) -> Key:
    """Create a key from a string. When a list of string is given,
    it will create a dotted key.

    :Example:

    >>> doc = document()
    >>> doc.append(key('foo'), 1)
    >>> doc.append(key(['bar', 'baz']), 2)
    >>> print(doc.as_string())
    foo = 1
    bar.baz = 2
    """
    if isinstance(k, str):
        return SingleKey(k)
    keys = [SingleKey(_k) for _k in k]
    if len(keys) == 1:
        return keys[0]
    return DottedKey(keys)


def value(raw: str) -> _Item:
    """Parse a simple value from a string.

    :Example:

    >>> value("1")
    1
    >>> value("true")
    True
    >>> value("[1, 2, 3]")
    [1, 2, 3]
    """
    parser = Parser(raw)
    v = parser._parse_value()
    if not parser.end():
        raise parser.parse_error(UnexpectedCharError, char=parser._current)
    return v


def key_value(src: str) -> tuple[Key, _Item]:
    """Parse a key-value pair from a string.

    Anything other than whitespace or a comment after the pair is an error,
    as it is when the same text is given to :func:`parse`.

    :Example:

    >>> key_value("foo = 1")
    (Key('foo'), 1)
    """
    parser = Parser(src)
    k, v = parser._parse_key_value(parse_comment=True)
    if not parser.end():
        raise parser.parse_error(UnexpectedCharError, char=parser._current)
    return k, v


def ws(src: str) -> Whitespace:
    """Create a whitespace from a string."""
    return Whitespace(src, fixed=True)


def nl() -> Whitespace:
    """Create a newline item."""
    return ws("\n")


def comment(string: str) -> Comment:
    """Create a comment item.

    A multiline string produces one ``#``-prefixed line per line so that the
    result is still valid TOML.
    """
    validate_comment(string, multiline=True)
    lines = string.split("\n")
    rendered = "\n".join(f"# {line}" if line else "#" for line in lines)
    return Comment(Trivia(comment_ws="  ", comment=rendered))


def register_encoder(encoder: E) -> E:
    """Add a custom encoder, which should be a function that will be called
    if the value can't otherwise be converted.

    The encoder should return a TOMLKit item or raise a ``ConvertError``.

    Example::

        @register_encoder
        def encode_custom_dict(obj, _parent=None, _sort_keys=False):
            if isinstance(obj, CustomDict):
                tbl = table()
                for key, value in obj.items():
                    # Pass along parameters when encoding nested values
                    tbl[key] = item(value, _parent=tbl, _sort_keys=_sort_keys)
                return tbl
            raise ConvertError("Not a CustomDict")
    """
    CUSTOM_ENCODERS.append(encoder)
    return encoder


def unregister_encoder(encoder: Encoder) -> None:
    """Unregister a custom encoder."""
    with contextlib.suppress(ValueError):
        CUSTOM_ENCODERS.remove(encoder)
