"""Incremental rendering support.

The TOML renderers in :mod:`tomlkit.items` and :mod:`tomlkit.container` never
build the whole document in memory: they push small fragments (at most one
scalar literal, one header or one piece of trivia at a time) through a
``write(fragment)`` sink.

:class:`StringWriter` is the buffered sink used by ``as_string()`` and
:func:`tomlkit.dumps`; :func:`tomlkit.iter_dumps` and :func:`tomlkit.dump`
plug in sinks that forward every fragment straight to the caller or a file,
so a large document can be written out in chunks without ever holding the
rendered output as one string.

A sink can abort rendering at any time by raising an exception. Rendering
never mutates the document, so an aborted run can simply be retried.
"""

from __future__ import annotations

from collections.abc import Callable


# A sink for rendered fragments.
Writer = Callable[[str], None]


class StringWriter:
    """Accumulate rendered fragments and return the full string at the end."""

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, fragment: str) -> None:
        self._parts.append(fragment)

    def getvalue(self) -> str:
        return "".join(self._parts)
