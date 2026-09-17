/* SVG chart builders: return strings, no dependencies.
 *
 * Two constraints shape all of this. The CSP forbids inline style attributes, so
 * every visual property is either a presentation attribute (x, y, width, fill) or a
 * CSS class — colour lives entirely in app.css, which is what makes theming CSS-only.
 * And a fixed pixel viewBox scales crisply, where preserveAspectRatio="none" would
 * stretch the label text.
 *
 * Sizing is left entirely to the `svg` rule in app.css (width:100%, height:auto).
 * Setting them as *attributes* here does not work: `height="auto"` is not a valid SVG
 * length and the browser logs an error for every chart while still rendering. */

"use strict";

var CHART_W = 640;
var CHART_H = 150;
var PAD_L = 4;
var PAD_B = 20;

function _scale(values) {
  var max = 0;
  for (var i = 0; i < values.length; i++) {
    var v = values[i];
    if (typeof v === "number" && isFinite(v) && v > max) max = v;
  }
  return max || 1;
}

/* An all-zero series must say so rather than drawing bars of height zero.
 * It is not a contrived case: `cost_usd` is null for every run whose model cannot be
 * priced, and a chart of invisible bars reads as a broken chart, not as "no data". */
function _allZero(values) {
  for (var i = 0; i < values.length; i++) {
    var v = values[i];
    if (typeof v === "number" && isFinite(v) && v > 0) return false;
  }
  return true;
}

/* Width and height are viewBox units, not pixels — but the ratio between them is what
 * the browser scales to the container, so a chart's aspect ratio has to match the shape
 * of the card it lives in. A 640x150 box in a quarter-width tile renders text slightly
 * under 10px; the same box across a full-width strip stretches it to 22px. Hence the
 * per-chart dimensions rather than one global pair. */
function _frame(inner, title, width, height) {
  width = width || CHART_W;
  height = height || CHART_H;
  return (
    '<figure><figcaption>' + esc(title) + "</figcaption>" +
    '<svg viewBox="0 0 ' + width + " " + height + '" ' +
    'role="img" aria-label="' + esc(title) + '">' +
    inner +
    '<line class="axis" x1="0" y1="' + (height - PAD_B) + '" x2="' + width +
    '" y2="' + (height - PAD_B) + '"></line>' +
    "</svg></figure>"
  );
}

/* Vertical bars, newest last. `labelFor` may return "" to skip a label. */
function bars(series, opts) {
  opts = opts || {};
  var values = series.map(function (d) { return d.value; });
  if (!series.length || _allZero(values)) return _empty(opts.title, opts.emptyNote);
  var max = opts.max || _scale(values);
  var usable = CHART_H - PAD_B - 6;
  var slot = (CHART_W - PAD_L * 2) / series.length;
  var width = Math.max(1, Math.min(slot - 2, 28));
  var out = "";
  for (var i = 0; i < series.length; i++) {
    var d = series[i];
    var value = typeof d.value === "number" && isFinite(d.value) ? d.value : 0;
    var h = Math.max(value > 0 ? 1 : 0, (value / max) * usable);
    var x = PAD_L + slot * i + (slot - width) / 2;
    var cls = "bar" + (d.cls ? " " + d.cls : "");
    out +=
      '<rect class="' + cls + '" x="' + x.toFixed(1) + '" y="' + (CHART_H - PAD_B - h).toFixed(1) +
      '" width="' + width.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="1">' +
      "<title>" + esc(d.label || "") + "</title></rect>";
  }
  return _frame(out, opts.title);
}

/* Two-part stacked bars — used for prompt vs completion tokens. */
function stack(series, opts) {
  opts = opts || {};
  var totals = series.map(function (d) { return (d.a || 0) + (d.b || 0); });
  if (!series.length || _allZero(totals)) return _empty(opts.title, opts.emptyNote);
  var max = _scale(totals);
  var usable = CHART_H - PAD_B - 6;
  var slot = (CHART_W - PAD_L * 2) / series.length;
  var width = Math.max(1, Math.min(slot - 2, 28));
  var out = "";
  for (var i = 0; i < series.length; i++) {
    var d = series[i];
    var ha = ((d.a || 0) / max) * usable;
    var hb = ((d.b || 0) / max) * usable;
    var x = PAD_L + slot * i + (slot - width) / 2;
    var base = CHART_H - PAD_B;
    out +=
      '<rect class="bar" x="' + x.toFixed(1) + '" y="' + (base - ha).toFixed(1) +
      '" width="' + width.toFixed(1) + '" height="' + ha.toFixed(1) + '" rx="1">' +
      "<title>" + esc(d.label || "") + "</title></rect>" +
      '<rect class="bar-2" x="' + x.toFixed(1) + '" y="' + (base - ha - hb).toFixed(1) +
      '" width="' + width.toFixed(1) + '" height="' + hb.toFixed(1) + '" rx="1">' +
      "<title>" + esc(d.label || "") + "</title></rect>";
  }
  var legend =
    '<div class="legend">' +
    '<span class="legend-item"><span class="swatch swatch-1"></span>' + esc(opts.aLabel || "a") + "</span>" +
    '<span class="legend-item"><span class="swatch swatch-2"></span>' + esc(opts.bLabel || "b") + "</span>" +
    "</div>";
  return _frame(out, opts.title) + legend;
}

/* A filled line, for a series over time. */
function line(series, opts) {
  opts = opts || {};
  var lineValues = series.map(function (d) { return d.value; });
  if (series.length < 2 || _allZero(lineValues)) return _empty(opts.title, opts.emptyNote);
  var max = opts.max || _scale(lineValues);
  var usable = CHART_H - PAD_B - 6;
  var step = (CHART_W - PAD_L * 2) / (series.length - 1);
  var points = [];
  for (var i = 0; i < series.length; i++) {
    var value = typeof series[i].value === "number" && isFinite(series[i].value) ? series[i].value : 0;
    var x = PAD_L + step * i;
    var y = CHART_H - PAD_B - (value / max) * usable;
    points.push(x.toFixed(1) + "," + y.toFixed(1));
  }
  var base = CHART_H - PAD_B;
  var area =
    '<polygon class="line-area" points="' + PAD_L + "," + base + " " +
    points.join(" ") + " " + (PAD_L + step * (series.length - 1)).toFixed(1) + "," + base + '"></polygon>';
  var stroke = '<polyline class="line" points="' + points.join(" ") + '"></polyline>';
  return _frame(area + stroke, opts.title);
}

/* A single proportion, 0..1. */
function meter(fraction, opts) {
  opts = opts || {};
  var clamped = Math.max(0, Math.min(1, typeof fraction === "number" && isFinite(fraction) ? fraction : 0));
  var h = 26;
  var inner =
    '<rect class="meter-track" x="0" y="' + ((CHART_H - PAD_B) / 2 - h / 2) + '" width="' + CHART_W +
    '" height="' + h + '" rx="4"></rect>' +
    '<rect class="meter-fill" x="0" y="' + ((CHART_H - PAD_B) / 2 - h / 2) + '" width="' +
    (CHART_W * clamped).toFixed(1) + '" height="' + h + '" rx="4"></rect>' +
    '<text class="axis-text" x="8" y="' + ((CHART_H - PAD_B) / 2 + 24) + '">' +
    esc(opts.caption || "") + "</text>";
  return _frame(inner, opts.title);
}

/* Context pressure per turn: a fraction of capacity, 0..1, with compaction markers.
 *
 * The one rule that shapes this: a turn with no budget snapshot renders a GAP, never an
 * interpolated point. `note_context_budget` is best-effort — its exception is swallowed —
 * so a missing snapshot means "not recorded", and drawing a line through it would invent
 * a measurement precisely where the harness admits it has none. Missing turns get an
 * axis tick instead, so the absence is visible rather than smoothed away.
 *
 * Scale is pinned to 0..1 rather than fitted to the data, so two runs are comparable by
 * eye and a run that never got near its window looks like one. */
var PRESSURE_W = 1180;
var PRESSURE_H = 240;
var PRESSURE_PAD_L = 34;   // room for the percentage labels

function pressure(series, opts) {
  opts = opts || {};
  var known = series.filter(function (d) {
    return typeof d.value === "number" && isFinite(d.value);
  });
  if (!series.length || !known.length) return _empty(opts.title, opts.emptyNote);

  var base = PRESSURE_H - PAD_B;
  var usable = base - 14;
  var span = PRESSURE_W - PRESSURE_PAD_L - 8;
  var step = series.length > 1 ? span / (series.length - 1) : 0;
  var xOf = function (i) {
    return series.length > 1 ? PRESSURE_PAD_L + step * i : PRESSURE_PAD_L + span / 2;
  };
  var yOf = function (v) { return base - Math.max(0, Math.min(1, v)) * usable; };

  // A percentage of the window means nothing without a scale to read it against.
  var out = "";
  [0.25, 0.5, 0.75, 1].forEach(function (level) {
    var y = yOf(level);
    out +=
      '<line class="grid" x1="' + PRESSURE_PAD_L + '" y1="' + y.toFixed(1) +
      '" x2="' + PRESSURE_W + '" y2="' + y.toFixed(1) + '"></line>' +
      '<text class="axis-text" x="' + (PRESSURE_PAD_L - 6) + '" y="' + (y + 3).toFixed(1) +
      '" text-anchor="end">' + Math.round(level * 100) + "%</text>";
  });

  if (typeof opts.threshold === "number") {
    var ty = yOf(opts.threshold).toFixed(1);
    out +=
      '<line class="threshold" x1="' + PRESSURE_PAD_L + '" y1="' + ty + '" x2="' + PRESSURE_W +
      '" y2="' + ty + '"></line>' +
      '<text class="axis-text threshold-text" x="' + (PRESSURE_W - 4) + '" y="' +
      (yOf(opts.threshold) - 5).toFixed(1) + '" text-anchor="end">compacts at ' +
      Math.round(opts.threshold * 100) + "%</text>";
  }

  // One polyline per contiguous stretch of recorded turns. A stretch of length 1 draws
  // no line, only its dot — which is the correct picture of an isolated measurement.
  var run = [];
  var flush = function () {
    if (run.length > 1) out += '<polyline class="line" points="' + run.join(" ") + '"></polyline>';
    run = [];
  };
  for (var i = 0; i < series.length; i++) {
    var d = series[i];
    var value = typeof d.value === "number" && isFinite(d.value) ? d.value : null;
    if (value === null) { flush(); continue; }
    run.push(xOf(i).toFixed(1) + "," + yOf(value).toFixed(1));
  }
  flush();

  for (var j = 0; j < series.length; j++) {
    var point = series[j];
    var x = xOf(j);
    var recorded = typeof point.value === "number" && isFinite(point.value);
    var tip = esc(point.label || "");
    if (point.marks) {
      out +=
        '<line class="mark' + (point.overflow ? " mark-err" : "") + '" x1="' + x.toFixed(1) +
        '" y1="4" x2="' + x.toFixed(1) + '" y2="' + base + '"><title>' + tip + "</title></line>";
    }
    if (recorded) {
      out +=
        '<circle class="dot' + (point.overflow ? " dot-err" : "") + '" cx="' + x.toFixed(1) +
        '" cy="' + yOf(point.value).toFixed(1) + '" r="4"><title>' + tip + "</title></circle>";
    } else {
      // The gap marker: on the axis, not on the line, so it cannot be misread as a value.
      out +=
        '<line class="gap-tick" x1="' + x.toFixed(1) + '" y1="' + (base - 5) + '" x2="' +
        x.toFixed(1) + '" y2="' + (base + 5) + '"><title>' + tip + "</title></line>";
    }
    if (point.tick) {
      // The first and last labels anchor inward: centred on the end of the span they
      // overflow the viewBox and the browser clips them mid-word.
      var anchor = j === 0 ? "start" : j === series.length - 1 ? "end" : "middle";
      out +=
        '<text class="axis-text" x="' + x.toFixed(1) + '" y="' + (base + 15) +
        '" text-anchor="' + anchor + '">' + esc(point.tick) + "</text>";
    }
  }
  return _frame(out, opts.title, PRESSURE_W, PRESSURE_H);
}

function _empty(title, note) {
  return (
    '<figure><figcaption>' + esc(title) + "</figcaption>" +
    '<div class="stat-sub">' + esc(note || "Not enough data yet.") + "</div></figure>"
  );
}
