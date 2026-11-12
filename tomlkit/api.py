from __future__ import annotations

import contextlib
import datetime as _datetime
import inspect
import re

from collections.abc import Iterable
from collections.abc import Mapping
from typing import IO
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

from tomlkit._utils import RFC_3339_TIME_V1_0
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
from tomlkit.toml_version import TOMLVersion
from tomlkit.toml_version import normalize_version


if TYPE_CHECKING:
    from tomlkit.items import Encoder

    E = TypeVar("E", bound=Encoder)


def loads(
    string: str | bytes, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> TOMLDocument:
    """
    Parses a string into a TOMLDocument.

    Alias for parse().
    """
    return parse(string, version=version)


def dumps(
    data: Mapping[str, Any],
    sort_keys: bool = False,
    *,
    version: TOMLVersion | str | None = None,
) -> str:
    """
    Dumps a TOMLDocument into a string.

    :param data: a dict-like object to dump
    :param sort_keys: if true, sort the keys in alphabetic order
    :param version: output spec version. When ``data`` is a
        :class:`TOMLDocument` the default is the version it was parsed or
        created with; otherwise it defaults to TOML 1.0. Writing 1.1 content
        as 1.0 rewrites the representable constructs losslessly
        (``\\e``/``\\xXX`` -> ``\\u`` escapes, seconds-less times, multiline
        inline tables -> single-line) and raises
        :class:`tomlkit.toml_version.OutputVersionError` for values with no
        1.0 spelling, naming their location.
    """
    target = _resolve_output_version(data, version)

    if isinstance(data, (Table, InlineTable, Container)):
        if not sort_keys:
            return data.as_string(version=target)

        return item(data, _sort_keys=True).as_string(version=target)

    if isinstance(data, Mapping):
        return item(dict(data), _sort_keys=sort_keys).as_string(version=target)

    try:
        # mapping-like wrappers (e.g. dotty_dict's Dotty) delegate
        # ``as_string`` to the document they wrap; rendering it directly
        # preserves the original layout, which re-encoding through a plain
        # dict would lose. Only pass the version along when the delegate
        # accepts it, so wrappers with a plain ``as_string()`` keep working.
        if _accepts_version_kwarg(data.as_string):  # type: ignore[attr-defined]
            return data.as_string(version=target)  # type: ignore[attr-defined]
        return data.as_string()  # type: ignore[attr-defined]
    except AttributeError as ex:
        msg = f"Expecting Mapping or TOML Table or Container, {type(data)} given"
        raise TypeError(msg) from ex


def _accepts_version_kwarg(as_string: Any) -> bool:
    try:
        parameters = inspect.signature(as_string).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == "version" or p.kind is inspect.Parameter.VAR_KEYWORD
        for p in parameters
    )


def _resolve_output_version(
    data: Any, version: TOMLVersion | str | None
) -> TOMLVersion:
    if version is not None:
        return normalize_version(version)
    doc_version = getattr(data, "version", None)
    if isinstance(doc_version, TOMLVersion):
        return doc_version
    return TOMLVersion.V1_0


def load(
    fp: IO[str] | IO[bytes], *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> TOMLDocument:
    """
    Load toml document from a file-like object.
    """
    return parse(fp.read(), version=version)


def dump(
    data: Mapping[str, Any],
    fp: IO[str],
    *,
    sort_keys: bool = False,
    version: TOMLVersion | str | None = None,
) -> None:
    """
    Dump a TOMLDocument into a writable file stream.

    :param data: a dict-like object to dump
    :param sort_keys: if true, sort the keys in alphabetic order
    :param version: output spec version; see :func:`dumps` for the rules

    :Example:

    >>> with open("output.toml", "w") as fp:
    ...     tomlkit.dump(data, fp)
    """
    fp.write(dumps(data, sort_keys=sort_keys, version=version))


def parse(
    string: str | bytes, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> TOMLDocument:
    """
    Parses a string or bytes into a TOMLDocument.

    ``version`` selects the TOML spec version the input is validated
    against. The default, ``"1.0"``, accepts only TOML 1.0 documents;
    ``"1.1"`` additionally accepts the TOML 1.1 extensions (``\\e`` and
    ``\\xXX`` escapes, seconds-less and offset local times, and newlines,
    comments and trailing commas in inline tables). A document that uses
    TOML 1.1-only syntax while parsed as 1.0 raises
    :class:`tomlkit.toml_version.InputVersionError` naming the construct.
    """
    return Parser(string, version=version).parse()


def document(*, version: TOMLVersion | str = TOMLVersion.V1_0) -> TOMLDocument:
    """
    Returns a new TOMLDocument instance.

    ``version`` records the spec version the document is written for; it is
    used as the default output version by :func:`dumps`.
    """
    return TOMLDocument(version=normalize_version(version))


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


def date(
    raw: str, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> Date:
    """Create a TOML date.

    ``version`` selects which spec grammar the literal is validated against.
    """
    value = parse_rfc3339(raw, version=normalize_version(version))
    if isinstance(value, _datetime.datetime) or not isinstance(value, _datetime.date):
        raise ValueError("date() only accepts date strings.")

    return item(value)


def time(
    raw: str, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> Time:
    """Create a TOML time.

    TOML 1.0 requires the seconds field and forbids an offset; ``version="1.1"``
    additionally accepts a seconds-less (``07:32``) and an offset-time
    (``07:32:00Z``) literal and keeps its original spelling.
    """
    target = normalize_version(version)
    if target is TOMLVersion.V1_0 and RFC_3339_TIME_V1_0.match(raw) is None:
        raise ValueError(
            f"time() only accepts TOML 1.0 time strings (HH:MM:SS), got {raw!r}"
        )
    value = parse_rfc3339(raw, version=target)
    if not isinstance(value, _datetime.time):
        raise ValueError("time() only accepts time strings.")

    result = item(value)
    if target is TOMLVersion.V1_1:
        # Keep the original 1.1 spelling (e.g. seconds-less ``07:32``).
        result = Time(
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            value.tzinfo,
            result.trivia,
            raw,
            fold=value.fold,
        )
    return result


def datetime(
    raw: str, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> DateTime:
    """Create a TOML datetime.

    ``version="1.1"`` also accepts a seconds-less literal and keeps its
    original spelling.
    """
    target = normalize_version(version)
    value = parse_rfc3339(raw, version=target)
    if not isinstance(value, _datetime.datetime):
        raise ValueError("datetime() only accepts datetime strings.")

    if target is TOMLVersion.V1_1:
        # Keep the original 1.1 spelling (e.g. seconds-less or space-separated).
        return DateTime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            value.tzinfo,
            Trivia(),
            raw,
            fold=value.fold,
        )
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


def value(
    raw: str, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> _Item:
    """Parse a simple value from a string.

    :Example:

    >>> value("1")
    1
    >>> value("true")
    True
    >>> value("[1, 2, 3]")
    [1, 2, 3]
    """
    parser = Parser(raw, version=version)
    v = parser._parse_value()
    if not parser.end():
        raise parser.parse_error(UnexpectedCharError, char=parser._current)
    return v


def key_value(
    src: str, *, version: TOMLVersion | str = TOMLVersion.V1_0
) -> tuple[Key, _Item]:
    """Parse a key-value pair from a string.

    Anything other than whitespace or a comment after the pair is an error,
    as it is when the same text is given to :func:`parse`.

    :Example:

    >>> key_value("foo = 1")
    (Key('foo'), 1)
    """
    parser = Parser(src, version=version)
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
