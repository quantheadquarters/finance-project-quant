"""Check the frozen score without requiring QuantConnect's cloud runtime."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple


class Symbol(NamedTuple):
    value: str


def test_cloud_example_uses_the_frozen_price_rule():
    source = Path(__file__).resolve().parents[1] / "examples/quantconnect_spy_momentum.py"
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    rank = namespace["rank_closes"]

    a, b, short = Symbol("A"), Symbol("B"), Symbol("SHORT")
    closes = {
        a: [100 + i for i in range(253)],
        b: [100 + 2 * i for i in range(253)],
        short: [100 + i for i in range(252)],
    }
    assert rank(closes) == [b, a]
    closes[b][-1] = 1  # Fails the same 200-day-average check as the local candidate.
    assert rank(closes) == [a]
