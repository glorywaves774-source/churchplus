/* Chart.js wrapper. Charts are declared in HTML:
 *   <canvas data-chart='{"type":"line","labels":[...],"series":[{"label":"...","data":[...]}]}'></canvas>
 * Colours come from the design tokens and follow light/dark mode automatically. */
(function () {
  "use strict";
  if (!window.Chart) { return; }
  var charts = [];

  function token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function palette() {
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    return {
      series: dark
        ? ["#9a86ff", "#e2c25a", "#4cc792", "#75a8ff", "#ff9f7b", "#c58cff"]
        : ["#5b3fd1", "#c9a227", "#1a8a5c", "#2367c9", "#d9673f", "#9b4fd6"],
      text: token("--cp-text-muted"),
      grid: dark ? "rgba(255,255,255,0.07)" : "rgba(18,32,74,0.07)",
      surface: token("--cp-surface-solid")
    };
  }

  function build(canvas) {
    var spec = JSON.parse(canvas.getAttribute("data-chart"));
    var p = palette();
    var type = spec.type || "line";
    var isRound = type === "doughnut" || type === "pie";
    var datasets = (spec.series || []).map(function (s, i) {
      var color = s.color || p.series[i % p.series.length];
      var ds = { label: s.label, data: s.data, borderColor: color, backgroundColor: color, borderWidth: 2 };
      if (type === "line") {
        ds.tension = 0.35; ds.pointRadius = 0; ds.pointHoverRadius = 4; ds.fill = i === 0 && spec.fill !== false;
        ds.backgroundColor = function (ctx) {
          var area = ctx.chart.chartArea;
          if (!area) { return color; }
          var g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
          g.addColorStop(0, color + "33"); g.addColorStop(1, color + "00");
          return g;
        };
      }
      if (type === "bar") { ds.borderRadius = 6; ds.borderSkipped = false; ds.maxBarThickness = 28; ds.borderWidth = 0; }
      if (isRound) { ds.backgroundColor = s.data.map(function (_, j) { return p.series[j % p.series.length]; }); ds.borderColor = p.surface; ds.borderWidth = 3; }
      return ds;
    });
    var money = spec.currency ? new Intl.NumberFormat(undefined, { style: "currency", currency: spec.currency, maximumFractionDigits: 0 }) : null;
    var fmt = function (v) { return money ? money.format(v) : new Intl.NumberFormat().format(v); };
    var options = {
      responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: datasets.length > 1 || isRound, position: "bottom", labels: { color: p.text, usePointStyle: true, boxWidth: 8, padding: 16, font: { family: "Inter Variable, Inter, sans-serif", size: 12 } } },
        tooltip: { padding: 10, cornerRadius: 10, callbacks: { label: function (c) { return " " + (c.dataset.label || c.label) + ": " + fmt(c.parsed.y !== undefined ? c.parsed.y : c.parsed); } } }
      }
    };
    if (isRound) { options.cutout = type === "doughnut" ? "68%" : 0; options.interaction = { mode: "nearest", intersect: true };
      options.plugins.tooltip.callbacks.label = function (c) { return " " + c.label + ": " + fmt(c.parsed); }; }
    else {
      options.scales = {
        x: { grid: { display: false }, border: { display: false }, ticks: { color: p.text, font: { size: 11 }, maxRotation: 0, autoSkipPadding: 12 }, stacked: !!spec.stacked },
        y: { grid: { color: p.grid }, border: { display: false }, beginAtZero: true, stacked: !!spec.stacked,
             ticks: { color: p.text, font: { size: 11 }, maxTicksLimit: 5, callback: function (v) { return money ? fmt(v) : v; } } }
      };
    }
    return new window.Chart(canvas, { type: type, data: { labels: spec.labels, datasets: datasets }, options: options });
  }

  function renderAll() {
    charts.forEach(function (c) { c.destroy(); });
    charts = Array.prototype.map.call(document.querySelectorAll("canvas[data-chart]"), build);
  }
  renderAll();

  new MutationObserver(function (m) {
    if (m.some(function (r) { return r.attributeName === "data-theme"; })) { renderAll(); }
  }).observe(document.documentElement, { attributes: true });
})();
