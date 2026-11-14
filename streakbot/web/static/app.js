"use strict";

// Streak Panel page script. The panel is split into sections that are re-rendered independently,
// and a section's DOM is only replaced when its HTML actually changed. Text fields are built once per
// selected bot and only have their values synced while they're not focused, so live updates never
// wipe what the owner is typing or swallow a click.

const $ = (sel, el = document) => el.querySelector(sel);

// Apple devices already show Apple's fire emoji; everywhere else use the bundled Noto fire (see style.css).
const isApple = /Mac|iPhone|iPad|iPod/.test(navigator.userAgentData?.platform || navigator.platform || "");
if (!isApple) document.documentElement.classList.add("noto-fire");
const state = {
  bots: [],
  selected: null,      // bot id
  chats: {},           // bot id -> [{username, display_name, avatar_url}] | "loading" | {error}
  cookieBotId: null,   // null: the dialog adds a bot; a number: it updates that bot's login
  events: null,
};

// --- helpers ---------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status, data = {}) { super(message); this.status = status; this.data = data; }
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-Panel"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new ApiError("Can't reach the panel. Check that the server is running.", 0);
  }
  if (r.status === 401 && path !== "/api/login") {
    showLogin();
    throw new ApiError("Your session ended. Log in again.", 401);
  }
  if (r.status === 202 || r.status === 204) return null;
  let data = {};
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new ApiError(data.error || `Request failed (${r.status}).`, r.status, data);
  return data;
}

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const dot = (level) => `<span class="dot ${esc(level)}" aria-hidden="true"></span>`;
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const toRecipient = ({ username, display_name }) => ({ username, display_name });

/** Replace an element's content only if it changed, so unchanged parts keep focus, scroll and clicks. */
function setHTML(el, html) {
  if (el && el._html !== html) {
    el.innerHTML = html;
    el._html = html;
  }
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

/** Run an action; show its error as a toast instead of throwing. Returns true if it succeeded. */
async function attempt(fn) {
  try {
    await fn();
    return true;
  } catch (e) {
    if (e.status !== 401) toast(e.message, true);
    return false;
  }
}

/** Disable a button and show `busyText` on it while `fn` runs. */
async function withBusy(button, busyText, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = busyText;
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function duration(ms) {
  const min = Math.round(Math.abs(ms) / 60000);
  if (min < 1) return "less than a minute";
  if (min < 60) return plural(min, "minute");
  const h = Math.floor(min / 60), m = min % 60;
  if (h < 24) return m ? `${plural(h, "hour")} ${plural(m, "minute")}` : plural(h, "hour");
  return plural(Math.round(h / 24), "day");
}

const dayFormatters = new Map();
function dayIn(date, tz) {
  if (!dayFormatters.has(tz)) {
    dayFormatters.set(tz, new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }));
  }
  return dayFormatters.get(tz).format(date);
}

function describeNext(bot) {
  if (bot.paused) return "Nothing goes out until you resume.";
  if (!bot.send_time || !bot.next_send_at) return "Not scheduled. Pick a send time below.";
  const when = new Date(bot.next_send_at);
  const now = new Date();
  if (when <= now) return "Due now. It goes out within a few minutes.";
  const today = dayIn(now, bot.timezone);
  const tomorrow = dayIn(new Date(now.getTime() + 86400000), bot.timezone);
  const day = dayIn(when, bot.timezone);
  const label = day === today ? "today" : day === tomorrow ? "tomorrow" : day;
  return `Next send ${label} at ${bot.send_time} (${bot.timezone.replace(/_/g, " ")}), in ${duration(when - now)}.`;
}

function describeLast(bot) {
  if (!bot.last_run_at) return "No sends yet.";
  return `Last send ${duration(new Date() - new Date(bot.last_run_at))} ago: ${bot.last_run_summary || "done"}.`;
}

function describeLogin(bot) {
  if (!bot.session_expires_at) return "";
  const d = new Date(bot.session_expires_at);
  return `TikTok login expires ${d.toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" })}.`;
}

const current = () => state.bots.find((b) => b.id === state.selected) || null;
const isNarrow = () => window.matchMedia("(max-width: 760px)").matches;

// --- timezones -------------------------------------------------------------------------

function utcOffset(zone) {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longOffset" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName");
    const off = part ? part.value.replace("GMT", "") : "";
    return off ? `UTC${off}` : "UTC+00:00";
  } catch {
    return "";
  }
}

const zoneEntries = new Map();  // zone -> {value, search, city, words, label}
function zoneEntry(zone) {
  if (!zoneEntries.has(zone)) {
    const name = zone.replace(/_/g, " ");
    const search = name.toLowerCase();
    let label = null;
    zoneEntries.set(zone, {
      value: zone, search, city: search.split("/").pop(), words: search.split(/[/ ]/),
      get label() { return (label ??= `${name} (${utcOffset(zone)})`); },  // computed only when shown
    });
  }
  return zoneEntries.get(zone);
}
const zoneLabel = (zone) => (zone ? zoneEntry(zone).label : "");

let allZones = null;
function timezones() {
  if (!allZones) {
    const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [];
    if (!zones.includes("UTC")) zones.push("UTC");
    allZones = zones.sort().map(zoneEntry);
  }
  return allZones;
}

// "los" → Los Angeles first, then names with a word starting "los", then any name containing it.
function matchZones(query) {
  const q = query.trim().toLowerCase().replace(/_/g, " ");
  if (!q) return timezones();
  const buckets = [[], [], []];
  for (const z of timezones()) {
    if (z.city.startsWith(q)) buckets[0].push(z);
    else if (z.words.some((w) => w.startsWith(q))) buckets[1].push(z);
    else if (z.search.includes(q)) buckets[2].push(z);
  }
  return buckets.flat();  // each bucket keeps the alphabetical order of timezones()
}

const MAX_ZONE_RESULTS = 80;
const combo = { matches: [], active: -1 };

function showZones(input) {
  const list = $("#tz-list");
  const typed = input.value === zoneLabel(input.dataset.value) ? "" : input.value;
  combo.matches = matchZones(typed).slice(0, MAX_ZONE_RESULTS);
  const saved = combo.matches.findIndex((z) => z.value === input.dataset.value);
  combo.active = combo.matches.length ? (typed || saved < 0 ? 0 : saved) : -1;
  list.innerHTML = combo.matches.length
    ? combo.matches.map((z, i) => `<li role="option" id="tz-opt-${i}" data-zone="${esc(z.value)}">${esc(z.label)}</li>`).join("")
    : `<li class="none">No timezone matches “${esc(typed.trim())}”.</li>`;
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
  moveZone(input, 0);
}

function moveZone(input, step) {
  if (!combo.matches.length) return;
  combo.active = Math.max(0, Math.min(combo.matches.length - 1, combo.active + step));
  document.querySelectorAll("#tz-list [role=option]").forEach((li, i) => {
    li.setAttribute("aria-selected", String(i === combo.active));
    if (i === combo.active) li.scrollIntoView({ block: "nearest" });
  });
  input.setAttribute("aria-activedescendant", `tz-opt-${combo.active}`);
}

function hideZones(input) {
  $("#tz-list").hidden = true;
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
}

function chooseZone(input, zone) {
  hideZones(input);
  input.value = zoneLabel(zone);
  const changed = zone !== input.dataset.value;
  input.dataset.value = zone;
  input.blur();
  if (changed) patchBot({ timezone: zone }, "tz");
}

// --- views ----------------------------------------------------------------------------

function showLogin() {
  if (state.events) { state.events.close(); state.events = null; }
  $("#main-view").hidden = true;
  $("#login-view").hidden = false;
  $("#login-password").focus();
}

async function showMain() {
  $("#login-view").hidden = true;
  $("#main-view").hidden = false;
  connectEvents();
  await load();
}

function setLive(on) {
  const el = $("#live");
  el.classList.toggle("on", on);
  el.textContent = on ? "Live" : "Reconnecting…";
}

function connectEvents() {
  if (state.events) state.events.close();
  const es = new EventSource("/api/events");
  state.events = es;
  es.addEventListener("changed", () => load());
  es.onopen = () => { setLive(true); load(); };  // also catches up on anything missed while disconnected
  es.onerror = () => {
    setLive(false);
    if (es.readyState === EventSource.CLOSED) {
      // The server refused the stream (logged out or restarting): check, then retry.
      setTimeout(async () => {
        try { await api("GET", "/api/session"); connectEvents(); } catch { /* login shown or still down */ }
      }, 3000);
    }
  };
}

// Concurrent load() calls share one fetch; a call made while one is in flight triggers one more after it.
let loading = null, reloadQueued = false;
async function load() {
  if (loading) { reloadQueued = true; return loading; }
  loading = (async () => {
    do {
      reloadQueued = false;
      try {
        state.bots = (await api("GET", "/api/bots")).bots;
      } catch (e) {
        if (e.status !== 401) setLive(false);
        return;
      }
    } while (reloadQueued);
    if (state.selected !== null && !current()) state.selected = null;
    if (state.selected === null && state.bots.length && !isNarrow()) state.selected = state.bots[0].id;
    render();
  })();
  try { await loading; } finally { loading = null; }
}

function render() {
  renderBoard();
  renderPanel();
}

function renderBoard() {
  let healthy = 0, attention = 0;
  for (const b of state.bots) {
    if (b.status.level === "ok" || b.status.level === "busy") healthy++;
    else if (b.status.level !== "off") attention++;
  }
  const n = state.bots.length;
  setHTML($("#board-summary"), n
    ? `${healthy} of ${n} healthy` + (attention ? `, ${attention} ${attention === 1 ? "needs" : "need"} attention` : "")
    : "");
  setHTML($("#bot-list"), state.bots.map((b) => `
    <li><button data-id="${b.id}" aria-current="${b.id === state.selected}" title="${esc(b.status.label)}">
      ${dot(b.status.level)}
      <span class="bot-name">${esc(b.username)}</span>
      <span class="bot-time ${b.send_time ? "" : "unset"}">${b.paused ? "Paused" : esc(b.send_time || "No time")}</span>
    </button></li>`).join(""));
  $("#empty-list").hidden = n > 0;
  $(".layout").classList.toggle("show-panel", state.selected !== null);
}

/** Build the panel's skeleton for a newly selected bot: static parts and the editable fields. */
function buildPanel(bot) {
  const panel = $("#panel");
  panel.dataset.bot = String(bot.id);
  panel._html = undefined;
  panel.innerHTML = `
    <button class="btn ghost sm back" data-action="back">← All bots</button>
    <div class="page-head">
      <div id="sec-title"></div>
      <div id="sec-actions" class="actions"></div>
    </div>
    <div id="sec-alert" class="alert" role="alert"></div>

    <section class="card">
      <div class="card-title"><h3>Message and time</h3></div>
      <div class="fields">
        <div class="wide">
          <label for="f-message">Message <span class="saved" data-saved="message">Saved</span></label>
          <input id="f-message" maxlength="200" autocomplete="off">
        </div>
        <div>
          <label for="f-time">Send time <span class="saved" data-saved="time">Saved</span></label>
          <input id="f-time" type="time">
        </div>
        <div>
          <label for="f-tz">Timezone <span class="saved" data-saved="tz">Saved</span></label>
          <div class="combo">
            <input id="f-tz" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="tz-list"
              autocomplete="off" spellcheck="false" placeholder="Type a city, e.g. Los Angeles">
            <ul id="tz-list" role="listbox" aria-label="Timezones" hidden></ul>
          </div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-title"><h3>Recipients <span id="sec-count"></span></h3></div>
      <div id="sec-recipients"></div>
      <form class="add-row" data-form="add-recipient">
        <input id="f-add" placeholder="Add by @username or profile link" autocomplete="off" spellcheck="false" aria-label="Add by username">
        <button class="btn" type="submit">Add</button>
      </form>
      <div id="sec-chats"></div>
    </section>

    <section class="card">
      <div class="card-title"><h3>Login</h3></div>
      <div id="sec-login" class="login-row"></div>
    </section>`;
}

/** Copy the bot's values into the editable fields, except the one being edited. */
function syncFields(bot) {
  const idle = (el) => el && document.activeElement !== el;
  const message = $("#f-message"), time = $("#f-time"), tz = $("#f-tz");
  if (idle(message) && message.value !== bot.message) message.value = bot.message;
  if (idle(time) && time.value !== (bot.send_time || "")) time.value = bot.send_time || "";
  if (idle(tz)) {
    tz.dataset.value = bot.timezone || "";
    tz.value = zoneLabel(bot.timezone);
  }
}

function renderPanel() {
  const panel = $("#panel");
  const bot = current();
  if (!bot) {
    delete panel.dataset.bot;
    setHTML(panel, `<p class="panel-empty">${state.bots.length ? "Pick a bot on the left to see and change its settings." : ""}</p>`);
    return;
  }
  if (panel.dataset.bot !== String(bot.id)) buildPanel(bot);
  syncFields(bot);

  const lvl = bot.status.level;
  const login = describeLogin(bot);
  setHTML($("#sec-title"), `
    <h2>${esc(bot.username)}</h2>
    <div class="facts">
      <p><span class="status-line ${esc(lvl)}">${dot(lvl)}${esc(bot.status.label)}</span><span class="sep">·</span>${esc(describeNext(bot))}</p>
      <p>${esc(describeLast(bot))}${login ? `<span class="sep">·</span>${esc(login)}` : ""}</p>
    </div>`);

  setHTML($("#sec-actions"), `
    <button class="btn" data-action="test" ${bot.running ? "disabled" : ""}>Test send</button>
    <button class="btn" data-action="pause">${bot.paused ? "Resume" : "Pause"}</button>
    <button class="btn primary" data-action="send-now" ${bot.running ? "disabled" : ""}
      title="Send the daily message to everyone now. The scheduled send still happens.">Send now</button>`);

  setHTML($("#sec-alert"), bot.status.label === "Login expired" ? `
    <p>TikTok logged this bot out, so its messages can't go out. Paste a fresh <code>sessionid</code> to fix it.</p>
    <button class="btn primary sm" data-action="update-login">Update login</button>` : "");

  setHTML($("#sec-count"), bot.recipients.length ? `(${bot.recipients.length})` : "");
  setHTML($("#sec-recipients"), bot.recipients.length
    ? `<ul class="recipients">${bot.recipients.map((r) => `
        <li><span class="avatar" aria-hidden="true">${esc((r.display_name || r.username).trim().charAt(0).toUpperCase())}</span>
          <span class="who">${esc(r.display_name)} <span class="handle">@${esc(r.username)}</span></span>
          <button class="icon-btn" data-action="remove-recipient" data-username="${esc(r.username)}" aria-label="Remove @${esc(r.username)}">${icon("x")}</button></li>`).join("")}</ul>`
    : `<p class="empty-note">Nobody yet. Pick people from your recent chats, or add them by username.</p>`);

  setHTML($("#sec-chats"), renderChats(bot));

  setHTML($("#sec-login"), `
    <p>Sends as @${esc(bot.username)}${bot.region ? `, region ${esc(bot.region)}` : ""}.</p>
    <div>
      <button class="btn" data-action="update-login">Update login</button>
      <button class="btn danger" data-action="delete">Delete bot</button>
    </div>`);
}

function renderChats(bot) {
  const chats = state.chats[bot.id];
  const loaded = Array.isArray(chats);
  const toggle = `<button class="btn ghost sm chats-toggle" data-action="load-chats" ${chats === "loading" ? "disabled" : ""}>
    ${chats === "loading" ? "Opening TikTok…" : loaded ? "Refresh recent chats" : "Show recent chats"}</button>`;
  if (!chats) return toggle;
  if (chats === "loading") return `${toggle}<p class="hint">Reading recent chats from TikTok. This can take up to a minute.</p>`;
  if (chats.error) return `${toggle}<p class="form-error">${esc(chats.error)}</p>`;
  if (!chats.length) return `${toggle}<p class="hint">No recent chats found. Add people by username instead.</p>`;
  const saved = new Set(bot.recipients.map((r) => r.username));
  return `${toggle}<ul class="chats">${chats.map((c) => `
    <li><label>
      <input type="checkbox" data-chat="${esc(c.username)}" ${saved.has(c.username) ? "checked" : ""}>
      ${c.avatar_url ? `<img src="${esc(c.avatar_url)}" alt="" referrerpolicy="no-referrer" loading="lazy">` : ""}
      <span>${esc(c.display_name)} <span class="handle">@${esc(c.username)}</span></span>
    </label></li>`).join("")}</ul>`;
}

function flashSaved(key) {
  const el = $(`[data-saved="${key}"]`);
  if (!el) return;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1500);
}

// --- actions ---------------------------------------------------------------------------

function applyUpdate(bot, updated) {
  Object.assign(bot, updated);
  render();
}

async function patchBot(fields, savedKey) {
  const bot = current();
  if (!bot) return;
  const ok = await attempt(async () => applyUpdate(bot, await api("PATCH", `/api/bots/${bot.id}`, fields)));
  if (ok) flashSaved(savedKey);
  else syncFields(bot);  // put the saved value back
}

async function saveRecipients(bot, recipients) {
  await attempt(async () => applyUpdate(bot, await api("PUT", `/api/bots/${bot.id}/recipients`, { recipients })));
}

async function loadChats(bot) {
  state.chats[bot.id] = "loading";
  renderPanel();
  try {
    state.chats[bot.id] = (await api("GET", `/api/bots/${bot.id}/chats`)).chats;
  } catch (e) {
    state.chats[bot.id] = { error: e.message };
  }
  renderPanel();
}

async function addRecipient(bot, input, button) {
  const raw = input.value.trim();
  if (!raw) return;
  await withBusy(button, "Checking…", () => attempt(async () => {
    const { profile } = await api("GET", `/api/bots/${bot.id}/lookup?username=${encodeURIComponent(raw)}`);
    if (bot.recipients.some((r) => r.username === profile.username)) {
      toast(`@${profile.username} is already on the list.`);
      return;
    }
    input.value = "";
    await saveRecipients(bot, [...bot.recipients.map(toRecipient), toRecipient(profile)]);
    toast(`Added ${profile.display_name} (@${profile.username}).`);
  }));
}

function openCookieDialog(botId = null) {
  state.cookieBotId = botId;
  const bot = botId === null ? null : state.bots.find((b) => b.id === botId);
  $("#cookie-title").textContent = bot ? `Update login for @${bot.username}` : "Add bot";
  $("#cookie-hint").innerHTML = bot
    ? `Paste a fresh <code>sessionid</code> cookie from @${esc(bot.username)}.`
    : "Paste the <code>sessionid</code> cookie of the TikTok account this bot should send from.";
  $("#cookie-submit").textContent = bot ? "Update login" : "Log in";
  $("#cookie-sessionid").value = "";
  $("#cookie-region").value = "";
  $("#region-row").hidden = true;
  $("#cookie-error").textContent = "";
  $("#login-dialog").showModal();
}

async function submitCookie() {
  const botId = state.cookieBotId;
  const body = { sessionid: $("#cookie-sessionid").value };
  const region = $("#cookie-region").value.trim();
  if (region) body.region = region;
  $("#cookie-error").textContent = "";
  await withBusy($("#cookie-submit"), "Checking with TikTok…", async () => {
    try {
      const result = await api("POST", botId === null ? "/api/bots" : `/api/bots/${botId}/login`, body);
      $("#cookie-sessionid").value = "";
      $("#login-dialog").close();
      state.selected = result.bot.id;
      toast(result.outcome === "added" ? `Added @${result.bot.username}.` : `Login updated for @${result.bot.username}.`);
      await load();
    } catch (e) {
      if (e.data && e.data.needs_region) {
        $("#region-row").hidden = false;
        $("#cookie-region").focus();
      }
      $("#cookie-error").textContent = e.message;
    }
  });
}

function openTestDialog(bot) {
  $("#test-usernames").replaceChildren(...bot.recipients.map((r) => {
    const o = document.createElement("option");
    o.value = r.username;
    o.label = r.display_name;
    return o;
  }));
  $("#test-username").value = bot.recipients[0] ? bot.recipients[0].username : "";
  $("#test-message").value = "test";
  $("#test-error").textContent = "";
  $("#test-ok").textContent = "";
  $("#test-dialog").showModal();
}

async function submitTest() {
  const bot = current();
  if (!bot) return;
  $("#test-error").textContent = "";
  $("#test-ok").textContent = "";
  await withBusy($("#test-submit"), "Sending…", async () => {
    try {
      const result = await api("POST", `/api/bots/${bot.id}/test`, {
        username: $("#test-username").value,
        message: $("#test-message").value,
      });
      if (result.ok) $("#test-ok").textContent = `It works. ${result.summary}.`;
      else $("#test-error").textContent = result.summary;
    } catch (e) {
      $("#test-error").textContent = e.message;
    }
  });
}

async function onAction(action, el) {
  if (action === "add-bot") { openCookieDialog(); return; }
  if (action === "back") { state.selected = null; render(); return; }
  const bot = current();
  if (!bot) return;
  switch (action) {
    case "test": openTestDialog(bot); break;
    case "send-now":
      if (await attempt(() => api("POST", `/api/bots/${bot.id}/send-now`))) toast(`Sending now from @${bot.username}.`);
      break;
    case "pause": await patchBot({ paused: !bot.paused }, null); break;
    case "load-chats": await loadChats(bot); break;
    case "remove-recipient":
      await saveRecipients(bot, bot.recipients.filter((r) => r.username !== el.dataset.username).map(toRecipient));
      break;
    case "update-login": openCookieDialog(bot.id); break;
    case "delete":
      if (!confirm(`Delete @${bot.username}? Its saved login and recipients are removed from the panel.`)) return;
      if (await attempt(() => api("DELETE", `/api/bots/${bot.id}`))) {
        state.selected = null;
        toast(`Deleted @${bot.username}.`);
        await load();
      }
      break;
  }
}

// --- wiring -------------------------------------------------------------------------------

/** Submit handler for a dialog form: the Cancel/Close button just closes it, anything else runs `fn`. */
const dialogSubmit = (fn, onCancel = () => {}) => (e) => {
  if (e.submitter && e.submitter.value === "cancel") { onCancel(); return; }
  e.preventDefault();
  fn();
};

function wire() {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("#login-error").textContent = "";
    try {
      await api("POST", "/api/login", { password: $("#login-password").value });
      $("#login-password").value = "";
      await showMain();
    } catch (err) {
      $("#login-error").textContent = err.message;
    }
  });

  $("#logout").addEventListener("click", async () => {
    try { await api("POST", "/api/logout"); } catch { /* ignore */ }
    showLogin();
  });

  document.addEventListener("click", (e) => {
    const zoneItem = e.target.closest("#tz-list [data-zone]");
    if (zoneItem) { chooseZone($("#f-tz"), zoneItem.dataset.zone); return; }
    const botButton = e.target.closest("#bot-list button[data-id]");
    if (botButton) {
      state.selected = Number(botButton.dataset.id);
      render();
      if (isNarrow()) window.scrollTo(0, 0);
      return;
    }
    const el = e.target.closest("[data-action]");
    if (el) onAction(el.dataset.action, el);
  });

  const panel = $("#panel");
  panel.addEventListener("submit", (e) => {
    const form = e.target.closest('[data-form="add-recipient"]');
    if (!form) return;
    e.preventDefault();
    const bot = current();
    if (bot) addRecipient(bot, $("#f-add", form), $("button", form));
  });
  panel.addEventListener("change", (e) => {
    const bot = current();
    if (!bot) return;
    const t = e.target;
    if (t.id === "f-message") patchBot({ message: t.value }, "message");
    else if (t.id === "f-time" && t.value) patchBot({ send_time: t.value }, "time");
    else if (t.dataset.chat) {
      const chat = (state.chats[bot.id] || []).find((c) => c.username === t.dataset.chat);
      const next = t.checked
        ? [...bot.recipients.map(toRecipient), toRecipient(chat)]
        : bot.recipients.filter((r) => r.username !== t.dataset.chat).map(toRecipient);
      saveRecipients(bot, next);
    }
  });
  panel.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.id === "f-message") e.target.blur();
    if (e.target.id !== "f-tz") return;
    const input = e.target;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if ($("#tz-list").hidden) showZones(input);
      else moveZone(input, e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const zone = combo.matches[combo.active];
      if (!$("#tz-list").hidden && zone) chooseZone(input, zone.value);
    } else if (e.key === "Escape") {
      input.value = zoneLabel(input.dataset.value);
      hideZones(input);
    }
  });
  panel.addEventListener("input", (e) => { if (e.target.id === "f-tz") showZones(e.target); });
  panel.addEventListener("focusin", (e) => {
    if (e.target.id === "f-tz") { e.target.select(); showZones(e.target); }
  });
  panel.addEventListener("mousedown", (e) => {
    if (e.target.closest("#tz-list")) e.preventDefault();  // keep focus in the box while choosing
  });
  panel.addEventListener("focusout", (e) => {
    if (e.target.id === "f-tz") {
      hideZones(e.target);
      e.target.value = zoneLabel(e.target.dataset.value);  // typed but didn't choose: put the saved one back
    } else if (e.target.matches("#f-message, #f-time")) {
      setTimeout(() => { const bot = current(); if (bot) syncFields(bot); }, 0);  // catch up on live changes
    }
  });

  $("#cookie-form").addEventListener("submit", dialogSubmit(submitCookie, () => { $("#cookie-sessionid").value = ""; }));
  $("#test-form").addEventListener("submit", dialogSubmit(submitTest));

  // Keep "in 3 hours" style text fresh; unchanged sections aren't touched.
  setInterval(() => { if (!$("#main-view").hidden) render(); }, 30000);
}

async function boot() {
  wire();
  try {
    await api("GET", "/api/session");
    await showMain();
  } catch (e) {
    if (e.status !== 401) {
      $("#login-error").textContent = e.message;
      showLogin();
    }
  }
}

boot();
