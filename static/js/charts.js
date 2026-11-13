/* Bağımlılıksız SVG grafik kütüphanesi: line, bars, donut, spark.
   Renkleri CSS değişkenlerinden okur; tema değişince yeniden çizilir. */
const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const color = (i) => css("--s" + ((i % 8) + 1));
  const mounted = new Set();
  const ro = "ResizeObserver" in window ? new ResizeObserver((es) => es.forEach((e) => e.target.__draw && e.target.__draw())) : null;

  function mount(el, draw) {
    el.classList.add("chart");
    el.__draw = () => { if (el.isConnected && el.clientWidth) draw(); };
    mounted.add(el);
    if (ro) ro.observe(el);
    el.__draw();
  }
  function redrawAll() { mounted.forEach((el) => { if (!el.isConnected) { mounted.delete(el); return; } el.__draw && el.__draw(); }); }
  document.addEventListener("themechange", redrawAll);

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toLocaleString("tr-TR", { maximumFractionDigits: 1 }) + " Mr";
    if (a >= 1e6) return (v / 1e6).toLocaleString("tr-TR", { maximumFractionDigits: 1 }) + " Mn";
    if (a >= 1e4) return (v / 1e3).toLocaleString("tr-TR", { maximumFractionDigits: 0 }) + " B";
    return v.toLocaleString("tr-TR", { maximumFractionDigits: a < 10 ? 2 : 0 });
  };

  function niceTicks(min, max, n = 4) {
    if (min === max) { const d = Math.abs(min) * 0.05 || 1; min -= d; max += d; }
    const span = max - min, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag, step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function tipEl(el) {
    let t = el.querySelector(".tip");
    if (!t) { t = document.createElement("div"); t.className = "tip"; el.appendChild(t); }
    return t;
  }
  function showTip(el, x, y, html) {
    const t = tipEl(el); t.innerHTML = html; t.classList.add("on");
    const w = el.clientWidth, tw = t.offsetWidth;
    t.style.left = Math.min(Math.max(x, tw / 2), w - tw / 2) + "px";
    t.style.top = Math.max(y, t.offsetHeight + 6) + "px";
  }
  const hideTip = (el) => { const t = el.querySelector(".tip"); if (t) t.classList.remove("on"); };
  const empty = (el, msg = "Henüz veri yok") => { el.innerHTML = `<div class="empty">${esc(msg)}</div>`; };

  /* ---------- Çizgi / alan ---------- */
  function line(el, o) {
    mount(el, () => {
      const labels = o.labels || [], S = o.series || [];
      const all = S.flatMap((s) => s.values).filter((v) => v != null && isFinite(v));
      if (!labels.length || !all.length) return empty(el, o.empty);
      const fmt = o.fmt || compact, W = el.clientWidth, H = o.height || 240;
      const m = { l: 52, r: 12, t: 10, b: 24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
      const ticks = niceTicks(Math.min(...all, o.includeZero ? 0 : Infinity), Math.max(...all, o.includeZero ? 0 : -Infinity));
      const y0 = ticks[0], y1 = ticks[ticks.length - 1];
      const X = (i) => m.l + (labels.length === 1 ? iw / 2 : (i / (labels.length - 1)) * iw);
      const Y = (v) => m.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;
      let g = "";
      ticks.forEach((t) => { g += `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(compact(t))}</text>`; });
      const step = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor(iw / 80))));
      labels.forEach((l, i) => { if (i % step === 0 || i === labels.length - 1 && labels.length > 1 && (labels.length - 1) % step > step / 2) g += `<text x="${X(i)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : "middle"}">${esc((o.xfmt || ((v) => v))(l))}</text>`; });
      S.forEach((s, si) => {
        const c = s.color || color(si);
        const pts = s.values.map((v, i) => (v == null ? null : [X(i), Y(v)])).filter(Boolean);
        if (!pts.length) return;
        if (pts.length === 1) { g += `<circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="4" fill="${c}"/>`; return; }
        const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
        if (o.area && si === 0) {
          const base = Y(Math.max(y0, Math.min(0, y1)));
          g += `<path d="${d}L${pts[pts.length - 1][0].toFixed(1)} ${base}L${pts[0][0].toFixed(1)} ${base}Z" fill="${c}" opacity=".12"/>`;
        }
        g += `<path d="${d}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
      });
      el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(o.label || "Çizgi grafik")}">${g}<g class="cross" style="display:none"><line class="axis-line" y1="${m.t}" y2="${m.t + ih}"/></g><rect class="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}"/></svg>${S.length > 1 ? `<div class="legend">${S.map((s, i) => `<span><i style="background:${s.color || color(i)}"></i>${esc(s.name)}</span>`).join("")}</div>` : ""}`;
      const svg = el.querySelector("svg"), cross = svg.querySelector(".cross"), xl = cross.querySelector("line");
      const hit = svg.querySelector(".hit");
      const move = (ev) => {
        const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
        let i = labels.length === 1 ? 0 : Math.round(((px - m.l) / iw) * (labels.length - 1));
        i = Math.max(0, Math.min(labels.length - 1, i));
        cross.style.display = ""; xl.setAttribute("x1", X(i)); xl.setAttribute("x2", X(i));
        cross.querySelectorAll("circle").forEach((c) => c.remove());
        let rows = "", ty = Infinity;
        S.forEach((s, si) => {
          const v = s.values[i]; if (v == null) return;
          const c = s.color || color(si);
          const dot = document.createElementNS(NS, "circle");
          dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(v)); dot.setAttribute("r", 4.5);
          dot.setAttribute("fill", c); dot.setAttribute("stroke", css("--surface")); dot.setAttribute("stroke-width", 2);
          cross.appendChild(dot); ty = Math.min(ty, Y(v));
          rows += `<div class="r"><span><i style="background:${c}"></i>${esc(s.name)}</span><b>${esc((s.fmt || fmt)(v))}</b></div>`;
        });
        showTip(el, X(i), ty - 8, `<div class="t">${esc((o.xfmt || ((v) => v))(labels[i]))}</div>${rows}`);
      };
      hit.addEventListener("mousemove", move); hit.addEventListener("touchmove", (e) => move(e.touches[0]), { passive: true });
      hit.addEventListener("mouseleave", () => { cross.style.display = "none"; hideTip(el); });
    });
  }

  /* ---------- Çubuk ---------- */
  function bars(el, o) {
    mount(el, () => {
      const labels = o.labels || [], vals = o.values || [];
      if (!labels.length) return empty(el, o.empty);
      const fmt = o.fmt || compact, W = el.clientWidth, signed = o.signed !== false;
      const colorOf = (v, i) => (o.colors ? o.colors[i % o.colors.length] : signed ? (v < 0 ? css("--down") : css("--up")) : o.color || color(0));
      let g = "", svgH;
      if (o.horizontal) {
        const rowH = 28, m = { l: Math.min(150, Math.max(70, W * 0.32)), r: 56, t: 4, b: 18 };
        svgH = m.t + m.b + rowH * labels.length;
        const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1, iw = W - m.l - m.r;
        const X = (v) => m.l + ((v - lo) / span) * iw, zero = X(0);
        labels.forEach((l, i) => {
          const y = m.t + i * rowH, v = vals[i], x = Math.min(zero, X(v)), w = Math.max(2, Math.abs(X(v) - zero));
          const lab = String(l).length > 22 ? String(l).slice(0, 21) + "…" : l;
          g += `<text x="${m.l - 8}" y="${y + rowH / 2 + 4}" text-anchor="end" style="fill:var(--text-2)">${esc(lab)}</text>
            <rect x="${x}" y="${y + 5}" width="${w}" height="${rowH - 10}" rx="3" fill="${colorOf(v, i)}"/>
            <text x="${v < 0 ? zero + 6 : x + w + 6}" y="${y + rowH / 2 + 4}" text-anchor="start" style="fill:var(--text-2)">${esc(fmt(v))}</text>
            <rect class="hit" data-i="${i}" x="0" y="${y}" width="${W}" height="${rowH}"/>`;
        });
        g += `<line class="axis-line" x1="${zero}" x2="${zero}" y1="${m.t}" y2="${svgH - m.b}"/>`;
      } else {
        const H = o.height || 220, m = { l: 52, r: 8, t: 10, b: 26 }, ih = H - m.t - m.b, iw = W - m.l - m.r;
        svgH = H;
        const ticks = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals)), y0 = ticks[0], y1 = ticks[ticks.length - 1];
        const Y = (v) => m.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih, zero = Y(0), slot = iw / labels.length, bw = Math.min(46, slot * 0.62);
        ticks.forEach((t) => { g += `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${m.l - 8}" y="${Y(t) + 4}" text-anchor="end">${esc(compact(t))}</text>`; });
        const every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor(iw / 64))));
        labels.forEach((l, i) => {
          const cx = m.l + slot * i + slot / 2, v = vals[i], yy = Math.min(Y(v), zero), h = Math.max(1.5, Math.abs(Y(v) - zero));
          g += `<rect x="${cx - bw / 2}" y="${yy}" width="${bw}" height="${h}" rx="3" fill="${colorOf(v, i)}"/>`;
          if (i % every === 0) g += `<text x="${cx}" y="${H - 8}" text-anchor="middle">${esc(l)}</text>`;
          g += `<rect class="hit" data-i="${i}" x="${m.l + slot * i}" y="${m.t}" width="${slot}" height="${ih}"/>`;
        });
        g += `<line class="axis-line" x1="${m.l}" x2="${W - m.r}" y1="${zero}" y2="${zero}"/>`;
      }
      el.innerHTML = `<svg viewBox="0 0 ${W} ${svgH}" width="${W}" height="${svgH}" role="img" aria-label="${esc(o.label || "Çubuk grafik")}">${g}</svg>`;
      el.querySelectorAll(".hit").forEach((h) => {
        h.addEventListener("mousemove", (ev) => {
          const i = +h.dataset.i, r = el.getBoundingClientRect();
          showTip(el, ev.clientX - r.left, ev.clientY - r.top - 10, `<div class="t">${esc(labels[i])}</div><div class="r"><b>${esc(fmt(vals[i]))}</b></div>${o.extra ? `<div class="t">${esc(o.extra[i] || "")}</div>` : ""}`);
        });
        h.addEventListener("mouseleave", () => hideTip(el));
      });
    });
  }

  /* ---------- Halka ---------- */
  function donut(el, o) {
    mount(el, () => {
      let items = (o.items || []).filter((x) => x.value > 0).sort((a, b) => b.value - a.value);
      if (!items.length) return empty(el, o.empty);
      if (items.length > 8) { const rest = items.slice(7).reduce((s, x) => s + x.value, 0); items = items.slice(0, 7).concat({ name: "Diğer", value: rest }); }
      const total = items.reduce((s, x) => s + x.value, 0), fmt = o.fmt || compact, S = 180, R = 84, r = 54, c = S / 2;
      let a = -Math.PI / 2, paths = "";
      items.forEach((it, i) => {
        const frac = it.value / total, b = a + Math.min(frac, 0.99999) * 2 * Math.PI, large = b - a > Math.PI ? 1 : 0;
        const p = (rad, ang) => [c + rad * Math.cos(ang), c + rad * Math.sin(ang)].map((n) => n.toFixed(2)).join(" ");
        paths += `<path data-i="${i}" d="M${p(R, a)}A${R} ${R} 0 ${large} 1 ${p(R, b)}L${p(r, b)}A${r} ${r} 0 ${large} 0 ${p(r, a)}Z" fill="${color(i)}" stroke="${css("--surface")}" stroke-width="2"/>`;
        a = b;
      });
      el.innerHTML = `<div class="donut-wrap"><div style="position:relative"><svg viewBox="0 0 ${S} ${S}" role="img" aria-label="${esc(o.label || "Dağılım")}">${paths}
        <text x="${c}" y="${c - 2}" text-anchor="middle" style="fill:var(--text);font-size:15px;font-weight:700">${esc(o.center || fmt(total))}</text>
        <text x="${c}" y="${c + 15}" text-anchor="middle">${esc(o.centerLabel || "Toplam")}</text></svg></div>
        <div class="donut-list">${items.map((it, i) => `<div class="it"><i style="background:${color(i)}"></i><span class="n" title="${esc(it.name)}">${esc(it.name)}</span><span class="p">%${(it.value / total * 100).toLocaleString("tr-TR", { maximumFractionDigits: 1 })}</span></div>`).join("")}</div></div>`;
      const box = el.querySelector(".donut-wrap > div");
      el.querySelectorAll("path").forEach((p) => {
        p.addEventListener("mousemove", (ev) => {
          const it = items[+p.dataset.i], r2 = box.getBoundingClientRect(), e2 = el.getBoundingClientRect();
          showTip(el, ev.clientX - e2.left, ev.clientY - e2.top - 10, `<div class="r"><span><i style="background:${color(+p.dataset.i)}"></i>${esc(it.name)}</span></div><div class="r"><b>${esc(fmt(it.value))}</b><span>%${(it.value / total * 100).toFixed(1)}</span></div>`);
        });
        p.addEventListener("mouseleave", () => hideTip(el));
      });
    });
  }

  /* ---------- Mini grafik ---------- */
  function spark(el, values, colorVar) {
    const v = (values || []).filter((x) => x != null && isFinite(x));
    if (v.length < 2) { el.innerHTML = ""; return; }
    const W = 96, H = 34, mn = Math.min(...v), mx = Math.max(...v), sp = mx - mn || 1;
    const pts = v.map((x, i) => [(i / (v.length - 1)) * W, H - 3 - ((x - mn) / sp) * (H - 6)]);
    const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
    const c = css(colorVar || "--accent");
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true"><path d="${d}L${W} ${H}L0 ${H}Z" fill="${c}" opacity=".12"/><path d="${d}" fill="none" stroke="${c}" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
  }

  return { line, bars, donut, spark, compact, color, redrawAll };
})();
