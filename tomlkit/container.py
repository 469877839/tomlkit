from __future__ import annotations

import copy
import math

from collections.abc import Iterator
from typing import TYPE_CHECKING
from typing import Any


if TYPE_CHECKING:
    from typing import Self

from tomlkit._compat import decode
from tomlkit._types import _CustomDict
from tomlkit._utils import merge_dicts
from tomlkit.exceptions import InvalidStringError
from tomlkit.exceptions import KeyAlreadyPresent
from tomlkit.exceptions import NonExistentKey
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT
from tomlkit.items import Comment
from tomlkit.items import InlineTable
from tomlkit.items import Item
from tomlkit.items import Key
from tomlkit.items import Null
from tomlkit.items import SingleKey
from tomlkit.items import String
from tomlkit.items import StringType
from tomlkit.items import Table
from tomlkit.items import Trivia
from tomlkit.items import Whitespace
from tomlkit.items import item as _item


def _is_empty_aot(item: Item) -> bool:
    """Whether ``item`` is an array of tables with no elements left."""
    return isinstance(item, AoT) and not item.body


def _emits_header(key: Key, table: Table, fragment: bool = False) -> bool:
    """Whether rendering ``table`` under ``key`` emits a ``[header]`` line.

    A super table normally renders as nothing but the path prefix of its
    children, so it opens no scope of its own. A table that has been emptied
    (e.g. by deleting the last child of a dotted key) falls back to an
    explicit header, otherwise the key would silently disappear from the
    serialized document while staying in the mapping. That fallback does not
    apply to an out-of-order fragment: the fragments of one key share a single
    header, rendered by whichever part still has content.
    """
    return (
        not table.is_super_table()
        or (
            not fragment
            and not any(
                body_key is not None and not isinstance(body_item, Null)
                for body_key, body_item in table.value.body
            )
        )
        or (
            any(
                not isinstance(v, (Table, AoT, Whitespace, Null))
                for _, v in table.value.body
            )
            and not key.is_dotted()
        )
        or (
            any(
                k is not None and k.is_dotted()
                for k, v in table.value.body
                if isinstance(v, Table)
            )
            and not key.is_dotted()
        )
    )


def _header_less_aots(
    body: list[tuple[Key | None, Item]], prefix: str = ""
) -> list[tuple[str, AoT]]:
    """Every emptied array of tables reachable without crossing a header.

    An emptied array of tables has no ``[[key]]`` header left to render, so it
    falls back to the inline ``key = []`` form -- and TOML reads a bare
    key/value pair into whichever table the closest preceding header opened.
    So the fallbacks of a scope have to be written at the top of that scope,
    with a key relative to it, and that includes the ones sitting inside super
    tables, which emit no header to separate them.

    Returns ``(dotted key, aot)`` pairs, keyed relative to the scope ``body``
    belongs to.
    """
    found: list[tuple[str, AoT]] = []
    for k, v in body:
        if k is None:
            continue
        path = f"{prefix}.{k.as_string()}" if prefix else k.as_string()
        if _is_empty_aot(v):
            found.append((path, v))
        elif isinstance(v, Table) and not _emits_header(k, v):
            found.extend(_header_less_aots(v.value.body, path))
    return found


def _render_empty_aot(key: str, aot: AoT) -> str:
    """Render an emptied array of tables as an inline empty array."""
    return (
        f"{aot.trivia.indent}"
        f"{decode(key)}"
        f" = []"
        f"{aot.trivia.comment_ws}"
        f"{decode(aot.trivia.comment)}"
        f"{aot.trivia.trail or chr(10)}"
    )


class Container(_CustomDict):  # type: ignore[type-arg]
    """
    A container for items within a TOMLDocument.

    This class implements the `dict` interface with copy/deepcopy protocol.
    """

    def __init__(self, parsed: bool = False) -> None:
        self._map: dict[Key, int | tuple[int, ...]] = {}
        self._body: list[tuple[Key | None, Item]] = []
        self._parsed = parsed
        self._table_keys: list[Key] = []
        # number of already-validated fragments and the temp container they
        # were merged into, per out-of-order key; lets parse-time validation
        # resume where the previous pass stopped instead of re-merging every
        # fragment (quadratic) on each append
        self._validation_cache: dict[Key, tuple[int, Container]] = {}
        # superset of the keys mapped to an index tuple, so validating all
        # out-of-order tables doesn't have to scan every key in the map;
        # stale entries are filtered by the per-key isinstance check
        self._out_of_order_keys: set[Key] = set()

    @property
    def body(self) -> list[tuple[Key | None, Item]]:
        return self._body

    def unwrap(self) -> dict[str, Any]:
        """Returns as pure python object (ppo)"""
        unwrapped: dict[str, Any] = {}
        # Resolve each key straight from _map, which already holds the parsed
        # Key objects and their body index, instead of via self.items(): the
        # inherited MutableMapping iteration goes through __getitem__, which
        # rebuilds a SingleKey from the bare string on every key only to throw
        # it away. Out-of-order keys (a tuple index) still go through
        # OutOfOrderTableProxy so their validation (and fragment merge) runs
        # exactly as before. _map is keyed for lookup, not ordered: replacing a
        # value re-inserts its key and moves it last, so take the order from the
        # body index, which is what dumps() and items() follow.
        for key, idx in sorted(
            self._map.items(),
            key=lambda item: item[1][0] if isinstance(item[1], tuple) else item[1],
        ):
            if isinstance(idx, tuple):
                value: Any = OutOfOrderTableProxy(self, idx)
            else:
                value = self._body[idx][1]
            unwrapped[key.key] = value.unwrap() if hasattr(value, "unwrap") else value

        return unwrapped

    @property
    def value(self) -> dict[str, Any]:
        """The wrapped dict value"""
        d: dict[str, Any] = {}
        for k, v in self._body:
            if k is None:
                continue

            key_str = k.key
            val: Any = v.value

            if isinstance(val, Container):
                val = val.value

            if key_str in d:
                merge_dicts(d[key_str], val)
            else:
                d[key_str] = val

        return d

    def parsing(self, parsing: bool) -> None:
        self._parsed = parsing
        self._validation_cache.clear()

        for _, v in self._body:
            if isinstance(v, Table):
                v.value.parsing(parsing)
            elif isinstance(v, AoT):
                for t in v.body:
                    t.value.parsing(parsing)

    def add(self, key: Key | Item | str, item: Any = None) -> Container:
        """
        Adds an item to the current Container.

        :Example:

        >>> # add a key-value pair
        >>> doc.add('key', 'value')
        >>> # add a comment or whitespace or newline
        >>> doc.add(comment('# comment'))
        """
        if item is None:
            if not isinstance(key, (Comment, Whitespace)):
                raise ValueError(
                    "Non comment/whitespace items must have an associated key"
                )

            return self.append(None, key)

        assert not isinstance(key, Item)
        return self.append(key, item)

    def _validate_dotted_key_path(
        self, head: SingleKey, parts: list[SingleKey]
    ) -> None:
        """Validate every prefix of a dotted key against existing entries.

        A dotted key only ever creates implicit (super) tables, so any prefix
        that already renders as an explicit ``[header]`` would be declared
        twice; a prefix that resolves to a value would be replaced by a table.
        Raising before any mutation keeps the document untouched on rejection.
        """
        if head not in self._map:
            return

        container = self._container_for(head)
        for part in parts[:-1]:
            if part not in container._map:
                return
            container = self._container_for(part, container)

    def _container_for(
        self, key: Key, container: Container | None = None
    ) -> Container:
        """The prefix table's own container.

        A dotted key may extend an implicit (super) table -- that is how
        sibling dotted keys accumulate -- but it must not reopen a table that
        renders its own ``[header]``, nor replace a value.
        """
        source = self if container is None else container
        index = source._map[key]
        if isinstance(index, tuple):
            return OutOfOrderTableProxy(source, index)._internal_container
        stored_key, item = source._body[index]
        assert stored_key is not None
        if not isinstance(item, Table) or _emits_header(stored_key, item):
            raise TOMLKitError("Redefinition of an existing table")
        return item.value

    def _handle_dotted_key(self, key: Key, value: Item) -> None:
        if isinstance(value, (Table, AoT)):
            raise TOMLKitError("Can't add a table to a dotted key")
        name, *mid, last = key
        self._validate_dotted_key_path(name, [*mid, last])
        name._dotted = True
        # Non-parsing containers so later user edits use the strict placement
        # rules (e.g. a table replaced by a value must move before following
        # header-rendering siblings).
        table = current = Table(Container(False), Trivia(), False, is_super_table=True)
        for _name in mid:
            _name._dotted = True
            new_table = Table(Container(False), Trivia(), False, is_super_table=True)
            current.append(_name, new_table)
            current = new_table

        last.sep = key.sep
        current.append(last, value)

        self.append(name, table)
        return

    def _get_last_index_before_table(self) -> int:
        last_index = -1
        for i, (k, v) in enumerate(self._body):
            if isinstance(v, Null):
                continue  # Null elements are inserted after deletion

            if isinstance(v, Whitespace) and not v.is_fixed():
                continue

            if isinstance(v, (Table, AoT)) and k is not None and not k.is_dotted():
                break

            if (
                isinstance(v, Table)
                and k is not None
                and k.is_dotted()
                and self._renders_table_header(v)
            ):
                # A dotted-key super table renders inline (`a.b = 1`) only as
                # long as none of its children render a `[table]` header; once
                # one does, anything appended after it would land inside that
                # table's scope.
                break
            last_index = i
        return last_index + 1

    def _renders_table_header(self, table: Table) -> bool:
        if not any(
            body_key is not None and not isinstance(body_item, Null)
            for body_key, body_item in table.value.body
        ):
            # An emptied table renders an explicit header of its own (see
            # _emits_header), so anything appended after it would be captured
            # by that header's scope.
            return True
        for k, v in table.value.body:
            if isinstance(v, AoT):
                return True
            if isinstance(v, Table):
                if k is not None and k.is_dotted() and v.is_super_table():
                    if self._renders_table_header(v):
                        return True
                else:
                    return True
        return False

    def _validate_out_of_order_table(self, key: Key | None = None) -> None:
        if key is None:
            for k in list(self._out_of_order_keys):
                assert k is not None
                self._validate_out_of_order_table(k)
            return
        if key not in self._map:
            return
        current_idx = self._map[key]
        if not isinstance(current_idx, tuple):
            return
        if self._parsed:
            # while parsing, every fragment appended to an out-of-order key
            # triggers a validation pass; resume from the cached temp
            # container so each fragment is merged (and deep-copied) once
            # instead of on every later pass. Fragments are only ever
            # appended during parsing, so a count prefix stays valid; any
            # other mutation clears the cache.
            validated, temp = self._validation_cache.get(key, (0, None))
            if validated > len(current_idx):
                validated, temp = 0, None
            try:
                temp = OutOfOrderTableProxy.validate(
                    self, current_idx[validated:], temp
                )
            except Exception:
                # the temp container may be partially mutated; don't let a
                # caught-and-retried failure resume from a poisoned cache
                self._validation_cache.pop(key, None)
                raise
            self._validation_cache[key] = (len(current_idx), temp)
            return
        OutOfOrderTableProxy.validate(self, current_idx)

    def append(
        self, key: Key | str | None, item: Any, validate: bool = True
    ) -> Container:
        """Similar to :meth:`add` but both key and value must be given."""
        if not isinstance(key, Key) and key is not None:
            key = SingleKey(key)

        if not isinstance(item, Item):
            item = _item(item)

        if key is not None and key.is_multi():
            self._handle_dotted_key(key, item)
            return self

        if isinstance(item, (AoT, Table)) and item.name is None:
            assert isinstance(key, Key)
            item.name = key.key

        prev = self._previous_item()
        prev_ws = isinstance(prev, Whitespace) or ends_with_whitespace(prev)
        if isinstance(item, Table):
            if not self._parsed:
                item.invalidate_display_name()
            if (
                self._body
                and not (self._parsed or item.trivia.indent or prev_ws)
                and key is not None
                and not key.is_dotted()
            ):
                item.trivia.indent = "\n"

        if isinstance(item, AoT) and self._body and not self._parsed:
            item.invalidate_display_name()
            if item and not ("\n" in item[0].trivia.indent or prev_ws):
                item[0].trivia.indent = "\n" + item[0].trivia.indent

        if key is not None and key in self:
            current_idx = self._map[key]
            if isinstance(current_idx, tuple):
                current_body_element = self._body[current_idx[-1]]
            else:
                current_body_element = self._body[current_idx]

            current = current_body_element[1]

            if isinstance(item, Table):
                if not isinstance(current, (Table, AoT)):
                    raise KeyAlreadyPresent(key)

                if item.is_aot_element():
                    # New AoT element found later on
                    # Adding it to the current AoT
                    if not isinstance(current, AoT):
                        current = AoT([current, item], parsed=self._parsed)

                        self._replace(key, key, current)
                    else:
                        current.append(item)

                    return self
                elif isinstance(current, AoT):
                    if not item.is_aot_element():
                        if item.is_super_table() and len(current.body):
                            # A sub-table header such as `[fruit.apple.texture]`
                            # appearing after the array `[[fruit]]` (possibly with
                            # unrelated tables in between) extends the last element
                            # of the array, per the TOML spec.
                            last = current[-1]
                            for k, v in item.value.body:
                                last.value.append(k, v)

                            return self
                        # Tried to define a table after an AoT with the same name.
                        raise KeyAlreadyPresent(key)

                    current.append(item)

                    return self
                elif current.is_super_table():
                    if item.is_super_table():
                        # We need to merge both super tables. A fragment that
                        # renders its own ``[header]`` cannot join an
                        # out-of-order key whose other parts already render
                        # one: the output would declare the header twice.
                        if not self._parsed and _emits_header(key, item):
                            raise KeyAlreadyPresent(key)
                        if (
                            key.is_dotted()
                            or (
                                current_body_element[0] is not None
                                and current_body_element[0].is_dotted()
                            )
                            or self._table_keys[-1] != current_body_element[0]
                        ):
                            if key.is_dotted() and not self._parsed:
                                idx = self._get_last_index_before_table()
                            else:
                                idx = len(self._body)

                            if idx < len(self._body):
                                self._insert_at(idx, key, item)
                                added_idx, inserted = idx, True
                            else:
                                added_idx, inserted = len(self._body), False
                                self._raw_append(key, item)

                            if validate:
                                try:
                                    self._validate_out_of_order_table(key)
                                except Exception:
                                    # The fragment is already in the body, so a
                                    # rejected merge must roll back exactly the
                                    # entry it added before propagating.
                                    self._rollback_appended(
                                        key, added_idx, inserted=inserted
                                    )
                                    raise

                            return self

                        # Merging in place cannot be undone, so validate the
                        # exact same append sequence on a throwaway copy first.
                        assert isinstance(current, Table)
                        if not self._parsed:
                            self._validate_merge(current, item)
                        # Merge the new super table's body into the existing one
                        # in place. Previously this deep-copied `current` before
                        # appending, which is O(size of current) on every merge
                        # and therefore O(n^2) when many subtables share a super
                        # table (e.g. consecutive `[a.b.c]` / `[a.b.d]` headers).
                        # Mutating in place is O(1) per merge. The defensive copy
                        # that protected the out-of-order validation pass has been
                        # moved into OutOfOrderTableProxy (its only consumer).
                        for k, v in item.value.body:
                            current.append(k, v)

                        return self
                    elif (
                        current_body_element[0] is not None
                        and current_body_element[0].is_dotted()
                    ):
                        raise TOMLKitError("Redefinition of an existing table")
                    else:
                        # Merging a concrete table into an existing implicit/super
                        # table is only valid if it does not redefine existing
                        # subtrees via dotted keys and does not change prior types.
                        assert isinstance(current, Table)
                        self._validate_table_candidate(current, item)
                elif not item.is_super_table():
                    raise KeyAlreadyPresent(key)
                else:
                    # An existing concrete table (current) is being extended by
                    # a super-table (item) — e.g. [a] b=1 then [a.b] c=2 out of
                    # order, or [a] b.c=1 then [a.b] d=2.  Validate that the
                    # super-table does not redefine any existing key, raising
                    # early at parse time.  When validation passes, fall through
                    # — _raw_append below will create an out-of-order entry and
                    # preserve table ordering in the document.
                    assert isinstance(current, Table)
                    self._validate_table_candidate(current, item)
                    self._validate_concrete_extension(key, item)
            elif isinstance(item, AoT):
                if not isinstance(current, AoT):
                    # Tried to define an AoT after a table with the same name.
                    raise KeyAlreadyPresent(key)

                for table in item.body:
                    current.append(table)

                return self
            else:
                raise KeyAlreadyPresent(key)

        is_table = isinstance(item, (Table, AoT))
        if (
            key is not None
            and self._body
            and not self._parsed
            and (not is_table or key.is_dotted())
        ):
            # If there is already at least one table in the current container
            # and the given item is not a table, we need to find the last
            # item that is not a table and insert after it
            # If no such item exists, insert at the top of the table
            last_index = self._get_last_index_before_table()

            if last_index < len(self._body):
                after_item = self._body[last_index][1]
                if not (
                    isinstance(after_item, Whitespace)
                    or "\n" in after_item.trivia.indent
                ):
                    after_item.trivia.indent = "\n" + after_item.trivia.indent
                return self._insert_at(last_index, key, item)
            else:
                previous_item = self._body[-1][1]
                if isinstance(previous_item, Table) and previous_item.is_super_table():
                    previous_child = previous_item.value._previous_item()
                    if (
                        previous_child is not None
                        and not isinstance(previous_child, Whitespace)
                        and "\n" in previous_item.trivia.trail
                        and "\n" not in previous_child.trivia.trail
                    ):
                        previous_child.trivia.trail += previous_item.trivia.trail
                if not (
                    isinstance(previous_item, Whitespace)
                    or ends_with_whitespace(previous_item)
                    or "\n" in previous_item.trivia.trail
                ):
                    previous_item.trivia.trail += "\n"

        self._raw_append(key, item)
        if validate and key is not None:
            try:
                self._validate_out_of_order_table(key)
            except Exception:
                # _raw_append already registered the entry; a failed validation
                # must leave no trace, so undo it before propagating.
                self._rollback_appended(key, len(self._body) - 1)
                raise
        return self

    def _raw_append(self, key: Key | None, item: Item) -> None:
        if key is not None and key in self._map:
            current_idx = self._map[key]
            if not isinstance(current_idx, tuple):
                current_idx = (current_idx,)

            current = self._body[current_idx[-1]][1]
            if not isinstance(current, Table):
                raise KeyAlreadyPresent(key)

            self._map[key] = (*current_idx, len(self._body))
            self._out_of_order_keys.add(key)
        elif key is not None:
            self._map[key] = len(self._body)

        self._body.append((key, item))
        if isinstance(item, (Table, AoT)) and key is not None:
            self._table_keys.append(key)

        if key is not None:
            dict.__setitem__(self, key.key, item.value)

    def _remove_at(self, idx: int) -> None:
        key = self._body[idx][0]
        assert key is not None
        index = self._map.get(key)
        if index is None:
            raise NonExistentKey(key)
        self._validation_cache.clear()
        self._body[idx] = (None, Null())

        if isinstance(index, tuple):
            index_list = list(index)
            index_list.remove(idx)
            if len(index_list) == 1:
                self._map[key] = index_list.pop()
            else:
                self._map[key] = tuple(index_list)
        else:
            dict.__delitem__(self, key.key)
            self._map.pop(key)

    def remove(self, key: Key | str) -> Container:
        """Remove a key from the container."""
        if not isinstance(key, Key):
            key = SingleKey(key)

        idx = self._map.pop(key, None)
        if idx is None:
            raise NonExistentKey(key)

        self._validation_cache.clear()
        if isinstance(idx, tuple):
            for i in idx:
                self._body[i] = (None, Null())
        else:
            self._body[idx] = (None, Null())

        dict.__delitem__(self, key.key)

        return self

    def _insert_at(self, idx: int, key: Key | str, item: Any) -> Container:
        if idx > len(self._body) - 1:
            raise ValueError(f"Unable to insert at position {idx}")

        if not isinstance(key, Key):
            key = SingleKey(key)

        item = _item(item)

        if idx > 0:
            previous_item = self._body[idx - 1][1]
            if not (
                isinstance(previous_item, Whitespace)
                or ends_with_whitespace(previous_item)
                or isinstance(item, (AoT, Table))
                or "\n" in previous_item.trivia.trail
            ):
                previous_item.trivia.trail += "\n"

        # Increment indices after the current index
        for k, v in self._map.items():
            if isinstance(v, tuple):
                new_indices = []
                for v_ in v:
                    if v_ >= idx:
                        v_ = v_ + 1

                    new_indices.append(v_)

                self._map[k] = tuple(new_indices)
            elif v >= idx:
                self._map[k] = v + 1

        if key in self._map:
            current_idx = self._map[key]
            if not isinstance(current_idx, tuple):
                current_idx = (current_idx,)
            self._map[key] = (*current_idx, idx)
            self._out_of_order_keys.add(key)
        else:
            self._map[key] = idx
        self._body.insert(idx, (key, item))

        dict.__setitem__(self, key.key, item.value)

        return self

    def item(self, key: Key | str) -> Item | OutOfOrderTableProxy:
        """Get an item for the given key."""
        if not isinstance(key, Key):
            key = SingleKey(key)

        idx = self._map.get(key)
        if idx is None:
            raise NonExistentKey(key)

        if isinstance(idx, tuple):
            # The item we are getting is an out of order table
            # so we need a proxy to retrieve the proper objects
            # from the parent container
            return OutOfOrderTableProxy(self, idx)

        return self._body[idx][1]

    def last_item(self) -> Item | None:
        """Get the last item."""
        if self._body:
            return self._body[-1][1]
        return None

    def _validate_table_candidate(self, current: Table, candidate: Table) -> None:
        for k, v in candidate.value.body:
            if k is None:
                continue

            if k in current.value._map:
                existing = current.value.item(k)
                if isinstance(existing, (Table, AoT)) != isinstance(v, (Table, AoT)):
                    raise KeyAlreadyPresent(k)
                if k.is_dotted() and not (
                    isinstance(existing, Table) and existing.is_super_table()
                ):
                    raise TOMLKitError("Redefinition of an existing table")
                if isinstance(existing, Table) and isinstance(v, Table):
                    if not existing.is_super_table() and not v.is_super_table():
                        # Both sides are concrete `[table]` definitions of the
                        # same name; the table is declared twice.
                        raise KeyAlreadyPresent(k)
                    # One side is still an implicit/super table, so a duplicate
                    # (if any) is nested deeper - keep checking the subtree.
                    self._validate_table_candidate(existing, v)
                continue

            if not k.is_dotted():
                # Even when the candidate key itself is not dotted, an
                # existing dotted key may already use it as a prefix —
                # e.g.  [a] b.c=1 then [a.b] d=2  (b prefixes b.c).
                for existing_key in current.value._map:
                    if existing_key.is_dotted() and next(iter(existing_key)) == k:
                        raise TOMLKitError("Redefinition of an existing table")
                continue

            head = next(iter(k))
            if head in current.value._map:
                raise TOMLKitError("Redefinition of an existing table")

    def _validate_merge(self, current: Table, candidate: Table) -> None:
        """Check two super-table fragments can merge before mutating either.

        The in-place merge cannot be undone, so replay the exact same append
        sequence into a throwaway copy of the current table first; any key
        conflict or type change raises there instead of leaving the live table
        half-merged.
        """
        self._validate_table_candidate(current, candidate)
        temp_table = Table(Container(False), Trivia(), False, is_super_table=True)
        for body_key, body_item in current.value.body:
            temp_table.value.append(body_key, copy.copy(body_item), validate=True)
        for k, v in candidate.value.body:
            temp_table.value.append(k, copy.deepcopy(v), validate=True)
        temp_table.value._validate_out_of_order_table()

    def _validate_concrete_extension(self, key: Key, candidate: Table) -> None:
        """Check a super fragment extending a concrete header-bearing table.

        A fragment that emits its own ``[key]`` header cannot join an existing
        concrete table: the output would declare the header twice. Only a
        headerless fragment (whose body is entirely tables) can extend it the
        way the parser nests ``[a.b]`` under an existing ``[a]``.
        """
        if _emits_header(key, candidate):
            raise KeyAlreadyPresent(key)

    def _rollback_appended(self, key: Key, idx: int, inserted: bool = False) -> None:
        """Undo one ``_raw_append``/``_insert_at`` of ``key`` at body ``idx``.

        The mutation only just happened, so no other entry can reference the
        added body position. Removing it shifts later indices down by one,
        including the tuple fragments of every out-of-order table.
        """
        index = self._map.pop(key)
        if isinstance(index, tuple):
            remaining = tuple(i for i in index if i != idx)
            if len(remaining) == 1:
                self._map[key] = remaining[0]
            elif remaining:
                self._map[key] = remaining

        if not isinstance(index, tuple) or len(index) == 1:
            dict.__delitem__(self, key.key)
            if not inserted and key in self._table_keys:
                # _raw_append registered a table key; drop the last occurrence,
                # which is the fragment being rolled back.
                for position in range(len(self._table_keys) - 1, -1, -1):
                    if self._table_keys[position] == key:
                        del self._table_keys[position]
                        break
        self._out_of_order_keys.discard(key)
        if key in self._map:
            self._out_of_order_keys.add(key)

        del self._body[idx]
        for map_key, map_index in list(self._map.items()):
            if isinstance(map_index, tuple):
                self._map[map_key] = tuple(
                    i - 1 if i > idx else i for i in map_index
                )
            elif map_index > idx:
                self._map[map_key] = map_index - 1

        self._validation_cache.pop(key, None)
        if key in self:
            dict.__setitem__(self, key.key, self.item(key).value)

    def as_string(self) -> str:
        """Render as TOML string."""
        s = ""
        for path, aot in _header_less_aots(self._body):
            s += _render_empty_aot(path, aot)
        body = self._body
        if not self._parsed:
            # An emptied implicit table falls back to an explicit header
            # (see _emits_header). If a later sibling renders inline, that
            # header would capture it, so the emptied table is deferred past
            # the inline entries. Only emptied tables can move: they carry no
            # content, so reordering them changes nothing in the data model
            # and preserves every declared table's position.
            body = self._defer_emptied_tables(body)
        for k, v in body:
            if k is not None:
                if _is_empty_aot(v):
                    continue
                if isinstance(v, Table):
                    if (
                        s.strip(" ")
                        and not s.strip(" ").endswith("\n")
                        and "\n" not in v.trivia.indent
                    ):
                        s += "\n"
                    s += self._render_table(k, v)
                elif isinstance(v, AoT):
                    if (
                        s.strip(" ")
                        and not s.strip(" ").endswith("\n")
                        and "\n" not in v.trivia.indent
                    ):
                        s += "\n"
                    s += self._render_aot(k, v)
                else:
                    s += self._render_simple_item(k, v)
            else:
                s += self._render_simple_item(k, v)

        return s


    def _render_aot(self, key: Key, aot: AoT, prefix: str | None = None) -> str:
        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        cur = ""
        _key = decode(_key)
        for table in aot.body:
            cur += self._render_aot_table(table, prefix=_key)

        return cur


    def _render_aot_table(self, table: Table, prefix: str | None = None) -> str:
        cur = ""
        _key = prefix or ""
        open_, close = "[[", "]]"

        cur += (
            f"{table.trivia.indent}"
            f"{open_}"
            f"{decode(_key)}"
            f"{close}"
            f"{table.trivia.comment_ws}"
            f"{decode(table.trivia.comment)}"
            f"{table.trivia.trail}"
        )

        for path, aot in _header_less_aots(table.value.body):
            cur += _render_empty_aot(path, aot)
        for k, v in table.value.body:
            if k is not None and _is_empty_aot(v):
                continue
            elif isinstance(v, Table):
                assert k is not None
                if v.is_super_table():
                    if k.is_dotted():
                        # A dotted child normally renders inline; only when it
                        # needs its own ``[parent.a]`` header (e.g. it was
                        # emptied) does the header take the enclosing path.
                        if _emits_header(k, v):
                            cur += self._render_table(k, v, prefix=_key)
                        else:
                            cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                assert k is not None
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(k, v)

        return cur


    def _render_simple_item(
        self, key: Key | None, item: Item, prefix: str | None = None
    ) -> str:
        if key is None:
            return item.as_string()

        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        return (
            f"{item.trivia.indent}"
            f"{decode(_key)}"
            f"{key.sep}"
            f"{decode(item.as_string())}"
            f"{item.trivia.comment_ws}"
            f"{decode(item.trivia.comment)}"
            f"{item.trivia.trail}"
        )


    def _first_header_index(self) -> int | None:
        """Index of the first body entry that opens a ``[header]`` scope."""
        for i, (k, v) in enumerate(self._body):
            if k is None or isinstance(v, (Null, Whitespace)):
                continue
            if isinstance(v, AoT):
                return i
            if isinstance(v, Table):
                if isinstance(self._map.get(k), tuple):
                    # An out-of-order table is a header-bearing entry.
                    return i
                if _emits_header(k, v) or self._renders_table_header(v):
                    # A dotted super table renders inline itself, but any
                    # descendant that emits a header opens a scope too.
                    return i
        return None

    @staticmethod
    def _renders_inline_entry(key: Key, item: Item) -> bool:
        """Whether the entry renders inline (a value or a dotted super table)."""
        if isinstance(item, AoT):
            return False
        if isinstance(item, Table):
            return not _emits_header(key, item)
        return True

    @staticmethod
    def _is_header_value(item: Item) -> bool:
        """Whether the item renders its own ``[header]`` (not inline)."""
        return isinstance(item, (Table, AoT)) and not isinstance(
            item, InlineTable
        )

    def _defer_emptied_tables(
        self, body: list[tuple[Key | None, Item]]
    ) -> list[tuple[Key | None, Item]]:
        """Move emptied implicit tables after any later inline entries."""
        leading: list[tuple[Key | None, Item]] = []
        deferred: list[tuple[Key | None, Item]] = []
        for index, (key, item) in enumerate(body):
            if self._is_emptied_implicit_table(key, item) and any(
                self._is_inline_entry(lk, li)
                for lk, li in body[index + 1 :]
            ):
                deferred.append((key, item))
            else:
                leading.append((key, item))
        return leading + deferred

    def _is_emptied_implicit_table(self, key: Key | None, item: Item) -> bool:
        if key is None or not isinstance(item, Table) or isinstance(item, AoT):
            return False
        if isinstance(self._map.get(key), tuple):
            return False
        # An emptied implicit table only ever existed to carry its children:
        # it was created by a dotted key or by a header path, so its key is
        # dotted or the table is a super table. A table that was declared
        # explicitly (its own ``[header]``) is not moved.
        if not (key.is_dotted() or item.is_super_table()):
            return False
        return not any(
            body_key is not None and not isinstance(body_item, Null)
            for body_key, body_item in item.value.body
        )

    def _is_inline_entry(self, key: Key | None, item: Item) -> bool:
        if key is None:
            return not isinstance(item, (Whitespace, Null, Comment))
        if isinstance(item, AoT):
            return False
        if isinstance(item, Table):
            # Whether this entry (or anything below it) emits a header at all.
            return not (
                _emits_header(key, item) or self._renders_table_header(item)
            )
        return True

    def _render_table(
        self,
        key: Key,
        table: Table,
        prefix: str | None = None,
        fragment: bool = False,
    ) -> str:
        cur = ""

        if table.display_name is not None:
            _key = table.display_name
        else:
            _key = key.as_string()

            if prefix is not None:
                _key = prefix + "." + _key

        header_emitted = _emits_header(key, table, fragment=fragment)
        if header_emitted:
            open_, close = "[", "]"
            if table.is_aot_element():
                open_, close = "[[", "]]"

            newline_in_table_trivia = (
                "\n" if "\n" not in table.trivia.trail and len(table.value) > 0 else ""
            )
            cur += (
                f"{table.trivia.indent}"
                f"{open_}"
                f"{decode(_key)}"
                f"{close}"
                f"{table.trivia.comment_ws}"
                f"{decode(table.trivia.comment)}"
                f"{table.trivia.trail}"
                f"{newline_in_table_trivia}"
            )
        elif table.trivia.indent == "\n":
            cur += table.trivia.indent

        if header_emitted:
            # A super table opens no scope, so its fallbacks belong to -- and
            # were already written by -- the nearest enclosing header instead.
            for path, aot in _header_less_aots(table.value.body):
                cur += _render_empty_aot(path, aot)
        for k, v in table.value.body:
            if k is not None and _is_empty_aot(v):
                continue
            elif isinstance(v, Table):
                if (
                    cur.strip(" ")
                    and not cur.strip(" ").endswith("\n")
                    and "\n" not in v.trivia.indent
                ):
                    cur += "\n"
                assert k is not None
                if v.is_super_table():
                    if k.is_dotted() and not key.is_dotted():
                        # A dotted child normally renders inline (``a.b = ...``)
                        # without a prefix, unless it was emptied and now needs
                        # its own ``[parent.a]`` header -- that header has to be
                        # fully qualified from the enclosing table.
                        if _emits_header(k, v):
                            cur += self._render_table(k, v, prefix=_key)
                        else:
                            cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                if (
                    cur.strip(" ")
                    and not cur.strip(" ").endswith("\n")
                    and "\n" not in v.trivia.indent
                ):
                    cur += "\n"
                assert k is not None
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(
                    k, v, prefix=_key if key.is_dotted() else None
                )

        return cur

    def _render_aot(self, key: Key, aot: AoT, prefix: str | None = None) -> str:
        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        cur = ""
        _key = decode(_key)
        for table in aot.body:
            cur += self._render_aot_table(table, prefix=_key)

        return cur

    def _render_aot_table(self, table: Table, prefix: str | None = None) -> str:
        cur = ""
        _key = prefix or ""
        open_, close = "[[", "]]"

        cur += (
            f"{table.trivia.indent}"
            f"{open_}"
            f"{decode(_key)}"
            f"{close}"
            f"{table.trivia.comment_ws}"
            f"{decode(table.trivia.comment)}"
            f"{table.trivia.trail}"
        )

        for path, aot in _header_less_aots(table.value.body):
            cur += _render_empty_aot(path, aot)
        for k, v in table.value.body:
            if k is not None and _is_empty_aot(v):
                continue
            elif isinstance(v, Table):
                assert k is not None
                if v.is_super_table():
                    if k.is_dotted():
                        # A dotted child normally renders inline; only when it
                        # needs its own ``[parent.a]`` header (e.g. it was
                        # emptied) does the header take the enclosing path.
                        if _emits_header(k, v):
                            cur += self._render_table(k, v, prefix=_key)
                        else:
                            cur += self._render_table(k, v)
                    else:
                        cur += self._render_table(k, v, prefix=_key)
                else:
                    cur += self._render_table(k, v, prefix=_key)
            elif isinstance(v, AoT):
                assert k is not None
                cur += self._render_aot(k, v, prefix=_key)
            else:
                cur += self._render_simple_item(k, v)

        return cur

    def _render_simple_item(
        self, key: Key | None, item: Item, prefix: str | None = None
    ) -> str:
        if key is None:
            return item.as_string()

        _key = key.as_string()
        if prefix is not None:
            _key = prefix + "." + _key

        return (
            f"{item.trivia.indent}"
            f"{decode(_key)}"
            f"{key.sep}"
            f"{decode(item.as_string())}"
            f"{item.trivia.comment_ws}"
            f"{decode(item.trivia.comment)}"
            f"{item.trivia.trail}"
        )

    def __len__(self) -> int:
        return dict.__len__(self)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.keys(self))

    # Dictionary methods
    def __getitem__(self, key: Key | str) -> Any:
        item = self.item(key)
        if isinstance(item, Item) and item.is_boolean():
            return item.value

        return item

    def __contains__(self, key: object) -> bool:
        # Native membership test. The inherited ``MutableMapping.__contains__``
        # resolves the value via ``__getitem__``/``item()`` (and builds a
        # ``NonExistentKey`` on every absent key) only to discard it. Resolve the
        # key the same way ``item()`` does -- ``str`` becomes a ``SingleKey``
        # (a non-str/non-``Key`` argument still raises ``TypeError``) -- then
        # probe ``_map`` directly. For an out-of-order table the proxy is still
        # built so its validation runs exactly as before.
        if not isinstance(key, Key):
            key = SingleKey(key)  # type: ignore[arg-type]
        idx = self._map.get(key)
        if idx is None:
            return False
        if isinstance(idx, tuple):
            # during parsing every fragment append re-probes membership;
            # if the validation cache covers exactly these fragments they
            # are already known to merge cleanly, so skip rebuilding the
            # proxy (which is linear in the number of fragments)
            validated, _ = self._validation_cache.get(key, (0, None))
            if not (self._parsed and validated == len(idx)):
                OutOfOrderTableProxy(self, idx)
        return True

    def _item_for_key(
        self, key: Key | str, value: object, parent: Item | None = None
    ) -> Item:
        # Inspect plain strings before conversion, so an explicit String keeps
        # the caller's chosen quoting style.
        if isinstance(value, str) and not isinstance(value, String):
            previous = self.get(key)
            if isinstance(previous, String) and previous._t is StringType.SLL:
                try:
                    return String.from_raw(value, StringType.SLL)
                except InvalidStringError:
                    # Some values need the escaping provided by a basic string.
                    pass
        return _item(value, _parent=parent)

    def __setitem__(self, key: Key | str, value: Any) -> None:
        if key in self:
            old_key = next(filter(lambda k: k == key, self._map))
            value = self._item_for_key(key, value)
            self._replace(old_key, key, value)
        else:
            self.append(key, value)

    def __delitem__(self, key: Key | str) -> None:
        self.remove(key)

    def setdefault(self, key: Key | str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def _replace(self, key: Key | str, new_key: Key | str, value: Item) -> None:
        if not isinstance(key, Key):
            key = SingleKey(key)

        idx = self._map.get(key)
        if idx is None:
            raise NonExistentKey(key)

        self._replace_at(idx, new_key, value)

    def _replace_at(
        self, idx: int | tuple[int, ...], new_key: Key | str, value: Item
    ) -> None:
        value = _item(value)
        self._validation_cache.clear()

        if isinstance(idx, tuple):
            for i in idx[1:]:
                self._body[i] = (None, Null())

            idx = idx[0]

        k, v = self._body[idx]
        assert k is not None
        # A dotted key renders its value inline (e.g. ``a.b = 1``), which is only
        # consistent with a super table. When the replacement value renders with
        # its own ``[header]`` instead (a non-super table, or an array of tables),
        # keeping the dotted key duplicates the prefix onto the header (#524) and
        # lets the header swallow following dotted siblings (#542). Drop the dotted
        # key so the replacement renders as a plain table/array of tables.
        dotted_to_header = k.is_dotted() and (
            isinstance(value, AoT)
            or (isinstance(value, Table) and not value.is_super_table())
        )
        # That new header also captures every sibling that renders inline -- plain
        # values and dotted keys -- if any still follow it (#513), so it must be
        # moved past them, exactly as a value-to-table change already is.
        reposition_dotted = dotted_to_header and any(
            not isinstance(cur_val, (Null, Whitespace))
            and not (
                isinstance(cur_val, (Table, AoT))
                and (cur_key is None or not cur_key.is_dotted())
            )
            for cur_key, cur_val in self._body[idx + 1 :]
        )
        if not isinstance(new_key, Key):
            if (
                self._is_header_value(value) != self._is_header_value(v)
                or new_key != k.key
                or dotted_to_header
            ):
                new_key = SingleKey(new_key)
            else:  # Inherit the sep of the old key
                new_key = k

        del self._map[k]
        self._map[new_key] = idx
        if new_key != k:
            dict.__delitem__(self, k.key)

        if (
            self._is_header_value(value) != self._is_header_value(v)
            or reposition_dotted
        ):
            self.remove(k)
            if self._is_header_value(value):
                # New tables must appear after all entries that render inline:
                # plain values and dotted keys (which are super tables). Skip
                # those and insert before the first real ``[header]`` so the new
                # table cannot swallow a following sibling on round-trip.
                for i in range(idx, len(self._body)):
                    cur_key, cur_val = self._body[i]
                    if isinstance(cur_val, (AoT, Table)) and not (
                        cur_key is not None and cur_key.is_dotted()
                    ):
                        self._insert_at(i, new_key, value)
                        idx = i
                        break
                else:
                    idx = -1
                    self.append(new_key, value)
            else:
                # The replaced table's slot lies in the table region, where a
                # plain value would be captured by any preceding (or
                # following) ``[header]`` on round-trip, so it has to live in
                # the container's header-free prefix. Use append(), which
                # routes to a plain value's usual slot; it must come before
                # every header entry, not merely before the first table.
                idx = -1
                first_header = self._first_header_index()
                if first_header is not None:
                    header_item = self._body[first_header][1]
                    if not (
                        isinstance(header_item, Whitespace)
                        or "\n" in header_item.trivia.indent
                    ):
                        header_item.trivia.indent = "\n" + header_item.trivia.indent
                    self._insert_at(first_header, new_key, value)
                    idx = first_header
                else:
                    self.append(new_key, value)
        else:
            # Copying trivia
            if not isinstance(value, (Whitespace, AoT)):
                value.trivia.indent = v.trivia.indent
                value.trivia.comment_ws = value.trivia.comment_ws or v.trivia.comment_ws
                value.trivia.comment = value.trivia.comment or v.trivia.comment
                value.trivia.trail = v.trivia.trail
            self._body[idx] = (new_key, value)
            if not self._parsed and self._renders_inline_entry(new_key, value):
                # An entry that renders inline stays in whatever header scope
                # precedes it; if one does, move it into the container's
                # header-free prefix so it keeps its own scope.
                first_header = self._first_header_index()
                if first_header is not None and first_header < idx:
                    entry = self._body.pop(idx)
                    self._body.insert(first_header, entry)
                    for map_key, map_index in list(self._map.items()):
                        if map_index == idx:
                            self._map[map_key] = first_header
                        elif isinstance(map_index, tuple):
                            self._map[map_key] = tuple(
                                i + 1 if first_header <= i < idx else i
                                for i in map_index
                            )
                        elif first_header <= map_index < idx:
                            self._map[map_key] = map_index + 1
                    idx = first_header

        if value is not v and hasattr(value, "invalidate_display_name"):
            value.invalidate_display_name()

        if isinstance(value, Table):
            # Insert a cosmetic new line for tables if:
            # - it does not have it yet OR is not followed by one
            # - it is not the last item, or
            # - The table being replaced has a newline
            result = self._previous_item_with_index()
            assert result is not None
            last, _ = result
            idx = last if idx < 0 else idx
            has_ws = ends_with_whitespace(value)
            replace_has_ws = (
                isinstance(v, Table)
                and v.value.body
                and isinstance(v.value.body[-1][1], Whitespace)
            )
            next_ws = idx < last and isinstance(self._body[idx + 1][1], Whitespace)
            if (idx < last or replace_has_ws) and not (next_ws or has_ws):
                value.append(None, Whitespace("\n"))

            assert isinstance(new_key, Key)
            dict.__setitem__(self, new_key.key, value.value)

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, dict):
            return NotImplemented

        return bool(_equal_with_nan(self.value, other))

    def _getstate(self, protocol: int) -> tuple[bool]:
        return (self._parsed,)

    def __reduce__(self) -> tuple[type, tuple[bool], tuple[Any, ...]]:
        return self.__reduce_ex__(2)

    def __reduce_ex__(self, protocol: int) -> tuple[type, tuple[bool], tuple[Any, ...]]:  # type: ignore[override]
        return (
            self.__class__,
            self._getstate(protocol),
            (self._map, self._body, self._parsed, self._table_keys),
        )

    def __setstate__(self, state: tuple[Any, ...]) -> None:
        self._map = state[0]
        self._body = state[1]
        self._parsed = state[2]
        self._table_keys = state[3]
        self._out_of_order_keys = {
            k for k, v in self._map.items() if isinstance(v, tuple)
        }

        for key, item in self._body:
            if key is not None:
                dict.__setitem__(self, key.key, item.value)

    def copy(self) -> Self:
        return copy.copy(self)

    def __copy__(self) -> Self:
        c = self.__class__(self._parsed)
        # A copied document has to be independently editable: sharing the item
        # objects would make trivia edits (comment/indent/trail) on one copy
        # silently rewrite the other. Keys carry no trivia, so they can be
        # shared; the items, index and table bookkeeping get their own copies.
        for key, item in self._body:
            copied_item = copy.copy(item)
            c._body.append((key, copied_item))
            if key is not None:
                dict.__setitem__(c, key.key, copied_item.value)

        c._map.update(self._map)
        c._table_keys.extend(self._table_keys)
        c._out_of_order_keys |= self._out_of_order_keys

        return c

    def _previous_item_with_index(
        self, idx: int | None = None, ignore: tuple[type, ...] = (Null,)
    ) -> tuple[int, Item] | None:
        """Find the immediate previous item before index ``idx``"""
        if idx is None or idx > len(self._body):
            idx = len(self._body)
        for i in range(idx - 1, -1, -1):
            v = self._body[i][-1]
            if not isinstance(v, ignore):
                return i, v
        return None

    def _previous_item(
        self, idx: int | None = None, ignore: tuple[type, ...] = (Null,)
    ) -> Item | None:
        """Find the immediate previous item before index ``idx``.
        If ``idx`` is not given, the last item is returned.
        """
        prev = self._previous_item_with_index(idx, ignore)
        return prev[-1] if prev else None


class OutOfOrderTableProxy(_CustomDict):  # type: ignore[type-arg]
    @staticmethod
    def validate(
        container: Container,
        indices: tuple[int, ...],
        temp_container: Container | None = None,
    ) -> Container:
        """Validate out of order tables in the given container"""
        # Append all items to a temp container to see if there is any error.
        # We deep-copy each value before appending: appending a super table
        # merges it in place into a matching one already in temp_container, and
        # those values are the live subtables of `container`. Without the copy
        # the merge would mutate the caller's tables (and corrupt a later
        # validation pass). The container merge itself is now copy-free for
        # speed, so this is where the isolation lives.
        # Passing a previous pass's temp container back in (with the already
        # validated indices stripped from `indices`) resumes the validation
        # incrementally.
        if temp_container is None:
            temp_container = Container(True)
        for i in indices:
            _, item = container._body[i]

            if isinstance(item, Table):
                for k, v in item.value.body:
                    temp_container.append(k, copy.deepcopy(v), validate=True)

        temp_container._validate_out_of_order_table()
        return temp_container

    def __init__(self, container: Container, indices: tuple[int, ...]) -> None:
        self._container = container
        self._internal_container = Container(True)
        self._tables: list[Table] = []
        self._tables_map: dict[Key, list[int]] = {}

        for i in indices:
            _, _item = self._container._body[i]

            if isinstance(_item, Table):
                self._tables.append(_item)
                table_idx = len(self._tables) - 1
                for k, v in _item.value.body:
                    merged = self._merge_aot_fragment(k, v)
                    if merged is None:
                        self._internal_container._raw_append(k, v)
                    else:
                        v = merged
                    key_indices = self._tables_map.setdefault(k, [])  # type: ignore[arg-type]
                    if table_idx not in key_indices:
                        key_indices.append(table_idx)
                    if k is not None:
                        dict.__setitem__(self, k.key, v)

        self._internal_container._validate_out_of_order_table()

    def _merge_aot_fragment(self, key: Key | None, item: Item) -> AoT | None:
        """
        Merge an array-of-tables fragment from a later out-of-order table part.

        An AoT whose elements are split across out-of-order parts of the same
        table arrives here once per part; ``_raw_append`` only knows how to
        chain duplicate ``Table`` parts and would raise ``KeyAlreadyPresent``.
        The fragments are presented as a new merged ``AoT`` referencing the
        live element tables, without mutating either fragment (the parts keep
        rendering their own elements).

        Returns the merged ``AoT``, or ``None`` if this is not such a fragment.
        """
        internal = self._internal_container
        if key is None or not isinstance(item, AoT) or key not in internal._map:
            return None
        idx = internal._map[key]
        if isinstance(idx, tuple):
            # A tuple index means the key already resolved to several body
            # positions, which here only happens for a degenerate collision
            # (e.g. a non-AoT part sharing the key). Three or more genuine AoT
            # fragments do not reach this branch: each later fragment merges
            # into the single growing AoT below, so ``idx`` stays an int.
            return None
        existing = internal._body[idx][1]
        if not isinstance(existing, AoT):
            return None

        merged = AoT([*existing.body, *item.body], parsed=True)
        internal._body[idx] = (internal._body[idx][0], merged)
        dict.__setitem__(internal, key.key, merged.value)
        return merged

    def unwrap(self) -> dict[str, Any]:
        return self._internal_container.unwrap()

    @property
    def value(self) -> dict[str, Any]:
        return self._internal_container.value

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return repr(self.value)

    def __contains__(self, key: object) -> bool:
        # Native membership test. The inherited ``MutableMapping.__contains__``
        # resolves the value via ``__getitem__`` (and builds a ``NonExistentKey``
        # on every absent key) only to discard it. Probe the internal container
        # directly -- the same predicate ``__getitem__`` already uses -- which is
        # itself a native ``_map`` lookup that still rebuilds the proxy for an
        # out-of-order key so its validation runs exactly as before.
        return key in self._internal_container

    def __getitem__(self, key: Key | str) -> Any:
        if key not in self._internal_container:
            raise NonExistentKey(key)

        return self._internal_container[key]

    def __setitem__(self, key: Key | str, value: Any) -> None:
        from .items import item as _item_fn

        def _is_table_or_aot(it: Any) -> bool:
            return isinstance(_item_fn(it), (Table, AoT))

        _key: Key = key if isinstance(key, Key) else SingleKey(key)

        if _key in self._tables_map:
            # Overwrite the first occurrence and remove this key from later parts.
            map_indices = self._tables_map[_key]
            target = self._tables[map_indices[0]]
            old_value = target[key]
            if _is_table_or_aot(old_value) and not _is_table_or_aot(value):
                # A table cannot be replaced by a value in the part that
                # renders it as a header. Rehearse the rearrangement on a
                # detached copy first, so a rejected edit leaves the original
                # table in place instead of deleting it.
                rehearsal = copy.deepcopy(self)
                rehearsal._switch_table_to_value(_key, value)
                self._switch_table_to_value(_key, value)
                self._internal_container[_key] = value
                if key is not None:
                    dict.__setitem__(self, key, value)
                return
            while len(map_indices) > 1:
                table = self._tables[map_indices.pop()]
                del table[key]
                if not table:
                    self._remove_table(table)
            self._tables[map_indices[0]][key] = value
        elif self._tables:
            # A new entry needs a part whose rendering can carry it: plain
            # values need an inline-capable part, and a new table needs one
            # that renders a header of its own -- a headerless fragment would
            # make the new ``[key]`` land in the enclosing scope instead.
            table_index = self._choose_value_part(
                require_header=_is_table_or_aot(value)
            )
            if table_index is None:
                raise KeyAlreadyPresent(_key)
            self._tables[table_index][key] = value
            self._tables_map[_key] = [table_index]
        else:
            self._container[key] = value

        self._internal_container[key] = value
        if key is not None:
            dict.__setitem__(self, key, value)

    def _choose_value_part(
        self, exclude: Key | None = None, require_header: bool = False
    ) -> int | None:
        """Pick the fragment that must host a new entry.

        Plain values land in the first part that already carries them. If none
        does, they must go into a part that renders its own ``[header]``; a
        headerless fragment (e.g. one nested through a dotted key) cannot take
        one, since forcing a header there duplicates the enclosing table's
        path. A new *table* always requires a header-rendering part.

        ``exclude`` is a key about to be removed from every part; a part that
        would be left with nothing cannot host the entry either.
        """

        def usable(table: Table) -> bool:
            if any(k is not None and k != exclude for k in table.value._map):
                return True
            fragment_key = self._key_of_table(table)
            return fragment_key is not None and _emits_header(fragment_key, table)

        def has_plain_value(table: Table) -> bool:
            for k in table.value._map:
                if k is None or k == exclude:
                    continue
                if not isinstance(table.value.item(k), (Table, AoT)):
                    return True
            return False

        if not require_header:
            for i, table in enumerate(self._tables):
                if usable(table) and has_plain_value(table):
                    return i
        for i, table in enumerate(self._tables):
            if not usable(table):
                continue
            fragment_key = self._key_of_table(table)
            if fragment_key is not None and _emits_header(fragment_key, table):
                return i
        return None

    def _key_of_table(self, table: Table) -> Key | None:
        """The key under which ``table`` sits in the backing container."""
        for body_key, body_item in self._container._body:
            if body_key is not None and body_item is table:
                return body_key
        return None

    def _switch_table_to_value(self, _key: Key, value: Any) -> None:
        """Replace a table-valued key with a plain value across its parts."""
        map_indices = self._tables_map[_key]
        target = self._tables[map_indices[0]]
        target_index = self._choose_value_part()
        if target_index is None:
            raise KeyAlreadyPresent(_key)
        host = self._tables[target_index]
        while len(map_indices) > 1:
            table = self._tables[map_indices.pop()]
            del table[_key]
            if not table:
                self._remove_table(table)
        del target[_key]
        if not target and host is not target:
            # An emptied part that is not the host has nothing left to render.
            # The host itself is kept even when it empties: it is about to
            # receive the value.
            self._remove_table(target)
        host[_key] = value
        self._rebuild_tables_map()

    def _rebuild_tables_map(self) -> None:
        """Recompute key -> part indices after fragments were added or dropped."""
        self._tables_map.clear()
        for i, table in enumerate(self._tables):
            for k in table.value._map:
                if k is None:
                    continue
                indices = self._tables_map.setdefault(k, [])
                if i not in indices:
                    indices.append(i)

    def _remove_table(self, table: Table) -> None:
        """Remove table from the parent container"""
        self._tables.remove(table)
        for idx, body_item in enumerate(self._container._body):
            if body_item[1] is table:
                self._container._remove_at(idx)
                break

    def __delitem__(self, key: Key | str) -> None:
        _key: Key = key if isinstance(key, Key) else SingleKey(key)
        if _key not in self._tables_map:
            raise NonExistentKey(key)

        for i in reversed(self._tables_map[_key]):
            table = self._tables[i]
            del table[key]
            if not table and len(self._tables) > 1:
                self._remove_table(table)

        del self._tables_map[_key]
        del self._internal_container[key]
        if key is not None:
            dict.__delitem__(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(dict.keys(self))

    def __len__(self) -> int:
        return dict.__len__(self)

    def setdefault(self, key: Key | str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]


def ends_with_whitespace(it: Any) -> bool:
    """Returns ``True`` if the given item ``it`` is a ``Table`` or ``AoT`` object
    ending with a ``Whitespace``.
    """
    if isinstance(it, Whitespace):
        return True
    if isinstance(it, Table):
        previous = it.value._previous_item()
        return previous is not None and ends_with_whitespace(previous)
    return isinstance(it, AoT) and len(it) > 0 and ends_with_whitespace(it[-1])


def _equal_with_nan(left: Any, right: Any) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_equal_with_nan(left[k], right[k]) for k in left)

    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_equal_with_nan(l, r) for l, r in zip(left, right))  # noqa: E741

    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True

    return bool(left == right)
