# Contributing

Thanks for considering a contribution. This project stays useful and trustworthy
because of a few firm rules. Please read these before opening a PR.

## The one rule that matters

**Analyzers and synthesis must be deterministic and tested.**

Given the same inputs, an analyzer must always produce the same output. That means:

- No randomness in analysis or synthesis.
- No network calls inside an analyzer. Analyzers read from the `Cache`, never the
  internet. Fetching belongs in `ingestion/`.
- No LLM deciding or computing anything. A language model may only write the
  `thesis` string in the `narrative/` layer, and may never change a numeric field.

If a change would break determinism, it belongs in a different layer, or it doesn't
belong here.

## Adding a data source

Write an adapter in `ingestion/` that pulls from the source and outputs the
normalized models in `cache/models.py`. Do not teach analyzers about a source's
native format. Prefer keyless or free-tier sources, and gate anything requiring
credentials behind config so the default clone still runs.

## Adding an analyzer

Put it in `analyzers/`, make it a pure function from a cache model to a
`SignalSource`, and add unit tests that pin its behavior on fixed inputs. Look at
`crypto_trend.py` and `tests/test_core.py` as the pattern.

## Dev workflow

```bash
pip install -e ".[dev]"
pytest -q          # tests must pass
ruff check .       # lint must be clean
ruff format --check .
```

Start with [Build your own model](docs/BUILD_YOUR_MODEL.md) for a complete
strategy, factor, or data-provider path. A useful first PR can improve the
guided experiment, a source adapter's diagnostics, or a network-free fixture.
Include the data source, dates, benchmark, costs, and negative results when a
change makes a research claim. Do not commit `.env` or downloaded market data.

## Scope and honesty

Heuristics in this repo are scaffolds, not claims of profit. If you add an
analyzer, describe its logic plainly and do not imply edge it hasn't demonstrated.
Calibration and proof of value are the validation layer's job, not marketing copy.
