import copy
import io
import pickle

from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path

import pytest

from tomlkit import array
from tomlkit import decimal_
from tomlkit import dumps
from tomlkit import item
from tomlkit import key_value
from tomlkit import load
from tomlkit import loads
from tomlkit import parse
from tomlkit import value
from tomlkit.exceptions import InvalidNumberError
from tomlkit.items import DecimalFloat
from tomlkit.items import Float
from tomlkit.items import Integer
from tomlkit.items import Trivia
from tomlkit.toml_file import TOMLFile


CONTENT = """\
short = 0.1
small = 5e-300
big = 1e400
under = 1_000.25
exp = 6.022e23
negzero = -0.0
plus = +1.5
finf = inf
ninf = -inf
nnan = nan
nnan_neg = -nan
num = 42
"""

FINITE_KEYS = ("short", "small", "big", "under", "exp", "negzero", "plus")
FLOAT_KEYS = FINITE_KEYS + ("finf", "ninf", "nnan", "nnan_neg")


def test_default_strategy_keeps_float_behavior() -> None:
    doc = parse(CONTENT)

    assert isinstance(doc["short"], Float)
    assert isinstance(doc["short"], float)
    assert not isinstance(doc["short"], DecimalFloat)
    assert doc["short"] == 0.1
    assert doc.as_string() == CONTENT


def test_decimal_strategy_reads_exact_values() -> None:
    doc = parse(CONTENT, parse_float=Decimal)

    for key in FLOAT_KEYS:
        assert isinstance(doc[key], DecimalFloat), key
        assert isinstance(doc[key], Decimal), key
        assert not isinstance(doc[key], Float), key

    # exact decimal values, no binary float error
    assert doc["short"].unwrap() == Decimal("0.1")
    assert doc["short"].unwrap() != 0.1
    assert doc["small"].unwrap() == Decimal("5e-300")
    assert doc["big"].unwrap() == Decimal("1e400")
    assert doc["under"].unwrap() == Decimal("1000.25")
    assert doc["exp"].unwrap() == Decimal("6.022e23")

    # integers are unaffected by the strategy
    assert isinstance(doc["num"], Integer)
    assert doc["num"] == 42


def test_decimal_strategy_preserves_raw_lexemes() -> None:
    doc = parse(CONTENT, parse_float=Decimal)

    assert doc["under"].as_string() == "1_000.25"
    assert doc["exp"].as_string() == "6.022e23"
    assert doc["negzero"].as_string() == "-0.0"
    assert doc["plus"].as_string() == "+1.5"
    assert doc["nnan_neg"].as_string() == "-nan"
    assert doc.as_string() == CONTENT


def test_decimal_negative_zero_and_non_finite() -> None:
    doc = parse(CONTENT, parse_float=Decimal)

    negzero = doc["negzero"].unwrap()
    assert negzero == 0
    assert negzero.is_signed()

    assert doc["finf"].unwrap().is_infinite()
    assert not doc["finf"].unwrap().is_signed()
    assert doc["ninf"].unwrap().is_infinite()
    assert doc["ninf"].unwrap().is_signed()
    assert doc["nnan"].unwrap().is_nan()
    assert not doc["nnan"].unwrap().is_signed()
    assert doc["nnan_neg"].unwrap().is_nan()
    assert doc["nnan_neg"].unwrap().is_signed()


def test_modified_decimal_generates_valid_toml_and_reparses() -> None:
    doc = parse(CONTENT, parse_float=Decimal)
    for key in FINITE_KEYS:
        doc[key] = doc[key] + Decimal("1")

    rendered = dumps(doc)
    # unmodified keys still carry their original text
    assert "finf = inf" in rendered
    assert "nnan_neg = -nan" in rendered
    assert "num = 42" in rendered

    # re-parse under both strategies
    decimal_doc = parse(rendered, parse_float=Decimal)
    float_doc = parse(rendered)
    assert decimal_doc["short"].unwrap() == Decimal("1.1")
    assert float_doc["short"] == 1.1
    assert isinstance(float_doc["short"], Float)
    assert decimal_doc["big"].unwrap() == Decimal("1e400") + 1
    assert decimal_doc["under"].unwrap() == Decimal("1001.25")


def test_modified_non_finite_stays_valid() -> None:
    doc = parse(CONTENT, parse_float=Decimal)
    doc["finf"] = doc["finf"] + 1
    doc["ninf"] = doc["ninf"] - 1
    doc["nnan"] = doc["nnan"] + 1
    doc["nnan_neg"] = -doc["nnan_neg"]

    rendered = dumps(doc)
    assert "finf = inf" in rendered
    assert "ninf = -inf" in rendered
    assert "nnan = nan" in rendered
    assert "nnan_neg = -nan" in rendered

    reparsed = parse(rendered, parse_float=Decimal)
    assert reparsed["finf"].unwrap().is_infinite()
    assert reparsed["ninf"].unwrap().is_signed()
    assert reparsed["nnan"].unwrap().is_nan()
    assert reparsed["nnan_neg"].unwrap().is_signed()


def test_decimal_arithmetic_returns_decimal_float() -> None:
    doc = parse("a = 0.1\n", parse_float=Decimal)
    original = doc["a"]

    result = original + Decimal("0.2")
    assert isinstance(result, DecimalFloat)
    assert result == Decimal("0.3")
    assert result.as_string() == "0.3"
    assert result.trivia is not original.trivia

    doc["a"] = result
    assert doc.as_string() == "a = 0.3\n"

    # int operands stay exact
    assert (doc["a"] + 1).as_string() == "1.3"
    assert (doc["a"] * 2).as_string() == "0.6"
    assert (2 * doc["a"]).as_string() == "0.6"
    assert (doc["a"] - Decimal("0.3")).as_string() == "0.0"

    # sign conventions mirror Float
    doc = parse("a = +1.5\nb = -0.0\n", parse_float=Decimal)
    assert (doc["a"] + 1).as_string() == "+2.5"
    assert (doc["b"] + 1).as_string() == "+1.0"
    # negating -0.0 keeps the explicit sign marker, exactly like Float
    assert (-doc["b"]).as_string() == "+0.0"


def test_decimal_mixed_float_arithmetic_raises() -> None:
    doc = parse("a = 0.1\n", parse_float=Decimal)

    with pytest.raises(TypeError):
        doc["a"] + 0.1
    with pytest.raises(TypeError):
        0.1 + doc["a"]
    with pytest.raises(TypeError):
        doc["a"] * 1.5
    with pytest.raises(TypeError):
        doc["a"] - 1.5

    # comparisons with float are allowed and exact (stdlib Decimal rules)
    assert doc["a"] < 0.2
    assert doc["a"] != 0.1
    assert doc["a"] == Decimal("0.1")
    assert doc["a"] == parse("b = 0.1\n", parse_float=Decimal)["b"]


def test_decimal_non_finite_comparisons() -> None:
    doc = parse("i = inf\nn = nan\n", parse_float=Decimal)

    # infinities compare normally, including against float
    assert doc["i"] == doc["i"]
    assert doc["i"] == float("inf")
    assert doc["i"] > 0

    # NaN never equals itself; ordering raises InvalidOperation
    assert doc["n"] != doc["n"]
    assert doc["n"] != float("nan")
    with pytest.raises(InvalidOperation):
        doc["n"] < 1


def test_decimal_round() -> None:
    doc = parse("a = 2.5\n", parse_float=Decimal)

    rounded = round(doc["a"])
    assert isinstance(rounded, Integer)
    assert rounded == 2

    rounded_digits = round(doc["a"], 1)
    assert isinstance(rounded_digits, DecimalFloat)
    assert rounded_digits == Decimal("2.5")

    nan_doc = parse("a = nan\n", parse_float=Decimal)
    with pytest.raises(ValueError):
        round(nan_doc["a"])
    inf_doc = parse("a = inf\n", parse_float=Decimal)
    with pytest.raises(OverflowError):
        round(inf_doc["a"])


def test_decimal_copy_and_pickle() -> None:
    doc = parse("a = 1_000.25  # keep\n", parse_float=Decimal)
    original = doc["a"]

    copied = copy.copy(original)
    assert isinstance(copied, DecimalFloat)
    assert copied.as_string() == "1_000.25"
    assert copied.unwrap() == Decimal("1000.25")

    deep = copy.deepcopy(doc)
    assert isinstance(deep["a"], DecimalFloat)
    assert deep["a"].as_string() == "1_000.25"

    restored = pickle.loads(pickle.dumps(original))
    assert isinstance(restored, DecimalFloat)
    assert restored.as_string() == "1_000.25"
    assert restored.unwrap() == Decimal("1000.25")

    doc_copy = copy.copy(doc)
    assert isinstance(doc_copy["a"], DecimalFloat)


def test_decimal_backfill_in_containers_and_arrays() -> None:
    doc = parse("a = 0.1\nb = [0.2, 0.3]\n", parse_float=Decimal)

    # replacing with a plain Decimal is supported through item()
    doc["a"] = Decimal("2.5")
    assert isinstance(doc["a"], DecimalFloat)

    doc["b"][0] = Decimal("9.9")
    doc["b"].append(Decimal("0.4"))
    assert isinstance(doc["b"][0], DecimalFloat)

    rendered = dumps(doc)
    assert rendered == "a = 2.5\nb = [9.9, 0.3, 0.4]\n"

    reparsed = parse(rendered, parse_float=Decimal)
    assert reparsed.unwrap() == {
        "a": Decimal("2.5"),
        "b": [Decimal("9.9"), Decimal("0.3"), Decimal("0.4")],
    }

    # building documents from Python data containing Decimal
    built = dumps({"p": Decimal("0.1"), "q": [Decimal("1.5"), 2]})
    assert built == "p = 0.1\nq = [1.5, 2]\n"
    assert parse(built, parse_float=Decimal)["p"].unwrap() == Decimal("0.1")


def test_decimal_constructor() -> None:
    created = decimal_("1_000.25")
    assert isinstance(created, DecimalFloat)
    assert created.as_string() == "1_000.25"
    assert created.unwrap() == Decimal("1000.25")

    created_decimal = decimal_(Decimal("1.50"))
    assert created_decimal.unwrap() == Decimal("1.50")
    assert created_decimal.as_string() == "1.50"

    assert item(Decimal("4.56")).as_string() == "4.56"

    with pytest.raises(ValueError, match="Invalid TOML float"):
        decimal_("1.2.3")


def test_decimal_entry_points() -> None:
    text = "a = 0.1\n"

    assert isinstance(loads(text, parse_float=Decimal)["a"], DecimalFloat)

    single = value("1_000.25", parse_float=Decimal)
    assert isinstance(single, DecimalFloat)
    assert single.as_string() == "1_000.25"

    arr = array("[0.1, 1, 1.5]", parse_float=Decimal)
    assert isinstance(arr[0], DecimalFloat)
    assert isinstance(arr[1], Integer)

    key, val = key_value("foo = 6.022e23", parse_float=Decimal)
    assert key.key == "foo"
    assert isinstance(val, DecimalFloat)
    assert val.unwrap() == Decimal("6.022e23")

    loaded = load(io.StringIO(text), parse_float=Decimal)
    assert isinstance(loaded["a"], DecimalFloat)


def test_toml_file_read_decimal(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("threshold = 0.1\n")

    toml_file = TOMLFile(path)
    assert isinstance(toml_file.read()["threshold"], Float)

    doc = toml_file.read(parse_float=Decimal)
    assert isinstance(doc["threshold"], DecimalFloat)
    doc["threshold"] = doc["threshold"] + Decimal("0.2")
    toml_file.write(doc)

    assert path.read_text() == "threshold = 0.3\n"
    reparsed = toml_file.read(parse_float=Decimal)
    assert reparsed["threshold"].unwrap() == Decimal("0.3")


def test_custom_parse_float_callable() -> None:
    def as_float(literal: str) -> float:
        return float(literal)

    doc = parse("a = 1.5\n", parse_float=as_float)
    assert isinstance(doc["a"], Float)

    def as_decimal(literal: str) -> Decimal:
        return Decimal(literal)

    doc = parse("a = 1.5\n", parse_float=as_decimal)
    assert isinstance(doc["a"], DecimalFloat)
    assert doc["a"].as_string() == "1.5"

    def returning_item(literal: str) -> DecimalFloat:
        return DecimalFloat(Decimal(literal), Trivia(), literal)

    doc = parse("a = 1.5\n", parse_float=returning_item)
    assert isinstance(doc["a"], DecimalFloat)

    def returning_str(literal: str) -> str:
        return literal

    with pytest.raises(TypeError, match="parse_float must return"):
        parse("a = 1.5\n", parse_float=returning_str)


def test_decimal_hash_and_equality() -> None:
    first = parse("a = 0.1\n", parse_float=Decimal)["a"]
    second = parse("a = 0.1\n", parse_float=Decimal)["a"]

    assert first == second
    assert hash(first) == hash(second)
    assert hash(first) == hash(Decimal("0.1"))

    # usable as a dict key
    mapping = {first: "value"}
    assert mapping[second] == "value"


def test_invalid_numbers_raise_same_error_as_default() -> None:
    # malformed numbers still raise the normal parse error under the decimal
    # strategy instead of leaking decimal.InvalidOperation
    with pytest.raises(InvalidNumberError):
        parse("a = 01.5\n", parse_float=Decimal)
    with pytest.raises(InvalidNumberError):
        parse("a = 1.2.3\n", parse_float=Decimal)
    with pytest.raises(InvalidNumberError):
        parse("a = 01.5\n")


def test_integral_results_stay_float_literals() -> None:
    doc = parse("a = 4.0\nb = 2.0\n", parse_float=Decimal)

    # Decimal division can drop the fractional part; the rendered literal
    # must still parse back as a float, not an integer
    doc["a"] = doc["a"] // 2
    doc["b"] = 2 ** doc["b"]
    rendered = dumps(doc)
    assert rendered == "a = 2.0\nb = 4.0\n"

    reparsed = parse(rendered, parse_float=Decimal)
    assert isinstance(reparsed["a"], DecimalFloat)
    assert isinstance(reparsed["b"], DecimalFloat)
    assert reparsed["a"].unwrap() == 2
    assert reparsed["b"].unwrap() == 4


def test_decimal_context_precision_is_honored() -> None:
    from decimal import localcontext

    doc = parse("a = 1.0\n", parse_float=Decimal)
    with localcontext() as context:
        context.prec = 5
        doc["a"] = doc["a"] / Decimal("7")

    rendered = dumps(doc)
    assert rendered == "a = 0.14286\n"
    reparsed = parse(rendered, parse_float=Decimal)
    assert reparsed["a"].unwrap() == Decimal("0.14286")


def test_dumps_with_decimal_and_sort_keys() -> None:
    rendered = dumps(
        {"b": Decimal("0.1"), "a": [Decimal("1.5"), {"c": Decimal("2.5")}]},
        sort_keys=True,
    )
    assert rendered == "a = [1.5, {c = 2.5}]\nb = 0.1\n"
