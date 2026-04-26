// StakeMate Mini App — single-page client.
//
// Hash-based routing:  #/  → dashboard, #/v/<addr>  → validator detail,
//                      #/d/<delegator>/<staker>  → delegator detail,
//                      #/settings  → notification settings.
//
// Auth modes (matches api/auth.py):
//   - Telegram WebApp: window.Telegram.WebApp.initData → X-Telegram-Init-Data.
//   - Local dashboard: ?tg_id=NNN query param appended to API calls.

const tg = (window.Telegram && window.Telegram.WebApp) ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  // Match the page palette to Telegram's chrome (status bar etc.).
  document.documentElement.setAttribute("data-tg", "1");
  if (tg.colorScheme) document.documentElement.style.colorScheme = tg.colorScheme;
}

const API_BASE = window.__API_BASE__ || "";

const state = {
  status: null,         // /api/v1/status
  entries: null,        // /api/v1/users/me/entries
  notification: null,   // /api/v1/users/me/notification-config
  prices: null,         // CoinGecko-derived USD per symbol (best-effort)
  profile: null,        // /api/v1/users/me/profile (id, name, language)
  locale: null,         // loaded /api/v1/locales/{lang} bundle
};

// ---------------------------------------------------------------------------
// i18n
//
// The Mini App pulls the same JSON bundle the bot uses (`locales/<lang>.json`)
// from the server on boot, then falls back to the inline default if a key is
// missing. ``t(key, "fallback", {placeholder: value})`` keeps the call site
// readable in English even before a key is added to all 8 locales.
// ---------------------------------------------------------------------------

function t(key, fallback, vars) {
  const bundle = state.locale || {};
  let value = bundle[key];
  if (value === undefined || value === null || value === "") {
    value = fallback ?? key;
  }
  if (vars) {
    for (const [k, v] of Object.entries(vars)) {
      value = value.replaceAll(`{${k}}`, String(v));
    }
  }
  return value;
}

const SUPPORTED_LOCALES = [
  { code: "en", label: "English" },
  { code: "ru", label: "Русский" },
  { code: "ua", label: "Українська" },
  { code: "de", label: "Deutsch" },
  { code: "es", label: "Español" },
  { code: "ko", label: "한국어" },
  { code: "pl", label: "Polski" },
  { code: "zh", label: "中文" },
];

async function loadLocale(lang) {
  // 404 / network error → keep whatever's already loaded (English fallback).
  try {
    const bundle = await api(`/api/v1/locales/${encodeURIComponent(lang)}`);
    if (bundle && typeof bundle === "object") state.locale = bundle;
  } catch (err) {
    console.warn("locale fetch failed", err);
  }
}

async function loadProfileAndLocale() {
  try {
    state.profile = await api("/api/v1/users/me/profile");
  } catch (err) {
    // Local-auth missing tg_id, no user yet, etc. — default to English.
    state.profile = { language: "en" };
  }
  await loadLocale(state.profile.language || "en");
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (tg && tg.initData) h["X-Telegram-Init-Data"] = tg.initData;
  return h;
}

function appendAuthQuery(path) {
  if (tg && tg.initData) return path;
  const id = new URLSearchParams(location.search).get("tg_id");
  if (!id) return path;
  return path + (path.includes("?") ? "&" : "?") + "tg_id=" + encodeURIComponent(id);
}

async function api(path, { method = "GET", body = null } = {}) {
  const url = API_BASE + appendAuthQuery(path);
  const res = await fetch(url, {
    method,
    headers: authHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${detail || res.statusText}`);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtAddr(a) {
  if (!a || a.length < 14) return a || "—";
  return `${a.slice(0, 8)}…${a.slice(-4)}`;
}

function fmtAmount(value, symbol) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return symbol ? `0 ${symbol}` : "0";
  let formatted;
  if (n >= 1_000_000) formatted = (n / 1_000_000).toFixed(2) + "M";
  else if (n >= 10_000) formatted = Math.round(n).toLocaleString("en-US");
  else if (n >= 1) formatted = n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  else if (n >= 0.0001) formatted = n.toLocaleString("en-US", { maximumFractionDigits: 6 });
  // Anything smaller is dust — collapse to a single "<0.0001" string
  // instead of scientific notation. ``1.33e-9 SolvBTC`` was the literal
  // user-reported display when the on-chain decimals were wrong.
  else formatted = "<0.0001";
  return symbol ? `${formatted} ${symbol}` : formatted;
}

function fmtUsd(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const n = Number(value);
  if (n === 0) return "$0.00";
  if (n >= 1_000_000) return "$" + (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1000) return "$" + Math.round(n).toLocaleString("en-US");
  if (n >= 1) return "$" + n.toFixed(2);
  return "$" + n.toFixed(4);
}

function fmtBps(bps) {
  if (bps === null || bps === undefined) return "—";
  return (Number(bps) / 100).toFixed(2) + "%";
}

const TOKEN_DECIMALS = { STRK: 18, WBTC: 8, LBTC: 8, tBTC: 18, SolvBTC: 18 };

// ---------------------------------------------------------------------------
// Aggregations from /entries DTOs
// ---------------------------------------------------------------------------

function entryUnclaimedBySymbol(entry) {
  const out = {};
  if (!entry || !entry.data) return out;
  if (entry.kind === "validator") {
    const amt = Number(entry.data.unclaimed_rewards_own_strk || 0);
    if (amt) out["STRK"] = (out["STRK"] || 0) + amt;
  } else if (entry.kind === "delegator") {
    for (const pos of entry.data.positions || []) {
      const amt = Number(pos.unclaimed_rewards_decimal || 0);
      if (amt) out["STRK"] = (out["STRK"] || 0) + amt; // V2 rewards always STRK
    }
  }
  return out;
}

function entryStakedBySymbol(entry) {
  const out = {};
  if (!entry || !entry.data) return out;
  if (entry.kind === "validator") {
    const own = Number(entry.data.amount_own_strk || 0);
    if (own) out["STRK"] = (out["STRK"] || 0) + own;
    for (const p of entry.data.pools || []) {
      const sym = p.token_symbol || "STRK";
      const amt = Number(p.amount_decimal || 0);
      if (amt) out[sym] = (out[sym] || 0) + amt;
    }
  } else if (entry.kind === "delegator") {
    for (const pos of entry.data.positions || []) {
      const sym = pos.token_symbol || "STRK";
      const amt = Number(pos.amount_decimal || 0);
      if (amt) out[sym] = (out[sym] || 0) + amt;
    }
  }
  return out;
}

function symbolToUsd(symbol, amount, prices) {
  if (!prices) return null;
  const p = prices[symbol] ?? prices[symbol?.toUpperCase()];
  if (p === undefined || p === null) return null;
  return Number(amount) * Number(p);
}

function totalUsd(bySymbol, prices) {
  if (!prices) return null;
  let total = 0;
  let any = false;
  for (const [sym, amt] of Object.entries(bySymbol)) {
    const usd = symbolToUsd(sym, amt, prices);
    if (usd !== null) {
      total += usd;
      any = true;
    }
  }
  return any ? total : null;
}

async function loadPrices() {
  // CoinGecko free tier — no key required. Fallback gracefully on failure;
  // the UI just hides USD numbers.
  try {
    const ids = ["starknet", "wrapped-bitcoin", "lombard-staked-btc", "tbtc", "solv-protocol-solvbtc"];
    const url = `https://api.coingecko.com/api/v3/simple/price?ids=${ids.join(",")}&vs_currencies=usd`;
    const r = await fetch(url);
    if (!r.ok) throw new Error("price fetch " + r.status);
    const data = await r.json();
    return {
      STRK:    data["starknet"]?.usd ?? null,
      WBTC:    data["wrapped-bitcoin"]?.usd ?? null,
      LBTC:    data["lombard-staked-btc"]?.usd ?? null,
      tBTC:    data["tbtc"]?.usd ?? null,
      SolvBTC: data["solv-protocol-solvbtc"]?.usd ?? null,
    };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

let toastTimer = null;
function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

// ---------------------------------------------------------------------------
// Clipboard helpers
// ---------------------------------------------------------------------------

async function copyText(text) {
  // navigator.clipboard requires HTTPS — fine in production, fine in
  // Telegram WebApp (always HTTPS). Fall back to execCommand for the
  // tiny fraction of contexts that block the modern API.
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Renamed from ``t`` to ``ta`` to avoid shadowing the global i18n
      // helper ``t(key, fallback, vars)``.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast(t("webapp_copied_toast", "Copied"));
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
  } catch (err) {
    toast("Copy failed");
  }
}

/** Render a copyable monospace address chip. Returns HTML string. */
function copyableAddr(addr, { full = false } = {}) {
  if (!addr) return '<span class="muted">—</span>';
  const display = full ? addr : `${addr.slice(0, 8)}…${addr.slice(-6)}`;
  const safe = escapeHtml(addr);
  return `<span class="addr-copy addr-mono" data-copy="${safe}" title="Tap to copy">${escapeHtml(display)}</span>`;
}

/** Wire up tap-to-copy for any [data-copy] element inside the view. */
function bindCopyHandlers() {
  for (const el of viewEl.querySelectorAll("[data-copy]")) {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      copyText(el.dataset.copy);
    });
  }
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

function parseRoute() {
  const hash = location.hash.replace(/^#/, "") || "/";
  const parts = hash.split("/").filter(Boolean);
  if (parts.length === 0) return { name: "dashboard" };
  if (parts[0] === "v" && parts[1]) return { name: "validator", address: parts[1] };
  if (parts[0] === "d" && parts[2]) return { name: "delegator", delegator: parts[1], staker: parts[2] };
  if (parts[0] === "settings") return { name: "settings" };
  return { name: "dashboard" };
}

function navigate(hash) {
  if (location.hash === hash) renderRoute();
  else location.hash = hash;
}

window.addEventListener("hashchange", renderRoute);

// ---------------------------------------------------------------------------
// Topbar
// ---------------------------------------------------------------------------

const topbarTitle = document.getElementById("topbar-title");
const topbarSub = document.getElementById("topbar-sub");

document.querySelector("[data-action='back']").addEventListener("click", () => {
  if (history.length > 1 && location.hash !== "#/") history.back();
  else navigate("#/");
});
document.querySelector("[data-action='settings']").addEventListener("click", () => navigate("#/settings"));

function setTopbar(title, sub = "") {
  topbarTitle.textContent = title;
  topbarSub.textContent = sub;
}

// ---------------------------------------------------------------------------
// View rendering
// ---------------------------------------------------------------------------

const viewEl = document.getElementById("view");

function renderTemplate(id) {
  const tpl = document.getElementById(id);
  viewEl.innerHTML = "";
  viewEl.appendChild(tpl.content.cloneNode(true));
  applyI18n(viewEl);
}

function applyI18n(root) {
  // Walk every element with ``data-i18n`` and substitute its text content.
  // Uses the inline text already in the DOM as the English fallback so a
  // missing key still renders something sensible.
  for (const el of root.querySelectorAll("[data-i18n]")) {
    const key = el.dataset.i18n;
    el.textContent = t(key, el.textContent || key);
  }
  // Same for placeholders on form inputs.
  for (const el of root.querySelectorAll("[data-i18n-placeholder]")) {
    const key = el.dataset.i18nPlaceholder;
    el.placeholder = t(key, el.placeholder || key);
  }
}

function bindings() {
  const out = {};
  for (const el of viewEl.querySelectorAll("[data-bind]")) out[el.dataset.bind] = el;
  return out;
}

function _hasAuth() {
  // Authoritative: real initData hash from Telegram WebApp container.
  if (tg && tg.initData && tg.initData.length > 0) return true;
  // Local dashboard fallback: ?tg_id=NNN in the URL is OK in `local`/`both`
  // server modes (used while the deployment doesn't have HTTPS).
  if (new URLSearchParams(location.search).get("tg_id")) return true;
  return false;
}

function renderAuthHelp() {
  // Telegram Desktop has a known issue where ReplyKeyboardMarkup
  // ``web_app`` buttons open the Mini App without ``tgWebAppData`` in
  // the URL fragment — so initData stays empty and the API returns 401.
  // BotFather's Menu Button (the blue button left of the message input)
  // is the reliable entry point on every platform; explain that here
  // instead of dropping a raw 401.
  const isDesktopMissing = tg && tg.platform === "tdesktop";
  const title = isDesktopMissing ? "Open from the menu button" : "Open this Mini App in Telegram";
  const body = isDesktopMissing
    ? `On Telegram Desktop the keyboard button can't pass your account info due to a Telegram-side limitation.<br><br>Use the <b>blue button</b> on the left of the message input in your chat with the bot — it works on every platform.`
    : `This page needs to be launched from inside Telegram so the bot can verify who you are.<br><br>Open the bot in Telegram and tap the <b>menu button</b> (left of the message input).`;

  setTopbar("StakeMate", "");
  viewEl.innerHTML = `
    <div class="hero" style="text-align:center">
      <div style="font-size:48px; line-height:1; margin-bottom:12px">🔒</div>
      <h2 style="margin:0 0 12px; font-size:20px; font-weight:700; letter-spacing:-0.01em">${escapeHtml(title)}</h2>
      <p class="muted" style="font-size:14px; line-height:1.5; margin:0">${body}</p>
    </div>
  `;
}

async function renderRoute() {
  const route = parseRoute();
  document.getElementById("app").dataset.view = route.name;

  // Stop early if we have no way to authenticate. Otherwise every view
  // would fire its own /api/v1/users/me/* call and get back 401.
  if (!_hasAuth()) {
    renderAuthHelp();
    return;
  }

  // Load the user's profile + matching locale once. Subsequent route
  // renders reuse ``state.locale`` synchronously via ``t()``.
  if (state.locale === null) {
    await loadProfileAndLocale();
  }

  try {
    if (route.name === "dashboard") await renderDashboard();
    else if (route.name === "validator") await renderValidator(route.address);
    else if (route.name === "delegator") await renderDelegator(route.delegator, route.staker);
    else if (route.name === "settings") await renderSettings();
  } catch (err) {
    // Render the diagnostic block alongside the error so the user can
    // tell whether the failure is "Telegram never sent initData" vs
    // "initData was sent and the API still rejected it".
    const diag = `
      <div class="placeholder" style="text-align:left">
        <div style="color: var(--red); font-weight:600; margin-bottom:8px">Error</div>
        <div style="font-family:ui-monospace,monospace; font-size:12px; word-break:break-all; margin-bottom:12px">${escapeHtml(err.message)}</div>
        <hr style="border:0; border-top:0.5px solid var(--separator); margin:12px 0">
        <div style="font-weight:600; margin-bottom:4px">Diagnostics</div>
        <div style="font-size:12px; line-height:1.6">
          <div>Telegram.WebApp present: <b>${!!tg}</b></div>
          <div>initData length: <b>${tg && tg.initData ? tg.initData.length : 0}</b></div>
          <div>initDataUnsafe.user: <b>${tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? escapeHtml(JSON.stringify(tg.initDataUnsafe.user)) : "—"}</b></div>
          <div>platform: <b>${escapeHtml(tg && tg.platform || "—")}</b></div>
          <div>version: <b>${escapeHtml(tg && tg.version || "—")}</b></div>
          <div style="word-break:break-all; margin-top:8px">URL: ${escapeHtml(location.href)}</div>
        </div>
      </div>`;
    viewEl.innerHTML = diag;
    setTopbar("Error", "");
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>\"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// ---------------------------------------------------------------------------
// Dashboard view
// ---------------------------------------------------------------------------

async function renderDashboard() {
  setTopbar("Portfolio", "");
  renderTemplate("tpl-dashboard");
  const $ = bindings();

  // Kick off all reads in parallel; show whatever lands first.
  const [status, entries, prices] = await Promise.all([
    state.status ? Promise.resolve(state.status) : api("/api/v1/status").then((s) => (state.status = s)),
    api("/api/v1/users/me/entries").then((e) => (state.entries = e)),
    state.prices !== null ? Promise.resolve(state.prices) : loadPrices().then((p) => (state.prices = p)),
  ]);

  $.epochChip.textContent = `epoch ${status.current_epoch}`;
  setTopbar("Portfolio", `${status.network} · epoch ${status.current_epoch}`);

  const validators = entries.filter((e) => e.kind === "validator");
  const delegations = entries.filter((e) => e.kind === "delegator");
  $.countsChip.textContent = `${validators.length} validator${validators.length === 1 ? "" : "s"} · ${delegations.length} delegation${delegations.length === 1 ? "" : "s"}`;

  // Aggregate stake & unclaimed across the portfolio.
  const stakedTotal = {};
  const unclaimedTotal = {};
  for (const e of entries) {
    for (const [s, a] of Object.entries(entryStakedBySymbol(e))) stakedTotal[s] = (stakedTotal[s] || 0) + a;
    for (const [s, a] of Object.entries(entryUnclaimedBySymbol(e))) unclaimedTotal[s] = (unclaimedTotal[s] || 0) + a;
  }

  const stakedUsd = totalUsd(stakedTotal, prices);
  const unclaimedUsd = totalUsd(unclaimedTotal, prices);

  // Hero — STRK as the primary number (it's the chain's native unit; users
  // tend to reason in "how much STRK do I hold" rather than "how many $").
  // USD goes underneath as a smaller secondary line. If the portfolio
  // contains BTC pools, the USD aggregate is also annotated to make it
  // clear it includes those pools (which the STRK number doesn't).
  const stakedStrk = stakedTotal["STRK"] || 0;
  const hasBtcPools = Object.keys(stakedTotal).some((s) => s !== "STRK");

  $.totalStakePrimary.textContent = stakedStrk > 0 || !hasBtcPools
    ? fmtAmount(stakedStrk, "STRK")
    : "—";
  $.totalStakeSecondary.textContent = stakedUsd !== null
    ? `≈ ${fmtUsd(stakedUsd)}${hasBtcPools ? " · incl. BTC pools" : ""}`
    : "";

  const unclaimedStrk = unclaimedTotal["STRK"] || 0;
  $.totalUnclaimedPrimary.textContent = fmtAmount(unclaimedStrk, "STRK");
  $.totalUnclaimedSecondary.textContent = unclaimedUsd !== null
    ? `≈ ${fmtUsd(unclaimedUsd)}`
    : "";

  // Entries list
  if (entries.length === 0) {
    $.entries.innerHTML = `<div class="placeholder">No tracked addresses yet.<br>Open the bot and use “Add Info”.</div>`;
    return;
  }
  $.entries.innerHTML = "";
  for (const e of entries) renderEntryCard(e, $.entries, prices);
}

function renderEntryCard(entry, container, prices) {
  const isValidator = entry.kind === "validator";
  const label = entry.label || fmtAddr(entry.address);
  const card = document.createElement("button");
  card.className = "row-card";
  card.type = "button";

  const unclaimed = entryUnclaimedBySymbol(entry);
  const unclaimedSum = unclaimed["STRK"] || 0;
  const unclaimedUsd = symbolToUsd("STRK", unclaimedSum, prices);

  // Build the right-side numeric block
  let rightHtml = "";
  if (unclaimedSum > 0) {
    rightHtml = `
      <div class="primary">${fmtAmount(unclaimedSum, "STRK")}</div>
      ${unclaimedUsd !== null ? `<div class="secondary">${fmtUsd(unclaimedUsd)}</div>` : ""}
    `;
  } else {
    rightHtml = `<div class="secondary">—</div>`;
  }

  // Build status sub-line
  let subHtml = "";
  if (isValidator) {
    const att = entry.data?.attestation;
    const isUnstaking = entry.data?.unstake_requested;
    if (isUnstaking) subHtml = `<span class="chip danger">unstaking</span>`;
    else if (att && att.missed_epochs > 0) subHtml = `<span class="chip warn">${att.missed_epochs} missed</span>`;
    else if (att) subHtml = `<span class="chip success">healthy</span>`;
    subHtml += ` <span>${fmtAddr(entry.address)}</span>`;
  } else {
    const positions = entry.data?.positions || [];
    subHtml = `<span>via ${fmtAddr(entry.data?.staker_address || entry.address)}</span>`;
    if (positions.length > 1) subHtml += ` <span class="chip">${positions.length} pools</span>`;
  }

  card.innerHTML = `
    <span class="icon ${isValidator ? "validator" : "delegator"}">${isValidator ? "🛡" : "🎱"}</span>
    <span class="body">
      <div class="label">${escapeHtml(label)}</div>
      <div class="sub">${subHtml}</div>
    </span>
    <span class="right">${rightHtml}</span>
    <svg class="chevron" viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
      <path fill="currentColor" d="M9.3 5.3a1 1 0 0 1 1.4 0l6 6a1 1 0 0 1 0 1.4l-6 6a1 1 0 1 1-1.4-1.4L14.6 12 9.3 6.7a1 1 0 0 1 0-1.4z"/>
    </svg>
  `;
  card.addEventListener("click", () => {
    if (isValidator) navigate(`#/v/${entry.address}`);
    else navigate(`#/d/${entry.data?.delegator_address || entry.address}/${entry.data?.staker_address || ""}`);
  });
  container.appendChild(card);
}

// ---------------------------------------------------------------------------
// Validator detail view
// ---------------------------------------------------------------------------

async function renderValidator(address) {
  setTopbar("Validator", fmtAddr(address));
  renderTemplate("tpl-detail");
  const $ = bindings();

  if (!state.entries) state.entries = await api("/api/v1/users/me/entries");
  if (state.prices === null) state.prices = await loadPrices();

  const lower = String(address).toLowerCase();
  const entry = state.entries.find(
    (e) => e.kind === "validator" && (e.address || "").toLowerCase() === lower
  );

  if (!entry || !entry.data) {
    viewEl.innerHTML = `<div class="placeholder">Validator not found in your tracked list.</div>`;
    return;
  }

  const data = entry.data;
  const label = entry.label || fmtAddr(entry.address);
  setTopbar("Validator", label);

  $.avatar.textContent = "🛡";
  $.label.textContent = label;
  $.address.classList.remove("addr-mono");
  $.address.innerHTML = copyableAddr(entry.address);

  // Status banner — same logic delegator detail uses, factored out so the
  // two views always stay in sync.
  $.statusBanner.innerHTML = renderValidatorStatusBanner(data);

  // Stats grid
  $.primaryStakeLabel.textContent = "Own stake";
  const ownStrk = Number(data.amount_own_strk || 0);
  $.primaryStake.textContent = fmtAmount(ownStrk, "STRK");
  const ownUsd = symbolToUsd("STRK", ownStrk, state.prices);
  $.primaryStakeUsd.textContent = ownUsd !== null ? fmtUsd(ownUsd) : "";

  const unclaimed = Number(data.unclaimed_rewards_own_strk || 0);
  $.unclaimed.textContent = fmtAmount(unclaimed, "STRK");
  const unclaimedUsd = symbolToUsd("STRK", unclaimed, state.prices);
  $.unclaimedUsd.textContent = unclaimedUsd !== null ? fmtUsd(unclaimedUsd) : "";

  if (data.commission_bps !== null && data.commission_bps !== undefined) {
    $.commission.textContent = fmtBps(data.commission_bps);
  } else {
    $.commissionStat.style.display = "none";
  }
  $.epoch.textContent = data.current_epoch ?? "—";

  // Total stake = own + every pool, summed cross-token via USD prices.
  // Per-token breakdown lives in the subtitle so the user sees both
  // "how much do I control" (USD aggregate) and the token mix.
  const totals = {};
  totals["STRK"] = ownStrk;
  for (const p of data.pools || []) {
    const sym = p.token_symbol || "STRK";
    totals[sym] = (totals[sym] || 0) + Number(p.amount_decimal || 0);
  }
  $.totalStakeBlock.innerHTML = renderTotalStakeHero(totals, state.prices);

  // Operator wallet (gas reserve for attestations) — surfaces the live
  // STRK balance so a validator owner / delegator can spot a drained
  // operator before missed attestations show up.
  $.operatorWalletBlock.innerHTML = renderOperatorWalletBlock(data, state);

  // Pools breakdown
  const pools = data.pools || [];
  if (pools.length) {
    $.poolsBlock.innerHTML = `
      <h3 class="section-title">Pools (${pools.length})</h3>
      <div class="pools">
        ${pools.map((p) => {
          const sym = p.token_symbol || "STRK";
          const amt = Number(p.amount_decimal || 0);
          const usd = symbolToUsd(sym, amt, state.prices);
          return `
            <div class="item">
              <div class="meta">
                <strong>${escapeHtml(sym)} pool</strong>
                <div class="sub">${copyableAddr(p.pool_contract)}</div>
              </div>
              <div class="amount">
                <strong>${fmtAmount(amt, sym)}</strong>
                ${usd !== null ? `<div class="muted small">${fmtUsd(usd)}</div>` : ""}
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  attachRemoveButton($.removeBtn, {
    kind: "validator",
    label,
    matcher: (v, _) => (v.address || "").toLowerCase() === lower,
  });
  bindCopyHandlers();
}

// ---------------------------------------------------------------------------
// Delegator detail view
// ---------------------------------------------------------------------------

async function renderDelegator(delegatorAddr, stakerAddr) {
  setTopbar("Delegation", fmtAddr(delegatorAddr));
  renderTemplate("tpl-detail");
  const $ = bindings();

  if (!state.entries) state.entries = await api("/api/v1/users/me/entries");
  if (state.prices === null) state.prices = await loadPrices();

  const dLower = String(delegatorAddr).toLowerCase();
  const sLower = String(stakerAddr || "").toLowerCase();
  const entry = state.entries.find((e) => {
    if (e.kind !== "delegator" || !e.data) return false;
    const dMatch = (e.data.delegator_address || "").toLowerCase() === dLower;
    const sMatch = !sLower || (e.data.staker_address || "").toLowerCase() === sLower;
    return dMatch && sMatch;
  });

  if (!entry || !entry.data) {
    viewEl.innerHTML = `<div class="placeholder">Delegation not found in your tracked list.</div>`;
    return;
  }

  const data = entry.data;
  const positions = data.positions || [];
  const label = entry.label || fmtAddr(entry.address);
  setTopbar("Delegation", label);

  $.avatar.textContent = "🎱";
  $.label.textContent = label;
  $.address.classList.remove("addr-mono");
  $.address.innerHTML = `
    <div>delegator ${copyableAddr(data.delegator_address)}</div>
    <div style="margin-top:4px">via ${copyableAddr(data.staker_address)}</div>
  `;

  // Surface the staker's status to delegators so they can see at a glance
  // if the validator they're delegating to is healthy. Fire-and-forget —
  // the delegator's own data DTO doesn't include the staker's attestation
  // record, so we hit /validators/{addr} separately. Render the banner
  // when it lands; on failure we just leave it blank (we don't want to
  // block the rest of the page on this one extra call).
  $.statusBanner.innerHTML = `<div class="banner muted">Loading validator status…</div>`;
  api(`/api/v1/validators/${data.staker_address}`)
    .then((vinfo) => {
      $.statusBanner.innerHTML = renderValidatorStatusBanner(vinfo);
      // Operator wallet info isn't on the delegator DTO — it lives on the
      // staker's ValidatorInfo. Render it here once the lookup resolves.
      $.operatorWalletBlock.innerHTML = renderOperatorWalletBlock(vinfo, state);
      bindCopyHandlers();
    })
    .catch((err) => {
      console.warn("validator status lookup failed", err);
      $.statusBanner.innerHTML = `<div class="banner muted">Validator status unavailable</div>`;
    });

  // Stats grid: total stake (per primary token) + total unclaimed (STRK).
  const stakedBySym = entryStakedBySymbol(entry);
  const primarySym = Object.keys(stakedBySym)[0] || "STRK";
  const primaryAmt = stakedBySym[primarySym] || 0;
  $.primaryStakeLabel.textContent = positions.length > 1 ? "Primary stake" : "Stake";
  $.primaryStake.textContent = fmtAmount(primaryAmt, primarySym);
  const primaryUsd = symbolToUsd(primarySym, primaryAmt, state.prices);
  $.primaryStakeUsd.textContent = primaryUsd !== null ? fmtUsd(primaryUsd) : "";

  const unclaimedSum = positions.reduce((acc, p) => acc + Number(p.unclaimed_rewards_decimal || 0), 0);
  $.unclaimed.textContent = fmtAmount(unclaimedSum, "STRK");
  const unclaimedUsd = symbolToUsd("STRK", unclaimedSum, state.prices);
  $.unclaimedUsd.textContent = unclaimedUsd !== null ? fmtUsd(unclaimedUsd) : "";

  // Commission (use first position; usually they share)
  if (positions[0]?.commission_bps !== undefined) {
    $.commission.textContent = fmtBps(positions[0].commission_bps);
  } else {
    $.commissionStat.style.display = "none";
  }
  $.epoch.textContent = state.status?.current_epoch ?? "—";

  $.totalStakeBlock.innerHTML = renderTotalStakeHero(stakedBySym, state.prices);

  // Positions block
  if (positions.length) {
    $.positionsBlock.innerHTML = `
      <h3 class="section-title">Positions (${positions.length})</h3>
      <div class="positions">
        ${positions.map((p) => {
          const sym = p.token_symbol || "STRK";
          const amt = Number(p.amount_decimal || 0);
          const reward = Number(p.unclaimed_rewards_decimal || 0);
          const usdAmt = symbolToUsd(sym, amt, state.prices);
          return `
            <div class="item">
              <div class="meta">
                <strong>${escapeHtml(sym)}</strong>
                <div class="sub">${copyableAddr(p.pool_contract)}</div>
              </div>
              <div class="amount">
                <strong>${fmtAmount(amt, sym)}</strong>
                <div class="muted small">${usdAmt !== null ? fmtUsd(usdAmt) + " · " : ""}🎁 ${fmtAmount(reward, "STRK")}</div>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  attachRemoveButton($.removeBtn, {
    kind: "delegator",
    label,
    matcher: (d) =>
      (d.delegator || "").toLowerCase() === dLower &&
      (!sLower || (d.staker || "").toLowerCase() === sLower),
  });
  bindCopyHandlers();
}

// ---------------------------------------------------------------------------
// "Remove from tracking" — confirms, PUTs the trimmed list, navigates back.
// ---------------------------------------------------------------------------

function bannerHTML(kind, title, sub) {
  // Two-line banner: bold one-glance verdict + plain-language explanation
  // so a delegator who doesn't know what "attestation" means still gets it.
  return `
    <div class="banner ${kind}">
      <div class="banner-body">
        <div class="banner-title">${title}</div>
        <div class="banner-sub">${sub}</div>
      </div>
    </div>
  `;
}

function renderValidatorStatusBanner(data) {
  // Picks the most actionable single line about the validator's health:
  //   1. Unstake requested  → red, exit pending
  //   2. Missed epochs > 0  → orange with count
  //   3. Attested this epoch → green confirmation
  //   4. Mid-epoch awaiting → neutral
  // Used by both the validator detail view (own data) and the delegator
  // detail view (their staker's data fetched via /api/v1/validators).
  if (!data) return "";
  const att = data.attestation;
  if (data.unstake_requested) {
    return bannerHTML(
      "danger",
      t("webapp_status_exiting_t", "🚫 Validator is exiting"),
      t("webapp_status_exiting_sub",
        "An unstake has been requested. New rewards will stop and your delegation will be returned after the unbonding period."),
    );
  }
  if (att && att.missed_epochs > 0) {
    const n = att.missed_epochs;
    return bannerHTML(
      "warn",
      t("webapp_status_missed_t", `⚠ Validator missed ${n} attestation${n === 1 ? "" : "s"}`, { n }),
      t("webapp_status_missed_sub",
        `Last confirmed in epoch ${att.last_epoch_attested}, current epoch ${att.current_epoch}. Skipped attestations reduce both the validator's and delegators' rewards.`,
        { last: att.last_epoch_attested, epoch: att.current_epoch, n }),
    );
  }
  if (att && att.is_attesting_this_epoch) {
    return bannerHTML(
      "success",
      t("webapp_status_healthy_t", "✓ Validator healthy"),
      t("webapp_status_healthy_sub",
        `Already attested for epoch ${att.current_epoch} — your rewards are accruing normally.`,
        { epoch: att.current_epoch }),
    );
  }
  if (att) {
    return bannerHTML(
      "muted",
      t("webapp_status_waiting_t", "⏳ Waiting for this epoch's attestation"),
      t("webapp_status_waiting_sub",
        `Validators must attest once per epoch. Epoch ${att.current_epoch} is still in progress — this is normal as long as it finishes before the epoch ends.`,
        { epoch: att.current_epoch }),
    );
  }
  return bannerHTML(
    "muted",
    t("webapp_status_unavailable_t", "Validator status unavailable"),
    t("webapp_status_unavailable_sub",
      "Couldn't reach the attestation contract just now. Reopen the Mini App in a few seconds."),
  );
}

function renderTotalStakeHero(totalsBySym, prices) {
  // Filter out zero entries up-front so the breakdown line stays clean
  // (e.g. validators with no BTC pools just see "X STRK", not
  // "X STRK · 0 WBTC · 0 LBTC").
  const nonZero = Object.fromEntries(
    Object.entries(totalsBySym || {}).filter(([_, v]) => Number(v) > 0)
  );
  if (Object.keys(nonZero).length === 0) return "";

  const usdAggregate = totalUsd(nonZero, prices);
  const breakdown = Object.entries(nonZero)
    .map(([sym, amt]) => fmtAmount(amt, sym))
    .join(" · ");

  // Headline: USD aggregate (cross-token sum), prominent.
  // Subtitle: per-token breakdown so the user sees the actual mix.
  // If we don't have a price for a symbol, fall back to showing the
  // breakdown as the headline — better than rendering "—".
  const headline = usdAggregate !== null ? `≈ ${fmtUsd(usdAggregate)}` : breakdown;
  const sub = usdAggregate !== null ? breakdown : "";

  return `
    <div class="hero">
      <div class="muted small">${escapeHtml(t("webapp_total_stake_caption", "Total stake (own + delegations)"))}</div>
      <div class="hero-value">${escapeHtml(headline)}</div>
      ${sub ? `<div class="hero-sub muted small">${escapeHtml(sub)}</div>` : ""}
    </div>
  `;
}

function renderOperatorWalletBlock(data, state) {
  // ``data`` is a ValidatorInfo (own card or fetched-via-staker). The
  // operator wallet is the one signing attestation txs; we surface its
  // STRK balance because running it dry causes silent missed attestations.
  // When the user has a low-balance threshold configured and the live
  // balance is below it, we colour the row warn/danger.
  if (!data || !data.operational_address || data.operational_address === "0x0") {
    return "";
  }
  const op = data.operational_address;
  const bal = data.operator_strk_balance;
  const balNum = bal !== null && bal !== undefined ? Number(bal) : null;
  const usdBal = balNum !== null
    ? symbolToUsd("STRK", balNum, state?.prices)
    : null;

  const threshold = Number(state?.notification?.operator_balance_min_strk || 0);
  let badge = "";
  let cardClass = "card";
  if (balNum !== null && threshold > 0) {
    const thrStr = fmtAmount(threshold, "STRK");
    if (balNum < threshold) {
      badge = `<span class="chip danger">${escapeHtml(t("webapp_below_threshold", `⚠ Below ${thrStr}`, { amount: thrStr }))}</span>`;
      cardClass += " card-warn";
    } else {
      badge = `<span class="chip success">${escapeHtml(t("webapp_above_threshold", `✓ Above ${thrStr}`, { amount: thrStr }))}</span>`;
    }
  }

  const balLine = balNum !== null
    ? `${fmtAmount(balNum, "STRK")}${usdBal !== null ? ` · ${fmtUsd(usdBal)}` : ""}`
    : t("webapp_balance_unavailable", "Balance unavailable");

  return `
    <div class="hero op-wallet ${cardClass.includes('warn') ? 'op-wallet-warn' : ''}">
      <div class="op-wallet-head">
        <div>
          <div class="muted small">${escapeHtml(t("webapp_operator_wallet_caption", "Operator wallet (gas reserve)"))}</div>
          <div class="addr-mono">${copyableAddr(op)}</div>
        </div>
        ${badge}
      </div>
      <div class="hero-value">${escapeHtml(balLine)}</div>
      <div class="muted small">
        ${escapeHtml(t("webapp_operator_wallet_explainer",
          "Validators sign attestations from this wallet. If the STRK balance runs out, attestations get missed silently — set a low-balance alert in Settings to catch it early."))}
      </div>
    </div>
  `;
}

function attachRemoveButton(btn, { kind, label, matcher }) {
  if (!btn) return;
  btn.hidden = false;
  const removeLabel = kind === "validator"
    ? t("webapp_remove_validator", "Remove validator from tracking")
    : t("webapp_remove_delegation", "Remove delegation from tracking");
  btn.textContent = removeLabel;
  btn.onclick = async () => {
    const confirmText = `${removeLabel} — “${label}”?`;
    const ok = (tg && tg.showConfirm)
      ? await new Promise((res) => tg.showConfirm(confirmText, res))
      : window.confirm(confirmText);
    if (!ok) return;

    btn.disabled = true;
    btn.textContent = "Removing…";
    try {
      const current = await api("/api/v1/users/me/tracking");
      const next = {
        validators: current.validators || [],
        delegations: current.delegations || [],
      };
      if (kind === "validator") {
        next.validators = next.validators.filter((v) => !matcher(v));
      } else {
        next.delegations = next.delegations.filter((d) => !matcher(d));
      }
      await api("/api/v1/users/me/tracking", { method: "PUT", body: next });
      // Force a fresh fetch on the dashboard.
      state.entries = null;
      toast("Removed");
      navigate("#/");
    } catch (err) {
      btn.disabled = false;
      btn.textContent = removeLabel;
      toast(err.message);
    }
  };
}

// ---------------------------------------------------------------------------
// Settings view
// ---------------------------------------------------------------------------

async function renderSettings() {
  setTopbar(t("settings", "Settings"), t("webapp_topbar_settings_sub", "Notifications"));
  renderTemplate("tpl-settings");
  const $ = bindings();

  const cfg = await api("/api/v1/users/me/notification-config");
  state.notification = cfg;

  // Pick the active mode from the persisted config. The API still accepts
  // both shapes (USD aggregate AND per-token), but the UI now exposes a
  // single-choice toggle so users don't accidentally arm two competing
  // alerts. Existing users who set both via the bot get USD here (it's
  // the higher-level / portfolio-wide one); they can switch to STRK if
  // they prefer.
  const initialMode =
    cfg.usd_threshold && cfg.usd_threshold > 0
      ? "usd"
      : cfg.token_thresholds && cfg.token_thresholds["STRK"] && cfg.token_thresholds["STRK"] > 0
        ? "strk"
        : "off";
  const initialAmount =
    initialMode === "usd"
      ? cfg.usd_threshold
      : initialMode === "strk"
        ? cfg.token_thresholds["STRK"]
        : 0;

  let mode = initialMode;
  const amountInput = document.getElementById("amount-input");
  const thresholdRow = document.getElementById("threshold-row");
  const segment = document.getElementById("mode-segment");

  function applyMode(next) {
    mode = next;
    for (const opt of segment.querySelectorAll(".seg-option")) {
      opt.setAttribute("aria-selected", String(opt.dataset.mode === mode));
    }
    if (mode === "off") {
      thresholdRow.hidden = true;
    } else {
      thresholdRow.hidden = false;
      $.amountLabel.textContent = t("webapp_threshold_label", "Threshold");
      if (mode === "usd") {
        $.prefix.hidden = false;
        $.suffix.hidden = true;
        $.modeHint.textContent = t("webapp_threshold_hint_usd",
          "Sum of unclaimed rewards across all your positions, converted via CoinGecko.");
        amountInput.placeholder = "0.00";
      } else {
        $.prefix.hidden = true;
        $.suffix.hidden = false;
        $.suffix.textContent = "STRK";
        $.modeHint.textContent = t("webapp_threshold_hint_strk",
          "Total unclaimed STRK across your portfolio. Validator pool rewards are always paid in STRK in V2.");
        amountInput.placeholder = "0";
      }
    }
  }

  applyMode(initialMode);
  amountInput.value = initialAmount && initialAmount > 0 ? initialAmount : "";

  // Operator wallet low-balance threshold — independent of the reward
  // notification mode. 0 means "off".
  const opBalanceInput = document.getElementById("op-balance-input");
  const opMin = Number(cfg.operator_balance_min_strk || 0);
  if (opBalanceInput) opBalanceInput.value = opMin > 0 ? opMin : "";

  // Language picker — populate options once, preselect current value, then
  // PUT on change and re-render the settings page in the new locale. We
  // reload state.profile / state.locale before re-rendering so every other
  // route also picks up the new language until the page is closed.
  const langPicker = document.getElementById("language-picker");
  if (langPicker) {
    langPicker.innerHTML = "";
    const currentLang = (state.profile && state.profile.language) || "en";
    for (const { code, label } of SUPPORTED_LOCALES) {
      const opt = document.createElement("option");
      opt.value = code;
      opt.textContent = label;
      if (code === currentLang) opt.selected = true;
      langPicker.appendChild(opt);
    }
    langPicker.addEventListener("change", async () => {
      const next = langPicker.value;
      langPicker.disabled = true;
      try {
        const updated = await api("/api/v1/users/me/language", {
          method: "PUT",
          body: { language: next },
        });
        state.profile = updated;
        await loadLocale(next);
        toast(t("webapp_saved", "Saved."));
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        // Re-enter the settings view so every label refreshes in the new
        // locale (instead of re-translating each binding by hand).
        await renderSettings();
      } catch (err) {
        toast(err.message);
        langPicker.disabled = false;
      }
    });
  }

  for (const opt of segment.querySelectorAll(".seg-option")) {
    opt.addEventListener("click", () => applyMode(opt.dataset.mode));
  }

  const saveBtn = document.getElementById("save-settings");
  const statusEl = document.getElementById("settings-status");
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    statusEl.textContent = t("webapp_saving", "Saving…");
    try {
      let payload;
      if (mode === "off") {
        payload = { usd_threshold: 0, token_thresholds: {} };
      } else {
        const v = Number(amountInput.value || 0);
        if (!(v > 0)) {
          statusEl.textContent = t("webapp_save_error_positive",
            "Enter a positive amount or pick Off.");
          saveBtn.disabled = false;
          return;
        }
        payload = mode === "usd"
          ? { usd_threshold: v, token_thresholds: {} }
          : { usd_threshold: 0, token_thresholds: { STRK: v } };
      }
      // Always include the operator-balance threshold in the payload —
      // it lives in the same notification_config blob and would be
      // wiped if we omitted it.
      const opMinNew = Number(opBalanceInput?.value || 0);
      payload.operator_balance_min_strk = opMinNew > 0 ? opMinNew : 0;
      await api("/api/v1/users/me/notification-config", { method: "PUT", body: payload });
      state.notification = payload;
      statusEl.textContent = t("webapp_saved", "Saved.");
      toast(t("webapp_saved", "Saved"));
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      setTimeout(() => (statusEl.textContent = ""), 2000);
    } catch (err) {
      statusEl.textContent = err.message;
    } finally {
      saveBtn.disabled = false;
    }
  };
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

renderRoute().catch((err) => toast(err.message));
