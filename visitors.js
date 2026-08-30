// Visitors page — renders the "visits per day" chart from the JSON blob
// embedded in visitors.html (patched at commit time by
// .github/scripts/update_visitor_count.py). No client-side network
// requests, per this site's zero-external-request rule.
(function () {
  "use strict";

  var dataEl = document.getElementById("visitor-daily-data");
  var raw = {};
  try {
    raw = JSON.parse((dataEl && dataEl.textContent) || "{}");
  } catch (e) {
    raw = {};
  }

  var days = Object.keys(raw).sort();
  var series = days.map(function (d) {
    return { date: d, count: raw[d] };
  });

  var RANGE_DAYS = { "7d": 7, "4w": 28, "1y": 365, all: Infinity };
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  var svg = document.getElementById("visitorChart");
  var tooltip = document.getElementById("chartTooltip");
  var tooltipDate = tooltip.querySelector(".chart-tooltip__date");
  var tooltipValue = tooltip.querySelector(".chart-tooltip__value");
  var statTotal = document.getElementById("statTotal");
  var statAvg = document.getElementById("statAvg");
  var statPeak = document.getElementById("statPeak");
  var statPeakDate = document.getElementById("statPeakDate");
  var tableBody = document.getElementById("dataTableBody");
  var buttons = Array.prototype.slice.call(document.querySelectorAll(".range-btn"));
  var tableToggle = document.getElementById("tableToggle");
  var tableWrap = document.getElementById("dataTableWrap");

  var SVG_NS = "http://www.w3.org/2000/svg";
  var W = 720, H = 260, PAD_L = 44, PAD_R = 12, PAD_T = 16, PAD_B = 12;

  function formatNumber(n) {
    return n.toLocaleString("en-US");
  }

  function formatDate(iso) {
    var parts = iso.split("-");
    return MONTHS[parseInt(parts[1], 10) - 1] + " " + parseInt(parts[2], 10) + ", " + parts[0];
  }

  function sliceForRange(key) {
    var n = RANGE_DAYS[key];
    if (!isFinite(n)) return series.slice();
    return series.slice(Math.max(0, series.length - n));
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var norm = v / mag;
    var nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
    return nice * mag;
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function renderStats(slice) {
    var total = slice.reduce(function (s, p) { return s + p.count; }, 0);
    var avg = slice.length ? total / slice.length : 0;
    var peak = slice.reduce(function (m, p) { return p.count > m.count ? p : m; }, { count: -1, date: "" });

    statTotal.textContent = formatNumber(total);
    statAvg.textContent = avg.toFixed(1);
    if (slice.length && peak.count >= 0) {
      statPeak.textContent = formatNumber(peak.count);
      statPeakDate.textContent = formatDate(peak.date);
    } else {
      statPeak.textContent = "0";
      statPeakDate.textContent = "";
    }
  }

  function renderTable(slice) {
    clear(tableBody);
    for (var i = slice.length - 1; i >= 0; i--) {
      var tr = document.createElement("tr");
      var tdDate = document.createElement("td");
      tdDate.textContent = slice[i].date;
      var tdCount = document.createElement("td");
      tdCount.textContent = String(slice[i].count);
      tr.appendChild(tdDate);
      tr.appendChild(tdCount);
      tableBody.appendChild(tr);
    }
  }

  function renderEmptyChart(message) {
    clear(svg);
    var text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", String(W / 2));
    text.setAttribute("y", String(H / 2));
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("class", "chart-empty");
    text.textContent = message;
    svg.appendChild(text);
  }

  function renderChart(slice) {
    clear(svg);

    if (!slice.length) {
      renderEmptyChart("No data yet");
      return;
    }

    var maxVal = niceMax(slice.reduce(function (m, p) { return Math.max(m, p.count); }, 0));
    var innerW = W - PAD_L - PAD_R;
    var innerH = H - PAD_T - PAD_B;

    function xAt(i) {
      return slice.length === 1 ? PAD_L + innerW / 2 : PAD_L + (i / (slice.length - 1)) * innerW;
    }
    function yAt(v) {
      return maxVal === 0 ? PAD_T + innerH : PAD_T + innerH - (v / maxVal) * innerH;
    }

    [0, maxVal / 2, maxVal].forEach(function (v) {
      var gy = yAt(v);
      var line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", String(PAD_L));
      line.setAttribute("x2", String(W - PAD_R));
      line.setAttribute("y1", String(gy));
      line.setAttribute("y2", String(gy));
      line.setAttribute("class", "chart-grid");
      svg.appendChild(line);

      var label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("x", String(PAD_L - 8));
      label.setAttribute("y", String(gy + 3));
      label.setAttribute("text-anchor", "end");
      label.setAttribute("class", "chart-axis-label");
      label.textContent = formatNumber(Math.round(v));
      svg.appendChild(label);
    });

    var areaD = "M " + xAt(0) + " " + yAt(0);
    slice.forEach(function (p, i) { areaD += " L " + xAt(i) + " " + yAt(p.count); });
    areaD += " L " + xAt(slice.length - 1) + " " + yAt(0) + " Z";
    var area = document.createElementNS(SVG_NS, "path");
    area.setAttribute("d", areaD);
    area.setAttribute("class", "chart-area");
    svg.appendChild(area);

    var lineD = "";
    slice.forEach(function (p, i) { lineD += (i === 0 ? "M " : " L ") + xAt(i) + " " + yAt(p.count); });
    var linePath = document.createElementNS(SVG_NS, "path");
    linePath.setAttribute("d", lineD);
    linePath.setAttribute("class", "chart-line");
    svg.appendChild(linePath);

    var lastI = slice.length - 1;
    var endDot = document.createElementNS(SVG_NS, "circle");
    endDot.setAttribute("cx", String(xAt(lastI)));
    endDot.setAttribute("cy", String(yAt(slice[lastI].count)));
    endDot.setAttribute("r", "4");
    endDot.setAttribute("class", "chart-dot");
    svg.appendChild(endDot);

    var crosshair = document.createElementNS(SVG_NS, "line");
    crosshair.setAttribute("x1", String(xAt(lastI)));
    crosshair.setAttribute("x2", String(xAt(lastI)));
    crosshair.setAttribute("y1", String(PAD_T));
    crosshair.setAttribute("y2", String(H - PAD_B));
    crosshair.setAttribute("class", "chart-crosshair");
    crosshair.style.opacity = "0";
    svg.appendChild(crosshair);

    var hoverDot = document.createElementNS(SVG_NS, "circle");
    hoverDot.setAttribute("r", "5");
    hoverDot.setAttribute("class", "chart-hover-dot");
    hoverDot.style.opacity = "0";
    svg.appendChild(hoverDot);

    var hit = document.createElementNS(SVG_NS, "rect");
    hit.setAttribute("x", String(PAD_L));
    hit.setAttribute("y", String(PAD_T));
    hit.setAttribute("width", String(innerW));
    hit.setAttribute("height", String(innerH));
    hit.setAttribute("fill", "transparent");
    hit.setAttribute("class", "chart-hit");
    svg.appendChild(hit);

    function indexFromClientX(clientX) {
      var rect = svg.getBoundingClientRect();
      var relX = ((clientX - rect.left) / rect.width) * W;
      var t = innerW === 0 ? 0 : (relX - PAD_L) / innerW;
      var i = Math.round(t * (slice.length - 1));
      return Math.max(0, Math.min(slice.length - 1, i));
    }

    function showAt(i) {
      var p = slice[i];
      var px = xAt(i);
      var py = yAt(p.count);
      crosshair.setAttribute("x1", String(px));
      crosshair.setAttribute("x2", String(px));
      crosshair.style.opacity = "1";
      hoverDot.setAttribute("cx", String(px));
      hoverDot.setAttribute("cy", String(py));
      hoverDot.style.opacity = "1";

      tooltipDate.textContent = formatDate(p.date);
      tooltipValue.textContent = formatNumber(p.count) + (p.count === 1 ? " visit" : " visits");
      tooltip.hidden = false;

      var wrapRect = svg.parentElement.getBoundingClientRect();
      var svgRect = svg.getBoundingClientRect();
      var scale = svgRect.width / W;
      var offsetX = svgRect.left - wrapRect.left;
      var offsetY = svgRect.top - wrapRect.top;
      tooltip.style.left = (offsetX + px * scale) + "px";
      tooltip.style.top = (offsetY + py * scale) + "px";
    }

    function hide() {
      crosshair.style.opacity = "0";
      hoverDot.style.opacity = "0";
      tooltip.hidden = true;
    }

    hit.addEventListener("pointermove", function (e) {
      showAt(indexFromClientX(e.clientX));
    });
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener(
      "touchmove",
      function (e) {
        if (e.touches && e.touches[0]) showAt(indexFromClientX(e.touches[0].clientX));
      },
      { passive: true }
    );
    hit.addEventListener("touchend", hide);
  }

  function render(rangeKey) {
    var slice = sliceForRange(rangeKey);
    renderStats(slice);
    renderTable(slice);
    renderChart(slice);
  }

  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      buttons.forEach(function (b) {
        b.classList.remove("is-active");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("is-active");
      btn.setAttribute("aria-pressed", "true");
      render(btn.getAttribute("data-range"));
    });
  });

  if (tableToggle && tableWrap) {
    tableToggle.addEventListener("click", function () {
      var expanded = tableToggle.getAttribute("aria-expanded") === "true";
      tableToggle.setAttribute("aria-expanded", String(!expanded));
      tableWrap.hidden = expanded;
      tableToggle.textContent = expanded ? "Show as table" : "Hide table";
    });
  }

  var initialBtn = document.querySelector(".range-btn.is-active") || buttons[0];
  render(initialBtn ? initialBtn.getAttribute("data-range") : "all");
})();
