/* Talking to an agent, with sources.
 *
 * The poll here is load-bearing in a way no other poll in this app is: it doubles as the
 * heartbeat that keeps a parked approval alive. An owner the server has not heard from within
 * its grace window has its pending asks denied — which is what makes closing this tab
 * mid-approval safe instead of leaving a run and its container wedged for the full five-minute
 * timeout. Three consequences shape this file:
 *
 * - The poll keeps running while it matters (a turn running, a send in flight, an approval
 *   pending) even when the tab is hidden or the user has gone to another dashboard page: the
 *   chat lives on the server either way, and stopping the heartbeat there used to deny the
 *   user's own approval and call it "User denied". It stops when the tab is closed, which is
 *   the abandonment the reaper exists for.
 * - Its intervals are derived from the grace window the server reports rather than hardcoded.
 * - Who settled an ask comes from the server's record, never from the transcript's wording.
 *
 * Everything the poll renders is rendered only when it changed: transcript rows are appended,
 * approval cards are keyed by ask id, so a button press, a focus or a text selection survives
 * the next tick.
 *
 * Sources — uploaded files and fetched URLs — become files in the agent's workspace, and the
 * next message names them. The server does that part; this view's job is to make it visible
 * which sources the agent has been told about and which are still queued. */

"use strict";

var CHAT_POLL_MS = 900;
/* While on another dashboard page, a chat needs only its heartbeat, not a 0.9 s transcript. */
var CHAT_BACKGROUND_MS = 5000;
/* A hidden tab's timers are throttled to at least 1 s, and to once a minute after five hidden
 * minutes in Chrome, so asking for less buys nothing. The server's grace window (90 s by
 * default) is sized to outlast that minute. */
var CHAT_HIDDEN_MS = 15000;
/* Backoff while the dashboard cannot be reached: doubling from the poll interval, capped. */
var CHAT_BACKOFF_MAX_MS = 30000;
var CHAT_MAX_ROWS = 400;
/* Must stay under the server's own decoded ceiling (grounding.MAX_SOURCE_BYTES, 8 MiB) so an
 * oversized file is refused here, with a sentence, instead of after a 12 MiB upload. */
var MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

var CHAT_ID_KEY = "garuda_chat_id";
var CHAT_DRAFT_KEY = "garuda_chat_draft:";
/* Memory fallback for when sessionStorage is unavailable (private mode, blocked storage). */
var CHAT_MEMORY = {};
/* Every chat this tab has open, by id, each with its own poller (`chat.poller`, never
 * STATE.poller: that slot belongs to whichever view is mounted, and a chat's heartbeat may
 * outlive its view). STATE.chat is the one on screen, or the one to resume. */
var CHAT_OPEN = {};

function chatStore(key, value) {
  try {
    if (value === undefined) {
      var stored = sessionStorage.getItem(key);
      return stored !== null ? stored : (CHAT_MEMORY[key] || null);
    }
    if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value);
  } catch (e) { /* storage blocked: the memory copy below still holds for this page */ }
  if (value === undefined) return CHAT_MEMORY[key] || null;
  if (value === null) delete CHAT_MEMORY[key]; else CHAT_MEMORY[key] = value;
  return value;
}

/* --- opening a chat ------------------------------------------------------- */

function chatView() {
  if (!canRun()) {
    render(
      '<div class="page-head"><h1>Chat</h1></div>' +
      '<div class="empty"><p><strong>This dashboard was started read-only.</strong></p>' +
      "<p>Relaunch <code>garuda web</code> without <code>--read-only</code>. It runs tools " +
      "as you, and asks before anything destructive unless you raise " +
      "<code>--max-permission</code>.</p></div>"
    );
    return Promise.resolve();
  }
  // A chat still being watched from another page is resumed as it is, rows and all.
  if (STATE.chat && STATE.chat.poller) return openChat(STATE.chat.id, STATE.chat.info);
  render('<div class="loading">Looking for your chat…</div>');
  return api("/api/chat")
    .then(function (payload) {
      if ((location.hash || "") !== "#/chat") return;
      var chats = payload.chats || [];
      var remembered = STATE.chat ? STATE.chat.id : chatStore(CHAT_ID_KEY);
      var match = null;
      chats.forEach(function (chat) { if (chat.chat_id === remembered) match = chat; });
      if (match) return openChat(match.chat_id, match);
      if (!remembered && chats.length) {
        // Opened from another tab, or before this tab lost its storage: rejoin the newest
        // rather than opening a second one by accident.
        var newest = chats[chats.length - 1];
        return openChat(newest.chat_id, newest);
      }
      renderChatStart({ gone: remembered || null, chats: chats });
      // Said once: the next visit is a plain start page, not the same warning forever.
      if (remembered) {
        chatStore(CHAT_ID_KEY, null);
        chatStore(CHAT_DRAFT_KEY + remembered, null);
      }
      if (STATE.chat && STATE.chat.id === remembered) {
        var stale = STATE.chat;
        STATE.chat = null;
        unwatchChat(stale);
      }
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="page-head"><h1>Chat</h1></div>' +
             '<div class="notice err"><div class="notice-title">Can\'t reach the dashboard</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div>" +
             '<div class="composer-row"><button type="button" class="btn" id="chat-retry-open">' +
             "Retry</button></div></div>");
      var retry = el("chat-retry-open");
      if (retry) retry.addEventListener("click", chatView);
    });
}

/* An explicit start. Opening the page (or pressing its number key) used to create a chat with
 * its own workspace and environment as a side effect. */
function renderChatStart(options) {
  options = options || {};
  var config = STATE.config || {};
  var gone = options.gone
    ? '<div class="notice chat-gone"><div class="notice-title">Your previous chat is gone</div>' +
      '<div class="notice-body">Chat <span class="mono">' + esc(String(options.gone).slice(0, 8)) +
      "</span> is no longer open on the server — it was ended, reclaimed after sitting " +
      "unwatched, or the dashboard was restarted. Its transcript stays in " +
      '<a href="#/runs">Runs</a>.</div></div>'
    : "";
  var live = (options.chats || []).map(function (chat) {
    return '<div class="source"><span class="mono">' + esc(chat.chat_id.slice(0, 8)) + "</span>" +
      '<span class="meta">' + esc(chat.agent) + " · " + esc(chat.permission_mode) + " · " +
      esc(fmt.num(chat.turns)) + " turns</span>" +
      '<span class="meta source-origin">' + esc(fmt.tail(chat.workspace, 48)) + "</span>" +
      '<span class="flag flag-warn" data-waiting-for="' + esc(chat.chat_id) + '"' +
      (chat.pending_approvals ? "" : " hidden") + ">waiting for your approval</span>" +
      '<button type="button" class="btn" data-resume="' + esc(chat.chat_id) + '">Resume</button></div>';
  }).join("");
  var workspaces = (config.workspaces || []).map(function (ws) {
    return '<option value="' + esc(ws.index) + '">' + esc(fmt.tail(ws.path, 60)) + "</option>";
  }).join("");
  var agents = (config.agents || []).map(function (name) {
    return '<option value="' + esc(name) + '"' + (name === config.default_agent ? " selected" : "") +
      ">" + esc(name) + "</option>";
  }).join("");
  var modes = (config.permission_modes || []).map(function (name) {
    return '<option value="' + esc(name) + '"' + (name === config.max_permission ? " selected" : "") +
      ">" + esc(name) + "</option>";
  }).join("");
  var models = (config.recent_models || []).map(function (name) {
    return '<option value="' + esc(name) + '"></option>';
  }).join("");
  render(
    '<div class="page-head"><h1>Chat</h1></div>' + gone +
    (live ? '<div class="card"><div class="page-head sub-head"><h2>Open chats</h2></div>' + live + "</div>" : "") +
    '<div class="card"><form id="chat-new-form" class="composer">' +
    '<div class="page-head sub-head"><h2>New chat</h2></div>' +
    '<div class="stat-sub">Opens a conversation with its own environment in the workspace you ' +
    "pick. It holds that workspace until you end it or it sits unwatched long enough for the " +
    "dashboard to reclaim it.</div>" +
    '<div class="composer-row chat-new-fields">' +
    (workspaces ? '<label>Workspace <select id="chat-new-workspace">' + workspaces + "</select></label>" : "") +
    (agents ? '<label>Agent <select id="chat-new-agent">' + agents + "</select></label>" : "") +
    '<label>Model <input id="chat-new-model" list="chat-new-models" placeholder="' +
    esc(config.default_model || "default") + '"></label>' +
    '<datalist id="chat-new-models">' + models + "</datalist>" +
    (modes ? '<label>Permission <select id="chat-new-permission">' + modes + "</select></label>" : "") +
    "</div>" +
    '<div class="composer-row"><button type="submit" class="btn btn-primary" id="chat-new-start">' +
    'Start chat</button><span class="stat-sub" id="chat-new-state"></span></div>' +
    "</form></div>"
  );
  el("chat-new-form").addEventListener("submit", function (event) {
    event.preventDefault();
    startChat();
  });
  view().querySelectorAll("[data-resume]").forEach(function (button) {
    button.addEventListener("click", function () {
      var id = button.getAttribute("data-resume");
      chatStore(CHAT_ID_KEY, id);
      openChat(id, null);
    });
  });
  renderOtherChats();
}

function startChat() {
  var button = el("chat-new-start");
  if (!button || button.disabled) return Promise.resolve();
  var config = STATE.config || {};
  var value = function (id) { var node = el(id); return node ? node.value : ""; };
  var spec = {
    workspace: value("chat-new-workspace") === "" ? 0 : Number(value("chat-new-workspace")),
    agent: value("chat-new-agent") || config.default_agent || undefined,
    model: value("chat-new-model").trim() || undefined,
    permission_mode: value("chat-new-permission") || config.max_permission || undefined
  };
  button.disabled = true;
  var state = el("chat-new-state");
  if (state) state.textContent = "Opening a workspace and an environment…";
  return api("/api/chat", { method: "POST", body: spec })
    .then(function (chat) {
      chatStore(CHAT_ID_KEY, chat.chat_id);
      if ((location.hash || "") === "#/chat") return openChat(chat.chat_id, chat);
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      var node = el("chat-new-state");
      if (node) node.textContent = "Could not start the chat — " + err.code + ": " + err.message;
      var again = el("chat-new-start");
      if (again) again.disabled = false;
    });
}

function newChatState(chatId, known) {
  return {
    id: chatId, info: known || null, offset: 0, poller: null,
    rows: [], seq: 0, renderedSeq: 0, liveHtml: null,
    approvals: [], settled: [], usedSettled: Object.create(null), seenAsks: Object.create(null),
    answering: Object.create(null), graceSeconds: null,
    sources: [], pending: [], lastEvent: null, childOpen: Object.create(null),
    sending: false, stopping: false, ending: false, fetchingUrl: false,
    uploads: Promise.resolve(), failures: 0, offline: null, mounted: false
  };
}

/* Mount a chat in the view. The chat being left (if another) is unmounted, not dropped: its
 * heartbeat continues while it has a turn running or an approval waiting. */
function openChat(chatId, known) {
  var chat = CHAT_OPEN[chatId] || null;
  var previous = STATE.chat;
  if (previous && previous !== chat) {
    unmountChat(previous);
    if (!previous.poller) delete CHAT_OPEN[previous.id];
  }
  if (!chat) {
    chat = newChatState(chatId, known);
    CHAT_OPEN[chatId] = chat;
  }
  STATE.chat = chat;
  chatStore(CHAT_ID_KEY, chatId);
  chat.mounted = true;
  chat.renderedSeq = 0;      // the shell is new, so every row it keeps is drawn again
  chat.liveHtml = null;
  render(chatShellHtml(chatId));
  var draft = chatStore(CHAT_DRAFT_KEY + chatId);
  if (draft) el("chat-task").value = draft;
  wireChat();
  renderChatInfo();
  renderChatLog();
  renderApprovals();
  renderOffline();
  renderOtherChats();
  setChatNavCount();
  watchChat(chat);
  refreshSources();
  return Promise.resolve();
}

/* Start (or hurry) a chat's poll. One Poller per chat this tab has open, each its own
 * heartbeat, so an approval waiting in one chat is not reaped while the user works in another. */
function watchChat(chat) {
  if (!chat.poller) {
    chat.poller = new Poller(function () { return pollChat(chat); }, CHAT_POLL_MS, {
      hiddenInterval: CHAT_HIDDEN_MS,
      keepAliveHidden: function () { return chatNeedsHeartbeat(chat); }
    });
    chatSetIntervals(chat);
    chat.poller.start();
  } else {
    chatSetIntervals(chat);
    chat.poller.now();
  }
}

/* Stop watching a chat entirely: it ended, vanished, or has nothing left to keep alive. */
function unwatchChat(chat) {
  if (chat.poller) { chat.poller.stop(); chat.poller = null; }
  if (CHAT_OPEN[chat.id] === chat && STATE.chat !== chat) delete CHAT_OPEN[chat.id];
}

function chatShellHtml(chatId) {
  return (
    '<div class="page-head"><h1>Chat</h1>' +
    '<span class="meta" id="chat-meta">' + esc(chatId.slice(0, 8)) + "</span>" +
    '<a class="meta" id="chat-trace" href="#/runs">trace</a>' +
    '<button type="button" class="btn" id="chat-stop" hidden>Stop</button>' +
    '<button type="button" class="btn" id="chat-new">New chat</button>' +
    '<button type="button" class="btn" id="chat-close">End chat</button>' +
    "</div>" +
    '<div id="chat-others"></div>' +
    '<div class="notice err" id="chat-offline" role="status" hidden>' +
    '<div class="notice-title">Can\'t reach the dashboard</div>' +
    '<div class="notice-body" id="chat-offline-body"></div>' +
    '<div class="composer-row"><button type="button" class="btn" id="chat-retry">Retry now</button>' +
    "</div></div>" +
    '<div id="approvals"></div>' +
    sourcesPanelHtml() +
    '<div class="card"><div id="chat-log" class="chat-log">' +
    '<div class="stat-sub" id="chat-empty">Nothing yet. Add sources above if you want the answer ' +
    "grounded in a document, then send a message.</div>" +
    '<div id="chat-rows" class="chat-rows"></div><div id="chat-live" class="chat-rows"></div>' +
    "</div></div>" +
    '<div class="card"><form id="chat-form" class="composer">' +
    '<textarea id="chat-task" rows="3" ' +
    'placeholder="Ask something. ⌘/Ctrl+Enter sends."></textarea>' +
    '<div class="composer-row">' +
    '<button type="submit" class="btn btn-primary" id="chat-send">Send</button>' +
    '<span class="stat-sub" id="chat-state"></span></div></form></div>'
  );
}

/* Take a chat out of the view. A chat that is running a turn, sending one, or waiting on an
 * approval keeps its heartbeat; anything else stops polling. Either way the chat itself stays
 * open on the server, and coming back resumes it. */
function unmountChat(chat) {
  if (!chat || !chat.mounted) return;
  chat.mounted = false;
  if (!chatNeedsHeartbeat(chat)) {
    if (chat.poller) { chat.poller.stop(); chat.poller = null; }
    if (STATE.chat !== chat) delete CHAT_OPEN[chat.id];
    return;
  }
  chatSetIntervals(chat);
  setChatNavCount();
  if (chat.approvals.length) {
    toast("Chat " + chat.id.slice(0, 8) + " is still waiting for your approval. It stays " +
          "parked while this tab is open; closing the tab denies it.");
  }
}

/* Called by the router on every navigation. */
function leaveChat() {
  if (STATE.chat) unmountChat(STATE.chat);
}

/* Show the New chat form from inside a chat, leaving the current one running. */
function showChatStart() {
  if (STATE.chat) {
    var chat = STATE.chat;
    unmountChat(chat);
    // Not the resume target any more unless something keeps it alive (then it is listed).
    if (!chat.poller) delete CHAT_OPEN[chat.id];
    STATE.chat = null;
    chatStore(CHAT_ID_KEY, null);
  }
  render('<div class="loading">Loading…</div>');
  return api("/api/chat")
    .then(function (payload) { renderChatStart({ chats: payload.chats || [] }); })
    .catch(function () { renderChatStart({ chats: [] }); });
}

/* Every chat this tab watches. Ends with the tab: a closed tab still leads to denial. */
function stopChat() {
  Object.keys(CHAT_OPEN).forEach(function (id) {
    var chat = CHAT_OPEN[id];
    if (chat.poller) { chat.poller.stop(); chat.poller = null; }
  });
  CHAT_OPEN = {};
  STATE.chat = null;
  setChatNavCount();
}

function chatNeedsHeartbeat(chat) {
  return !!(chat && (chat.sending || chat.approvals.length || (chat.info && chat.info.busy)));
}

/* Poll comfortably faster than the server's grace window: this request *is* the heartbeat,
 * and missing it denies the user's own pending approval. */
function chatSetIntervals(chat) {
  if (!chat || !chat.poller) return;
  var grace = typeof chat.graceSeconds === "number" ? chat.graceSeconds * 1000 : null;
  var visible = chat.mounted ? CHAT_POLL_MS : CHAT_BACKGROUND_MS;
  var hidden = CHAT_HIDDEN_MS;
  if (grace !== null) {
    visible = Math.max(400, Math.min(visible, grace / 4));
    hidden = Math.max(500, Math.min(hidden, grace / 6));
  }
  if (chat.failures) {
    var backoff = Math.min(CHAT_BACKOFF_MAX_MS, CHAT_POLL_MS * Math.pow(2, chat.failures));
    visible = Math.max(visible, backoff);
    hidden = Math.max(hidden, backoff);
  }
  chat.poller.interval = visible;
  chat.poller.hiddenInterval = hidden;
}

function chatPollNow(chat) {
  chat = chat || STATE.chat;
  if (chat && chat.poller) chat.poller.now();
}

/* Chats this tab keeps alive that are not on screen, with an approval waiting. */
function waitingChats() {
  return Object.keys(CHAT_OPEN).map(function (id) { return CHAT_OPEN[id]; })
    .filter(function (chat) { return !chat.mounted && chat.approvals.length; });
}

function setChatNavCount() {
  var link = el("nav-chat");
  if (!link) return;
  var badge = el("nav-chat-count");
  var count = waitingChats().reduce(function (sum, chat) { return sum + chat.approvals.length; }, 0);
  if (!badge) {
    if (!count) return;
    badge = document.createElement("span");
    badge.className = "nav-count";
    badge.id = "nav-chat-count";
    link.appendChild(badge);
  }
  badge.textContent = String(count);
  badge.hidden = count === 0;
  badge.title = count + " chat approval" + (count === 1 ? "" : "s") + " waiting";
}

/* Other chats waiting for an answer, each a link back: a strip above the current chat, and a
 * marker on the New chat page's list of open chats. Redrawn only when its markup changes. */
function renderOtherChats() {
  var node = el("chat-others");
  if (node) {
    var html = waitingChats().map(function (chat) {
      var tools = chat.approvals.map(function (ask) { return ask.tool_name || "a tool call"; });
      return '<div class="notice approval chat-other" data-chat="' + esc(chat.id) + '">' +
        '<div class="notice-title">Chat <span class="mono">' + esc(chat.id.slice(0, 8)) +
        "</span> is waiting for your approval</div>" +
        '<div class="composer-row"><span class="stat-sub">' + esc(tools.join(", ")) + "</span>" +
        '<button type="button" class="btn btn-primary" data-switch="' + esc(chat.id) + '">' +
        "Go to it</button></div></div>";
    }).join("");
    if (node._drawn !== html) {
      node._drawn = html;
      node.innerHTML = html;
      node.querySelectorAll("[data-switch]").forEach(function (button) {
        button.addEventListener("click", function () { openChat(button.getAttribute("data-switch"), null); });
      });
    }
  }
  view().querySelectorAll("[data-waiting-for]").forEach(function (flag) {
    var chat = CHAT_OPEN[flag.getAttribute("data-waiting-for")];
    if (chat) flag.hidden = !chat.approvals.length;
  });
}
/* --- sources -------------------------------------------------------------- */

function sourcesPanelHtml() {
  return (
    '<div class="card sources">' +
    '<div class="page-head sub-head"><h2>Sources</h2>' +
    '<span class="meta" id="sources-meta"></span></div>' +
    '<div class="stat-sub">Files land in the agent\'s workspace and the next message tells it ' +
    "to read them. It uses its own file tools, so it can re-read and quote them — nothing is " +
    "pasted into the prompt.</div>" +
    '<div class="composer-row">' +
    '<label class="btn" for="src-file">Add files' +
    '<input type="file" id="src-file" multiple hidden></label>' +
    '<input type="url" id="src-url" placeholder="https://… fetch a page as a source">' +
    '<button type="button" class="btn" id="src-add-url">Fetch</button>' +
    "</div>" +
    '<div id="sources-list"></div></div>'
  );
}

function refreshSources() {
  var chat = STATE.chat;
  if (!chat) return Promise.resolve();
  return api("/api/chat/" + encodeURIComponent(chat.id) + "/sources")
    .then(function (payload) {
      if (STATE.chat !== chat) return;
      chat.sources = payload.sources || [];
      chat.pending = payload.pending || [];
      renderSources();
    })
    .catch(function (err) {
      // Not worth a banner, but not silence either: a stale list reads as the truth.
      var meta = el("sources-meta");
      if (meta && STATE.chat === chat) meta.textContent = "could not refresh the list — " + err.message;
    });
}

function renderSources() {
  var chat = STATE.chat;
  var list = el("sources-list");
  var meta = el("sources-meta");
  if (!list || !chat) return;
  if (!chat.sources.length) {
    list.innerHTML = '<div class="stat-sub">No sources yet.</div>';
    if (meta) meta.textContent = "";
    return;
  }
  var pending = {};
  chat.pending.forEach(function (path) { pending[path] = 1; });
  if (meta) {
    meta.textContent = chat.sources.length + " · " +
      (chat.pending.length
        ? chat.pending.length + " not yet sent to the agent"
        : "all sent");
  }
  list.innerHTML = chat.sources.map(function (source) {
    return (
      '<div class="source">' +
      '<span class="fchip f-' + (source.kind === "url" ? "net" : "read") + '">' +
      (source.kind === "url" ? "⇅" : "◇") + "</span>" +
      '<span class="mono">' + esc(source.path) + "</span>" +
      '<span class="meta">' + esc(fmt.bytes(source.bytes)) + "</span>" +
      (source.reader ? '<span class="meta">' + esc(source.reader) + "</span>" : "") +
      (source.origin
        ? '<span class="meta source-origin">' + esc(fmt.truncate(source.origin, 60)) + "</span>"
        : "") +
      (pending[source.path]
        ? '<span class="flag flag-warn">queued for next message</span>'
        : '<span class="flag">sent</span>') +
      "</div>"
    );
  }).join("");
}

function wireChat() {
  // toggle does not bubble. Capture expansion before a polling render replaces it.
  el("chat-log").addEventListener("toggle", function (event) {
    var target = event.target;
    if (STATE.chat && target.classList.contains("chat-child-preview") && target.isConnected) {
      STATE.chat.childOpen[target.dataset.source] = target.open;
    }
  }, true);
  el("chat-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendChatTurn();
  });
  el("chat-close").addEventListener("click", endChat);
  el("chat-new").addEventListener("click", showChatStart);
  el("chat-stop").addEventListener("click", stopChatTurn);
  el("chat-retry").addEventListener("click", function () {
    var body = el("chat-offline-body");
    if (body) body.textContent = "Retrying…";
    chatPollNow();
  });
  var box = el("chat-task");
  // Cmd/Ctrl+Enter sends, since Enter has to stay newline in a multi-line box.
  box.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      sendChatTurn();
    }
  });
  // Kept per chat, so leaving for another page and coming back does not lose what was typed.
  box.addEventListener("input", function () {
    if (STATE.chat) chatStore(CHAT_DRAFT_KEY + STATE.chat.id, box.value || null);
  });
  el("src-file").addEventListener("change", function () {
    uploadFiles(this.files);
    this.value = "";   // so choosing the same file twice fires `change` twice
  });
  el("src-add-url").addEventListener("click", addUrlSource);
  el("src-url").addEventListener("keydown", function (event) {
    if (event.key === "Enter") { event.preventDefault(); addUrlSource(); }
  });
}

/* Uploads go one at a time, sequentially — including across two picks in a row, which is why
 * the chain lives on the chat rather than in this call. Concurrently would also let two
 * requests race for the same `grounding/<name>` — the server suffixes on collision, so the
 * result would be correct but the names would depend on arrival order, which is a confusing
 * thing to explain to someone who dropped in five files. */
function uploadFiles(files) {
  var chat = STATE.chat;
  var list = Array.prototype.slice.call(files || []);
  if (!list.length || !chat) return;
  list.forEach(function (file) {
    chat.uploads = chat.uploads.then(function () { return uploadOne(chat, file); });
  });
  chat.uploads = chat.uploads.then(function () {
    if (STATE.chat === chat) return refreshSources();
  });
}

function uploadOne(chat, file) {
  if (file.size > MAX_UPLOAD_BYTES) {
    toast(file.name + " is " + fmt.bytes(file.size) + " — over the " +
          fmt.bytes(MAX_UPLOAD_BYTES) + " limit.");
    return Promise.resolve();
  }
  toast("Uploading " + file.name + "…");
  return readAsBase64(file)
    .then(function (b64) {
      return api("/api/chat/" + encodeURIComponent(chat.id) + "/sources", {
        method: "POST",
        body: { kind: "file", name: file.name, content_b64: b64 }
      });
    })
    .then(function (payload) {
      toast("Added " + payload.source.path);
    })
    .catch(function (err) { toast(file.name + ": " + err.message); });
}

/* FileReader, and its base64 taken off the data URL. Reading as an ArrayBuffer and encoding
 * by hand would mean a chunked loop over a typed array to avoid blowing the argument limit in
 * String.fromCharCode — the browser already has a correct implementation. */
function readAsBase64(file) {
  return new Promise(function (resolve, reject) {
    var reader = new FileReader();
    reader.onload = function () {
      var result = String(reader.result || "");
      var comma = result.indexOf(",");
      resolve(comma === -1 ? "" : result.slice(comma + 1));
    };
    reader.onerror = function () { reject(new Error("could not be read")); };
    reader.readAsDataURL(file);
  });
}

function addUrlSource() {
  var chat = STATE.chat;
  var box = el("src-url");
  if (!chat || !box || chat.fetchingUrl) return;
  var url = (box.value || "").trim();
  if (!url) return;
  chat.fetchingUrl = true;
  var button = el("src-add-url");
  if (button) button.disabled = true;
  toast("Fetching " + url + "…");
  api("/api/chat/" + encodeURIComponent(chat.id) + "/sources", {
    method: "POST", body: { kind: "url", url: url }
  })
    .then(function (payload) {
      var current = el("src-url");
      if (current && STATE.chat === chat && current.value.trim() === url) current.value = "";
      toast("Saved as " + payload.source.path);
      return refreshSources();
    })
    .catch(function (err) { toast(url + ": " + err.message); })
    .then(function () {
      chat.fetchingUrl = false;
      var again = el("src-add-url");
      if (again && STATE.chat === chat) again.disabled = false;
    });
}

/* --- turns ---------------------------------------------------------------- */

function sendChatTurn() {
  var chat = STATE.chat;
  var box = el("chat-task");
  if (!chat || !box || chat.sending) return;
  var task = (box.value || "").trim();
  if (!task) return;
  chat.sending = true;
  var send = el("chat-send");
  if (send) send.disabled = true;
  api("/api/chat/" + encodeURIComponent(chat.id) + "/turn",
      { method: "POST", body: { task: task } })
    .then(function () {
      // Only clear what was sent: text typed while the request was in flight stays.
      var current = el("chat-task");
      if (current && STATE.chat === chat && current.value.trim() === task) {
        current.value = "";
        chatStore(CHAT_DRAFT_KEY + chat.id, null);
      } else if (!current) {
        if ((chatStore(CHAT_DRAFT_KEY + chat.id) || "").trim() === task) {
          chatStore(CHAT_DRAFT_KEY + chat.id, null);
        }
      }
      // Deliberately NOT echoed here: the harness records the turn as a `user_message` event
      // and the tail delivers it a moment later. And not polled directly either: a second
      // poll racing the poller's own read the same offset and appended every event twice.
      chatPollNow();
      return refreshSources();
    })
    .catch(function (err) {
      if (err.status === 404) { chatGone(chat); return; }
      if (err.status === 401) { stopChat(); render(tokenRequiredPanel()); return; }
      toast(err.status === 409
        ? "Still working on the previous turn — stop it or wait for it to finish."
        : "Not sent — " + err.code + ": " + err.message);
    })
    .then(function () {
      chat.sending = false;
      var again = el("chat-send");
      if (again && STATE.chat === chat) again.disabled = false;
    });
}

function stopChatTurn() {
  var chat = STATE.chat;
  if (!chat || chat.stopping) return;
  chat.stopping = true;
  var button = el("chat-stop");
  if (button) button.disabled = true;
  api("/api/chat/" + encodeURIComponent(chat.id) + "/stop", { method: "POST" })
    .then(function (payload) {
      toast(payload.stopped ? "Stopping the current turn…" : "Nothing was running.");
      chatPollNow();
    })
    .catch(function (err) {
      if (err.status === 404) { chatGone(chat); return; }
      toast("Could not stop — " + err.code + ": " + err.message);
    })
    .then(function () {
      chat.stopping = false;
      var again = el("chat-stop");
      if (again && STATE.chat === chat) again.disabled = false;
    });
}

/* One poll carries the chat state, the new events and the approvals, applied together once
 * all three have arrived, so nothing renders out of order: an approval card cannot appear
 * before the tool call that caused it, and a denial row is labelled from the broker's record
 * of who settled it — which the broker writes before the event exists. */
function pollChat(chat) {
  if (!chat || CHAT_OPEN[chat.id] !== chat) return Promise.resolve();
  var info, tail;
  // {keep: true}: this poll is the heartbeat and outlives the page that started it, so a
  // navigation must not drop it (core.js api(): a dropped read never settles).
  var keep = { keep: true };
  return api("/api/chat/" + encodeURIComponent(chat.id), keep)
    .then(function (payload) {
      info = payload;
      return api("/api/runs/" + encodeURIComponent(info.session_id) +
                 "/tail?offset=" + chat.offset, keep);
    })
    .then(function (payload) {
      tail = payload;
      // Polling /api/approvals is what refreshes the heartbeat, so it happens every tick
      // whether or not anything is pending.
      return api("/api/approvals?owner=" + encodeURIComponent(chat.id), keep);
    })
    .then(function (approvals) {
      if (CHAT_OPEN[chat.id] !== chat) return;
      var wasOffline = chat.failures > 0;
      chat.failures = 0;
      chat.offline = null;
      chat.info = info;
      if (typeof approvals.grace_seconds === "number") chat.graceSeconds = approvals.grace_seconds;
      chat.settled = approvals.settled || [];
      if (tail.truncated) {
        chat.offset = 0; chat.rows = []; chat.renderedSeq = 0;
        chat.usedSettled = Object.create(null);
        var rows = el("chat-rows");
        if (rows) rows.innerHTML = "";
      } else {
        chat.offset = tail.offset;
        (tail.events || []).forEach(function (event) { ingestChatEvent(chat, event); });
      }
      chat.approvals = approvals.approvals || [];
      noticeNewAsks(chat);
      chatSetIntervals(chat);
      if (chat.mounted && STATE.chat === chat) {
        renderChatInfo();
        renderChatLog();
        renderApprovals();
        if (wasOffline) renderOffline();
      } else if (!chatNeedsHeartbeat(chat)) {
        // Off-screen and nothing left to keep alive: stop. Coming back resumes from here.
        unwatchChat(chat);
      }
      setChatNavCount();
      renderOtherChats();
    })
    .catch(function (err) {
      if (CHAT_OPEN[chat.id] !== chat) return;
      if (err.status === 401) { stopChat(); render(tokenRequiredPanel()); return; }
      if (err.status === 404 && info === undefined) { chatGone(chat); return; }
      // Unreachable or failing: say so instead of leaving "working… turn N" on screen, and
      // back off rather than hammering a dashboard that is down.
      chat.failures += 1;
      chat.offline = { status: err.status || null, code: err.code, message: err.message };
      chatSetIntervals(chat);
      if (STATE.chat === chat) renderOffline();
    });
}

function noticeNewAsks(chat) {
  chat.approvals.forEach(function (ask) {
    if (chat.seenAsks[ask.ask_id]) return;
    chat.seenAsks[ask.ask_id] = 1;
    if (!chat.mounted) {
      toast("Chat " + chat.id.slice(0, 8) + " needs your approval for " +
            (ask.tool_name || "a tool call") + ".");
    }
  });
}

/* The server no longer has this chat. Say so; never open a replacement silently. */
function chatGone(chat) {
  if (CHAT_OPEN[chat.id] !== chat) return;
  var current = STATE.chat === chat;
  var mounted = current && chat.mounted;
  if (current) STATE.chat = null;
  unwatchChat(chat);
  delete CHAT_OPEN[chat.id];
  setChatNavCount();
  renderOtherChats();
  chatStore(CHAT_DRAFT_KEY + chat.id, null);
  // Off-screen, it is kept so the next visit to the Chat page can say the chat is gone; on
  // screen, it is said right now.
  if (current) chatStore(CHAT_ID_KEY, mounted ? null : chat.id);
  if (mounted && (location.hash || "") === "#/chat") {
    api("/api/chat")
      .then(function (payload) { renderChatStart({ gone: chat.id, chats: payload.chats || [] }); })
      .catch(function () { renderChatStart({ gone: chat.id, chats: [] }); });
  } else {
    toast("Your chat " + chat.id.slice(0, 8) + " is no longer open on the server.");
  }
}

function renderOffline() {
  var chat = STATE.chat;
  var node = el("chat-offline");
  if (!node || !chat) return;
  if (!chat.offline) { node.hidden = true; return; }
  var wait = chat.poller ? Math.round(chat.poller.interval / 100) / 10 : null;
  var why = chat.offline.status
    ? "It answered " + chat.offline.status + " — " + chat.offline.code + ": " + chat.offline.message
    : "No answer: " + chat.offline.message;
  el("chat-offline-body").textContent = why + ". What is shown below may be out of date. " +
    (wait !== null ? "Retrying in " + wait + "s; " : "") + "this clears by itself when it answers.";
  node.hidden = false;
  var state = el("chat-state");
  if (state) state.textContent = "status unknown — the dashboard is not answering";
}

function renderChatInfo() {
  var chat = STATE.chat;
  var info = chat && chat.info;
  if (!info || !info.session_id) return;
  var state = el("chat-state");
  var text = info.busy
    ? "working… turn " + info.turns
    : info.turns + (info.turns === 1 ? " turn" : " turns") + " · " +
      info.permission_mode + " · " + info.workspace;
  // Only on change: rewriting text nodes every tick clears a selection inside them.
  if (state && state.textContent !== text) state.textContent = text;
  var stop = el("chat-stop");
  if (stop && stop.hidden !== !info.busy) stop.hidden = !info.busy;
  var trace = el("chat-trace");
  var href = "#/runs/" + info.session_id;
  if (trace && trace.getAttribute("href") !== href) trace.setAttribute("href", href);
}

function ingestChatEvent(chat, event) {
  var payload = event.payload || {};
  chat.lastEvent = event.timestamp;
  if (event.type === "model_response") {
    if (payload.reasoning) appendChatRow(chat, "thinking", payload.reasoning);
    if (payload.content) appendChatRow(chat, "agent", payload.content);
  } else if (event.type === "tool_call") {
    appendChatRow(chat, "tool", payload.name + " " + JSON.stringify(payload.arguments || {}),
                  false, payload.name);
  } else if (event.type === "tool_result") {
    appendChatRow(chat, "result", String(payload.content || ""), payload.is_error);
  } else if (event.type === "permission_ask" && payload.approved === false) {
    appendChatRow(chat, "denied", (payload.name || "a tool") + " — " + denialText(chat, payload));
  } else if (event.type === "user_message" && payload.content) {
    appendChatRow(chat, payload.subagent ? "subagent" : "you", payload.content);
  } else if (event.type === "session_end") {
    appendChatRow(chat, "system", "turn ended · " + (payload.reason || "done"));
  }
  if (chat.rows.length > CHAT_MAX_ROWS) chat.rows = chat.rows.slice(-CHAT_MAX_ROWS);
}

/* The engine words every refused ask as "User denied: <action>", whoever refused it, so the
 * words are not evidence. The broker's record of the settled ask is. */
var USER_DENIED_PREFIX = "User denied: ";

function denialText(chat, payload) {
  var reason = String(payload.reason || "");
  var action = reason.indexOf(USER_DENIED_PREFIX) === 0 ? reason.slice(USER_DENIED_PREFIX.length) : null;
  var record = null;
  for (var i = 0; i < chat.settled.length && !record; i++) {
    var item = chat.settled[i];
    if (chat.usedSettled[item.ask_id] || item.approved) continue;
    if ((payload.id && item.tool_call_id && item.tool_call_id === payload.id) ||
        (action !== null && item.action === action)) {
      record = item;
    }
  }
  if (record) {
    chat.usedSettled[record.ask_id] = 1;
    return record.summary;
  }
  if (action !== null) return "denied (who decided is unknown: the dashboard has no record of this ask)";
  return reason || "denied";
}

function appendChatRow(chat, kind, text, isError, toolName) {
  chat.seq += 1;
  chat.rows.push({
    seq: chat.seq, kind: kind, text: String(text), error: !!isError,
    family: toolName ? toolFamily(toolName) : null
  });
}

function chatRowHtml(row) {
  return (
    '<div class="chat-row chat-' + row.kind + (row.family ? " f-" + row.family : "") + '">' +
    '<span class="chat-who">' + esc(row.kind) + "</span>" +
    "<pre" + (row.error ? ' class="out-err"' : "") + ">" +
    esc(fmt.truncate(row.text, 4000)) + "</pre></div>"
  );
}

/* Rows are appended, never redrawn: a row's text does not change once it exists, and
 * redrawing every tick cleared any selection inside it within a second. The transient part —
 * the streaming preview and child previews — is redrawn only when its markup differs. */
function renderChatLog() {
  var chat = STATE.chat;
  var node = el("chat-log");
  var rowsNode = el("chat-rows");
  var liveNode = el("chat-live");
  if (!chat || !node || !rowsNode || !liveNode) return;
  // The DOM changes before the asynchronous toggle event. Preserve it even if
  // this render replaces the element before the capture listener can run.
  liveNode.querySelectorAll(".chat-child-preview").forEach(function (child) {
    chat.childOpen[child.dataset.source] = child.open;
  });
  // Whether to stick to the bottom is decided *before* anything changes: afterwards the
  // scrollHeight has already grown, so every comparison says "not at the bottom".
  var atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
  var changed = false;

  var fresh = chat.rows.filter(function (row) { return row.seq > chat.renderedSeq; });
  if (fresh.length) {
    rowsNode.insertAdjacentHTML("beforeend", fresh.map(chatRowHtml).join(""));
    chat.renderedSeq = fresh[fresh.length - 1].seq;
    changed = true;
  }
  while (rowsNode.childElementCount > CHAT_MAX_ROWS) rowsNode.removeChild(rowsNode.firstElementChild);

  var preview = chat.info && chat.info.preview;
  if (preview && preview.after !== chat.lastEvent) preview = null;
  var children = chat.info && chat.info.child_previews;
  if (children && children.after !== chat.lastEvent) children = null;
  var items = children ? children.items : [];
  Object.keys(chat.childOpen).forEach(function (source) {
    if (!items.some(function (item) { return item.source === source; })) {
      delete chat.childOpen[source];
    }
  });
  var liveHtml = items.map(function (item) {
    return '<details class="chat-row chat-subagent chat-child-preview" data-source="' +
      esc(item.source) + '"' + (chat.childOpen[item.source] ? " open" : "") + '>' +
      '<summary><span class="chat-who">↳ ' + esc(item.agent) + '</span> · depth ' +
      Number(item.depth) + ' · writing…</summary><pre>' + esc(item.text) + '</pre></details>';
  }).join("") + (preview
    ? '<div class="chat-row chat-agent chat-preview"><span class="chat-who">writing…</span><pre>' +
      esc(preview.text) + '</pre></div>' : "");
  if (liveHtml !== chat.liveHtml) {
    liveNode.innerHTML = liveHtml;
    chat.liveHtml = liveHtml;
    changed = true;
  }

  var empty = el("chat-empty");
  // An empty log collapses to a hairline, which reads as a broken pane. Say what it is.
  if (empty) empty.hidden = !!(chat.rows.length || liveHtml);
  if (changed && atBottom) node.scrollTop = node.scrollHeight;
}

/* --- approvals ------------------------------------------------------------ */

/* A card is created once per ask and kept until the ask is gone; only its "waiting" text is
 * updated in place. Rebuilding every tick swallowed any press that spanned a tick and threw
 * keyboard focus back to <body>. The card shows the real tool name and arguments when they
 * could be recovered, and the raw screened string when they could not — degraded, never
 * wrong. */
function renderApprovals() {
  var chat = STATE.chat;
  var node = el("approvals");
  if (!node || !chat) return;
  var byId = {};
  chat.approvals.forEach(function (ask) { byId[ask.ask_id] = ask; });
  Array.prototype.slice.call(node.children).forEach(function (card) {
    if (!byId[card.getAttribute("data-ask")]) node.removeChild(card);
  });
  chat.approvals.forEach(function (ask) {
    var card = null;
    for (var i = 0; i < node.children.length; i++) {
      if (node.children[i].getAttribute("data-ask") === ask.ask_id) card = node.children[i];
    }
    if (!card) {
      node.insertAdjacentHTML("beforeend", approvalCardHtml(chat, ask));
      card = node.lastElementChild;
      wireApprovalCard(card, ask);
    }
    var wait = card.querySelector(".approval-wait");
    var text = " waiting " + Math.round((ask.waiting_ms || 0) / 1000) + "s";
    if (wait && wait.textContent !== text) wait.textContent = text;
  });
}

function approvalCardHtml(chat, ask) {
  var body = ask.arguments
    ? '<pre class="args">' + esc(JSON.stringify(ask.arguments, null, 1)) + "</pre>"
    : '<pre class="args">' + esc(ask.action) + "</pre>";
  var grace = typeof chat.graceSeconds === "number" ? chat.graceSeconds + "s" : "a short while";
  return (
    '<div class="notice approval" data-ask="' + esc(ask.ask_id) + '">' +
    '<div class="notice-title">Approve <span class="mono">' +
    esc(ask.tool_name || "this tool call") + "</span>?" +
    '<span class="meta approval-wait"></span>' +
    "</div>" + body +
    '<div class="composer-row">' +
    '<button type="button" class="btn btn-primary" data-approve="' + esc(ask.ask_id) + '">Approve</button>' +
    '<button type="button" class="btn" data-deny="' + esc(ask.ask_id) + '">Deny</button>' +
    '<span class="stat-sub approval-note">It waits while this tab is open, even in the ' +
    "background or on another dashboard page. Closing the tab denies it after " + esc(grace) +
    " — the agent carries on as if refused, and the transcript says why.</span></div>" +
    '<div class="stat-sub approval-status" role="status"></div></div>'
  );
}

function wireApprovalCard(card, ask) {
  // The owner and digest name the exact ask this card showed; a stale card is refused.
  card.querySelector("[data-approve]").addEventListener("click", function () {
    answerApproval(card, ask, true);
  });
  card.querySelector("[data-deny]").addEventListener("click", function () {
    answerApproval(card, ask, false);
  });
}

function answerApproval(card, ask, approved) {
  var chat = STATE.chat;
  if (!chat || chat.answering[ask.ask_id]) return;
  chat.answering[ask.ask_id] = true;
  var buttons = card.querySelectorAll("button");
  var setDisabled = function (value) {
    for (var i = 0; i < buttons.length; i++) buttons[i].disabled = value;
  };
  var status = card.querySelector(".approval-status");
  setDisabled(true);
  if (status) status.textContent = approved ? "Approving…" : "Denying…";
  api("/api/approvals/" + encodeURIComponent(ask.ask_id), {
    method: "POST", body: { approved: approved, owner: ask.owner, digest: ask.digest }
  })
    .then(function () {
      if (status) status.textContent = approved ? "Approved." : "Denied.";
      chatPollNow();
    })
    .catch(function (err) {
      var settled = err.status === 409 || err.status === 404 || err.status === 410;
      // The server's own words: it knows whether this was a timeout, the reaper, another tab
      // or a stale card, and one fixed sentence for all of them was a guess.
      var text = err.code + ": " + err.message;
      if (status) status.textContent = text;
      toast(text);
      if (!settled) {
        // Nothing was decided (the request failed), so the buttons come back.
        delete chat.answering[ask.ask_id];
        setDisabled(false);
      }
      chatPollNow();
    });
}

function endChat() {
  var chat = STATE.chat;
  if (!chat || chat.ending) return;
  var running = chat.sending || (chat.info && chat.info.busy);
  var waiting = chat.approvals.length;
  if ((running || waiting) && !window.confirm(
    "End this chat?" +
    (running ? " The turn in progress will be stopped." : "") +
    (waiting ? " " + waiting + " pending approval" + (waiting === 1 ? " will be" : "s will be") +
               " denied." : "") +
    " Its workspace is released.")) {
    return;
  }
  chat.ending = true;
  var button = el("chat-close");
  if (button) { button.disabled = true; button.textContent = "Ending…"; }
  api("/api/chat/" + encodeURIComponent(chat.id), { method: "DELETE" })
    .then(function () {
      if (STATE.chat === chat) STATE.chat = null;
      unwatchChat(chat);
      delete CHAT_OPEN[chat.id];
      setChatNavCount();
      chatStore(CHAT_ID_KEY, null);
      chatStore(CHAT_DRAFT_KEY + chat.id, null);
      toast("Chat closed and its workspace released.");
      location.hash = "#/runs";
    })
    .catch(function (err) {
      chat.ending = false;
      if (err.status === 404) { chatGone(chat); return; }
      toast("Could not end the chat — " + err.code + ": " + err.message);
      var again = el("chat-close");
      if (again && STATE.chat === chat) { again.disabled = false; again.textContent = "End chat"; }
    });
}
