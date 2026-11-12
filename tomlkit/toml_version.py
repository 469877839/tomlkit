from __future__ import annotations

from enum import Enum

from tomlkit.exceptions import ParseError
from tomlkit.exceptions import TOMLKitError


class TOMLVersion(str, Enum):
    """Supported TOML specification versions.

    The enum members double as their canonical string spelling (``"1.0"``,
    ``"1.1"``), so they can be used wherever a plain version string is
    accepted.
    """

    V1_0 = "1.0"
    V1_1 = "1.1"

    def __str__(self) -> str:
        return self.value


def normalize_version(version: TOMLVersion | str) -> TOMLVersion:
    """Coerce a version argument into a :class:`TOMLVersion`.

    Accepts :class:`TOMLVersion` members and the strings ``"1.0"`` /
    ``"1.1"`` (with or without a trailing ``.0``). Anything else raises
    :class:`ValueError`.
    """
    if isinstance(version, TOMLVersion):
        return version
    if not isinstance(version, str):
        msg = f"Unsupported TOML version: {version!r}"
        raise ValueError(msg)
    normalized = version.strip()
    if normalized in {"1", "1.0", "1.0.0"}:
        return TOMLVersion.V1_0
    if normalized in {"1.1", "1.1.0"}:
        return TOMLVersion.V1_1
    msg = f"Unsupported TOML version: {version!r}"
    raise ValueError(msg)


class InputVersionError(ParseError):
    """A document uses TOML 1.1 syntax while parsed as TOML 1.0.

    This is still a positioned parse error (hence the :class:`ParseError`
    base), with :attr:`feature` naming the TOML 1.1-only construct so callers
    can distinguish a version mismatch from an ordinary syntax error.
    """

    def __init__(self, line: int, col: int, feature: str) -> None:
        self.feature = feature
        message = (
            f"{feature} is only valid in TOML 1.1, but the input is parsed "
            f"as TOML 1.0"
        )
        super().__init__(line, col, message=message)


class OutputVersionError(TOMLKitError):
    """A value or structure cannot be represented in the target output version.

    Unlike :class:`InputVersionError`, this is raised while serializing a
    document whose value model has no lossless spelling in the requested
    version. ``line``/``col`` locate the item inside the rendered document
    when that position is known.
    """

    def __init__(
        self, feature: str, *, line: int | None = None, col: int | None = None
    ) -> None:
        self.line = line
        self.col = col
        self.feature = feature
        location = (
            f" at line {line} col {col}" if line is not None and col is not None else ""
        )
        super().__init__(
            f"Cannot represent {feature} in the requested TOML output version{location}"
        )


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def downgrade_v11_escapes(original: str) -> str:
    """Rewrite TOML 1.1 escapes to their lossless TOML 1.0 spellings.

    ``\\e`` becomes ``\\u001b`` and ``\\xHH`` becomes ``\\u00HH``; both name
    the same scalar value and every TOML 1.0 parser understands the ``\\u``
    form. Other escapes (including an escaped backslash followed by ``e``)
    are copied verbatim, so the rewrite never changes the represented value.
    """
    if "\\" not in original:
        return original

    out: list[str] = []
    i = 0
    n = len(original)
    while i < n:
        ch = original[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue

        nxt = original[i + 1]
        if nxt == "e":
            out.append(r"\u001b")
            i += 2
        elif (
            nxt == "x"
            and i + 3 < n
            and original[i + 2] in _HEX_DIGITS
            and original[i + 3] in _HEX_DIGITS
        ):
            out.append(f"\\u00{original[i + 2 : i + 4].lower()}")
            i += 4
        else:
            # Keep the escape (and its introducer) intact; any payload is
            # copied character by character on later iterations.
            out.append(ch)
            out.append(nxt)
            i += 2

    return "".join(out)


def downgrade_v11_key_path(key_path: str) -> str:
    """Downgrade the quoted parts of a dotted key path / table header.

    A header may mix bare keys and quoted fragments (``a."b".'c'``); only
    the contents of basic (double-quoted) fragments follow the basic-string
    escape rules, so literal fragments and bare keys are left untouched.
    """
    out: list[str] = []
    in_basic = False
    in_literal = False
    i = 0
    n = len(key_path)
    while i < n:
        ch = key_path[i]
        if not in_basic and not in_literal and ch == '"':
            in_basic = True
            out.append(ch)
            i += 1
            continue
        if not in_basic and not in_literal and ch == "'":
            in_literal = True
            out.append(ch)
            i += 1
            continue
        if in_basic and ch == '"':
            in_basic = False
            out.append(ch)
            i += 1
            continue
        if in_literal and ch == "'":
            in_literal = False
            out.append(ch)
            i += 1
            continue
        if in_basic and ch == "\\" and i + 1 < n:
            nxt = key_path[i + 1]
            if nxt == "e":
                out.append(r"\u001b")
                i += 2
                continue
            if nxt == "x" and i + 3 < n and all(
                key_path[i + j] in _HEX_DIGITS for j in (2, 3)
            ):
                out.append(f"\\u00{key_path[i + 2 : i + 4].lower()}")
                i += 4
                continue
            out.append(ch)
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)
