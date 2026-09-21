"""Write a self-contained-data TradingView Lightweight Charts backtest report.

The generated HTML loads the pinned chart library from its CDN, but embeds all
market and backtest data locally. Opening it makes no market-data request.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from alpha_engine.cache.models import PriceSeries
from alpha_engine.strategy.engine import StrategyBacktest

_LIBRARY = (
    "https://unpkg.com/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js"
)


def write_backtest_chart(path: str | Path, series: PriceSeries, report: StrategyBacktest) -> Path:
    """Create an interactive candle + equity report and return its path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    allowed = set(report.timestamps)
    candles = [
        {
            "time": int(c.ts.timestamp()),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
        }
        for c in series.candles
        if c.ts in allowed
    ]
    markers = [
        {
            "time": int(ts.timestamp()),
            "position": "belowBar" if signal > 0 else "aboveBar",
            "color": "#22c55e" if signal > 0 else "#ef4444",
            "shape": "arrowUp" if signal > 0 else "arrowDown",
            "text": "LONG" if signal > 0 else "SHORT",
        }
        for ts, signal, confirmed in zip(report.timestamps, report.signals, report.confirmed)
        if signal and confirmed
    ]
    equity = [
        {"time": int(ts.timestamp()), "value": value}
        for ts, value in zip(report.timestamps, report.equity_curve)
    ]
    payload = json.dumps({"candles": candles, "markers": markers, "equity": equity}).replace(
        "</", "<\\/"
    )
    title = html.escape(f"{report.strategy} · {report.asset}")
    metrics = report.metrics
    warning = (
        '<p class="warning">LOOKAHEAD DETECTED — metrics are void.</p>'
        if report.lookahead_violations
        else ""
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><script src="{_LIBRARY}"></script>
<style>
body{{margin:0;background:#0b1020;color:#e5e7eb;font:14px system-ui,sans-serif}}
main{{max-width:1200px;margin:auto;padding:24px}}h1{{margin:0 0 8px}}.stats{{display:flex;gap:20px;flex-wrap:wrap;margin:16px 0}}
.panel{{height:480px;border:1px solid #243044;border-radius:8px;margin:16px 0}}#equity{{height:220px}}.warning{{color:#fca5a5;font-weight:700}}
a{{color:#93c5fd}}footer{{color:#94a3b8;line-height:1.5;margin-top:24px}}
</style></head><body><main><h1>{title}</h1>{warning}
<div class="stats"><span>Return <b>{metrics.total_return_pct:+.2f}%</b></span><span>Sharpe <b>{metrics.sharpe:+.3f}</b></span><span>Max drawdown <b>{metrics.max_drawdown_pct:.2f}%</b></span><span>Trades <b>{metrics.trades}</b></span></div>
<div id="price" class="panel" aria-label="Candlestick chart with strategy signals"></div>
<div id="equity" class="panel" aria-label="Backtest equity curve"></div>
<footer>RESEARCH ONLY. A backtest is not a forecast or investment advice.<br>
Charts by <a href="https://www.tradingview.com/" rel="noopener">TradingView Lightweight Charts™</a>.</footer>
<script>
const data={payload};
const options={{layout:{{background:{{type:'solid',color:'#0b1020'}},textColor:'#cbd5e1'}},grid:{{vertLines:{{color:'#182033'}},horzLines:{{color:'#182033'}}}},timeScale:{{timeVisible:true}}}};
const price=LightweightCharts.createChart(document.getElementById('price'),options);
const candles=price.addSeries(LightweightCharts.CandlestickSeries,{{upColor:'#22c55e',downColor:'#ef4444',borderVisible:false,wickUpColor:'#22c55e',wickDownColor:'#ef4444'}});
candles.setData(data.candles); LightweightCharts.createSeriesMarkers(candles,data.markers); price.timeScale().fitContent();
const equity=LightweightCharts.createChart(document.getElementById('equity'),options);
const line=equity.addSeries(LightweightCharts.LineSeries,{{color:'#60a5fa',lineWidth:2}}); line.setData(data.equity); equity.timeScale().fitContent();
for(const [chart,element] of [[price,document.getElementById('price')],[equity,document.getElementById('equity')]]) new ResizeObserver(([e])=>chart.applyOptions({{width:e.contentRect.width}})).observe(element);
</script></main></body></html>"""
    path.write_text(document)
    return path
