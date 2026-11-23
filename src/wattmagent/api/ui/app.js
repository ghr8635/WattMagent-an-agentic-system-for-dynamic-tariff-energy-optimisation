"use strict";
// WattMagent web page. No framework, no build step. All text is set with textContent, never as HTML.
// The API key lives in this variable only: not in storage, not in the URL, not in a cookie.
(function () {
  var SVG = "http://www.w3.org/2000/svg";
  var apiKey = "";
  var day = "today";

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function sv(tag, attrs, text) {
    var e = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, String(attrs[k])); });
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function eur(x) { return x === null || x === undefined ? "-" : x.toFixed(2) + " EUR"; }
  function hhmm(iso) { return iso.slice(11, 16); }
  function sum(a) { return a.reduce(function (s, v) { return s + v; }, 0); }

  function setStatus(text, isError) {
    var s = $("status");
    s.textContent = text;
    s.className = isError ? "status error" : "status";
  }

  async function call(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({ "X-API-Key": apiKey }, opts.headers || {});
    var res = await fetch(path, opts);
    var body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      var why = body && body.detail ? (typeof body.detail === "string" ? body.detail : "request refused") : "HTTP " + res.status;
      var err = new Error(res.status === 401 || res.status === 403 ? "The API key was not accepted." : why);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function chartFrame(w, h) {
    return sv("svg", { viewBox: "0 0 " + w + " " + h, role: "img", preserveAspectRatio: "xMidYMid meet" });
  }
  function hourTicks(svg, slots, x0, bw, y) {
    slots.forEach(function (s, i) {
      if (s.hour % 3 === 0) svg.appendChild(sv("text", { x: x0 + i * bw + bw / 2, y: y, "text-anchor": "middle", "class": "tick" }, String(s.hour)));
    });
  }
  function nowMarker(svg, slots, x0, bw, y0, y1) {
    var firstOpen = slots.findIndex(function (s) { return !s.elapsed; });
    if (firstOpen > 0) svg.appendChild(sv("line", { x1: x0 + firstOpen * bw, x2: x0 + firstOpen * bw, y1: y0, y2: y1, "class": "nowline" }));
  }

  function activeSlots(v) {
    var n = v.slots.length, on = new Array(n).fill(false), s = v.series;
    Object.keys(s.cycles_kw).forEach(function (k) { s.cycles_kw[k].forEach(function (x, i) { if (x > 1e-9) on[i] = true; }); });
    ["tank_kw", "battery_charge_kw", "battery_discharge_kw"].forEach(function (k) {
      (s[k] || []).forEach(function (x, i) { if (x > 1e-9) on[i] = true; });
    });
    return on;
  }

  function drawPrice(v) {
    var host = $("chart-price"); clear(host);
    var W = 720, H = 220, x0 = 40, x1 = W - 8, y0 = 26, y1 = H - 24, n = v.slots.length, bw = (x1 - x0) / n;
    var prices = v.slots.map(function (s) { return s.price_eur_kwh * 100; });
    var lo = Math.min(0, Math.min.apply(null, prices)), hi = Math.max.apply(null, prices) * 1.1 || 1;
    function y(p) { return y1 - ((p - lo) / (hi - lo)) * (y1 - y0); }
    var svg = chartFrame(W, H); svg.setAttribute("aria-label", "Day-ahead price per hour in cents per kWh, planned hours shaded");
    var on = activeSlots(v);
    on.forEach(function (a, i) { if (a) svg.appendChild(sv("rect", { x: x0 + i * bw, y: y0, width: bw, height: y1 - y0, "class": "band" })); });
    for (var t = 0; t <= 4; t++) {
      var val = lo + ((hi - lo) * t) / 4;
      svg.appendChild(sv("line", { x1: x0, x2: x1, y1: y(val), y2: y(val), "class": "axis" }));
      svg.appendChild(sv("text", { x: x0 - 4, y: y(val) + 4, "text-anchor": "end", "class": "tick" }, val.toFixed(0)));
    }
    v.slots.forEach(function (s, i) {
      var top = y(prices[i]), base = y(0);
      var bar = sv("rect", { x: x0 + i * bw + 1, y: Math.min(top, base), width: Math.max(bw - 2, 1), height: Math.abs(base - top), "class": "price-bar" + (s.elapsed ? " past" : "") });
      bar.appendChild(sv("title", {}, hhmm(s.start) + "  " + prices[i].toFixed(2) + " ct/kWh"));
      svg.appendChild(bar);
    });
    svg.appendChild(sv("text", { x: 4, y: 10, "class": "charttitle" }, "ct/kWh"));
    nowMarker(svg, v.slots, x0, bw, y0, y1);
    hourTicks(svg, v.slots, x0, bw, H - 8);
    host.appendChild(svg);
    $("price-note").textContent = v.price_basis + ". Shaded: hours in which the plan uses energy. Faded: hours already past.";
  }

  function drawTimeline(v) {
    var host = $("chart-timeline"); clear(host);
    var s = v.series, n = v.slots.length, rows = [];
    Object.keys(s.cycles_kw).forEach(function (k) { rows.push({ name: k, data: s.cycles_kw[k], cls: "" }); });
    if (s.tank_kw && sum(s.tank_kw) > 0) rows.push({ name: "hot water tank", data: s.tank_kw, cls: " tank" });
    if (s.battery_charge_kw && sum(s.battery_charge_kw) > 0) rows.push({ name: "battery charging", data: s.battery_charge_kw, cls: " charge" });
    if (s.battery_discharge_kw && sum(s.battery_discharge_kw) > 0) rows.push({ name: "battery discharging", data: s.battery_discharge_kw, cls: " discharge" });
    var W = 720, x0 = 150, x1 = W - 8, rh = 26, H = rows.length * rh + 28, bw = (x1 - x0) / n;
    var svg = chartFrame(W, H); svg.setAttribute("aria-label", "Hours in which each appliance, the tank and the battery are planned to run");
    rows.forEach(function (r, ri) {
      var y = 4 + ri * rh;
      svg.appendChild(sv("text", { x: x0 - 8, y: y + 16, "text-anchor": "end", "class": "rowlabel" }, r.name));
      r.data.forEach(function (kw, i) {
        var on = kw > 1e-9;
        var c = sv("rect", { x: x0 + i * bw + 1, y: y, width: Math.max(bw - 2, 1), height: rh - 6, rx: 2, "class": "cell" + (on ? " on" + r.cls : "") + (v.slots[i].elapsed ? " past" : "") });
        c.appendChild(sv("title", {}, r.name + " " + hhmm(v.slots[i].start) + (on ? ": " + kw.toFixed(2) + " kW" : ": off")));
        svg.appendChild(c);
      });
    });
    nowMarker(svg, v.slots, x0, bw, 0, H - 24);
    hourTicks(svg, v.slots, x0, bw, H - 8);
    host.appendChild(svg);
    var list = $("items"); clear(list);
    v.items.forEach(function (it) {
      list.appendChild(el("li", "", it.appliance + ": " + hhmm(it.start_at) + " to " + hhmm(it.end_at) + ", " + it.energy_kwh.toFixed(2) + " kWh"));
    });
  }

  function drawEnergy(v, m) {
    var host = $("chart-energy"); clear(host);
    var s = v.series, n = v.slots.length;
    var cyc = v.slots.map(function (_, i) { return sum(Object.keys(s.cycles_kw).map(function (k) { return s.cycles_kw[k][i]; })); });
    var parts = [
      { cls: "seg-base", data: s.base_kw }, { cls: "seg-cycle", data: cyc },
      { cls: "seg-tank", data: s.tank_kw && s.tank_kw.length ? s.tank_kw : new Array(n).fill(0) },
      { cls: "seg-batt", data: s.battery_charge_kw && s.battery_charge_kw.length ? s.battery_charge_kw : new Array(n).fill(0) }
    ];
    var totals = v.slots.map(function (_, i) { return sum(parts.map(function (p) { return p.data[i]; })); });
    var W = 720, H = 220, x0 = 40, x1 = W - 8, y0 = 26, y1 = H - 24, bw = (x1 - x0) / n, measured = m ? m.kwh.filter(function (x) { return x !== null; }) : [], hi = Math.max.apply(null, totals.concat(measured)) * 1.1 || 1;
    function y(k) { return y1 - (k / hi) * (y1 - y0); }
    var svg = chartFrame(W, H); svg.setAttribute("aria-label", "Planned electricity use per hour in kW, stacked by source");
    for (var t = 0; t <= 4; t++) {
      var val = (hi * t) / 4;
      svg.appendChild(sv("line", { x1: x0, x2: x1, y1: y(val), y2: y(val), "class": "axis" }));
      svg.appendChild(sv("text", { x: x0 - 4, y: y(val) + 4, "text-anchor": "end", "class": "tick" }, val.toFixed(1)));
    }
    v.slots.forEach(function (sl, i) {
      var acc = 0;
      parts.forEach(function (p) {
        var kw = p.data[i];
        if (kw <= 1e-9) return;
        var r = sv("rect", { x: x0 + i * bw + 1, y: y(acc + kw), width: Math.max(bw - 2, 1), height: y(acc) - y(acc + kw), "class": p.cls });
        r.appendChild(sv("title", {}, hhmm(sl.start) + "  " + kw.toFixed(2) + " kW"));
        svg.appendChild(r);
        acc += kw;
      });
    });
    if (m) {
      var pts = [];
      m.kwh.forEach(function (x, i) {
        if (x === null) return;
        var cx = x0 + i * bw + bw / 2, cy = y(x);
        pts.push(cx.toFixed(1) + "," + cy.toFixed(1));
        var d = sv("circle", { cx: cx, cy: cy, r: 3.5, "class": "meas-dot" });
        d.appendChild(sv("title", {}, hhmm(v.slots[i].start) + "  measured " + x.toFixed(3) + " kWh"));
        svg.appendChild(d);
      });
      if (pts.length > 1) svg.appendChild(sv("polyline", { points: pts.join(" "), "class": "meas-line" }));
    }
    svg.appendChild(sv("text", { x: 4, y: 10, "class": "charttitle" }, "kW (bars: planned, dots: measured)"));
    nowMarker(svg, v.slots, x0, bw, y0, y1);
    hourTicks(svg, v.slots, x0, bw, H - 8);
    host.appendChild(svg);
    var lg = $("legend-energy"); clear(lg);
    [["l-base", "base load"], ["l-cycle", "appliance cycles"], ["l-tank", "hot water tank"], ["l-batt", "battery charging"]].forEach(function (p) {
      var d = el("span"); d.appendChild(el("i", p[0])); d.appendChild(document.createTextNode(p[1])); lg.appendChild(d);
    });
    if (m) { var md = el("span"); md.appendChild(el("i", "l-meas")); md.appendChild(document.createTextNode("measured (" + m.source + ")")); lg.appendChild(md); }
  }

  function lineChart(hostId, label, unit, data, slots, cls) {
    var host = $(hostId); clear(host);
    if (!data || !data.length) return;
    var W = 720, H = 150, x0 = 40, x1 = W - 8, y0 = 28, y1 = H - 24, n = slots.length, bw = (x1 - x0) / n;
    var lo = Math.min.apply(null, data), hi = Math.max.apply(null, data);
    if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
    function y(p) { return y1 - ((p - lo) / (hi - lo)) * (y1 - y0); }
    var svg = chartFrame(W, H); svg.setAttribute("aria-label", label);
    [lo, (lo + hi) / 2, hi].forEach(function (val) {
      svg.appendChild(sv("line", { x1: x0, x2: x1, y1: y(val), y2: y(val), "class": "axis" }));
      svg.appendChild(sv("text", { x: x0 - 4, y: y(val) + 4, "text-anchor": "end", "class": "tick" }, val.toFixed(1)));
    });
    var pts = data.map(function (d, i) { return (x0 + i * bw + bw / 2).toFixed(1) + "," + y(d).toFixed(1); }).join(" ");
    svg.appendChild(sv("polyline", { points: pts, "class": "line" + (cls ? " " + cls : "") }));
    svg.appendChild(sv("text", { x: 4, y: 10, "class": "charttitle" }, label + " (" + unit + ")"));
    nowMarker(svg, slots, x0, bw, y0, y1);
    hourTicks(svg, slots, x0, bw, H - 8);
    host.appendChild(svg);
  }

  function drawSummary(v) {
    var box = $("summary"); clear(box);
    function stat(value, name, cls) { var d = el("div", "stat" + (cls ? " " + cls : "")); d.appendChild(el("b", "", value)); d.appendChild(el("span", "", name)); box.appendChild(d); }
    stat(v.decision, "decision" + (v.decision === "optimised" ? " (verified)" : " (safe fallback)"), v.decision === "optimised" ? "ok" : "warn");
    stat(eur(v.cost.planned_eur), "planned bill");
    stat(eur(v.cost.run_immediately_eur), "if everything ran at once");
    stat(eur(v.cost.saving_eur), "difference (model, not a saving claim)", "ok");
    stat(v.date + " " + v.timezone, "planned day");
    var ul = $("labels"); clear(ul);
    v.labels.concat(["Cost basis: " + v.cost.basis + "."], v.left_out.length ? ["Not planned here: " + v.left_out.join(", ") + " (no verified weather)."] : [], v.reasons || []).forEach(function (t) { ul.appendChild(el("li", "", t)); });
  }

  function render(v, m) {
    var ban = $("banner");
    ban.hidden = !v.banner; ban.textContent = v.banner || "";
    drawSummary(v); drawPrice(v); drawTimeline(v); drawEnergy(v, m);
    var hasStore = v.series.battery_stored_kwh.length > 0 || v.series.tank_temp_c.length > 0;
    $("storage-card").hidden = !hasStore;
    lineChart("chart-battery", "Battery stored energy", "kWh", v.series.battery_stored_kwh, v.slots, "");
    lineChart("chart-tank", "Tank temperature", "deg C", v.series.tank_temp_c, v.slots, "tank");
  }

  function drawDecisions(rows) {
    var body = document.querySelector("#decisions tbody"); clear(body);
    if (!rows.length) { var tr0 = el("tr"); var td0 = el("td", "", "No decisions recorded yet."); td0.colSpan = 6; tr0.appendChild(td0); body.appendChild(tr0); return; }
    rows.forEach(function (r) {
      var tr = el("tr");
      [r.at ? r.at.replace("T", " ").slice(0, 19) : "-", r.kind || "-", r.decision || "-",
        r.verifier_passed === null || r.verifier_passed === undefined ? "-" : (r.verifier_passed ? "passed" : "rejected" + (r.rules && r.rules.length ? ": " + r.rules.join(", ") : "")),
        eur(r.cost_eur), (r.reasons || []).join("; ") || "-"].forEach(function (t) { tr.appendChild(el("td", "", t)); });
      body.appendChild(tr);
    });
  }

  function measuredNote(m, v, problem) {
    var n = $("measured-note");
    if (problem) { n.textContent = problem; return; }
    var planned = 0;
    v.slots.forEach(function (s, i) {
      if (m.kwh[i] === null) return;
      planned += v.series.base_kw[i] + sum(Object.keys(v.series.cycles_kw).map(function (k) { return v.series.cycles_kw[k][i]; })) +
        ((v.series.tank_kw || [])[i] || 0) + ((v.series.battery_charge_kw || [])[i] || 0);
    });
    n.textContent = "Measured: " + m.total_kwh.toFixed(2) + " kWh over " + m.hours_known + " finished hours with a reading; the plan for those same hours is " +
      planned.toFixed(2) + " kWh. The two are not expected to match: the plan uses a fixed base-load profile and assumed appliances, and the meter may cover more or less than the plan.";
  }

  async function loadMeasured() {
    try { return { m: await call("/v1/ui/consumption?day=" + day), problem: "" }; }
    catch (e) {
      if (e.status === 404) return { m: null, problem: "Measured consumption is not connected (see docs/web-ui.md)." };
      return { m: null, problem: e.message };
    }
  }

  async function load() {
    if (!apiKey) return;
    setStatus("Loading ...", false);
    try {
      var results = await Promise.all([call("/v1/ui/day?day=" + day), call("/v1/ui/decisions?limit=20"), loadMeasured()]);
      render(results[0], results[2].m); drawDecisions(results[1].decisions);
      measuredNote(results[2].m, results[0], results[2].problem);
      setStatus("Plan for " + results[0].date + " (" + results[0].day + "), made " + results[0].generated_at.slice(11, 16) + " UTC.", false);
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  function uid() {
    var a = new Uint8Array(12); crypto.getRandomValues(a);
    return Array.prototype.map.call(a, function (b) { return b.toString(16).padStart(2, "0"); }).join("");
  }

  async function ask(text) {
    var log = $("chat-log");
    log.appendChild(el("div", "msg me", text));
    try {
      var res = await call("/v1/plans", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": uid() }, body: JSON.stringify({ text: text }) });
      log.appendChild(el("div", "msg", res.message || res.status));
      load();
    } catch (e) {
      log.appendChild(el("div", "msg err", e.message));
    }
  }

  $("key-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    apiKey = $("key").value.trim();
    $("key").value = "";  // the field is emptied; the key is kept in memory only
    load();
  });
  $("chat-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var t = $("chat-text").value.trim();
    if (!t) return;
    if (!apiKey) { setStatus("Enter the API key first.", true); return; }
    $("chat-text").value = "";
    ask(t);
  });
  function pickDay(d) {
    day = d;
    $("day-today").setAttribute("aria-pressed", String(d === "today"));
    $("day-tomorrow").setAttribute("aria-pressed", String(d === "tomorrow"));
    load();
  }
  $("day-today").addEventListener("click", function () { pickDay("today"); });
  $("day-tomorrow").addEventListener("click", function () { pickDay("tomorrow"); });
  $("theme").addEventListener("click", function () {
    var root = document.documentElement;
    var dark = root.getAttribute("data-theme") === "dark" || (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    root.setAttribute("data-theme", dark ? "light" : "dark");
  });
})();
