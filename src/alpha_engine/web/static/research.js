"use strict";

const byId = (id) => document.getElementById(id);
const stamp = (value) => value ? new Date(value).toLocaleDateString() : "unknown";
const money = (value) => value == null ? "missing" : Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
let activeAsset = "AAPL";
let strategies = [];
let lastExperiment = null;

async function tool(name, args = {}) {
  const response = await fetch(`/api/v1/tools/${name}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(args),
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function line(parent, text, className = "") {
  const node = document.createElement("p");
  node.className = className;
  node.textContent = text;
  parent.append(node);
  return node;
}

function candleChart(candles) {
  const root = byId("price-chart");
  root.replaceChildren();
  const bars = candles.slice(-90);
  if (!bars.length) return line(root, "No candles in this window.");
  const width = 900, height = 280, left = 58, right = 12, top = 14, bottom = 30;
  const min = Math.min(...bars.map((c) => c.low));
  const max = Math.max(...bars.map((c) => c.high));
  const range = max - min || 1;
  const y = (price) => top + (max - price) / range * (height - top - bottom);
  const spacing = (width - left - right) / bars.length;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${activeAsset} daily candlesticks from ${stamp(bars[0].ts)} to ${stamp(bars.at(-1).ts)}`);
  const add = (name, attrs) => {
    const node = document.createElementNS(svg.namespaceURI, name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    svg.append(node);
    return node;
  };
  for (let i = 0; i <= 4; i++) {
    const value = min + range * i / 4, py = y(value);
    add("line", { x1: left, y1: py, x2: width - right, y2: py, stroke: "currentColor", "stroke-opacity": 0.15 });
    const label = add("text", { x: left - 8, y: py + 3, "text-anchor": "end", fill: "currentColor", "font-size": 11 });
    label.textContent = value.toFixed(0);
  }
  bars.forEach((bar, index) => {
    const x = left + spacing * (index + 0.5);
    const color = bar.close >= bar.open ? "#22c55e" : "#ef4444";
    add("line", { x1: x, x2: x, y1: y(bar.low), y2: y(bar.high), stroke: color, "stroke-width": 1.5 });
    add("rect", { x: x - Math.max(1, spacing * 0.28), y: Math.min(y(bar.open), y(bar.close)), width: Math.max(2, spacing * 0.56), height: Math.max(1, Math.abs(y(bar.open) - y(bar.close))), fill: color });
  });
  const start = add("text", { x: left, y: height - 5, fill: "currentColor", "font-size": 11 });
  start.textContent = stamp(bars[0].ts);
  const end = add("text", { x: width - right, y: height - 5, "text-anchor": "end", fill: "currentColor", "font-size": 11 });
  end.textContent = stamp(bars.at(-1).ts);
  root.append(svg);
}

function renderEvidence(data) {
  const demo = data.price.provider === "synthetic-demo";
  byId("options-section").hidden = demo;
  const status = byId("source-status");
  status.replaceChildren();
  const sourceLine = (name, state, detail) => line(status, `${name}: ${state} · ${detail}`, state === "ready" ? "research-item-title" : "research-warning");
  sourceLine("Daily prices", data.price.stale ? "stale" : "ready", demo ? `${data.price.candles.length} invented bars; regenerate with .venv/bin/python scripts/load_demo.py` : `${data.price.candles.length} bars; refresh: ./start.sh scan ${activeAsset} --days 365 --no-record`);
  if (demo) {
    line(status, "DEMO uses invented candles. Filings, news, macro data and options are intentionally absent.", "research-warning");
  } else {
    sourceLine("SEC filings", data.fundamentals.periods.length ? (data.fundamentals.stale ? "stale" : "ready") : "missing", `${data.fundamentals.periods.length} periods shown; set SEC_USER_AGENT and run ./start.sh ingest ${activeAsset}`);
    sourceLine("Company news", data.news.items.length ? (data.news.stale ? "stale" : "ready") : "missing", `${data.news.items.length} latest headlines returned; set FINNHUB_API_KEY and run ./start.sh ingest ${activeAsset}`);
    const macro = data.macro?.series || [];
    sourceLine("FRED macro", macro.length === 3 && macro.every((s) => s.observations && !s.stale) ? "ready" : "missing or stale", `${macro.filter((s) => s.observations).length}/3 series cached; set FRED_API_KEY and run ./start.sh scan ${activeAsset} --no-record`);
  }
  const price = data.price;
  const readiness = byId("evaluation-readiness");
  readiness.replaceChildren();
  line(readiness, "Historical alpha evaluation: blocked", "research-warning");
  if (demo) line(readiness, "Prices are synthetic. Load real dated data before evaluating a rule.");
  else {
    if (price.adjustment_policy === "unverified") line(readiness, "Price adjustment for splits and dividends is unverified.");
    if (price.candles.length < 252) line(readiness, "Fewer than 252 daily bars are cached for this stock.");
    line(readiness, "This single-stock cache has no dated broad-universe membership or delisted-company returns.");
    line(readiness, "No sealed holdout or forward paper record is attached to this rule.");
    line(readiness, "Current headlines and model-priced options cannot validate historical news or option fills.");
  }
  candleChart(price.candles);
  byId("price-meta").textContent = `${price.candles.length} bars · last ${stamp(price.last_bar_at)} · fetched ${stamp(price.fetched_at)}`;
  byId("price-note").textContent = demo ? "SYNTHETIC DEMO — these invented candles are for learning the controls, not market research." : `${price.stale ? "STALE — refresh with a no-record scan. " : ""}${price.candles.length < 100 ? "Short history: run ./start.sh scan " + activeAsset + " --days 365 --no-record before judging backtests. " : ""}Provider: ${price.provider || "unrecorded"}; currency: ${price.currency || "unrecorded"}; adjustment policy: ${price.adjustment_policy}. Historical daily candles, not live quotes.`;

  const fundamentals = byId("fundamentals");
  fundamentals.replaceChildren();
  if (data.fundamentals.stale && !demo) line(fundamentals, "Fundamentals are missing or stale.", "research-warning");
  if (data.fundamentals.analysis?.weight > 0) line(fundamentals, `Deterministic read: ${data.fundamentals.analysis.direction} · ${data.fundamentals.analysis.detail}`, "research-item-title");
  if (!data.fundamentals.periods.length) line(fundamentals, demo ? "No real filings in the synthetic demo." : "No company filings cached. Run ingest after setting SEC_USER_AGENT.");
  const moreFundamentals = document.createElement("details");
  const filingSummary = document.createElement("summary");
  filingSummary.textContent = "Show older filing periods";
  moreFundamentals.append(filingSummary);
  for (const [index, item] of data.fundamentals.periods.entries()) {
    const block = document.createElement("div");
    block.className = "research-item";
    line(block, `${item.period} · period ended ${stamp(item.ts)} · public ${stamp(item.available_at)} · ${item.source || "source unknown"}`, "research-item-title");
    line(block, `Revenue $${money(item.revenue)} · net income $${money(item.net_income)} · operating cash flow $${money(item.operating_cash_flow)}`);
    (index < 4 ? fundamentals : moreFundamentals).append(block);
  }
  if (data.fundamentals.periods.length > 4) fundamentals.append(moreFundamentals);

  const headlines = byId("headlines");
  headlines.replaceChildren();
  if (data.news.stale && !demo) line(headlines, "Headlines are missing or stale.", "research-warning");
  if (data.news.analysis?.weight > 0) line(headlines, `Deterministic headline read: ${data.news.analysis.direction} · ${data.news.analysis.detail}`, "research-item-title");
  if (!data.news.items.length) line(headlines, demo ? "No real headlines in the synthetic demo." : "No company-tagged headlines cached. News sentiment cannot be evaluated yet.");
  const moreHeadlines = document.createElement("details");
  const headlineSummary = document.createElement("summary");
  headlineSummary.textContent = "Show older headlines";
  moreHeadlines.append(headlineSummary);
  for (const [index, item] of data.news.items.entries()) {
    const block = document.createElement("div");
    block.className = "research-item";
    const title = line(block, item.headline, "research-item-title");
    if (/^https?:\/\//.test(item.url)) {
      const link = document.createElement("a");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Source";
      title.append(" · ", link);
    }
    line(block, `${stamp(item.ts)} · ${item.source} · wording score ${item.lexicon_score == null ? "unscored" : item.lexicon_score.toFixed(2)}`);
    (index < 8 ? headlines : moreHeadlines).append(block);
  }
  if (data.news.items.length > 8) headlines.append(moreHeadlines);

  const signal = byId("signal-view");
  signal.replaceChildren();
  if (!data.latest_recorded_signal) return line(signal, "No recorded engine view for this stock.");
  const current = data.latest_recorded_signal;
  line(signal, `${current.signal.direction} · ${(current.signal.confidence * 100).toFixed(1)}% model confidence · recorded ${stamp(current.recorded_at)} · invalidation ${money(current.signal.invalidation_level)}`, "research-item-title");
  line(signal, "This is an older engine research view, not a probability that the stock will rise or fall.");
  for (const source of current.signal.signal_sources) line(signal, `${source.name}: ${source.direction} · ${source.detail}`);
}

async function loadResearch(asset) {
  activeAsset = asset.toUpperCase();
  byId("research-status").textContent = `Loading ${activeAsset} from the local cache…`;
  try {
    const data = await tool("research", { asset: activeAsset, days: 365 });
    renderEvidence(data);
    byId("research-status").textContent = `${activeAsset} evidence loaded. ${data.note}`;
  } catch (error) {
    byId("research-status").textContent = `${error.message}. Run ./start.sh scan ${activeAsset} --days 365 --no-record, then reload.`;
    byId("source-status").replaceChildren();
    byId("evaluation-readiness").replaceChildren();
    line(byId("source-status"), `Daily prices missing. Run ./start.sh scan ${activeAsset} --days 365 --no-record, then reload.`, "research-warning");
  }
}

function showParams() {
  const root = byId("strategy-params");
  root.replaceChildren();
  const choice = strategies.find((s) => s.key === byId("strategy-select").value);
  if (!choice) return;
  for (const [key, value] of Object.entries(choice.params)) {
    const label = document.createElement("label");
    label.textContent = key.replaceAll("_", " ");
    const input = document.createElement("input");
    input.name = key;
    input.type = typeof value === "number" ? "number" : "text";
    if (input.type === "number") input.step = "any";
    input.value = value;
    label.append(input);
    root.append(label);
  }
}

function selectedParams() {
  const params = {};
  for (const input of byId("strategy-params").querySelectorAll("input")) {
    if (input.type === "number" && (input.value === "" || !Number.isFinite(Number(input.value)))) throw new Error(`Enter a finite number for ${input.name}`);
    params[input.name] = input.type === "number" ? Number(input.value) : input.value;
  }
  return params;
}

function downloadJson(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function loadStrategies() {
  const root = byId("strategy-select");
  try {
    const data = await tool("list_strategies");
    strategies = data.strategies;
    for (const strategy of strategies) {
      const option = document.createElement("option");
      option.value = strategy.key;
      option.textContent = `${strategy.name} (${strategy.key})`;
      root.append(option);
    }
    showParams();
  } catch (error) {
    line(byId("strategy-result"), `Strategy list unavailable: ${error.message}`, "research-warning");
  }
}

function equityChart(curves) {
  const values = curves.flatMap((curve) => curve.values || []);
  if (values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 800 160");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Account value through time: ${curves.map((curve) => curve.name).join(", ")}`);
  for (const curve of curves) {
    if (!curve.values?.length) continue;
    const path = curve.values.map((value, i) => `${i ? "L" : "M"}${(i / (curve.values.length - 1) * 800).toFixed(1)},${(150 - (value - min) / range * 140).toFixed(1)}`).join(" ");
    const node = document.createElementNS(svg.namespaceURI, "path");
    node.setAttribute("d", path);
    node.setAttribute("fill", "none");
    node.setAttribute("stroke", curve.color);
    node.setAttribute("stroke-width", "2");
    svg.append(node);
  }
  return svg;
}

byId("research-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadResearch(byId("asset-input").value.trim());
});
byId("demo-select").addEventListener("click", () => {
  byId("asset-input").value = "DEMO";
  loadResearch("DEMO");
});
byId("strategy-select").addEventListener("change", showParams);
byId("download-preset").addEventListener("click", () => {
  try {
    const strategy = byId("strategy-select").value;
    if (!strategies.some((item) => item.key === strategy)) throw new Error("Choose an available rule first");
    downloadJson({ version: 1, asset: activeAsset, strategy, params: selectedParams() }, `${activeAsset.toLowerCase()}-${strategy.toLowerCase()}-preset.json`);
    byId("preset-status").textContent = "Preset downloaded.";
  } catch (error) { byId("preset-status").textContent = error.message; }
});
byId("import-preset").addEventListener("change", async (event) => {
  try {
    const file = event.target.files?.[0];
    if (!file || file.size > 16_384) throw new Error("Choose a preset JSON file under 16 KB");
    const preset = JSON.parse(await file.text());
    const choice = strategies.find((item) => item.key === preset.strategy);
    if (preset.version !== 1 || !choice || !preset.params || typeof preset.params !== "object" || Array.isArray(preset.params)) throw new Error("Unsupported preset or rule unavailable on this server");
    if (Object.keys(preset.params).some((key) => !(key in choice.params))) throw new Error("Preset contains an unknown setting");
    byId("strategy-select").value = choice.key;
    showParams();
    for (const input of byId("strategy-params").querySelectorAll("input")) {
      const value = preset.params[input.name];
      if (value === undefined) continue;
      if (typeof value !== typeof choice.params[input.name] || (typeof value === "number" && !Number.isFinite(value))) throw new Error(`Invalid value for ${input.name}`);
      input.value = value;
    }
    if (typeof preset.asset === "string" && /^[A-Za-z0-9._&-]{1,24}$/.test(preset.asset)) {
      byId("asset-input").value = preset.asset;
      await loadResearch(preset.asset);
    }
    byId("preset-status").textContent = "Preset loaded. Run backtest to compare it.";
  } catch (error) { byId("preset-status").textContent = error.message; }
  event.target.value = "";
});
byId("try-example").addEventListener("click", () => {
  byId("strategy-select").value = "SMACrossover";
  showParams();
  byId("strategy-form").requestSubmit();
});
byId("strategy-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const root = byId("strategy-result");
  root.replaceChildren();
  lastExperiment = null;
  byId("download-experiment").hidden = true;
  line(root, "Running historical replay…");
  try {
    const params = selectedParams();
    const result = await tool("strategy_backtest", { asset: activeAsset, strategy: byId("strategy-select").value, params, days: 365, include_series: true });
    root.replaceChildren();
    if (result.warning) line(root, result.warning, "research-warning");
    if (result.lookahead_violations?.length) return;
    if (result.experiment.price_source === "synthetic-demo") line(root, "SYNTHETIC DEMO — all returns below come from invented prices and say nothing about a real stock.", "research-warning");
    if (result.bars < 100 || result.metrics.trades === 0) line(root, "Too little history or no completed trades: these metrics do not establish performance. Cache at least a year of prices before comparing rules.", "research-warning");
    line(root, `${result.strategy}: ${result.metrics.total_return_pct.toFixed(2)}% total return · ${result.metrics.trades} trades · Sharpe ${result.metrics.sharpe.toFixed(2)} · maximum drawdown ${result.metrics.max_drawdown_pct.toFixed(2)}%`, "research-item-title");
    if (result.benchmarks) {
      const buy = result.benchmarks.buy_and_hold;
      const risk = result.benchmarks.risk_matched;
      const trailing = result.benchmarks.trailing_volatility;
      line(root, `Same dates: buy and hold ${buy.total_return_pct.toFixed(2)}% · cash blended comparison ${risk.total_return_pct.toFixed(2)}% · strategy ${result.metrics.total_return_pct.toFixed(2)}%`, "research-item-title");
      line(root, trailing.available ? `Pre-set 10% volatility target baseline ${trailing.total_return_pct.toFixed(2)}% · maximum drawdown ${trailing.max_drawdown_pct.toFixed(2)}%. ${trailing.note}` : trailing.note, trailing.available ? "" : "research-warning");
      line(root, `Buy and hold maximum drawdown ${buy.max_drawdown_pct.toFixed(2)}%. ${result.benchmarks.method}`);
      line(root, `${risk.note} Stock fraction ${Math.round(risk.stock_fraction * 100)}%; target volatility ${risk.target_annual_volatility_pct.toFixed(2)}%, achieved ${risk.achieved_annual_volatility_pct.toFixed(2)}%.`, "research-warning");
    }
    line(root, `${result.bars} bars · ${result.metrics.note}`);
    line(root, `Price source ${result.experiment.price_source || "unknown"} · currency ${result.experiment.currency || "unknown"} · adjustment ${result.experiment.adjustment_policy} · snapshot ${result.experiment.price_snapshot_sha256.slice(0, 12)}…`);
    for (const caveat of result.experiment.limitations) line(root, caveat, "research-warning");
    const curves = [{ name: "Rule", values: result.equity_curve, color: "#60a5fa" }];
    if (result.benchmarks) curves.unshift(
      ...(result.benchmarks.trailing_volatility.available ? [{ name: "Trailing volatility", values: result.benchmarks.trailing_volatility.equity_curve, color: "#a78bfa" }] : []),
      { name: "Cash blended", values: result.benchmarks.risk_matched.equity_curve, color: "#eab308" },
      { name: "Buy and hold", values: result.benchmarks.buy_and_hold.equity_curve, color: "#22c55e" },
    );
    line(root, `Blue: rule · Green: buy and hold${result.benchmarks?.trailing_volatility.available ? " · Purple: pre-set risk baseline" : ""} · Yellow: hindsight cash blend`);
    const chart = equityChart(curves);
    if (chart) root.append(chart);
    lastExperiment = result;
    byId("download-experiment").hidden = false;
  } catch (error) { root.replaceChildren(); line(root, error.message, "research-warning"); }
});
byId("download-experiment").addEventListener("click", () => {
  if (!lastExperiment) return;
  downloadJson(lastExperiment, `${lastExperiment.experiment.asset.toLowerCase()}-${lastExperiment.experiment.strategy.toLowerCase()}-experiment.json`);
});
byId("options-button").addEventListener("click", async () => {
  const root = byId("options-result");
  root.replaceChildren();
  line(root, "Running model-priced replay…");
  try {
    const result = await tool("options_backtest", { asset: activeAsset, days: 365, step: 5 });
    root.replaceChildren();
    line(root, result.pricing_note, "research-warning");
    if (result.trades === 0) return line(root, "No sampled trades. Cache a longer price history before interpreting an options replay.", "research-warning");
    line(root, `${result.trades} sampled signals · average underlying directional move ${(result.avg_underlying_return * 100).toFixed(2)}% · average simulated option return ${(result.avg_option_return * 100).toFixed(2)}%`);
  } catch (error) { root.replaceChildren(); line(root, error.message, "research-warning"); }
});

loadResearch(activeAsset);
loadStrategies();
