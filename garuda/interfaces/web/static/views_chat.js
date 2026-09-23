/* Talking to an agent, with sources.
 *
 * The poll here is load-bearing in a way no other poll in this app is: it doubles as the
 * heartbeat that keeps a parked approval alive. An owner the server has not heard from within
 * its grace window has its pending asks denied — which is what makes closing this tab
 * mid-approval safe instead of leaving a run and its container wedged for the full five-minute
 * timeout. So the interval is derived from the grace window the server reports rather than
 * hardcoded, and `Poller` resuming immediately on `visibilitychange` matters here more than
 * anywhere else.
 *
 * Sources — uploaded files and fetched URLs — become files in the agent's workspace, and the
 * next message names them. The server does that part; this view's job is to make it visible
 * which sources the agent has been told about and which are still queued. */

"use strict";

var CHAT_POLL_MS = 900;
var CHAT_MAX_ROWS = 400;
/* Must stay under the server's own decoded ceiling (grounding.MAX_SOURCE_BYTES, 8 MiB) so an
 * oversized file is refused here, with a sentence, instead of after a 12 MiB upload. */
var MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

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
  render('<div class="loading">Opening a conversation…</div>');
  return api("/api/chat")
    .then(function (payload) {
      var chats = payload.chats || [];
      if (chats.length) {
        // Rejoin the most recent rather than opening a second: each chat holds a workspace and
        // an environment, and accumulating them by accident is what the reaper cleans up after.
        return openChat(chats[chats.length - 1].chat_id);
      }
      return api("/api/chat", { method: "POST", body: chatSpec() })
        .then(function (chat) { return openChat(chat.chat_id, chat); });
    })
    .catch(function (err) {
      if (err.status === 401) { render(tokenRequiredPanel()); return; }
      render('<div class="notice err"><div class="notice-title">Could not start a chat</div>' +
             '<div class="notice-body">' + esc(err.message) + "</div></div>");
    });
}

function chatSpec() {
  var config = STATE.config || {};
  return {
    agent: config.default_agent || undefined,
    permission_mode: config.max_permission || undefined,
    workspace: 0
  };
}

function openChat(chatId, known) {
  STATE.chat = {
    id: chatId, rows: [], offset: 0, info: known || null,
    approvals: [], sources: [], pending: []
  };
  render(chatShellHtml(chatId));
  wireChat();
  STATE.poller = new Poller(pollChat, CHAT_POLL_MS).start();
  refreshSources();
  return pollChat();
}

function chatShellHtml(chatId) {
  return (
    '<div class="page-head"><h1>Chat</h1>' +
    '<span class="meta" id="chat-meta">' + esc(chatId.slice(0, 8)) + "</span>" +
    '<a class="meta" id="chat-trace" href="#/runs">trace</a>' +
    '<button type="button" class="btn" id="chat-stop" hidden>Stop</button>' +
    '<button type="button" class="btn" id="chat-close">End chat</button>' +
    "</div>" +
    '<div id="approvals"></div>' +
    sourcesPanelHtml() +
    '<div class="card"><div id="chat-log" class="chat-log"></div></div>' +
    '<div class="card"><form id="chat-form" class="composer">' +
    '<textarea id="chat-task" rows="3" ' +
    'placeholder="Ask something. ⌘/Ctrl+Enter sends."></textarea>' +
    '<div class="composer-row">' +
    '<button type="submit" class="btn btn-primary" id="chat-send">Send</button>' +
    '<span class="stat-sub" id="chat-state"></span></div></form></div>'
  );
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
      if (!STATE.chat || STATE.chat.id !== chat.id) return;
      STATE.chat.sources = payload.sources || [];
      STATE.chat.pending = payload.pending || [];
      renderSources();
    })
    .catch(function () { /* the panel is not worth an error banner */ });
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
  el("chat-form").addEventListener("submit", function (event) {
    event.preventDefault();
    sendChatTurn();
  });
  el("chat-close").addEventListener("click", endChat);
  el("chat-stop").addEventListener("click", stopChatTurn);
  // Cmd/Ctrl+Enter sends, since Enter has to stay newline in a multi-line box.
  el("chat-task").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      sendChatTurn();
    }
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

/* Uploads go one at a time, sequentially. Concurrently would be faster and would also let two
 * requests race for the same `grounding/<name>` — the server suffixes on collision, so the
 * result would be correct but the names would depend on arrival order, which is a confusing
 * thing to explain to someone who dropped in five files. */
function uploadFiles(files) {
  var list = Array.prototype.slice.call(files || []);
  if (!list.length) return;
  var chain = Promise.resolve();
  list.forEach(function (file) {
    chain = chain.then(function () { return uploadOne(file); });
  });
  chain.then(refreshSources);
}

function uploadOne(file) {
  if (file.size > MAX_UPLOAD_BYTES) {
    toast(file.name + " is " + fmt.bytes(file.size) + " — over the " +
          fmt.bytes(MAX_UPLOAD_BYTES) + " limit.");
    return Promise.resolve();
  }
  toast("Uploading " + file.name + "…");
  return readAsBase64(file)
    .then(function (b64) {
      return api("/api/chat/" + encodeURIComponent(STATE.chat.id) + "/sources", {
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
  var box = el("src-url");
  var url = (box.value || "").trim();
  if (!url) return;
  var button = el("src-add-url");
  button.disabled = true;
  toast("Fetching " + url + "…");
  api("/api/chat/" + encodeURIComponent(STATE.chat.id) + "/sources", {
    method: "POST", body: { kind: "url", url: url }
  })
    .then(function (payload) {
      box.value = "";
      toast("Saved as " + payload.source.path);
      return refreshSources();
    })
    .catch(function (err) { toast(err.message); })
    .then(function () { button.disabled = false; });
}

/* --- turns ---------------------------------------------------------------- */

function sendChatTurn() {
  var box = el("chat-task");
  var task = (box.value || "").trim();
  if (!task) return;
  el("chat-send").disabled = true;
  api("/api/chat/" + encodeURIComponent(STATE.chat.id) + "/turn",
      { method: "POST", body: { task: task } })
    .then(function () {
      box.value = "";
      // Deliberately NOT echoed here: the harness records the turn as a `user_message` event
      // and the tail delivers it a moment later, so appending it optimistically showed every
      // message twice.
      return pollChat().then(refreshSources);
    })
    .catch(function (err) {
      if (err.status === 409) {
        toast("Still working on the previous turn — stop it or wait for it to finish.");
      } else if (err.status === 404) {
        toast("This chat was closed. Reopening…");
        return chatView();
      } else {
        toast(err.message);
      }
    })
    .then(function () { el("chat-send").disabled = false; });
}

function stopChatTurn() {
  var chat = STATE.chat;
  if (!chat) return;
  api("/api/chat/" + encodeURIComponent(chat.id) + "/stop", { method: "POST" })
    .then(function (payload) {
      toast(payload.stopped ? "Stopping the current turn…" : "Nothing was running.");
      return pollChat();
    })
    .catch(function (err) { toast(err.message); });
}

/* One poll carries the chat state, the new events and the pending approvals, so nothing
 * arrives out of order — an approval card cannot appear before the tool call that caused it,
 * and the "working" indicator cannot lag the turn finishing. */
function pollChat() {
  var chat = STATE.chat;
  if (!chat) return Promise.resolve();
  return api("/api/chat/" + encodeURIComponent(chat.id))
    .then(function (info) {
      chat.info = info;
      var state = el("chat-state");
      if (state) {
        state.textContent = info.busy
          ? "working… turn " + info.turns
          : info.turns + (info.turns === 1 ? " turn" : " turns") + " · " +
            info.permission_mode + " · " + info.workspace;
      }
      var stop = el("chat-stop");
      if (stop) stop.hidden = !info.busy;
      var trace = el("chat-trace");
      if (trace) trace.setAttribute("href", "#/runs/" + info.session_id);
      return api("/api/runs/" + encodeURIComponent(info.session_id) +
                 "/tail?offset=" + chat.offset);
    })
    .then(function (tail) {
      if (tail.truncated) { chat.offset = 0; chat.rows = []; return; }
      chat.offset = tail.offset;
      (tail.events || []).forEach(ingestChatEvent);
      renderChatLog();
      // Polling /api/approvals is what refreshes the heartbeat, so it happens every tick
      // whether or not anything is pending.
      return api("/api/approvals?owner=" + encodeURIComponent(chat.id));
    })
    .then(function (payload) {
      if (!payload) return;
      STATE.chat.approvals = payload.approvals || [];
      // Poll comfortably faster than the server's grace window: this request *is* the
      // heartbeat, and missing it denies the user's own pending approval.
      if (typeof payload.grace_seconds === "number" && STATE.poller) {
        STATE.poller.interval = Math.max(
          400, Math.min(CHAT_POLL_MS, (payload.grace_seconds * 1000) / 4)
        );
      }
      renderApprovals();
    })
    .catch(function (err) {
      if (err.status === 401) { stopChat(); render(tokenRequiredPanel()); return; }
      if (err.status === 404) { stopChat(); chatView(); return; }
    });
}

function ingestChatEvent(event) {
  var payload = event.payload || {};
  var chat = STATE.chat;
  if (event.type === "model_response") {
    if (payload.reasoning) appendChatRow("thinking", payload.reasoning);
    if (payload.content) appendChatRow("agent", payload.content);
  } else if (event.type === "tool_call") {
    appendChatRow("tool", payload.name + " " + JSON.stringify(payload.arguments || {}),
                  false, payload.name);
  } else if (event.type === "tool_result") {
    appendChatRow("result", String(payload.content || ""), payload.is_error);
  } else if (event.type === "permission_ask" && payload.approved === false) {
    appendChatRow("denied", (payload.name || "a tool") + " — " + (payload.reason || "denied"));
  } else if (event.type === "user_message" && payload.content) {
    appendChatRow(payload.subagent ? "subagent" : "you", payload.content);
  } else if (event.type === "session_end") {
    appendChatRow("system", "turn ended · " + (payload.reason || "done"));
  }
  chat.rows = chat.rows.slice(-CHAT_MAX_ROWS);
}

function appendChatRow(kind, text, isError, toolName) {
  STATE.chat.rows.push({
    kind: kind, text: String(text), error: !!isError,
    family: toolName ? toolFamily(toolName) : null
  });
}

function renderChatLog() {
  var node = el("chat-log");
  if (!node) return;
  // Whether to stick to the bottom is decided *before* the re-render: afterwards the
  // scrollHeight has already changed, so every comparison says "not at the bottom" and the
  // view stops following a running turn.
  var atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
  if (!STATE.chat.rows.length) {
    // An empty <div> collapses to a hairline, which reads as a broken pane rather than as an
    // empty one. Say what it is instead.
    node.innerHTML =
      '<div class="stat-sub">Nothing yet. Add sources above if you want the answer grounded ' +
      "in a document, then send a message.</div>";
    return;
  }
  node.innerHTML = STATE.chat.rows.map(function (row) {
    return (
      '<div class="chat-row chat-' + row.kind + (row.family ? " f-" + row.family : "") + '">' +
      '<span class="chat-who">' + esc(row.kind) + "</span>" +
      "<pre" + (row.error ? ' class="out-err"' : "") + ">" +
      esc(fmt.truncate(row.text, 4000)) + "</pre></div>"
    );
  }).join("");
  if (atBottom) node.scrollTop = node.scrollHeight;
}

/* An approval card shows the real tool name and arguments when they could be recovered, and
 * the raw screened string when they could not — degraded, never wrong. */
function renderApprovals() {
  var node = el("approvals");
  if (!node) return;
  var pending = STATE.chat.approvals;
  if (!pending.length) { node.innerHTML = ""; return; }
  node.innerHTML = pending.map(function (ask) {
    var body = ask.arguments
      ? '<pre class="args">' + esc(JSON.stringify(ask.arguments, null, 1)) + "</pre>"
      : '<pre class="args">' + esc(ask.action) + "</pre>";
    return (
      '<div class="notice approval">' +
      '<div class="notice-title">Approve <span class="mono">' +
      esc(ask.tool_name || "this tool call") + "</span>?" +
      '<span class="meta"> waiting ' + Math.round((ask.waiting_ms || 0) / 1000) + "s</span>" +
      "</div>" + body +
      '<div class="composer-row">' +
      '<button type="button" class="btn btn-primary" data-approve="' + esc(ask.ask_id) +
      '">Approve</button>' +
      '<button type="button" class="btn" data-deny="' + esc(ask.ask_id) + '">Deny</button>' +
      '<span class="stat-sub">Closing this tab denies it within a few seconds — the agent ' +
      "carries on as if you had said no.</span></div></div>"
    );
  }).join("");
  wireApprovalButtons(node);
}

function wireApprovalButtons(node) {
  var approve = node.querySelectorAll("[data-approve]");
  for (var i = 0; i < approve.length; i++) {
    approve[i].addEventListener("click", function () {
      answerApproval(this.getAttribute("data-approve"), true);
    });
  }
  var deny = node.querySelectorAll("[data-deny]");
  for (var j = 0; j < deny.length; j++) {
    deny[j].addEventListener("click", function () {
      answerApproval(this.getAttribute("data-deny"), false);
    });
  }
}

function answerApproval(askId, approved) {
  api("/api/approvals/" + encodeURIComponent(askId), {
    method: "POST", body: { approved: approved }
  })
    .then(function () { return pollChat(); })
    .catch(function (err) {
      if (err.status === 409 || err.status === 404) {
        // Answered by the timeout, the reaper, or another tab. Not an error worth alarming
        // about — just tell the truth and re-sync.
        toast("That approval had already been answered.");
        return pollChat();
      }
      toast(err.message);
    });
}

function endChat() {
  var chat = STATE.chat;
  if (!chat) return;
  api("/api/chat/" + encodeURIComponent(chat.id), { method: "DELETE" })
    .then(function () {
      stopChat();
      toast("Chat closed and its workspace released.");
      location.hash = "#/runs";
    })
    .catch(function (err) { toast(err.message); });
}

function stopChat() {
  if (STATE.poller) { STATE.poller.stop(); STATE.poller = null; }
  STATE.chat = null;
}
