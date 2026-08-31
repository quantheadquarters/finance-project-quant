/* Alpha Engine — research terminal frontend.
   Read-only: fetches JSON from /api/dashboard and /api/asset/<SYMBOL> and
   renders it. No framework, no build step, no external deps (strict CSP) —
   plain DOM + hand-rolled inline SVG charts following the dataviz mark specs. */

"use strict";

// ---- helpers -------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

const fmtPct = (v, d = 1) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
const fmtSignedPct = (v, d = 2) =>
  v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
const fmtNum = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const fmtDate = (v) =>
  v
    ? new Date(v).toLocaleDateString("en-US", {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      })
    : "—";
const titleize = (s) =>
  String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

// Read a CSS custom property so SVG fills follow the active theme.
const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Fixed categorical assignment: color follows the market, never its rank.
const marketColor = (m) => cssVar(`--cat-${m}`) || cssVar("--muted");
const dirClass = (d) => (d === "bullish" ? "bull" : d === "bearish" ? "bear" : "neutral");
const pill = (text, cls) => `<span class="pill ${cls}">${esc(titleize(text))}</span>`;

// ---- tooltip -------------------------------------------------------------

const tooltip = $("tooltip");
function showTooltip(html, x, y) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  const pad = 12;
  const rect = tooltip.getBoundingClientRect();
  let left = x + pad;
  if (left + rect.width > window.innerWidth - pad) left = x - rect.width - pad;
  tooltip.style.left = Math.max(pad, left) + "px";
  tooltip.style.top = Math.max(pad, y - rect.height - pad) + "px";
}
const hideTooltip = () => { tooltip.hidden = true; };
// Attach hover tooltip to every node matching sel inside root, html from dataset.
function wireTips(root, sel, htmlFn) {
  root.querySelectorAll(sel).forEach((node) => {
    node.addEventListener("mousemove", (e) => showTooltip(htmlFn(node.dataset), e.clientX, e.clientY));
    node.addEventListener("mouseleave", hideTooltip);
  });
}

// ---- KPI tiles -----------------------------------------------------------

function renderKpis(p) {
  const o = p.outcomes || {};
  $("k-total").textContent = p.total_records ?? 0;
  // Served, not hardcoded — the footer used to claim v0.1.0 on 0.5.0 code.
  if (p.version) $("version").textContent = "v" + p.version;
  $("k-assets").textContent = p.latest_count ?? 0;

  const hit = $("k-hit");
  const hitSub = $("k-hit-sub");
  if (o.hit_rate_suppressed) {
    // The number was withheld on purpose: too few records could be scored, and
    // the ones that dropped out are whole assets rather than a random sample.
    // Showing a bare "—" here would read as "no data yet", which is a different
    // and much less alarming thing than "this figure would mislead you".
    hit.textContent = "withheld";
    hit.className = "metric muted";
    hitSub.textContent = `only ${o.records_scored}/${o.records_total} records scoreable — missing prices for ${(o.skipped_assets || []).join(", ")}`;
    hitSub.title = o.hit_rate_suppressed;
  } else {
    hit.textContent = fmtPct(o.hit_rate);
    hit.className = "metric" + (o.hit_rate != null ? (o.hit_rate >= 0.5 ? " bull" : " bear") : "");
    hitSub.textContent = o.resolved ? `${o.hits}/${o.resolved} resolved · ${o.pending || 0} pending` : "awaiting resolved signals";
    hitSub.title = "";
  }

  const ret = $("k-ret");
  if (o.hit_rate_suppressed) {
    // Withheld for the same reason as the hit rate, and it is the more fragile
    // of the two: the biased subset flipped this figure's sign entirely.
    ret.textContent = "withheld";
    ret.className = "metric muted";
    $("k-ret-sub").textContent = "same partial sample as hit rate";
  } else {
    ret.textContent = o.avg_realized_return == null ? "—" : fmtSignedPct(o.avg_realized_return);
    ret.className = "metric" + (o.avg_realized_return != null ? (o.avg_realized_return >= 0 ? " bull" : " bear") : "");
    $("k-ret-sub").textContent = o.avg_realized_return != null ? "mean realized move" : "no resolved outcomes yet";
  }
}

// ---- regime + risk hero --------------------------------------------------

function regimePolarity(gate) {
  const g = (gate || "").toLowerCase();
  if (g.includes("bull") || g.includes("risk_on") || g.includes("risk-on")) return "bull";
  if (g.includes("bear") || g.includes("risk_off") || g.includes("risk-off")) return "bear";
  return "neutral";
}

function renderGauge(score) {
  // Semicircular arc, 0..100 mapped left→right, colored by band.
  const el = $("risk-gauge");
  const s = Math.max(0, Math.min(100, score ?? 0));
  const cx = 50, cy = 58, r = 40;
  const a0 = Math.PI, a1 = a0 + (s / 100) * Math.PI; // 180°→ up to 360°
  const pt = (a) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [sx, sy] = pt(a0), [ex, ey] = pt(a1), [bx, by] = pt(Math.PI * 2);
  const color = s >= 66 ? cssVar("--bull") : s >= 40 ? cssVar("--warn") : cssVar("--bear");
  el.innerHTML =
    `<path d="M${sx.toFixed(1)},${sy.toFixed(1)} A${r},${r} 0 0 1 ${bx.toFixed(1)},${by.toFixed(1)}" fill="none" stroke="${cssVar("--line-2")}" stroke-width="8" stroke-linecap="round"/>` +
    `<path d="M${sx.toFixed(1)},${sy.toFixed(1)} A${r},${r} 0 0 1 ${ex.toFixed(1)},${ey.toFixed(1)}" fill="none" stroke="${color}" stroke-width="8" stroke-linecap="round"/>`;
  const sc = $("risk-score");
  sc.textContent = s;
  sc.style.color = color;
}

function renderHero(p) {
  const risk = p.risk || {};
  const pol = regimePolarity(risk.regime_gate);
  const badge = $("regime-badge");
  badge.className = "regime-badge " + pol;
  badge.textContent = pol === "bull" ? "▲" : pol === "bear" ? "▼" : "◆";
  $("regime-name").textContent = risk.regime_gate ? titleize(risk.regime_gate) : "No regime read";
  $("regime-meta").textContent = risk.regime_confidence
    ? `${fmtPct(risk.regime_confidence, 0)} confidence · HMM overlay`
    : "insufficient history for a regime read";

  const port = p.portfolio || {};
  const bias = $("hero-bias");
  if (port.signal_count) {
    bias.innerHTML = `${pill(port.direction, dirClass(port.direction))} <span style="margin-left:8px">${(port.net_bias * 100).toFixed(0)}%</span>`;
    $("hero-bias-cap").textContent = `net bias across ${port.directional_count} directional of ${port.signal_count} signals`;
  } else {
    bias.textContent = "—";
  }
  renderGauge(risk.risk_score);
}

// ---- charts: markets bar -------------------------------------------------

function renderMarkets(byMarket) {
  const el = $("markets-chart");
  const entries = Object.entries(byMarket || {});
  if (!entries.length) return void (el.innerHTML = '<div class="chart-empty">No signals recorded yet</div>');
  const max = Math.max(...entries.map(([, c]) => c));
  el.innerHTML =
    `<div class="bars">` +
    entries
      .map(([m, c]) => {
        const w = ((c / max) * 100).toFixed(1);
        return `<div class="bar-row">
          <div class="bar-name"><span class="pill market"><span class="swatch" style="background:${marketColor(m)}"></span>${esc(titleize(m))}</span></div>
          <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${marketColor(m)}"></div></div>
          <div class="bar-value">${c}</div>
        </div>`;
      })
      .join("") +
    `</div>`;
}

// ---- charts: calibration -------------------------------------------------

function renderCalibration(cal) {
  const el = $("calibration-chart");
  const bins = (cal || []).filter((b) => b.count > 0);
  if (!bins.length) return void (el.innerHTML = '<div class="chart-empty">Awaiting resolved signals to score calibration</div>');

  const W = 420, H = 210, pad = { t: 24, r: 14, b: 34, l: 40 };
  const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b, cw = plotW / bins.length;
  const gridC = cssVar("--grid"), muted = cssVar("--muted"), textC = cssVar("--text");
  let svg = `<svg class="svg-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Calibration: realized hit rate per stated-confidence bucket">`;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + plotH * (1 - i / 4);
    svg += `<line x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}" stroke="${gridC}" stroke-dasharray="3,3"/>`;
    svg += `<text x="${pad.l - 7}" y="${y + 3}" text-anchor="end" fill="${muted}" font-size="9" font-family="monospace">${i * 25}%</text>`;
  }
  bins.forEach((b, i) => {
    const bw = Math.min(cw * 0.62, 44);
    const x = pad.l + i * cw + (cw - bw) / 2;
    const hr = b.hit_rate ?? 0;
    const bh = Math.max(hr * plotH, 1), by = H - pad.b - bh;
    const color = hr >= 0.5 ? cssVar("--bull") : cssVar("--bear");
    const r = Math.min(4, bw / 2, bh);
    // rounded data-end, square baseline
    svg += `<path d="M${x},${H - pad.b} v${-(bh - r)} q0,${-r} ${r},${-r} h${bw - 2 * r} q${r},0 ${r},${r} v${bh - r} z" fill="${color}" class="cal-bar" data-lo="${b.lo}" data-hi="${b.hi}" data-count="${b.count}" data-hits="${b.hits}" data-hr="${b.hit_rate ?? ""}"/>`;
    svg += `<text x="${x + bw / 2}" y="${H - pad.b + 14}" text-anchor="middle" fill="${muted}" font-size="8" font-family="monospace">${(b.lo * 100).toFixed(0)}–${(b.hi * 100).toFixed(0)}</text>`;
    if (b.hit_rate != null)
      svg += `<text x="${x + bw / 2}" y="${by - 5}" text-anchor="middle" fill="${textC}" font-size="9" font-family="monospace">${(hr * 100).toFixed(0)}%</text>`;
  });
  // diagonal = perfect calibration
  svg += `<line x1="${pad.l}" y1="${H - pad.b}" x2="${W - pad.r}" y2="${pad.t}" stroke="${cssVar("--info")}" stroke-opacity="0.5" stroke-dasharray="4,4"/>`;
  svg += `<text x="${W - pad.r}" y="${pad.t - 6}" text-anchor="end" fill="${muted}" font-size="8" font-family="monospace">perfect calibration</text>`;
  svg += `</svg>`;
  el.innerHTML = svg;
  wireTips(el, ".cal-bar", (d) => {
    const hr = d.hr === "" ? "—" : (Number(d.hr) * 100).toFixed(0) + "%";
    return `<b>confidence ${(d.lo * 100).toFixed(0)}–${(d.hi * 100).toFixed(0)}%</b><br/>${d.hits}/${d.count} hits → ${hr}`;
  });
}

// ---- charts: outcome donut -----------------------------------------------

function renderOutcomes(o) {
  const el = $("outcome-chart");
  if (!o || !o.total) return void (el.innerHTML = '<div class="chart-empty">No recorded outcomes yet</div>');
  const segs = [
    { label: "Hits", count: o.hits || 0, color: cssVar("--bull") },
    { label: "Misses", count: (o.resolved || 0) - (o.hits || 0), color: cssVar("--bear") },
    { label: "Pending", count: o.pending || 0, color: cssVar("--warn") },
    { label: "N/A", count: o.not_applicable || 0, color: cssVar("--muted") },
  ].filter((s) => s.count > 0);
  const total = segs.reduce((a, s) => a + s.count, 0) || 1;

  const R = 52, C = 60, sw = 14, circ = 2 * Math.PI * R, gap = 3;
  let off = 0;
  let ring = "";
  segs.forEach((s) => {
    const frac = s.count / total;
    const len = Math.max(frac * circ - gap, 0.5);
    ring += `<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="${s.color}" stroke-width="${sw}" stroke-linecap="round" stroke-dasharray="${len} ${circ - len}" stroke-dashoffset="${-off}" transform="rotate(-90 ${C} ${C})"/>`;
    off += frac * circ;
  });
  const legend = segs
    .map((s) => `<div class="row"><span class="swatch" style="background:${s.color}"></span>${s.label}<span class="lv">${s.count} · ${((s.count / total) * 100).toFixed(0)}%</span></div>`)
    .join("");
  el.innerHTML =
    `<div class="donut-wrap">
      <svg viewBox="0 0 120 120" width="120" height="120" role="img" aria-label="Outcome mix donut">
        ${ring}
        <text x="${C}" y="${C - 3}" text-anchor="middle" fill="${cssVar("--text")}" font-size="21" font-weight="700" font-family="monospace">${fmtPct(o.hit_rate, 0)}</text>
        <text x="${C}" y="${C + 13}" text-anchor="middle" fill="${cssVar("--muted")}" font-size="9" font-family="monospace">hit rate</text>
      </svg>
      <div class="donut-legend">${legend}</div>
    </div>`;
}

// ---- risk: position sizing ----------------------------------------------

function renderPositions(risk) {
  const el = $("positions");
  const ps = (risk && risk.position_sizes) || [];
  if (!ps.length) return void (el.innerHTML = '<div class="chart-empty">Position sizing needs cached price history</div>');
  const max = Math.max(...ps.map((p) => p.weight));
  const rows = [...ps].sort((a, b) => b.weight - a.weight);
  el.innerHTML =
    `<div class="bars">` +
    rows
      .map((p) => {
        const w = ((p.weight / max) * 100).toFixed(1);
        return `<div class="bar-row pos-row" data-asset="${esc(p.asset)}" data-w="${(p.weight * 100).toFixed(1)}" data-av="${(p.annualized_vol * 100).toFixed(0)}" data-dv="${(p.daily_vol * 100).toFixed(2)}">
          <div class="bar-name">${esc(p.asset)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${cssVar("--info")}"></div></div>
          <div class="bar-value">${(p.weight * 100).toFixed(1)}%</div>
        </div>`;
      })
      .join("") +
    `</div>`;
  wireTips(el, ".pos-row", (d) => `<b>${esc(d.asset)}</b><br/>weight ${d.w}% · annualized vol ${d.av}%<br/>daily vol ${d.dv}%`);
}

// ---- risk: tail risk table ----------------------------------------------

function renderTails(risk) {
  const el = $("tails");
  const tr = (risk && risk.tail_risks) || [];
  if (!tr.length) return void (el.innerHTML = '<div class="chart-empty">Tail risk needs cached price history</div>');
  const rows = [...tr]
    .sort((a, b) => a.var_95 - b.var_95)
    .map(
      (t) => `<tr>
        <td class="asset">${esc(t.asset)}</td>
        <td class="num neg">${fmtSignedPct(t.var_95)}</td>
        <td class="num neg">${fmtSignedPct(t.cvar_95)}</td>
        <td class="num muted">${fmtSignedPct(t.max_drawdown)}</td>
        <td class="num ${t.current_drawdown <= -0.1 ? "neg" : "muted"}">${fmtSignedPct(t.current_drawdown)}</td>
      </tr>`
    )
    .join("");
  let html =
    `<div class="table-wrap"><table>
      <thead><tr><th>Asset</th><th>VaR&nbsp;95%</th><th>CVaR&nbsp;95%</th><th>Max&nbsp;DD</th><th>Curr&nbsp;DD</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  (risk.concentration_warnings || []).forEach((w) => (html += `<div class="alert">${esc(w)}</div>`));
  el.innerHTML = html;
}

// ---- portfolio: bias + conviction + correlation --------------------------

function corrColor(v) {
  if (v == null) return "transparent";
  const a = Math.min(Math.abs(v), 1) * 0.6;
  const rgb = v >= 0 ? cssVar("--div-pos") : cssVar("--div-neg");
  return `rgba(${rgb}, ${a})`;
}

function renderPortfolio(p) {
  const el = $("portfolio");
  if (!p || !p.signal_count) return void (el.innerHTML = '<div class="chart-empty">No signals recorded yet</div>');
  let html = '<div class="hero-grid" style="grid-template-columns: 1fr 1fr; align-items:start">';

  // left: conviction weights
  html += "<div>";
  if (p.diversification_score != null)
    html += `<div style="margin-bottom:12px"><span style="font:700 18px var(--mono)">${(p.diversification_score * 100).toFixed(0)}%</span> <span style="color:var(--muted);font-size:12px">diversification — 100% = uncorrelated moves</span></div>`;
  const weights = Object.entries(p.conviction_weights || {}).sort((a, b) => b[1] - a[1]);
  if (weights.length) {
    const max = Math.max(...weights.map(([, w]) => w));
    html += '<div class="label">Conviction share</div><div class="bars">';
    weights.forEach(([asset, w]) => {
      html += `<div class="bar-row">
        <div class="bar-name">${esc(asset)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:${((w / max) * 100).toFixed(1)}%;background:${cssVar("--accent-2")}"></div></div>
        <div class="bar-value">${(w * 100).toFixed(0)}%</div>
      </div>`;
    });
    html += "</div>";
  }
  (p.concentration_flags || []).forEach((f) => (html += `<div class="alert">${esc(f)}</div>`));
  html += "</div>";

  // right: correlation heatmap
  const m = p.correlations;
  html += "<div>";
  if (m && m.assets && m.assets.length >= 2) {
    html += `<div class="label">Return correlation <span class="label-hint">${m.window}-day window</span></div>`;
    html += '<div class="table-wrap"><table class="corr-table"><thead><tr><th></th>';
    m.assets.forEach((a) => (html += `<th>${esc(a)}</th>`));
    html += "</tr></thead><tbody>";
    m.assets.forEach((a, i) => {
      html += `<tr><th class="rowhead">${esc(a)}</th>`;
      m.matrix[i].forEach((v) => {
        const txt = v == null ? "·" : v.toFixed(2);
        html += `<td class="cell" style="background:${corrColor(v)}">${txt}</td>`;
      });
      html += "</tr>";
    });
    html += "</tbody></table></div>";
    html += `<div class="legend"><span>−1</span><span class="legend-scale" style="background:linear-gradient(90deg, rgba(${cssVar("--div-neg")},0.6), var(--surface-2), rgba(${cssVar("--div-pos")},0.6))"></span><span>+1</span></div>`;
  } else {
    html += '<div class="chart-empty">Need ≥2 assets with cached prices for a correlation matrix</div>';
  }
  html += "</div></div>";
  el.innerHTML = html;
}

// ---- signal feed (sortable + filterable) ---------------------------------

const feed = { signals: [], markets: new Set(), search: "", sortKey: "recorded_at", sortDir: -1, active: new Set() };

function buildMarketChips() {
  const wrap = $("market-chips");
  wrap.innerHTML =
    `<span class="chip${feed.active.size === 0 ? " active" : ""}" data-market="__all">All</span>` +
    [...feed.markets]
      .map(
        (m) => `<span class="chip${feed.active.has(m) ? " active" : ""}" data-market="${esc(m)}"><span class="swatch" style="background:${marketColor(m)}"></span>${esc(titleize(m))}</span>`
      )
      .join("");
  wrap.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      const m = chip.dataset.market;
      if (m === "__all") feed.active.clear();
      else feed.active.has(m) ? feed.active.delete(m) : feed.active.add(m);
      buildMarketChips();
      renderFeedRows();
    })
  );
}

function sortedFilteredSignals() {
  let rows = feed.signals;
  if (feed.active.size) rows = rows.filter((r) => feed.active.has(r.market));
  if (feed.search) {
    const q = feed.search.toLowerCase();
    rows = rows.filter((r) => (r.asset + " " + (r.thesis || "")).toLowerCase().includes(q));
  }
  const k = feed.sortKey;
  return [...rows].sort((a, b) => {
    let av = a[k], bv = b[k];
    if (k === "recorded_at") { av = new Date(av).getTime(); bv = new Date(bv).getTime(); }
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") return feed.sortDir * av.localeCompare(bv);
    return feed.sortDir * (av - bv);
  });
}

function renderFeedRows() {
  const tbody = document.querySelector("#signals-table tbody");
  const rows = sortedFilteredSignals();
  if (!rows.length) return void (tbody.innerHTML = `<tr><td colspan="8" class="muted" style="text-align:center;padding:24px">No signals match this filter</td></tr>`);
  tbody.innerHTML = rows
    .map((r) => {
      const inv = r.invalidation_level != null ? fmtNum(r.invalidation_level) : "—";
      const sources = (r.sources || [])
        .map((s) => `<span class="source-tag ${dirClass(s.direction)}">${esc(s.name)}</span>`)
        .join("");
      return `<tr data-asset="${esc(r.asset)}" title="Show ${esc(r.asset)} history">
        <td class="asset">${esc(r.asset)}</td>
        <td><span class="pill market"><span class="swatch" style="background:${marketColor(r.market)}"></span>${esc(titleize(r.market))}</span></td>
        <td>${pill(r.direction, dirClass(r.direction))}</td>
        <td class="num">${fmtPct(r.confidence)}</td>
        <td class="num muted">${inv}</td>
        <td><div class="source-list">${sources}</div></td>
        <td class="muted nowrap">${fmtDate(r.recorded_at)}</td>
        <td><pre class="thesis" title="${esc(r.thesis)}">${esc(r.thesis)}</pre></td>
      </tr>`;
    })
    .join("");
  tbody.querySelectorAll("tr[data-asset]").forEach((row) =>
    row.addEventListener("click", () => loadAssetHistory(row.dataset.asset))
  );
}

function updateSortHeaders() {
  document.querySelectorAll("#signals-table th.sortable").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    const active = th.dataset.key === feed.sortKey;
    if (active) th.classList.add(feed.sortDir === 1 ? "sort-asc" : "sort-desc");
    th.setAttribute("aria-sort", active ? (feed.sortDir === 1 ? "ascending" : "descending") : "none");
  });
}

function initFeed(signals) {
  feed.signals = signals || [];
  feed.markets = new Set(feed.signals.map((s) => s.market));
  buildMarketChips();
  updateSortHeaders();
  renderFeedRows();
}

// wire sort + search once (keyboard-operable: headers act as buttons)
function toggleSort(th) {
  const k = th.dataset.key;
  if (feed.sortKey === k) feed.sortDir *= -1;
  else { feed.sortKey = k; feed.sortDir = k === "asset" ? 1 : -1; }
  updateSortHeaders();
  renderFeedRows();
}
document.querySelectorAll("#signals-table th.sortable").forEach((th) => {
  th.tabIndex = 0;
  th.setAttribute("role", "button");
  th.addEventListener("click", () => toggleSort(th));
  th.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleSort(th); }
  });
});
$("feed-search").addEventListener("input", (e) => {
  feed.search = e.target.value.trim();
  renderFeedRows();
});

// ---- per-asset detail ----------------------------------------------------

function outcomeCell(o) {
  if (!o) return pill("no data", "neutral");
  if (o.status === "pending") return pill("pending", "neutral");
  if (o.status === "not_applicable") return '<span class="muted">n/a</span>';
  return o.hit ? pill("✓ hit", "bull") : pill("✗ miss", "bear");
}

async function loadAssetHistory(asset) {
  const panel = $("asset-detail");
  const tbody = document.querySelector("#history-table tbody");
  try {
    const resp = await fetch(`/api/asset/${encodeURIComponent(asset)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    $("detail-asset").textContent = `${data.asset} · ${data.count} recorded signal${data.count === 1 ? "" : "s"}`;
    tbody.innerHTML = (data.history || [])
      .map((r) => {
        const ret = r.outcome && r.outcome.realized_return;
        const cls = ret == null ? "muted" : ret >= 0 ? "pos" : "neg";
        return `<tr>
          <td class="muted nowrap">${fmtDate(r.recorded_at)}</td>
          <td>${pill(r.direction, dirClass(r.direction))}</td>
          <td class="num">${fmtPct(r.confidence)}</td>
          <td class="num muted">${r.entry_price != null ? fmtNum(r.entry_price) : "—"}</td>
          <td class="num muted">${r.invalidation_level != null ? fmtNum(r.invalidation_level) : "—"}</td>
          <td>${outcomeCell(r.outcome)}</td>
          <td class="num ${cls}">${ret == null ? "—" : fmtSignedPct(ret)}</td>
          <td><pre class="thesis">${esc(r.thesis)}</pre></td>
        </tr>`;
      })
      .join("");
    panel.hidden = false;
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">Failed to load history: ${esc(err.message)}</td></tr>`;
    panel.hidden = false;
  }
}
$("detail-close").addEventListener("click", () => { $("asset-detail").hidden = true; });

// ---- reveal-on-scroll ----------------------------------------------------

const revealObserver = new IntersectionObserver(
  (entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("shown"); revealObserver.unobserve(e.target); } }),
  { threshold: 0.08 }
);
document.querySelectorAll(".card.reveal").forEach((c) => revealObserver.observe(c));

// ---- main render loop ----------------------------------------------------

let lastPayload = null;

function render(p) {
  lastPayload = p;
  $("updated").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  renderKpis(p);
  renderHero(p);
  renderMarkets(p.assets_by_market);
  renderCalibration((p.outcomes || {}).calibration);
  renderOutcomes(p.outcomes);
  renderPositions(p.risk);
  renderTails(p.risk);
  renderPortfolio(p.portfolio);
  initFeed(p.latest_signals);
}

async function refresh() {
  try {
    const resp = await fetch("/api/dashboard");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    render(await resp.json());
  } catch (err) {
    $("updated").textContent = `load failed: ${err.message}`;
  }
}

// ---- theme -----------------------------------------------------------------
// theme-toggle.js owns the button and the stored preference. This page only
// needs to know the theme changed, because the charts are inline SVG whose
// fills are read from CSS custom properties at draw time — they do not restyle
// themselves and have to be redrawn.

document.addEventListener("themechange", () => {
  if (lastPayload) render(lastPayload);
});

$("refresh-btn").addEventListener("click", refresh);

refresh();
setInterval(refresh, 60_000);
