from tomlkit.container import Container
from tomlkit.toml_version import TOMLVersion
from tomlkit.toml_version import normalize_version


class TOMLDocument(Container):
    """
    A TOML document.
    """

    def __init__(
        self,
        parsed: bool = False,
        version: TOMLVersion | str = TOMLVersion.V1_0,
    ) -> None:
        super().__init__(parsed)
        self._version = normalize_version(version)

    @property
    def version(self) -> TOMLVersion:
        """The TOML spec version used to parse (and, by default, to write)."""
        return self._version

    @version.setter
    def version(self, version: TOMLVersion | str) -> None:
        self._version = normalize_version(version)
