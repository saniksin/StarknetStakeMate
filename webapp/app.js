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
};

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
  else formatted = n.toExponential(2);
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
}

function bindings() {
  const out = {};
  for (const el of viewEl.querySelectorAll("[data-bind]")) out[el.dataset.bind] = el;
  return out;
}

async function renderRoute() {
  const route = parseRoute();
  document.getElementById("app").dataset.view = route.name;
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

  $.totalStakeUsd.textContent = stakedUsd !== null
    ? fmtUsd(stakedUsd)
    : (Object.keys(stakedTotal).length ? fmtAmount(stakedTotal["STRK"] || 0, "STRK") : "—");

  $.totalUnclaimedUsd.textContent = unclaimedUsd !== null
    ? fmtUsd(unclaimedUsd)
    : fmtAmount(unclaimedTotal["STRK"] || 0, "STRK");

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
  $.address.textContent = entry.address;

  // Status banner
  const att = data.attestation;
  let banner = "";
  if (data.unstake_requested) {
    banner = `<div class="banner danger">⚠️ Unstake requested — exit pending</div>`;
  } else if (att && att.missed_epochs > 0) {
    banner = `<div class="banner warn">⚠ ${att.missed_epochs} missed attestation epoch${att.missed_epochs === 1 ? "" : "s"} (current ${att.current_epoch}, last attested ${att.last_epoch_attested})</div>`;
  } else if (att && att.is_attesting_this_epoch) {
    banner = `<div class="banner success">✓ Attesting this epoch</div>`;
  } else if (att) {
    banner = `<div class="banner">Awaiting attestation in epoch ${att.current_epoch}</div>`;
  }
  $.statusBanner.innerHTML = banner;

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
                <div class="sub addr-mono">${fmtAddr(p.pool_contract)}</div>
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
  $.address.innerHTML = `
    <div>delegator <span class="addr-mono">${fmtAddr(data.delegator_address)}</span></div>
    <div>via <span class="addr-mono">${fmtAddr(data.staker_address)}</span></div>
  `;
  $.address.classList.remove("addr-mono");

  $.statusBanner.innerHTML = "";

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
                <div class="sub addr-mono">${fmtAddr(p.pool_contract)}</div>
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
}

// ---------------------------------------------------------------------------
// Settings view
// ---------------------------------------------------------------------------

async function renderSettings() {
  setTopbar("Settings", "Notifications");
  renderTemplate("tpl-settings");

  const cfg = await api("/api/v1/users/me/notification-config");
  state.notification = cfg;

  const usdInput = document.getElementById("usd-threshold");
  usdInput.value = cfg.usd_threshold && cfg.usd_threshold > 0 ? cfg.usd_threshold : "";

  // Discover token list from the user's own entries — only show what's
  // relevant to them.
  if (!state.entries) state.entries = await api("/api/v1/users/me/entries");
  const tokens = new Set(["STRK"]); // STRK is always relevant
  for (const e of state.entries) {
    for (const sym of Object.keys(entryStakedBySymbol(e))) tokens.add(sym);
  }

  const tokenContainer = document.getElementById("token-thresholds");
  tokenContainer.innerHTML = "";
  for (const sym of tokens) {
    const current = cfg.token_thresholds?.[sym];
    const row = document.createElement("div");
    row.className = "token-row";
    row.innerHTML = `
      <div class="sym"><span class="badge">${escapeHtml(sym.slice(0, 1))}</span> ${escapeHtml(sym)}</div>
      <div class="input-with-suffix">
        <input type="number" min="0" step="any" data-token="${escapeHtml(sym)}" value="${current && current > 0 ? current : ""}" placeholder="0" />
        <span class="suffix">${escapeHtml(sym)}</span>
      </div>
    `;
    tokenContainer.appendChild(row);
  }

  const saveBtn = document.getElementById("save-settings");
  const statusEl = document.getElementById("settings-status");
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    statusEl.textContent = "Saving…";
    try {
      const usd = Number(usdInput.value || 0);
      const tokenInputs = tokenContainer.querySelectorAll("input[data-token]");
      const tokenThresholds = {};
      for (const inp of tokenInputs) {
        const v = Number(inp.value || 0);
        if (v > 0) tokenThresholds[inp.dataset.token] = v;
      }
      const payload = { usd_threshold: usd > 0 ? usd : 0, token_thresholds: tokenThresholds };
      await api("/api/v1/users/me/notification-config", { method: "PUT", body: payload });
      state.notification = payload;
      statusEl.textContent = "Saved.";
      toast("Saved");
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
