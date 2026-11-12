import os
import re

from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

from tomlkit.api import loads
from tomlkit.toml_document import TOMLDocument


if TYPE_CHECKING:
    from _typeshed import StrPath as _StrPath
else:
    from typing import Union

    _StrPath = Union[str, os.PathLike]


class TOMLFile:
    """
    Represents a TOML file.

    :param path: path to the TOML file
    """

    def __init__(self, path: _StrPath) -> None:
        self._path = path
        self._linesep: str = os.linesep

    def read(
        self, *, parse_float: Callable[[str], Any] | None = None
    ) -> TOMLDocument:
        """Read the file content as a :class:`tomlkit.toml_document.TOMLDocument`.

        :param parse_float: strategy used to read float literals, see
            :func:`tomlkit.parse`.
        """
        with open(self._path, encoding="utf-8", newline="") as f:
            content = f.read()

            # check if consistent line endings
            num_newline = content.count("\n")
            if num_newline > 0:
                num_win_eol = content.count("\r\n")
                if num_win_eol == num_newline:
                    self._linesep = "\r\n"
                    content = content.replace("\r\n", "\n")
                elif num_win_eol == 0:
                    self._linesep = "\n"
                else:
                    self._linesep = "mixed"

            return loads(content, parse_float=parse_float)

    def write(self, data: TOMLDocument) -> None:
        """Write the TOMLDocument to the file."""
        content = data.as_string()

        # apply linesep
        if self._linesep == "\n":
            content = content.replace("\r\n", "\n")
        elif self._linesep == "\r\n":
            content = re.sub(r"(?<!\r)\n", "\r\n", content)

        with open(self._path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
