"""Stateful regression tests for document editing.

The checks here deliberately look at more than the Python mapping view: every
step compares the mapping, the serialized TOML as read back by the stdlib
``tomllib`` parser, the internal ``Container`` body/key index, the
``OutOfOrderTableProxy`` view, and previously retained ``Item`` references.

The operation sequences are generated from a fixed seed.  On failure the
seed, the generated operations and the smallest reproducing prefix are
written to ``tests/.stateful_failures/`` and a greedy shrinker is available to
reduce the sequence (and the starting document) to a short reproduction.
"""

from __future__ import annotations

import copy
import json
import math
import random
import tomllib

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import tomlkit

from tomlkit import dumps
from tomlkit import parse
from tomlkit.container import Container
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.exceptions import KeyAlreadyPresent
from tomlkit.exceptions import NonExistentKey
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import AoT
from tomlkit.items import InlineTable
from tomlkit.items import Item
from tomlkit.items import Key
from tomlkit.items import Null
from tomlkit.items import Table
from tomlkit.items import Whitespace


SEEDS = (20240101, 20240102, 20240103)
FAILURE_DIR = Path(__file__).parent / ".stateful_failures"
ILLEGAL_ERRORS = (TOMLKitError, ValueError, TypeError)
# Errors that mean "this recorded operation no longer applies to the document"
# (its holder or key was removed by an earlier, since-dropped operation).
INAPPLICABLE_ERRORS = (KeyError, IndexError, TypeError, AttributeError)


class Failure(AssertionError):
    """An invariant violation found by the harness itself.

    Only this type counts as "the failure reproduces" when shrinking, so a
    candidate sequence that merely breaks on a stale path or an inapplicable
    operation is not mistaken for a reproduction.
    """


def _equal(left: Any, right: Any) -> bool:
    """Structural equality that treats NaN as equal to NaN."""
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True

    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equal(left[k], right[k]) for k in left
        )

    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _equal(a, b) for a, b in zip(left, right)
        )

    return bool(left == right)


def _merge_into(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge fragment dicts the way an out-of-order proxy combines parts."""
    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _merge_into(target[key], value)
        else:
            target[key] = value


def check_structure(container: Container, path: str = "") -> list[str]:
    """Check ``body`` / ``_map`` / keyed views agree for one container.

    For every key, each body index the key maps to must hold that key, and
    every keyed, non-deleted body entry must be reachable from the key map.
    The ``dict`` view (which backs ``keys()``, ``len()`` and ``unwrap()``) must
    agree with the key map as well.
    """
    problems: list[str] = []

    for key, index in container._map.items():
        indices = index if isinstance(index, tuple) else (index,)
        if not indices:
            problems.append(f"{path}: key {key!r} mapped to an empty index")
            continue
        for i in indices:
            if not 0 <= i < len(container._body):
                problems.append(f"{path}: key {key!r} -> out-of-range body[{i}]")
                continue
            body_key, body_item = container._body[i]
            if body_key != key or isinstance(body_item, Null):
                problems.append(
                    f"{path}: key {key!r} -> body[{i}] holds {body_key!r}"
                    f" / {type(body_item).__name__}"
                )

    for i, (body_key, body_item) in enumerate(container._body):
        if body_key is None or isinstance(body_item, Null):
            continue
        if body_key not in container._map:
            problems.append(f"{path}: body[{i}] key {body_key!r} missing from _map")

    map_keys = {key.key for key in container._map}
    view_keys = set(dict.keys(container))
    if map_keys != view_keys:
        problems.append(
            f"{path}: key map {sorted(map_keys)} != dict view {sorted(view_keys)}"
        )

    for key, index in container._map.items():
        indices = index if isinstance(index, tuple) else (index,)
        if len(indices) < 2:
            continue
        fragment_keys: set[str] = set()
        merged: dict[str, Any] = {}
        for i in indices:
            fragment = container._body[i][1]
            if not isinstance(fragment, Table):
                problems.append(
                    f"{path}: out-of-order key {key!r} -> non-table fragment "
                    f"{type(fragment).__name__} at body[{i}]"
                )
                continue
            fragment_keys.update(
                k.key for k in fragment.value._map if k is not None
            )
            _merge_into(merged, fragment.unwrap())
        try:
            proxy: Any = OutOfOrderTableProxy(container, tuple(indices))
            proxy_keys = set(dict.keys(proxy))
            proxy_value = proxy.unwrap()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            problems.append(f"{path}: proxy for {key!r} raised {exc!r}")
            continue
        # The proxy is the public view of an out-of-order table: it must expose
        # exactly the union of its fragments, in both mapping and value form.
        if proxy_keys != fragment_keys:
            problems.append(
                f"{path}: proxy view {sorted(proxy_keys)} != "
                f"fragments {sorted(fragment_keys)}"
            )
        if not _equal(proxy_value, merged):
            problems.append(
                f"{path}: proxy value {proxy_value!r} != merged fragments {merged!r}"
            )

    for _, item in container._body:
        if isinstance(item, Table):
            problems.extend(check_structure(item.value, f"{path}.table"))
        elif isinstance(item, AoT):
            for index, element in enumerate(item.body):
                problems.extend(
                    check_structure(element.value, f"{path}.aot[{index}]")
                )

    return problems


def check_invariants(
    doc: tomlkit.TOMLDocument, retained: list[tuple[str, Item]], label: str
) -> None:
    """Assert the document stays coherent across body, map and proxy views."""
    problems = check_structure(doc, "root")
    if problems:
        raise Failure(f"[{label}] structure broken:\n" + "\n".join(problems))

    serialized = dumps(doc)
    try:
        from_tomllib = tomllib.loads(serialized)
    except Exception as exc:  # noqa: BLE001 - report the full document
        raise Failure(f"[{label}] output is not valid TOML: {exc}\n---\n{serialized}")

    try:
        unwrapped = doc.unwrap()
    except Exception as exc:  # noqa: BLE001
        raise Failure(f"[{label}] unwrap() raised {type(exc).__name__}: {exc}")

    if not _equal(from_tomllib, unwrapped):
        raise Failure(
            f"[{label}] tomllib view {from_tomllib!r} != unwrap() {unwrapped!r}\n"
            f"---\n{serialized}"
        )

    reparsed = parse(serialized)
    if not _equal(reparsed.unwrap(), unwrapped):
        raise Failure(
            f"[{label}] tomlkit round trip diverged\n---\n{serialized}"
        )

    # The mapping view and the internal map must describe the same keys,
    # including out-of-order tables resolved through the proxy.
    if set(doc) != set(unwrapped):
        raise Failure(
            f"[{label}] mapping keys {sorted(doc)} != unwrap keys "
            f"{sorted(unwrapped)}"
        )

    _check_retained(doc, retained, label)


def _check_retained(
    doc: tomlkit.TOMLDocument,
    retained: list[tuple[str, Item]],
    label: str,
) -> None:
    """Every retained key still resolves to the same object.

    The harness prunes a retained record when its key is deleted or replaced
    through the container API, so any surviving record must identify the exact
    live object: trivia or value edits through either path must describe one
    item, never two that silently diverge.
    """
    for path, item in retained:
        parts = path.split(".")
        holder: Any = doc
        for part in parts[:-1]:
            holder = holder[int(part)] if part.isdigit() else holder[part]
        live = _item_of(holder, parts[-1])
        if isinstance(live, OutOfOrderTableProxy):
            continue
        if item is not live:
            raise Failure(
                f"[{label}] retained {path!r} object diverged from the "
                "container's item"
            )


def _item_of(holder: Any, key: str) -> Any:
    """The item stored under ``key``, resolving through an out-of-order proxy."""
    if isinstance(holder, AoT):
        return holder[int(key)]
    if isinstance(holder, OutOfOrderTableProxy):
        return holder[key]
    return holder.item(key)


STARTER_DOCS: dict[str, str] = {
    "values": 'alpha = 1\nbeta = "two" # keep me\ngamma = true\n',
    "dotted": "a.b = 1\nouter.inner = {x = 1, y = 2} # inline\n",
    "tables": "root = 0 # top\n\n[t] # table\nx = 1\ny = 2 # leaf\n",
    "out_of_order": "[a.b]\nx = 1 # first\n[c]\ny = 2\n\n[a.d]\nz = 3 # later\n",
    "array_of_tables": "[[p]]\nx = 1 # one\n\n[[p]]\ny = 2\n",
    "mixed": (
        "top = 1\n\n"
        "[a.b]\nx = 1\n\n"
        "[c]\ny = 2 # mid\n\n"
        "[a.d]\nz = 3\n\n"
        "[[a.items]]\nname = 'first'\n\n"
        "[[a.items]]\nname = 'second'\n\n"
        "[t]\nq = {k = 1} # inline value\n"
    ),
}


@dataclass(frozen=True)
class Op:
    """One recorded edit, replayable from ``apply_op``."""

    name: str
    args: tuple[Any, ...] = ()

    def replayable(self) -> dict[str, Any]:
        return {"name": self.name, "args": [_repr_arg(a) for a in self.args]}


def _repr_arg(value: Any) -> Any:
    if isinstance(value, (str, int, bool, float, type(None))):
        return value
    if isinstance(value, tuple):
        return {"tuple": [_repr_arg(v) for v in value]}
    if isinstance(value, dict):
        return {"dict": {k: _repr_arg(v) for k, v in value.items()}}
    if isinstance(value, list):
        return {"list": [_repr_arg(v) for v in value]}
    return repr(value)


def _holders(doc: tomlkit.TOMLDocument) -> list[tuple[str, Any]]:
    """All mapping-like holders reachable in the document, with dotted paths."""
    found: list[tuple[str, Any]] = [("", doc)]

    def visit(holder: Any, prefix: str) -> None:
        for key in list(holder.keys()):
            value = holder[key]
            child_path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (Table, InlineTable, OutOfOrderTableProxy)):
                found.append((child_path, value))
                visit(value, child_path)
            elif isinstance(value, AoT):
                for index, element in enumerate(value):
                    element_path = f"{child_path}.{index}"
                    found.append((element_path, element))
                    visit(element, element_path)

    visit(doc, "")
    return found


def _aots(doc: tomlkit.TOMLDocument) -> list[tuple[str, AoT]]:
    found: list[tuple[str, AoT]] = []
    for path, holder in _holders(doc):
        for key in list(holder.keys()):
            value = holder[key]
            if isinstance(value, AoT):
                child_path = f"{path}.{key}" if path else key
                found.append((child_path, value))
    return found


def _new_scalar(rng: random.Random) -> Any:
    choice = rng.randrange(6)
    if choice == 0:
        return rng.randrange(-100, 100)
    if choice == 1:
        return rng.choice(["alpha", "beta", "can't", 'say "hi"', ""])
    if choice == 2:
        return rng.choice([True, False])
    if choice == 3:
        return rng.choice([1.5, -0.25, float("nan")])
    if choice == 4:
        return tomlkit.integer(rng.randrange(0, 999))
    return tomlkit.string(f"raw-{rng.randrange(20)}", literal=rng.choice([True, False]))


def _new_inline(rng: random.Random) -> dict[str, Any]:
    return {f"k{rng.randrange(4)}": _new_scalar(rng) for _ in range(rng.randrange(1, 3))}


def _new_table_fragment(rng: random.Random) -> Table:
    table = tomlkit.table(is_super_table=True)
    for _ in range(rng.randrange(1, 3)):
        key = rng.choice(["b", "d", "e", "new"])
        if rng.random() < 0.4:
            child = tomlkit.table()
            child[f"v{rng.randrange(5)}"] = _new_scalar(rng)
            table[key] = child
        else:
            table[key] = _new_scalar(rng)
    return table


class StateMachine:
    """Generates and applies edits while keeping a recorded op sequence."""

    def __init__(self, starter: str, seed: int, steps: int) -> None:
        self.starter = starter
        self.seed = seed
        self.steps = steps
        self.ops: list[Op] = []
        self.doc = parse(starter)
        self.rng = random.Random(seed)
        self.retained: list[tuple[str, Item]] = []

    def run(self) -> None:
        for step in range(self.steps):
            op = self._generate()
            self._apply(op, step)
            check_invariants(self.doc, self.retained, f"seed={self.seed} step={step} {op.name}")

    def _new_value(self) -> Any:
        roll = self.rng.random()
        if roll < 0.5:
            return _new_scalar(self.rng)
        if roll < 0.75:
            return _new_inline(self.rng)
        table = tomlkit.table(is_super_table=self.rng.random() < 0.5)
        table[f"v{self.rng.randrange(5)}"] = _new_scalar(self.rng)
        return table

    # -- reference bookkeeping ------------------------------------------------

    def _holder(self, path: str) -> Any:
        holder: Any = self.doc
        if path:
            for part in path.split("."):
                holder = holder[int(part)] if part.isdigit() else holder[part]
        return holder

    def _holder_path_alive(self, path: str) -> bool:
        try:
            self._holder(path)
        except (KeyError, IndexError, TypeError):
            return False
        return True

    def _full_path(self, holder_path: str, key: str) -> str:
        return f"{holder_path}.{key}" if holder_path else key

    def _prune(self, full_path: str) -> None:
        prefix = full_path + "."
        self.retained[:] = [
            (path, item)
            for path, item in self.retained
            if path != full_path and not path.startswith(prefix)
        ]

    def _maybe_retain(self, full_path: str) -> None:
        holder_path, _, key = full_path.rpartition(".")
        item = _item_of(self._holder(holder_path), key)
        if isinstance(item, Item) and not isinstance(
            item, (Table, InlineTable, AoT, Whitespace)
        ):
            if not any(path == full_path for path, _ in self.retained):
                self.retained.append((full_path, item))

    # -- operation generation -------------------------------------------------

    def _generate(self) -> Op:
        kind = self.rng.randrange(12)
        # Holders inside an AoT element use a nested dotted-key header scope
        # that the table model cannot round-trip when a child becomes a
        # header table (a pre-existing rendering limitation); their AoT
        # element operations are still covered via the aot_* operations.
        holders = [
            (path, holder)
            for path, holder in _holders(self.doc)
            if not self._is_inside_aot(path)
        ]
        holder_path, holder = self.rng.choice(holders)
        keys = [key for key in holder.keys() if not isinstance(holder[key], AoT)]
        aots = _aots(self.doc)

        if kind == 0 and keys:
            return Op("set_existing", (holder_path, self.rng.choice(keys)))
        if kind == 1:
            return Op("set_new", (holder_path, f"fresh{self.rng.randrange(8)}"))
        if kind == 2 and keys:
            return Op("delete", (holder_path, self.rng.choice(keys)))
        if kind == 3:
            return Op("delete_missing", (holder_path, f"missing{self.rng.randrange(5)}"))
        if kind == 4:
            return Op(
                "set_dotted",
                (holder_path, self.rng.choice(["d.k", "a.b", "dot.chain"])),
            )
        if kind == 5 and isinstance(holder, (tomlkit.TOMLDocument, Table)):
            return Op("append_fragment", (holder_path, "a"))
        if kind == 6 and keys:
            return Op("replace_with_inline", (holder_path, self.rng.choice(keys)))
        if kind == 7 and aots:
            aot_path, _ = self.rng.choice(aots)
            return Op(
                self.rng.choice(
                    ["aot_append", "aot_insert", "aot_set", "aot_delete", "aot_bad_set"]
                ),
                (aot_path,),
            )
        if kind == 8 and self.retained:
            path, _ = self.rng.choice(self.retained)
            return Op("trivia_edit", (path, f"# note {self.rng.randrange(20)}"))
        if kind == 9 and keys:
            return Op("setdefault", (holder_path, self.rng.choice(keys + ["fresh0"])))
        if kind == 10:
            return Op(self.rng.choice(["shallow_copy", "deep_copy"]), ())
        return Op("add_comment", (holder_path, f"# section {self.rng.randrange(20)}"))

    def _apply(self, op: Op, step: int, tolerant: bool = False) -> None:
        try:
            getattr(self, "_op_" + op.name)(*op.args)
        except INAPPLICABLE_ERRORS:
            if not tolerant:
                raise
            # A shrunk sequence can reference a holder an earlier (dropped)
            # operation created, so the step no longer applies. Skip it while
            # replaying the reduced sequence; the strict generated run still
            # surfaces such errors.
        self.ops.append(op)

    # -- operations -----------------------------------------------------------

    def _op_set_existing(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        full_path = self._full_path(holder_path, key)
        self._prune(full_path)
        value = self._new_value()
        if self._is_dotted_scope(holder_path):
            return
        self._try_illegal(lambda: holder.__setitem__(key, value))
        self._maybe_retain(full_path)

    def _op_set_new(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        self._prune(self._full_path(holder_path, key))
        value = self._new_value()
        if self._is_dotted_scope(holder_path):
            return
        self._try_illegal(lambda: holder.__setitem__(key, value))

    def _op_replace_with_inline(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        full_path = self._full_path(holder_path, key)
        self._prune(full_path)
        if self._is_dotted_scope(holder_path):
            return
        before = dumps(holder)

        def action() -> None:
            inline = tomlkit.inline_table()
            inline["inner"] = self.rng.randrange(10)
            holder[key] = inline

        try:
            action()
        except ILLEGAL_ERRORS:
            if dumps(holder) != before:
                raise Failure("a failed inline-table replacement changed the holder")
        else:
            container = getattr(holder, "value", holder)
            if isinstance(container, Container) and check_structure(
                container, full_path
            ):
                raise Failure("inline-table replacement broke body/map consistency")

    def _op_delete(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        full_path = self._full_path(holder_path, key)
        if self._is_dotted_scope(holder_path):
            # Emptying a dotted table makes it render an explicit header that
            # can capture later inline entries: the same pre-existing scope
            # limitation excluded for the set operations.
            return
        del holder[key]
        self._prune(full_path)

    def _op_delete_missing(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        with pytest.raises(NonExistentKey):
            del holder[key]

    def _op_set_dotted(self, holder_path: str, dotted: str) -> None:
        holder = self._holder(holder_path)
        if isinstance(holder, OutOfOrderTableProxy):
            return
        full_path = self._full_path(holder_path, dotted)
        # A dotted key takes a plain value, never a table, and it may legally
        # collide with an existing key, so the edit is treated as fallible.
        self._try_illegal(
            lambda: holder.append(tomlkit.key(dotted.split(".")), _new_scalar(self.rng))
        )
        self._prune(full_path)

    def _op_add_comment(self, holder_path: str, comment: str) -> None:
        holder = self._holder(holder_path)
        text = comment.lstrip("# ")
        if isinstance(holder, InlineTable):
            # TOML has no comments inside an inline table.
            with pytest.raises(ILLEGAL_ERRORS):
                holder.add(tomlkit.comment(text))
            return
        if isinstance(holder, OutOfOrderTableProxy):
            # A proxy is a view over several fragments and has no body to
            # receive a comment.
            return
        holder.add(tomlkit.comment(text))

    def _op_trivia_edit(self, path: str, comment: str) -> None:
        holder_path, _, key = path.rpartition(".")
        holder: Any = self.doc
        for part in holder_path.split(".") if holder_path else []:
            holder = holder[int(part)] if part.isdigit() else holder[part]
            if isinstance(holder, InlineTable):
                # A comment cannot live inside `{...}` (TOML has no comments
                # in inline tables), so there is no trivia to edit there.
                return
        item = _item_of(holder, key)
        assert isinstance(item, Item)
        item.trivia.comment = comment

    def _op_setdefault(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        self._try_illegal(lambda: holder.setdefault(key, self._new_value()))
        self._prune(self._full_path(holder_path, key))

    def _op_append_fragment(self, holder_path: str, key: str) -> None:
        holder = self._holder(holder_path)
        if isinstance(holder, OutOfOrderTableProxy):
            # The proxy has no append(); its tables are edited through
            # __setitem__ instead, covered by the set operations.
            return
        if self._is_dotted_scope(holder_path):
            return
        fragment = _new_table_fragment(self.rng)
        self._try_illegal(lambda: holder.append(key, fragment))
        full_path = self._full_path(holder_path, key)
        if key in holder:
            self._prune(full_path)

    def _is_inside_aot(self, holder_path: str) -> bool:
        """Whether the holder path descends into an array-of-tables element."""
        holder: Any = self.doc
        for part in holder_path.split(".") if holder_path else []:
            if isinstance(holder, AoT):
                return True
            holder = holder[int(part)] if part.isdigit() else holder[part]
        return False

    def _is_dotted_scope(self, holder_path: str) -> bool:
        """Whether the holder, or an ancestor, was created by a dotted key.

        A concrete table (or a new table) nested under such a prefix renders
        its header at the enclosing root scope, which the table model cannot
        round-trip. Those edits are outside this regression's scope.
        """
        holder: Any = self.doc
        parts = holder_path.split(".") if holder_path else []

        def _map_of(target: Any) -> dict[Any, Any]:
            if isinstance(target, Container):
                return target._map
            if isinstance(target, Table):
                return target.value._map
            return {}

        for part in parts:
            if isinstance(holder, AoT):
                holder = holder[int(part)]
                continue
            if not isinstance(holder, (Container, Table)):
                return False
            stored = next((k for k in _map_of(holder) if k.key == part), None)
            if stored is not None and stored.is_dotted():
                return True
            holder = holder[int(part)] if part.isdigit() else holder[part]
        # The holder itself was created by a dotted key (the last path part).
        if parts and isinstance(holder, (Container, Table)):
            parent_path = ".".join(parts[:-1])
            try:
                parent = self._holder(parent_path)
            except (KeyError, IndexError, TypeError):
                return False
            stored = next((k for k in _map_of(parent) if k.key == parts[-1]), None)
            if stored is not None and stored.is_dotted():
                return True
        return False

    def _op_aot_append(self, aot_path: str) -> None:
        aot = self._holder(aot_path)
        assert isinstance(aot, AoT)
        aot.append({"n": self.rng.randrange(50)})

    def _op_aot_insert(self, aot_path: str) -> None:
        aot = self._holder(aot_path)
        assert isinstance(aot, AoT)
        index = self.rng.randrange(0, len(aot) + 1)
        # Inserting shifts the indices of later elements, so drop all paths
        # below the array rather than chase the renumbering.
        self._prune(aot_path)
        aot.insert(index, {"n": self.rng.randrange(50)})

    def _op_aot_set(self, aot_path: str) -> None:
        aot = self._holder(aot_path)
        assert isinstance(aot, AoT)
        if not aot:
            return
        index = self.rng.randrange(len(aot))
        # The element at this index is replaced, so every retained item below
        # it becomes stale.
        self._prune(f"{aot_path}.{index}")
        aot[index] = {"n": self.rng.randrange(50)}

    def _op_aot_delete(self, aot_path: str) -> None:
        aot = self._holder(aot_path)
        assert isinstance(aot, AoT)
        if aot:
            index = self.rng.randrange(len(aot))
            del aot[index]
            # Removing one element shifts later indices, so nothing below the
            # array can keep a stable path afterwards.
            self._prune(aot_path)

    def _op_aot_bad_set(self, aot_path: str) -> None:
        aot = self._holder(aot_path)
        assert isinstance(aot, AoT)
        if not aot:
            aot.append({"n": 0})
        index = self.rng.randrange(len(aot))
        with pytest.raises(ILLEGAL_ERRORS):
            aot[index] = self.rng.randrange(100)

    def _op_shallow_copy(self) -> None:
        self._copy_and_edit(copy.copy)

    def _op_deep_copy(self) -> None:
        self._copy_and_edit(copy.deepcopy)

    def _copy_and_edit(self, copier: Callable[[Any], Any]) -> None:
        """Copy the document, then edit trivia on one side only.

        A copied document must be independent: changing a comment or indent on
        either side must not rewrite the other, and both must keep their body,
        key index and serialization coherent afterwards.
        """
        duplicate = copier(self.doc)
        marker = f"# copied {self.rng.randrange(10**6)}"

        def first_leaf(document: tomlkit.TOMLDocument) -> Item | None:
            for _, item in document.body:
                if isinstance(item, Item) and not isinstance(
                    item, (Table, AoT, Whitespace, Null)
                ):
                    return item
            return None

        original_leaf = first_leaf(self.doc)
        duplicate_leaf = first_leaf(duplicate)
        if original_leaf is None or duplicate_leaf is None:
            return

        duplicate_leaf.trivia.comment = marker
        if original_leaf.trivia.comment == marker:
            raise Failure("editing the copy rewrote the original's trivia")
        if dumps(duplicate).count(marker) != 1:
            raise Failure("the copied document lost or duplicated the edit")
        if marker in dumps(self.doc):
            raise Failure(
                "editing the copy changed the original's serialization"
            )

        original_leaf.trivia.comment = "# original"
        if duplicate_leaf.trivia.comment != marker:
            raise Failure("editing the original rewrote the copy's trivia")

        # Both copies stay independently editable: structural edits on the
        # duplicate must not appear in the original, and both must serialize.
        edit_key = f"copied{self.rng.randrange(10**6)}"
        duplicate[edit_key] = 1
        if edit_key in self.doc:
            raise Failure("editing the copy added a key to the original")
        check_invariants(duplicate, [], "copy edit")
        tomllib.loads(dumps(self.doc))

    def _try_illegal(self, action: Callable[[], Any]) -> None:
        """Run an op that may be rejected; a rejection must leave no trace."""
        before = dumps(self.doc)
        try:
            action()
        except ILLEGAL_ERRORS:
            if dumps(self.doc) != before:
                raise Failure("a rejected edit left the document changed")
            if check_structure(self.doc, "root"):
                raise Failure("a rejected edit broke body/map consistency")
            return
        check_invariants(self.doc, self.retained, "post illegal-attempt")


def _write_failure(
    seed: int,
    starter_name: str,
    starter: str,
    ops: list[Op],
    error: str,
) -> Path:
    FAILURE_DIR.mkdir(exist_ok=True)
    target = FAILURE_DIR / f"seed-{seed}-{starter_name}.json"
    target.write_text(
        json.dumps(
            {
                "seed": seed,
                "starter_name": starter_name,
                "starter": starter,
                "error": error,
                "ops": [op.replayable() for op in ops],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def replay(starter: str, ops: list[Op]) -> None:
    """Replay a recorded sequence, checking invariants after every step."""
    machine = StateMachine(starter, seed=0, steps=0)
    for step, op in enumerate(ops):
        machine._apply(op, step, tolerant=True)
        check_invariants(machine.doc, machine.retained, f"replay step={step} {op.name}")


def shrink(
    starter: str,
    ops: list[Op],
    reproduces: Callable[[str, list[Op]], bool] | None = None,
) -> tuple[str, list[Op]]:
    """Greedily shrink a failing operation sequence.

    First drop operations that are not needed for the failure, then shorten the
    starting document to the smallest prefix of complete TOML blocks that still
    reproduces. The result is a short, replayable reproduction.
    """
    if reproduces is None:
        reproduces = _reproduces
    failing = ops

    changed = True
    while changed:
        changed = False
        for index in range(len(failing)):
            candidate = failing[:index] + failing[index + 1 :]
            if reproduces(starter, candidate):
                failing = candidate
                changed = True
                break

    blocks = _toml_blocks(starter)
    while len(blocks) > 1 and reproduces("".join(blocks[1:]), failing):
        blocks = blocks[1:]
    while len(blocks) > 1 and reproduces(blocks[0], failing):
        blocks = [blocks[0]]

    return "".join(blocks), failing


def _reproduces(starter: str, ops: list[Op]) -> bool:
    try:
        replay(starter, ops)
    except Failure:
        # Only the same invariant violation counts as a reproduction; a
        # stale-path error after dropping an operation means the sequence is
        # no longer meaningful and must not be shrunk further.
        return True
    except (KeyError, IndexError, TypeError, TOMLKitError):
        return False
    return False


def _toml_blocks(content: str) -> list[str]:
    """Split a document into chunks that are each complete TOML on their own."""
    blocks: list[str] = []
    current: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("[") and current:
            blocks.append("".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("".join(current))
    return blocks


def test_shrinker_reduces_sequence_and_document() -> None:
    starter = STARTER_DOCS["mixed"]

    def reproduces(shrunk_starter: str, ops: list[Op]) -> bool:
        return "top = 1" in shrunk_starter and any(
            op.name == "delete" and op.args[1] == "top" for op in ops
        )

    ops = [
        Op("set_new", ("", "noise")),
        Op("delete", ("", "top")),
        Op("set_new", ("", "more_noise")),
        Op("delete", ("", "missing0")),
    ]
    reduced_starter, reduced_ops = shrink(starter, ops, reproduces=reproduces)
    assert [op.name for op in reduced_ops] == ["delete"]
    assert reduced_ops[0].args == ("", "top")
    assert reduced_starter.startswith("top = 1")


def test_starters_are_valid_toml() -> None:
    for starter in STARTER_DOCS.values():
        assert tomllib.loads(starter) == parse(starter).unwrap()


def test_illegal_append_leaves_document_unchanged() -> None:
    # [a] b = 1 then appending a table fragment that redefines b is rejected;
    # the rejected edit must not leave the new fragment in body or the key map.
    doc = parse("[a]\nb = 1\n")
    fragment = tomlkit.table(is_super_table=True)
    fragment["b"] = 2
    before = doc.as_string()

    with pytest.raises(KeyAlreadyPresent):
        doc.append("a", fragment)

    assert doc.as_string() == before
    assert tomllib.loads(before) == doc.unwrap()
    assert list(doc.body) == [(k, v) for k, v in doc.body]  # unchanged length
    assert not check_structure(doc, "root")


def test_illegal_fragment_merge_does_not_poison_out_of_order_table() -> None:
    # [a.b] then [a.d] makes `a` an out-of-order table; a later fragment that
    # redefines b as a value must be rejected without touching the live parts.
    doc = parse("[a.b]\nx = 1\n[c]\ny = 2\n[a.d]\nz = 3\n")
    before = doc.as_string()
    fragment = tomlkit.table(is_super_table=True)
    fragment["b"] = 5

    with pytest.raises(ILLEGAL_ERRORS):
        doc.append("a", fragment)

    assert doc.as_string() == before
    assert doc.unwrap() == tomllib.loads(before)
    assert doc["a"]["b"] == {"x": 1}


def test_append_fragment_that_would_emit_duplicate_header_is_rejected() -> None:
    # A super-table fragment holding plain values renders its own [a] header.
    # Merging one into the concrete [a] table would emit `[a]` twice, so the
    # edit must be refused instead of producing unparsable TOML.
    doc = parse("[a]\nb = 1\n")
    before = doc.as_string()
    fragment = tomlkit.table(is_super_table=True)
    fragment["c"] = 2

    with pytest.raises(ILLEGAL_ERRORS):
        doc.append("a", fragment)

    assert doc.as_string() == before
    assert tomllib.loads(doc.as_string()) == doc.unwrap()


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("starter_name", list(STARTER_DOCS))
def test_stateful_editing(seed: int, starter_name: str) -> None:
    starter = STARTER_DOCS[starter_name]
    machine = StateMachine(starter, seed=seed, steps=60)
    try:
        machine.run()
    except Failure as exc:
        _write_failure(seed, starter_name, starter, machine.ops, str(exc))
        minimum_starter, minimum_ops = shrink(starter, machine.ops)
        pytest.fail(
            f"{exc}\n\nReproduce with:\n"
            f"  starter={minimum_starter!r}\n"
            f"  ops={[(op.name, _repr_arg(op.args)) for op in minimum_ops]!r}"
        )
