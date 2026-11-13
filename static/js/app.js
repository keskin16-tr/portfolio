/* Ortak yardımcılar: tema, API, biçimlendirme, bildirim, modal, tablo */
(function () {
  const root = document.documentElement;
  const KEY = "portfolio-theme";
  const stored = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const store = (v) => { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) {} };
  function effective() {
    const t = root.dataset.theme;
    return t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function applyTheme(t) {
    if (t) root.dataset.theme = t; else delete root.dataset.theme;
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      const dark = effective() === "dark";
      btn.setAttribute("aria-pressed", dark);
      btn.title = dark ? "Açık temaya geç" : "Koyu temaya geç";
      btn.querySelector(".t-label").textContent = dark ? "Açık tema" : "Koyu tema";
      btn.querySelector(".t-sun").style.display = dark ? "" : "none";
      btn.querySelector(".t-moon").style.display = dark ? "none" : "";
    }
    document.dispatchEvent(new Event("themechange"));
  }
  window.toggleTheme = () => { const next = effective() === "dark" ? "light" : "dark"; store(next); applyTheme(next); };
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (!stored()) applyTheme(null); });
  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(stored());
    const b = document.getElementById("theme-toggle"); if (b) b.addEventListener("click", window.toggleTheme);
  });
})();

const nf = (d = 0) => new Intl.NumberFormat("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmt = {
  try: (n, d = 0) => nf(d).format(+n || 0) + " ₺",
  num: (n, d = 2) => new Intl.NumberFormat("tr-TR", { maximumFractionDigits: d }).format(+n || 0),
  pct: (n, d = 2) => (n > 0 ? "+" : "") + nf(d).format(+n || 0) + "%",
  signedTry: (n, d = 0) => (n > 0 ? "+" : n < 0 ? "−" : "") + nf(d).format(Math.abs(+n || 0)) + " ₺",
  money: (n, cur, d = 2) => new Intl.NumberFormat("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d }).format(+n || 0) + " " + ({ TRY: "₺", USD: "$", EUR: "€", GBP: "£" }[cur] || cur || ""),
  date: (s) => { if (!s) return ""; const d = new Date(String(s).slice(0, 10) + "T00:00:00"); return isNaN(d) ? s : d.toLocaleDateString("tr-TR", { day: "2-digit", month: "short", year: "numeric" }); },
  shortDate: (s) => { const d = new Date(String(s).slice(0, 10) + "T00:00:00"); return isNaN(d) ? s : d.toLocaleDateString("tr-TR", { day: "2-digit", month: "short" }); },
  time: (s) => { if (!s) return "—"; const d = new Date(s); return isNaN(d) ? s : d.toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }); },
};
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "flat");
const arrow = (n) => (n > 0 ? "▲" : n < 0 ? "▼" : "–");
const delta = (n, d = 2) => (n == null ? `<span class="delta flat">–</span>` : `<span class="delta ${cls(n)}">${arrow(n)} ${nf(d).format(Math.abs(n))}%</span>`);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(url, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && !(o.body instanceof FormData) && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  let res;
  try { res = await fetch(url, o); } catch (e) { throw new Error("Sunucuya ulaşılamadı. Uygulama çalışıyor mu?"); }
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((data && data.error) || `İstek başarısız (${res.status})`);
  return data;
}

function toast(msg, type = "ok") {
  let box = document.querySelector(".toasts");
  if (!box) { box = document.createElement("div"); box.className = "toasts"; box.setAttribute("role", "status"); document.body.appendChild(box); }
  const t = document.createElement("div"); t.className = "toast" + (type === "err" ? " err" : ""); t.textContent = msg;
  box.appendChild(t); setTimeout(() => t.remove(), type === "err" ? 6000 : 3000);
}

function confirmBox(message, okLabel = "Sil") {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.innerHTML = `<div class="dlg-body"><h2 style="margin-bottom:8px">Emin misiniz?</h2><p class="sub" style="margin:0">${esc(message)}</p></div>
      <div class="dlg-foot"><button class="btn" value="no">Vazgeç</button><button class="btn danger" value="yes">${esc(okLabel)}</button></div>`;
    document.body.appendChild(d);
    d.addEventListener("click", (e) => { const v = e.target.closest("button")?.value; if (v) { d.close(v); } });
    d.addEventListener("close", () => { resolve(d.returnValue === "yes"); d.remove(); });
    d.showModal();
  });
}

/* Basit sıralanabilir tablo başlığı bağlama */
function makeSorter(state, rerender) {
  return (th) => {
    const key = th.dataset.sort;
    state.dir = state.key === key ? -state.dir : (th.classList.contains("num") ? -1 : 1);
    state.key = key; rerender();
  };
}
function sortRows(rows, state) {
  const k = state.key; if (!k) return rows;
  return [...rows].sort((a, b) => {
    const x = a[k], y = b[k];
    if (typeof x === "number" && typeof y === "number") return (x - y) * state.dir;
    return String(x ?? "").localeCompare(String(y ?? ""), "tr") * state.dir;
  });
}
function sortHead(label, key, state, num = false) {
  const on = state.key === key;
  return `<th class="sortable${num ? " num" : ""}" data-sort="${key}" aria-sort="${on ? (state.dir > 0 ? "ascending" : "descending") : "none"}">${label}<span class="arrow">${on ? (state.dir > 0 ? "▲" : "▼") : ""}</span></th>`;
}
function bindSort(table, state, rerender) {
  const h = makeSorter(state, rerender);
  table.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => h(th)));
}

/* Piyasa fiyatlarını güncelle (tüm sayfalarda ortak düğme) */
async function refreshMarket(btn, after) {
  if (btn) { btn.disabled = true; btn.dataset.label = btn.innerHTML; btn.textContent = "Güncelleniyor…"; }
  try {
    const r = await api("/api/market/refresh", { method: "POST" });
    toast(r.error_count ? `${r.updated_count} varlık güncellendi, ${r.error_count} hata` : `${r.updated_count} varlığın fiyatı güncellendi`, r.error_count && !r.updated_count ? "err" : "ok");
    if (r.errors && r.errors.length) console.warn("Fiyat hataları:", r.errors);
    if (after) await after();
  } catch (e) { toast(e.message, "err"); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = btn.dataset.label; } }
}
const ICON_REFRESH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
    
