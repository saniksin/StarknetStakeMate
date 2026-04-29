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
  reorderMode: false,   // dashboard drag-and-drop reorder toggle
  // Snapshot of entries[] taken when reorder mode is entered, used to
  // roll back the optimistic UI if the PUT /tracking/order call fails.
  reorderInitial: null,
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
  // Translate the static topbar / header elements that live OUTSIDE the
  // route templates. ``renderTemplate`` only runs ``applyI18n`` on the
  // freshly cloned ``viewEl``, so anything in <header> (Settings button,
  // initial topbar title) would otherwise stay in English.
  if (typeof applyI18n === "function") applyI18n(document);
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

async function loadNotificationConfig() {
  // Pulled from the validator detail view as well as Settings so the
  // operator-wallet badge has the threshold ready on first render. We
  // swallow errors and return ``{}`` so a transient API blip degrades
  // to "no badge" instead of breaking the whole page.
  try {
    return await api("/api/v1/users/me/notification-config");
  } catch (err) {
    console.warn("notification-config fetch failed", err);
    return {};
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
  if (parts[0] === "add") return { name: "add" };
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
    else if (route.name === "add") await renderAdd();
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

  $.epochChip.textContent = t("webapp_epoch_chip", `epoch ${status.current_epoch}`, { epoch: status.current_epoch });
  setTopbar(
    t("webapp_topbar_portfolio", "Portfolio"),
    `${status.network} · ${t("webapp_epoch_chip", `epoch ${status.current_epoch}`, { epoch: status.current_epoch })}`,
  );

  // Epoch progress bar — slim, full-width strip under the chip row.
  // Replaces the old ``epochTailChip`` which packed "→ 9596 через 954
  // блока (~41 мин)" into a single chip and overflowed on narrow
  // phones. The bar shows where the chain stands inside the current
  // epoch (left → start, right → next-epoch boundary) plus a short
  // "31% to 9596" label that doesn't depend on plural noun forms.
  const progress = $.epochProgress;
  if (progress) {
    const tl = status.epoch_timeline;
    const pct = epochProgressPercent(tl);
    if (pct != null && tl) {
      progress.hidden = false;
      const minutes = Math.max(0, Math.floor((tl.seconds_left_in_epoch || 0) / 60));
      const minutesStr = tN("att_minutes", minutes, currentLang());
      $.epochProgressFill.style.width = `${pct}%`;
      $.epochProgressLabel.textContent = t(
        "epoch_progress_label",
        `${pct}% to ${tl.next_epoch}`,
        { percent: pct, next_epoch: tl.next_epoch, minutes: minutesStr },
      ) + ` · ~${minutesStr}`;
    } else {
      progress.hidden = true;
    }
  }

  const validators = entries.filter((e) => e.kind === "validator");
  const delegations = entries.filter((e) => e.kind === "delegator");
  // Plural-aware count chip — split into per-noun keys so ru/ua/pl get
  // the right case ("3 валидатора" not "3 валидаторов"). The "·"
  // separator stays in JS so the locale templates only carry the
  // pluralized noun.
  const validatorsTxt = tN(
    "webapp_validators_count", validators.length, currentLang(),
    `${validators.length} validators`,
  );
  const delegationsTxt = tN(
    "webapp_delegations_count", delegations.length, currentLang(),
    `${delegations.length} delegations`,
  );
  $.countsChip.textContent = `${validatorsTxt} · ${delegationsTxt}`;

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
  // ``innerHTML`` (not textContent) so we can wrap the "incl. BTC pools"
  // suffix in a ``.nowrap`` span. Without it the phrase used to wrap
  // mid-word ("вкл./BTC/пулы" on three lines) at narrow widths.
  if (stakedUsd !== null) {
    const inclTag = hasBtcPools
      ? ` · <span class="nowrap">${escapeHtml(t("webapp_incl_btc_pools", "incl. BTC pools"))}</span>`
      : "";
    $.totalStakeSecondary.innerHTML = `≈ ${escapeHtml(fmtUsd(stakedUsd))}${inclTag}`;
  } else {
    $.totalStakeSecondary.textContent = "";
  }

  const unclaimedStrk = unclaimedTotal["STRK"] || 0;
  $.totalUnclaimedPrimary.textContent = fmtAmount(unclaimedStrk, "STRK");
  $.totalUnclaimedSecondary.textContent = unclaimedUsd !== null
    ? `≈ ${fmtUsd(unclaimedUsd)}`
    : "";

  // Wire the "+ Add" CTA in the section row above the list. The button
  // lives inside the dashboard template (not the topbar) because mobile
  // patterns put primary content actions inline with their section so
  // they're discoverable but don't crowd the global header.
  const addBtn = viewEl.querySelector("[data-action='add']");
  if (addBtn) addBtn.addEventListener("click", () => navigate("#/add"));

  // Wire the "↕ Reorder" / "✓ Done" CTA. The button is hidden when
  // there's <2 entries (nothing to reorder). The same button doubles
  // as the "Done" affordance during reorder mode — saves a topbar slot.
  const reorderBtn = viewEl.querySelector("[data-action='reorder']");
  if (reorderBtn) {
    reorderBtn.hidden = entries.length < 2;
    reorderBtn.addEventListener("click", () => {
      if (state.reorderMode) {
        // Tapping Done commits the order. We never block on the network
        // — if it fails, the toast surfaces it and we roll back.
        disableReorderMode(true);
      } else {
        enableReorderMode();
      }
    });
  }

  // Entries list
  if (entries.length === 0) {
    $.entries.innerHTML = `<div class="placeholder">${escapeHtml(t("webapp_no_tracked_yet", "No tracked addresses yet. Tap “+ Add” above or open the bot."))}</div>`;
    return;
  }
  $.entries.innerHTML = "";
  for (const e of entries) renderEntryCard(e, $.entries, prices);

  // Re-apply the reorder visual state on every dashboard render. The
  // user can navigate away and come back via #/v/... → back, and we
  // want the mode to survive that round-trip if it was on.
  if (state.reorderMode) _applyReorderVisuals(true);
}

function renderEntryCard(entry, container, prices) {
  const isValidator = entry.kind === "validator";
  const label = entry.label || fmtAddr(entry.address);
  const card = document.createElement("button");
  card.className = "row-card";
  card.type = "button";

  // Identity attributes — used by the reorder save step to read the
  // post-drag order off the DOM. Validators key on ``address``, delegations
  // key on ``(delegator, staker)``. We stamp ``kind`` so the save step can
  // separate the two lists into the API payload.
  card.dataset.kind = entry.kind;
  if (isValidator) {
    card.dataset.address = entry.address;
  } else {
    card.dataset.delegator = entry.data?.delegator_address || entry.address;
    card.dataset.staker = entry.data?.staker_address || "";
  }

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

  // Drag handle is rendered once and toggled via .reorder-mode parent
  // class — keeps the card geometry stable so the layout shift between
  // normal/reorder mode is just a fade-in for the handle (no jank). The
  // ⠿ glyph (BRAILLE PATTERN DOTS-12345678) is the de-facto sortable
  // handle character on iOS / GitHub / Notion / Linear; users recognise
  // it without a tooltip.
  card.innerHTML = `
    <span class="drag-handle" aria-hidden="true">⠿</span>
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
  card.addEventListener("click", (ev) => {
    // Suppress navigation while in reorder mode — tapping the body
    // there has no semantic action; the only valid input is dragging
    // the handle. Without this guard a user mid-drag who let go on
    // the body would teleport into the detail view.
    if (state.reorderMode) {
      ev.preventDefault();
      return;
    }
    if (isValidator) navigate(`#/v/${entry.address}`);
    else navigate(`#/d/${entry.data?.delegator_address || entry.address}/${entry.data?.staker_address || ""}`);
  });

  // Pointer-event sortable hookup. Lives on the handle, not the body,
  // so vertical scroll on the rest of the card still works on touch.
  const handle = card.querySelector(".drag-handle");
  if (handle) _wireDragHandle(card, handle);

  container.appendChild(card);
}

// ---------------------------------------------------------------------------
// Reorder mode (drag-and-drop)
//
// Two-list sortable. Validators reorder among validators; delegations
// reorder among delegations. We enforce the section boundary by only
// considering siblings of the same ``data-kind`` when computing drop
// targets — dragging a validator past the last validator simply pins
// it at the end of the validators block, the cursor doesn't pull it
// into the delegations group.
//
// Pointer events (``pointerdown/move/up``) cover both mouse and touch
// in one code path. We start the drag only on ``pointerdown`` on the
// ``.drag-handle`` — never the body — so vertical scroll on the rest
// of the card still works on touch (the ``touch-action: none`` rule is
// scoped to the handle in CSS for the same reason).
// ---------------------------------------------------------------------------

function enableReorderMode() {
  state.reorderMode = true;
  // Snapshot the current order so we can roll back if the save fails.
  state.reorderInitial = Array.isArray(state.entries)
    ? state.entries.map((e) => ({ ...e }))
    : null;
  _applyReorderVisuals(true);
  if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
}

async function disableReorderMode(save) {
  state.reorderMode = false;
  // Force-flush any in-flight drag — if the user taps Done while still
  // holding a card (or a previous drag never received its terminal
  // event for some reason), the per-card cleanup hook resets transform
  // + dragging class so the rerender below paints clean cards.
  for (const el of viewEl.querySelectorAll(".row-card")) {
    if (typeof el._reorderCleanup === "function") {
      try { el._reorderCleanup(); } catch (_) {}
    }
  }
  _applyReorderVisuals(false);

  if (!save) {
    // Cancel — restore the original order if we have a snapshot.
    if (state.reorderInitial) state.entries = state.reorderInitial;
    state.reorderInitial = null;
    await renderDashboard();
    return;
  }

  // Read the post-drag order off the DOM, build the API payload.
  const cardEls = Array.from(viewEl.querySelectorAll(".row-card"));
  const validators = [];
  const delegations = [];
  for (const el of cardEls) {
    if (el.dataset.kind === "validator") {
      if (el.dataset.address) validators.push(el.dataset.address);
    } else if (el.dataset.kind === "delegator") {
      if (el.dataset.delegator && el.dataset.staker) {
        delegations.push([el.dataset.delegator, el.dataset.staker]);
      }
    }
  }

  // Optimistic update — sort ``state.entries`` to match the DOM order so
  // the next renderDashboard call paints the new order even before the
  // PUT lands. If the network fails we restore from the snapshot.
  const orderKey = (e) => {
    if (e.kind === "validator") return `v:${(e.address || "").toLowerCase()}`;
    return `d:${(e.data?.delegator_address || "").toLowerCase()}|${(e.data?.staker_address || "").toLowerCase()}`;
  };
  const targetIndex = new Map();
  cardEls.forEach((el, i) => {
    if (el.dataset.kind === "validator" && el.dataset.address) {
      targetIndex.set(`v:${el.dataset.address.toLowerCase()}`, i);
    } else if (el.dataset.kind === "delegator") {
      targetIndex.set(
        `d:${(el.dataset.delegator || "").toLowerCase()}|${(el.dataset.staker || "").toLowerCase()}`,
        i,
      );
    }
  });
  if (Array.isArray(state.entries)) {
    state.entries = [...state.entries].sort(
      (a, b) => (targetIndex.get(orderKey(a)) ?? 0) - (targetIndex.get(orderKey(b)) ?? 0),
    );
  }

  try {
    await api("/api/v1/users/me/tracking/order", {
      method: "PUT",
      body: { validators, delegations },
    });
    state.reorderInitial = null;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    toast(t("webapp_saved", "Saved"));
    await renderDashboard();
  } catch (err) {
    // Roll back the optimistic UI to the snapshot we took on enter.
    if (state.reorderInitial) state.entries = state.reorderInitial;
    state.reorderInitial = null;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("error");
    toast(t("webapp_reorder_save_failed", err.message));
    await renderDashboard();
  }
}

function _applyReorderVisuals(on) {
  // Toggle the mode class on the dashboard root + reveal the hint
  // paragraph + flip the reorder button label between "Reorder" and
  // "Done". CSS keys off the .reorder-mode class for the rest.
  const app = document.getElementById("app");
  if (app) app.classList.toggle("reorder-mode", on);

  const hint = viewEl.querySelector("[data-bind='reorderHint']");
  if (hint) hint.hidden = !on;

  const reorderBtn = viewEl.querySelector("[data-action='reorder']");
  if (reorderBtn) {
    reorderBtn.textContent = on
      ? t("webapp_reorder_done", "✓ Done")
      : t("webapp_reorder_button", "↕ Reorder");
    reorderBtn.classList.toggle("active", on);
  }

  // Hide the "+ Add" CTA in reorder mode — it's not actionable while
  // dragging and the two buttons sitting side-by-side at different
  // semantic levels was visually noisy.
  const addBtn = viewEl.querySelector("[data-action='add']");
  if (addBtn) addBtn.hidden = on;
}

// Drag-and-drop wiring. Plain pointer events — no Sortable.js dep so
// the Mini App stays a single-file ~1700-line bundle. The drop target
// resolution algorithm is "swap with the sibling whose midpoint we
// crossed", run on every pointermove. Cheap enough at 10 cards.
function _wireDragHandle(card, handle) {
  let dragging = false;
  let pointerId = null;
  let startY = 0;
  let cardCenterStart = 0;
  let translateY = 0;
  // Bound terminal listeners attached to ``window`` for the duration of
  // the drag — see ``onDown`` for the rationale. Stored at the
  // closure level so ``cleanup()`` can detach them symmetrically.
  let windowListenersBound = false;

  // Idempotent cleanup. Safe to call from any terminal pointer event
  // (pointerup / pointercancel / lostpointercapture / touchend /
  // touchcancel) AND from cancel paths in the parent reorder controller.
  // The ``dragging`` flag guards against double-cleanup so a redundant
  // ``lostpointercapture`` after a successful ``pointerup`` is a no-op.
  const cleanup = () => {
    if (!dragging && !windowListenersBound) return;
    dragging = false;
    if (pointerId !== null) {
      // ``releasePointerCapture`` throws ``InvalidPointerId`` if the
      // browser already lost capture on its own (which is exactly the
      // path that left the card stuck). Swallow it.
      try { handle.releasePointerCapture(pointerId); } catch (_) {}
    }
    pointerId = null;
    // Reset visual state. ``transform`` is the load-bearing one — if
    // it's left set the card visually stays "lifted" at its drag
    // offset even though the DOM is in the right place.
    card.style.transform = "";
    card.style.zIndex = "";
    card.classList.remove("dragging");
    if (windowListenersBound) {
      window.removeEventListener("pointerup", onUpWin, true);
      window.removeEventListener("pointercancel", onUpWin, true);
      window.removeEventListener("pointermove", onMoveWin, true);
      window.removeEventListener("touchend", onUpWin, true);
      window.removeEventListener("touchcancel", onUpWin, true);
      windowListenersBound = false;
    }
  };

  const onDown = (ev) => {
    if (!state.reorderMode) return;
    // Only the primary button / first touch — multi-finger zoom mid-drag
    // would otherwise dump us into a half-drag state we can't recover.
    if (ev.button !== undefined && ev.button !== 0) return;
    // Guard against re-entry: if a previous drag never cleaned up
    // (shouldn't happen now, but defence-in-depth) flush state first.
    if (dragging) cleanup();
    ev.preventDefault();
    pointerId = ev.pointerId;
    // ``setPointerCapture`` redirects subsequent move/up events to
    // ``handle`` even if the finger drifts off the element. iOS Safari
    // quietly drops capture when the captured element's parent chain
    // mutates (which our DOM swap does), so we ALSO bind the same
    // handlers on ``window`` as a safety net — whichever fires first
    // wins, and ``cleanup()`` is idempotent.
    try { handle.setPointerCapture(pointerId); } catch (_) {}
    dragging = true;
    startY = ev.clientY;
    const rect = card.getBoundingClientRect();
    cardCenterStart = rect.top + rect.height / 2;
    translateY = 0;
    card.classList.add("dragging");
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("medium");

    // Bind window-level fallbacks. ``capture: true`` is important so
    // we receive the event before any descendant ``stopPropagation``
    // (the copy-handler does this on its [data-copy] children).
    window.addEventListener("pointerup", onUpWin, true);
    window.addEventListener("pointercancel", onUpWin, true);
    window.addEventListener("pointermove", onMoveWin, true);
    // Old WebViews that don't synthesise pointer events from touches
    // still emit touch events — most notably Telegram WebView on some
    // Android builds. The handler is identical (cleanup-only).
    window.addEventListener("touchend", onUpWin, true);
    window.addEventListener("touchcancel", onUpWin, true);
    windowListenersBound = true;
  };

  const onMove = (ev) => {
    if (!dragging || ev.pointerId !== pointerId) return;
    ev.preventDefault();
    translateY = ev.clientY - startY;
    card.style.transform = `translateY(${translateY}px)`;

    // Find the sibling whose midpoint our card-center has crossed.
    // Restricted to siblings with the same ``data-kind`` so a validator
    // can't end up in the delegations group (and vice-versa).
    const center = cardCenterStart + translateY;
    const siblings = Array.from(
      card.parentElement?.querySelectorAll(`.row-card[data-kind="${card.dataset.kind}"]`) ?? [],
    ).filter((el) => el !== card);

    for (const sib of siblings) {
      const sibRect = sib.getBoundingClientRect();
      const sibCenter = sibRect.top + sibRect.height / 2;
      // ``DOCUMENT_POSITION_FOLLOWING`` means ``sib`` is after ``card``
      // in source order — i.e. ``card`` precedes ``sib``. We move past
      // ``sib`` only when the card-center crossed its midpoint in the
      // direction of motion. Wrap the bitwise AND in parens because
      // ``&`` has lower precedence than ``===`` and confuses the linter.
      const pos = card.compareDocumentPosition(sib);
      const sibIsAfter = (pos & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
      const sibIsBefore = (pos & Node.DOCUMENT_POSITION_PRECEDING) !== 0;

      if (translateY > 0 && sibIsAfter && center > sibCenter) {
        sib.parentElement.insertBefore(card, sib.nextSibling);
        // Reset the visual drag offset because the DOM moved under us.
        const newRect = card.getBoundingClientRect();
        cardCenterStart = newRect.top + newRect.height / 2 - translateY;
        break;
      }
      if (translateY < 0 && sibIsBefore && center < sibCenter) {
        sib.parentElement.insertBefore(card, sib);
        const newRect = card.getBoundingClientRect();
        cardCenterStart = newRect.top + newRect.height / 2 - translateY;
        break;
      }
    }
  };

  // Window-level move dispatcher: the spec says captured pointermoves
  // route to the captured element, but in practice iOS WebView leaks
  // them to ``window`` after a DOM mutation. Re-route those into the
  // same ``onMove`` so the visual transform keeps tracking the finger
  // even when capture is silently dropped.
  const onMoveWin = (ev) => {
    if (!dragging) return;
    // Touch events use ``changedTouches[0].clientY``; pointer events
    // expose ``clientY`` directly. Normalise.
    const clientY = ev.clientY != null
      ? ev.clientY
      : ev.changedTouches?.[0]?.clientY;
    if (clientY == null) return;
    // Only honour events that match our pointerId, except for touch
    // events (no pointerId field — we trust the dragging flag instead).
    if (ev.pointerId !== undefined && ev.pointerId !== pointerId) return;
    onMove({
      pointerId: pointerId,
      clientY,
      preventDefault: () => { try { ev.preventDefault(); } catch (_) {} },
    });
  };

  // Window-level terminal dispatcher. Fires on ANY of:
  //   pointerup, pointercancel, touchend, touchcancel
  // Crucial because the original ``handle.addEventListener('pointerup')``
  // path silently misses the event when capture has been lost — which is
  // the exact failure mode that left cards stuck after a drag.
  const onUpWin = (ev) => {
    if (!dragging) return;
    if (ev.pointerId !== undefined && ev.pointerId !== pointerId) return;
    cleanup();
  };

  // ``lostpointercapture`` is the canonical "you've lost capture" event.
  // On iOS WebView it fires when the captured element's parent chain
  // mutates (our DOM swap during pointermove triggers exactly this). The
  // window-level listeners above are a safety net; this one is the
  // primary signal that we should clean up.
  const onLostCapture = (ev) => {
    if (ev.pointerId !== undefined && ev.pointerId !== pointerId) return;
    cleanup();
  };

  handle.addEventListener("pointerdown", onDown);
  handle.addEventListener("pointermove", onMove);
  handle.addEventListener("pointerup", (ev) => {
    if (!dragging || ev.pointerId !== pointerId) return;
    cleanup();
  });
  handle.addEventListener("pointercancel", (ev) => {
    if (!dragging || ev.pointerId !== pointerId) return;
    cleanup();
  });
  handle.addEventListener("lostpointercapture", onLostCapture);
  // ``pointerleave`` on its own is too aggressive (fires whenever the
  // finger drifts off the 16px handle, which is constant on touch);
  // we rely on capture + window fallbacks instead.

  // Expose the cleanup so the higher-level reorder controller can
  // force-flush a stuck drag when the user taps Done / Cancel mid-gesture.
  card._reorderCleanup = cleanup;
}

// ---------------------------------------------------------------------------
// Validator detail view
// ---------------------------------------------------------------------------

async function renderValidator(address) {
  setTopbar(t("webapp_topbar_validator", "Validator"), fmtAddr(address));
  renderTemplate("tpl-detail");
  const $ = bindings();

  if (!state.entries) state.entries = await api("/api/v1/users/me/entries");
  if (state.prices === null) state.prices = await loadPrices();
  // ``state.notification`` was only loaded inside renderSettings(), so
  // the operator-wallet badge race-condition'd: first render saw
  // threshold=0 → no badge → after the user opened Settings and came
  // back, it suddenly appeared. Pre-load lazily here so the very first
  // render already has the threshold.
  if (state.notification === null) state.notification = await loadNotificationConfig();

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
  setTopbar(t("webapp_topbar_validator", "Validator"), label);

  $.avatar.textContent = "🛡";
  $.label.textContent = label;
  $.address.classList.remove("addr-mono");
  $.address.innerHTML = copyableAddr(entry.address);

  // Status banner — same logic delegator detail uses, factored out so the
  // two views always stay in sync.
  $.statusBanner.innerHTML = renderValidatorStatusBanner(data);
  bindInfoIconHandlers();

  // Stats grid
  $.primaryStakeLabel.textContent = t("webapp_own_stake_label", "Own stake");
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
      <h3 class="section-title">${escapeHtml(t("webapp_pools_section_title", `Pools (${pools.length})`, { n: pools.length }))}</h3>
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
  setTopbar(t("webapp_topbar_delegation", "Delegation"), fmtAddr(delegatorAddr));
  renderTemplate("tpl-detail");
  const $ = bindings();

  if (!state.entries) state.entries = await api("/api/v1/users/me/entries");
  if (state.prices === null) state.prices = await loadPrices();
  // Same race fix as in renderValidator — preload the threshold so the
  // delegator-side staker banner picks it up on first render.
  if (state.notification === null) state.notification = await loadNotificationConfig();

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
  setTopbar(t("webapp_topbar_delegation", "Delegation"), label);

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
      // Operator wallet block is intentionally NOT rendered on the
      // delegator detail view: a delegator can't act on their staker's
      // gas-reserve drainage from inside this card, so the block was
      // pure noise. The data is still one tap away on the validator
      // detail card if they actually need it.
      bindCopyHandlers();
      bindInfoIconHandlers();
    })
    .catch((err) => {
      console.warn("validator status lookup failed", err);
      $.statusBanner.innerHTML = `<div class="banner muted">Validator status unavailable</div>`;
    });

  // Stats grid: total stake (per primary token) + total unclaimed (STRK).
  const stakedBySym = entryStakedBySymbol(entry);
  const primarySym = Object.keys(stakedBySym)[0] || "STRK";
  const primaryAmt = stakedBySym[primarySym] || 0;
  $.primaryStakeLabel.textContent = positions.length > 1
    ? t("webapp_primary_stake_label", "Primary stake")
    : t("webapp_stake_label", "Stake");
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
      <h3 class="section-title">${escapeHtml(t("webapp_positions_section_title", `Positions (${positions.length})`, { n: positions.length }))}</h3>
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

// Use Intl.PluralRules where available so we don't ship two diverging
// CLDR tables (one in Python, one in JS). Telegram WebView has it on
// every platform we care about (iOS Safari ≥12.1, Android Chrome ≥63).
// Fallback below only triggers in ancient WebViews — keep behaviour
// matched to the Python ``services/i18n_plural.py`` table.
function pluralCategory(n, locale) {
  try {
    return new Intl.PluralRules(locale).select(Math.abs(Math.trunc(n)));
  } catch (_) {
    const lang = (locale || "en").toLowerCase().split("-")[0];
    n = Math.abs(Math.trunc(n));
    if (lang === "ru" || lang === "ua" || lang === "uk") {
      const m10 = n % 10, m100 = n % 100;
      if (m10 === 1 && m100 !== 11) return "one";
      if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return "few";
      return "many";
    }
    if (lang === "pl") {
      if (n === 1) return "one";
      const m10 = n % 10, m100 = n % 100;
      if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return "few";
      return "many";
    }
    if (lang === "ko" || lang === "zh") return "other";
    return n === 1 ? "one" : "other";
  }
}

function currentLang() {
  // The active locale is the one stored on the user profile (set via
  // /api/v1/users/me/profile and rotated by the language picker). The
  // legacy ``state.lang`` field never existed; reading it returned
  // ``undefined`` which cascaded into the English fallback.
  return (state && state.profile && state.profile.language) || "en";
}

function tN(keyBase, count, locale, fallback, extraVars) {
  // Same lookup contract as services/i18n_plural.py::t_n: try
  // {key}_{category} → {key}_other → {key} → fallback. We rely on
  // ``t`` for the actual locale read (it falls back to English).
  // ``extraVars`` lets callers pass {label}/{epoch}/etc. into templates
  // that need more than just the count placeholder.
  const cat = pluralCategory(count, locale);
  const candidates = [`${keyBase}_${cat}`];
  if (cat !== "other") candidates.push(`${keyBase}_other`);
  candidates.push(keyBase);
  const vars = Object.assign({ n: count, count: count }, extraVars || {});
  for (const c of candidates) {
    const v = t(c, null, vars);
    // ``t`` returns the raw key when nothing matched; treat that as miss.
    if (v && v !== c) return v;
  }
  return fallback != null ? fallback : String(count);
}

function renderEpochTail(timeline) {
  // One-line "next epoch in N blocks (~M min)" appended to every status
  // banner. Returns "" when EpochInfo / chain head wasn't fetched —
  // banner stays consistent with the Python renderer in that case.
  if (!timeline || timeline.blocks_left_in_epoch == null
      || timeline.blocks_left_in_epoch < 0) {
    return "";
  }
  // ``state.lang`` was a typo — the active language lives in
  // ``state.profile.language`` (see loadProfileAndLocale). Reading the
  // wrong field made every locale silently fall through to ``"en"``,
  // which then asked for ``att_blocks_other`` — but ru/ua/pl don't
  // ship that suffix (they have ``_one/_few/_many``), so tN landed in
  // the final ``String(count)`` branch and the Mini App printed
  // ``"875 (~37)"`` without any noun.
  const lang = currentLang();
  const blocks = tN("att_blocks", timeline.blocks_left_in_epoch, lang);
  const minutes = Math.max(0, Math.floor((timeline.seconds_left_in_epoch || 0) / 60));
  const minutesStr = tN("att_minutes", minutes, lang);
  return t(
    "epoch_tail_blocks",
    `Next epoch (${timeline.next_epoch}) in ${blocks} (~${minutesStr})`,
    {
      next_epoch: timeline.next_epoch,
      blocks: blocks,
      minutes: minutesStr,
    }
  );
}

function renderEpochTailShort(timeline) {
  // Compact form for the hero chip — same data as ``renderEpochTail``
  // but using the ``epoch_tail_short`` template that drops the leading
  // "Next epoch (...)" label. Designed to fit in a small chip on
  // mobile (≤ ~32 chars).
  if (!timeline || timeline.blocks_left_in_epoch == null
      || timeline.blocks_left_in_epoch < 0) {
    return "";
  }
  const lang = currentLang();
  const blocks = tN("att_blocks", timeline.blocks_left_in_epoch, lang);
  const minutes = Math.max(0, Math.floor((timeline.seconds_left_in_epoch || 0) / 60));
  const minutesStr = tN("att_minutes", minutes, lang);
  return t(
    "epoch_tail_short",
    `→ ${timeline.next_epoch} in ${blocks} (~${minutesStr})`,
    {
      next_epoch: timeline.next_epoch,
      blocks: blocks,
      minutes: minutesStr,
    }
  );
}

function fmtBlock(n) {
  if (n == null) return "";
  // Underscore separators mirror the Python renderer.
  return String(Math.trunc(n)).replace(/\B(?=(\d{3})+(?!\d))/g, "_");
}

// Hero-chip short form. Lives next to the epoch chip on the dashboard.
// Even tighter than ``renderEpochTailShort`` — ``→ 9596 ~41 мин`` — to
// fit on narrow phones without wrapping.
function renderEpochTailChip(timeline) {
  if (!timeline || timeline.blocks_left_in_epoch == null
      || timeline.blocks_left_in_epoch < 0) {
    return "";
  }
  const lang = currentLang();
  const minutes = Math.max(0, Math.floor((timeline.seconds_left_in_epoch || 0) / 60));
  const minutesStr = tN("att_minutes", minutes, lang);
  return t(
    "epoch_tail_chip",
    `→ ${timeline.next_epoch} ~${minutesStr}`,
    {
      next_epoch: timeline.next_epoch,
      minutes: minutesStr,
    }
  );
}

function epochProgressPercent(timeline) {
  // Position inside the current epoch as a 0-100 integer. Used by the
  // hero progress bar so the user can see how much of the epoch is
  // already gone without doing the math themselves.
  if (!timeline || !timeline.epoch_length_blocks
      || timeline.blocks_left_in_epoch == null) {
    return null;
  }
  const total = timeline.epoch_length_blocks;
  const left = Math.max(0, timeline.blocks_left_in_epoch);
  const pct = Math.min(100, Math.max(0, Math.round(((total - left) / total) * 100)));
  return pct;
}

// Grid-layout body for the waiting-state banner. The detail-view used
// to flatten ``current/target/window/left/next`` into one ``·``-joined
// line which forced the user to parse 6 numbers visually with no
// landmarks. This breaks them into label/value rows so each piece has
// its own column-aligned slot — readable like a key/value table.
function renderAttestationDetails(att, timeline) {
  const lang = currentLang();
  // Approx Starknet block time (~2.6s real, target 2-3s). The ~ already
  // telegraphs imprecision — no need to caveat further. Pulled out of
  // the inner block so the new "until target" row reuses the same coef.
  const APPROX_BLOCK_SEC = 2.6;
  // Each row is an object so we can attach an optional ``labelHtml``
  // override (used by "Целевой блок (?)" — a plain HTML-escaped label
  // can't carry a button). Backward-compat helper accepts the old
  // 2-tuple shape too.
  const rows = [];
  const _push = (label, value) => rows.push({ label, value });
  const _pushHtml = (labelHtml, value) =>
    rows.push({ labelHtml, value });

  if (att.current_block != null) {
    _push(
      t("att_label_current_block", "Current block"),
      `<code>${fmtBlock(att.current_block)}</code>`,
    );
  }
  if (att.target_block != null) {
    // Target-block row gets a (?) info button in the label cell. The
    // popup body lives in locales (``webapp_attestation_target_help``)
    // and explains that the *attestation transaction itself* can land
    // anywhere in the 60-block sign window, while the target block is
    // the one whose hash gets confirmed. The button uses
    // ``data-info-key`` so a single delegated click handler reads the
    // key and calls ``tg.showAlert`` (or window.alert as fallback).
    const targetLabel = t("att_label_target_block", "Assigned block");
    const helpAria = t(
      "webapp_attestation_target_help_aria", "What does this mean?",
    );
    const labelHtml = `${escapeHtml(targetLabel)}<button type="button" class="info-icon" data-info-key="webapp_attestation_target_help" aria-label="${escapeHtml(helpAria)}">?</button>`;
    _pushHtml(labelHtml, `<code>${fmtBlock(att.target_block)}</code>`);
  }
  // New row: "До целевого блока N (~X мин)". Hidden once the target
  // has already been crossed (``current >= target``) — at that point
  // the user is inside the sign window and "Until window close" is the
  // actionable line; showing ``-43 blocks`` would be noise.
  if (att.target_block != null && att.current_block != null) {
    const blocksToTarget = att.target_block - att.current_block;
    if (blocksToTarget > 0) {
      const seconds = Math.max(0, Math.floor(blocksToTarget * APPROX_BLOCK_SEC));
      const blocksStr = tN("att_blocks", blocksToTarget, lang);
      const timeStr = seconds < 60
        ? tN("att_seconds", seconds, lang)
        : tN("att_minutes", Math.floor(seconds / 60), lang);
      _push(
        t("att_label_until_target", "Time to target"),
        `${escapeHtml(blocksStr)} <span class="muted">(~${escapeHtml(timeStr)})</span>`,
      );
    }
  }
  if (att.target_block != null && att.attestation_window_blocks != null) {
    const winClose = att.target_block + att.attestation_window_blocks;
    _push(
      t("att_label_sign_window", "Sign window"),
      `<code>${fmtBlock(att.target_block)} → ${fmtBlock(winClose)}</code>`,
    );
    if (att.current_block != null) {
      const blocksLeft = winClose - att.current_block;
      if (blocksLeft < 0) {
        _push(
          t("att_label_window_closed", "Window status"),
          escapeHtml(t("att_window_closed", "Window closed in this epoch")),
        );
      } else {
        const seconds = Math.max(0, Math.floor(blocksLeft * APPROX_BLOCK_SEC));
        const blocksStr = tN("att_blocks", blocksLeft, lang);
        const timeStr = seconds < 60
          ? tN("att_seconds", seconds, lang)
          : tN("att_minutes", Math.floor(seconds / 60), lang);
        _push(
          t("att_label_window_left", "Until window close"),
          `${escapeHtml(blocksStr)} <span class="muted">(~${escapeHtml(timeStr)})</span>`,
        );
      }
    }
  }
  if (timeline && timeline.next_epoch != null
      && timeline.blocks_left_in_epoch != null
      && timeline.blocks_left_in_epoch >= 0) {
    const minutes = Math.max(0, Math.floor((timeline.seconds_left_in_epoch || 0) / 60));
    const blocksStr = tN("att_blocks", timeline.blocks_left_in_epoch, lang);
    const minutesStr = tN("att_minutes", minutes, lang);
    _push(
      `${t("att_label_next_epoch", "Next epoch")} ${timeline.next_epoch}`,
      `${escapeHtml(blocksStr)} <span class="muted">(~${escapeHtml(minutesStr)})</span>`,
    );
  }

  if (rows.length === 0) return "";
  const body = rows
    .map((r) => {
      const labelCell = r.labelHtml ?? escapeHtml(r.label);
      return `
      <div class="att-grid-row">
        <span class="att-grid-label">${labelCell}</span>
        <span class="att-grid-value">${r.value}</span>
      </div>
    `;
    })
    .join("");
  return `<div class="att-details-grid">${body}</div>`;
}

/**
 * Wire up the ``(?)`` info icons that ``renderAttestationDetails``
 * embeds in the grid. Idempotent — call after every render that
 * touches ``$.statusBanner``. Uses ``Telegram.WebApp.showAlert`` when
 * available (matches the rest of the app's confirm-via-tg pattern) and
 * falls back to ``window.alert`` for the local dashboard mode.
 */
function bindInfoIconHandlers() {
  for (const el of viewEl.querySelectorAll("[data-info-key]")) {
    if (el.dataset.infoBound === "1") continue;
    el.dataset.infoBound = "1";
    el.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const key = el.dataset.infoKey;
      const message = t(key, key);
      if (tg && typeof tg.showAlert === "function") {
        try { tg.showAlert(message); return; } catch (_) { /* fallthrough */ }
      }
      window.alert(message);
    });
  }
}

function bannerWithGrid(kind, title, gridHtml) {
  // Variant of ``bannerWithTail`` for the waiting-state banner that
  // already folds the epoch tail into the grid (last row). No standalone
  // tail div — keeps the banner compact and doesn't repeat data.
  return `
    <div class="banner ${kind}">
      <div class="banner-body">
        <div class="banner-title">${title}</div>
        ${gridHtml}
      </div>
    </div>
  `;
}

function bannerWithTail(kind, title, sub, tail) {
  const tailHtml = tail
    ? `<div class="banner-sub muted small">${escapeHtml(tail)}</div>`
    : "";
  return `
    <div class="banner ${kind}">
      <div class="banner-body">
        <div class="banner-title">${title}</div>
        <div class="banner-sub">${sub}</div>
        ${tailHtml}
      </div>
    </div>
  `;
}

function renderValidatorStatusBanner(data) {
  // Picks the most actionable verdict + appends the shared epoch tail
  // ("next epoch in N blocks") under it. Used by both the validator
  // detail view (own data) and the delegator detail view (their staker's
  // data fetched via /api/v1/validators).
  if (!data) return "";
  const att = data.attestation;
  const timeline = data.epoch_timeline;
  const tail = renderEpochTail(timeline);
  const lang = currentLang();

  if (data.unstake_requested) {
    return bannerWithTail(
      "danger",
      t("webapp_status_exiting_t", "🚫 Validator is exiting"),
      t("webapp_status_exiting_sub",
        "An unstake has been requested. New rewards will stop and your delegation will be returned after the unbonding period."),
      tail,
    );
  }
  if (att && att.missed_epochs > 0) {
    const n = att.missed_epochs;
    // tN picks the right ``_one/_few/_many/_other`` template per locale,
    // so ru/ua/pl get the noun in the correct case ("аттестацию" /
    // "аттестации" / "аттестаций") instead of the legacy "(s)" hack.
    return bannerWithTail(
      "warn",
      tN("webapp_status_missed_t", n, lang, `⚠ Validator missed ${n} attestation${n === 1 ? "" : "s"}`),
      t("webapp_status_missed_sub",
        `Last confirmed in epoch ${att.last_epoch_attested}, current epoch ${att.current_epoch}. Skipped attestations reduce both the validator's and delegators' rewards.`,
        { last: att.last_epoch_attested, epoch: att.current_epoch, n }),
      tail,
    );
  }
  if (att && att.is_attesting_this_epoch) {
    return bannerWithTail(
      "success",
      t("webapp_status_healthy_t", "✓ Validator healthy"),
      t("webapp_status_healthy_sub",
        `Already attested for epoch ${att.current_epoch} — your rewards are accruing normally.`,
        { epoch: att.current_epoch }),
      tail,
    );
  }
  if (att) {
    // Waiting state — when we have block-level data, switch to the
    // grid layout (label/value rows) so the user can scan the six
    // numbers (current/assigned/window/left/next epoch) by column
    // instead of parsing them out of a ``·``-joined string.
    if (att.target_block != null && att.attestation_window_blocks != null
        && att.current_block != null) {
      const grid = renderAttestationDetails(att, timeline);
      return bannerWithGrid(
        "muted",
        t("webapp_status_waiting_t", "⏳ Waiting for this epoch's attestation"),
        grid,
      );
    }
    // No block-level extras — degrade to the legacy single-line sub
    // and append the epoch tail underneath via ``bannerWithTail``.
    const sub = t(
      "webapp_status_waiting_sub",
      `Validators must attest once per epoch. Epoch ${att.current_epoch} is still in progress — this is normal as long as it finishes before the epoch ends.`,
      { epoch: att.current_epoch }
    );
    return bannerWithTail(
      "muted",
      t("webapp_status_waiting_t", "⏳ Waiting for this epoch's attestation"),
      sub,
      tail,
    );
  }
  return bannerWithTail(
    "muted",
    t("webapp_status_unavailable_t", "Validator status unavailable"),
    t("webapp_status_unavailable_sub",
      "Couldn't reach the attestation contract just now. Reopen the Mini App in a few seconds."),
    "",  // no tail when we have no data at all
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
  // ``stateClass`` drives the card border/tint. Three buckets:
  //   below  → "op-wallet-warn" (orange)
  //   above  → "op-wallet-ok"   (subtle green; symmetric reassurance)
  //   unset/no-data → no class  (neutral card)
  let stateClass = "";
  if (balNum !== null && threshold > 0) {
    const thrStr = fmtAmount(threshold, "STRK");
    if (balNum < threshold) {
      badge = `<span class="chip danger">${escapeHtml(t("webapp_below_threshold", `⚠ Below ${thrStr}`, { amount: thrStr }))}</span>`;
      stateClass = "op-wallet-warn";
    } else {
      badge = `<span class="chip success">${escapeHtml(t("webapp_above_threshold", `✓ Above ${thrStr}`, { amount: thrStr }))}</span>`;
      stateClass = "op-wallet-ok";
    }
  }

  const balLine = balNum !== null
    ? `${fmtAmount(balNum, "STRK")}${usdBal !== null ? ` · ${fmtUsd(usdBal)}` : ""}`
    : t("webapp_balance_unavailable", "Balance unavailable");

  return `
    <div class="hero op-wallet ${stateClass}">
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
  setTopbar(
    t("webapp_topbar_settings", "Settings"),
    t("webapp_topbar_settings_sub", "Notifications"),
  );
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
  // notification mode. 0 means "off". Gated on having at least one
  // validator enrolled in attestation alerts: the alert task only fires
  // for those, so allowing the input here would silently produce no
  // effect ("I set 100 STRK and never got pinged" surprise).
  const opBalanceInput = document.getElementById("op-balance-input");
  const opMin = Number(cfg.operator_balance_min_strk || 0);
  const attestationSubs = Array.isArray(cfg.attestation_alerts_for)
    ? cfg.attestation_alerts_for
    : [];
  if (opBalanceInput) {
    opBalanceInput.value = opMin > 0 ? opMin : "";
    if (attestationSubs.length === 0) {
      opBalanceInput.disabled = true;
      opBalanceInput.placeholder = "—";
      // Drop a hint right under the section header so the user knows why
      // the input is greyed out instead of just being broken.
      const section = opBalanceInput.closest(".form-card");
      if (section && !section.querySelector(".op-balance-locked-hint")) {
        const hint = document.createElement("p");
        hint.className = "muted small op-balance-locked-hint";
        hint.style.color = "var(--orange)";
        hint.textContent = t(
          "webapp_op_balance_needs_attestation",
          "Enable attestation alerts for at least one validator first — the operator-wallet alert is part of the same per-validator subscription set.",
        );
        section.appendChild(hint);
      }
    }
  }

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
        // Re-translate the static header (Settings button, topbar title)
        // immediately. ``renderSettings`` below only re-applies i18n inside
        // the freshly-cloned template; without this call the header keeps
        // the previous locale's text until the user closes and reopens
        // the Mini App.
        applyI18n(document);
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
// Add validator / delegator view
// ---------------------------------------------------------------------------

// Map every error code the API can return to a localized message. The
// codes ride inside the JSON detail (``{code, message}``) so we don't
// have to parse the free-form ``message`` string. Falls back to
// ``webapp_add_error_unknown`` when the backend invents a new code.
function _addErrorKeyForCode(role, code) {
  if (code === "invalid_address") return "webapp_add_error_invalid_address";
  if (code === "duplicate") {
    return role === "validator"
      ? "webapp_add_error_duplicate_validator"
      : "webapp_add_error_duplicate_delegator";
  }
  if (code === "limit_reached") return "webapp_add_error_limit_reached";
  if (code === "not_a_staker") return "webapp_add_error_not_a_staker";
  if (code === "not_a_delegator") return "webapp_add_error_not_a_delegator";
  return "webapp_add_error_unknown";
}

// The API returns errors as ``HTTPException(detail={code, message})``,
// which our ``api()`` helper has already serialized into ``err.message``
// (the textual response body). Pull the JSON back out so we can map by
// code; if parsing fails we still get a sensible ``unknown`` fallback.
function _parseAddError(err) {
  try {
    const body = JSON.parse(err.message.replace(/^\d+\s*/, ""));
    if (body && typeof body === "object" && body.detail) {
      if (typeof body.detail === "string") return { code: "unknown", message: body.detail };
      if (typeof body.detail === "object") return body.detail;
    }
  } catch (_) {
    // fall through
  }
  return { code: "unknown", message: String(err.message || "") };
}

async function renderAdd() {
  setTopbar(
    t("webapp_topbar_add", "Add"),
    t("webapp_topbar_add_sub", "New tracking entry"),
  );
  renderTemplate("tpl-add");
  const $ = bindings();

  let role = "validator";

  const segment = document.getElementById("add-role-segment");
  const addressInput = document.getElementById("address-input");
  const delegatorInput = document.getElementById("delegator-input");
  const labelInput = document.getElementById("label-input");
  const errorEl = document.getElementById("add-error");
  const statusEl = document.getElementById("add-status");
  const submitBtn = document.getElementById("add-submit");

  function applyRole(next) {
    role = next;
    for (const opt of segment.querySelectorAll(".seg-option")) {
      opt.setAttribute("aria-selected", String(opt.dataset.role === role));
    }
    // Hide the error every time the user toggles role — the previous
    // message would refer to the wrong field after a switch.
    errorEl.hidden = true;
    errorEl.textContent = "";

    if (role === "validator") {
      $.delegatorRow.hidden = true;
      $.addressLabel.textContent = t("webapp_add_validator_label", "Validator (staker) address");
      $.roleHint.textContent = t(
        "webapp_add_role_hint_validator",
        "Track a staker. We'll pull pool info, attestations, and rewards automatically.",
      );
    } else {
      $.delegatorRow.hidden = false;
      $.addressLabel.textContent = t("webapp_add_staker_label", "Validator (staker) address");
      $.roleHint.textContent = t(
        "webapp_add_role_hint_delegator",
        "Track your delegation in someone else's pool. Both addresses are required — we'll find the matching pools.",
      );
    }
  }

  applyRole("validator");
  for (const opt of segment.querySelectorAll(".seg-option")) {
    opt.addEventListener("click", () => applyRole(opt.dataset.role));
  }

  function showError(messageKey, extraMessage) {
    // ``extraMessage`` lets us surface the backend's free-form detail for
    // the ``unknown`` bucket — the localized key carries the user-facing
    // sentence, the backend message lives in a smaller code-style line.
    errorEl.hidden = false;
    let html = escapeHtml(t(messageKey, ""));
    if (!html) html = escapeHtml(extraMessage || messageKey);
    if (extraMessage && messageKey === "webapp_add_error_unknown") {
      html += `<br><span class="addr-mono small">${escapeHtml(extraMessage)}</span>`;
    }
    errorEl.innerHTML = html;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("error");
  }

  submitBtn.addEventListener("click", async () => {
    errorEl.hidden = true;
    errorEl.textContent = "";
    statusEl.textContent = t("webapp_add_submitting", "Submitting…");
    submitBtn.disabled = true;

    const address = (addressInput.value || "").trim();
    const delegator = (delegatorInput.value || "").trim();
    const label = (labelInput.value || "").trim();

    // Cheap client-side guard — the server validates the same things,
    // but failing fast saves a round-trip on obvious typos.
    if (!address) {
      showError("webapp_add_error_invalid_address");
      statusEl.textContent = "";
      submitBtn.disabled = false;
      return;
    }
    if (role === "delegator" && !delegator) {
      showError("webapp_add_error_invalid_address");
      statusEl.textContent = "";
      submitBtn.disabled = false;
      return;
    }

    try {
      if (role === "validator") {
        await api("/api/v1/users/me/tracking/validators", {
          method: "POST",
          body: { address, label },
        });
      } else {
        await api("/api/v1/users/me/tracking/delegations", {
          method: "POST",
          body: { delegator, staker: address, label },
        });
      }
      // Invalidate cached entries so the dashboard refetches the fresh
      // doc. ``state.notification`` doesn't change here; leave it cached.
      state.entries = null;
      statusEl.textContent = t("webapp_add_success", "Added.");
      toast(t("webapp_add_success", "Added."));
      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      navigate("#/");
    } catch (err) {
      const detail = _parseAddError(err);
      const key = _addErrorKeyForCode(role, detail.code);
      showError(key, detail.message);
      statusEl.textContent = "";
      submitBtn.disabled = false;
    }
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

renderRoute().catch((err) => toast(err.message));
