"""Check the frozen score without requiring QuantConnect's cloud runtime."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple


class Symbol(NamedTuple):
    value: str


def _example_functions():
    source = Path(__file__).resolve().parents[1] / "examples/quantconnect_spy_momentum.py"
    tree = ast.parse(source.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    namespace = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def test_cloud_example_uses_the_frozen_price_rule():
    rank = _example_functions()["rank_closes"]

    a, b, short = Symbol("A"), Symbol("B"), Symbol("SHORT")
    closes = {
        a: [100 + i for i in range(253)],
        b: [100 + 2 * i for i in range(253)],
        short: [100 + i for i in range(252)],
    }
    assert rank(closes) == [b, a]
    closes[b][-1] = 1  # Fails the same 200-day-average check as the local candidate.
    assert rank(closes) == [a]


def test_cloud_example_reads_iterable_slice_history():
    collect = _example_functions()["closes_from_slices"]
    a, b, other = Symbol("A"), Symbol("B"), Symbol("OTHER")
    history = iter(
        [
            SimpleNamespace(bars={a: SimpleNamespace(close=10), other: SimpleNamespace(close=30)}),
            SimpleNamespace(bars={a: SimpleNamespace(close=11), b: SimpleNamespace(close=20)}),
            SimpleNamespace(bars={}),
        ]
    )
    assert collect(history, [a, b]) == {a: [10.0, 11.0], b: [20.0]}
