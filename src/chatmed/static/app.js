// ChatMed demo UI (M5a AC4). Plain JS, no dependencies, no build step.
//
// Privacy (hard rule 1): everything the server returns is kept only in this page's memory.
// Nothing is written to browser storage or cookies, and the server sends
// Cache-Control: no-store. Every server value is rendered with textContent, never as HTML.
"use strict";

// Every status the API can return, and how visibly it is marked. The honesty statuses
// (the user must be told about them) are "warn" or "bad"; a test checks they are all here.
const STATUS = {
  ok: "good", joined: "good", match: "good", verified: "good",
  undeterminable: "warn", conflict: "bad", unverified: "warn", missing: "warn",
  invalid: "bad", unmatched: "bad", ambiguous: "bad", not_attempted: "warn",
  not_comparable: "warn", mismatch: "bad", fallback: "warn",
};

const state = { sessionId: null, busy: false };
const $ = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function badge(status) {
  return el("span", status, `badge ${STATUS[status] || "info"}`);
}

function showError(code) {
  const box = $("error");
  box.textContent = code ? `Error: ${code}` : "";
  box.hidden = !code;
}

async function errorCode(resp) {
  try {
    const body = await resp.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return "invalid_request";
  } catch (_) { /* not JSON */ }
  return `http_${resp.status}`;
}

function refreshButtons() {
  const open = state.sessionId !== null;
  $("create-btn").disabled = open || state.busy;
  $("tenant").disabled = open;
  $("close-btn").disabled = !open;
  $("upload-btn").disabled = !open || state.busy;
  $("ask-btn").disabled = !open || state.busy;
}

function resetSessionView(message) {
  state.sessionId = null;
  $("session-info").textContent = message;
  $("documents").replaceChildren();
  $("progress").replaceChildren();
  $("answer").replaceChildren();
  $("file").value = "";
  refreshButtons();
}

async function createSession(event) {
  event.preventDefault();
  showError(null);
  const resp = await fetch("/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant_id: $("tenant").value.trim() }),
  });
  if (!resp.ok) return showError(await errorCode(resp));
  state.sessionId = (await resp.json()).session_id;
  $("session-info").textContent = `Session open for tenant ${$("tenant").value.trim()}.`;
  refreshButtons();
}

async function closeSession() {
  if (!state.sessionId) return;
  const resp = await fetch(`/sessions/${encodeURIComponent(state.sessionId)}`, { method: "DELETE" });
  if (!resp.ok && resp.status !== 404) return showError(await errorCode(resp));
  resetSessionView("Session closed: documents, facts and history wiped on the server.");
}

function sessionGone(code) {
  if (code === "session_closed" || code === "session_not_found") {
    resetSessionView("Session closed by the server (idle timeout or shutdown).");
  }
}

async function upload(event) {
  event.preventDefault();
  showError(null);
  const file = $("file").files[0];
  if (!file) return;
  const form = new FormData();
  form.append("doc_type", $("doc-type").value.trim());
  form.append("file", file);
  state.busy = true;
  refreshButtons();
  const pending = el("p", `Parsing and extracting ${file.name}…`, "muted");
  $("documents").append(pending);
  try {
    const resp = await fetch(`/sessions/${encodeURIComponent(state.sessionId)}/documents`, {
      method: "POST", body: form,
    });
    pending.remove();
    if (!resp.ok) {
      const code = await errorCode(resp);
      showError(code);
      return sessionGone(code);
    }
    renderDocument(file.name, await resp.json());
    $("file").value = "";
  } finally {
    state.busy = false;
    refreshButtons();
  }
}

function renderDocument(name, doc) {
  const box = el("div", null, "doc");
  box.append(el("strong", name), el("span", ` · ${doc.doc_type} · ${doc.page_count} page(s) · `));
  box.append(el("code", doc.sha256.slice(0, 12)), el("br"));
  box.append(el("span", "Facts: "));
  for (const [status, count] of Object.entries(doc.statuses || {})) {
    box.append(badge(status), el("span", ` ${count}  `));
  }
  if (doc.join_status) box.append(el("span", " · subject join: "), badge(doc.join_status));
  box.append(el("br"), el("span", `Parser: ${doc.parser_id}`, "muted"));
  $("documents").append(box);
}

async function ask(event) {
  event.preventDefault();
  showError(null);
  const question = $("question").value.trim();
  if (!question) return;
  $("progress").replaceChildren();
  $("answer").replaceChildren();
  state.busy = true;
  refreshButtons();
  try {
    const resp = await fetch(`/sessions/${encodeURIComponent(state.sessionId)}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!resp.ok) {
      const code = await errorCode(resp);
      showError(code);
      return sessionGone(code);
    }
    await readNdjson(resp, handleEvent);
  } finally {
    state.busy = false;
    refreshButtons();
  }
}

// Reads an NDJSON stream line by line as it arrives.
async function readNdjson(resp, onEvent) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line));
    }
    if (done) break;
  }
}

function handleEvent(event) {
  const progress = $("progress");
  if (event.event === "started") progress.append(el("li", "Question received"));
  else if (event.event === "tool") progress.append(el("li", `Tool: ${event.name} (step ${event.iteration})`));
  else if (event.event === "answer") { progress.append(el("li", "Answer checked")); renderAnswer(event); }
  else if (event.event === "error") { showError(event.code); sessionGone(event.code); }
}

function renderAnswer(answer) {
  const box = $("answer");
  const outcome = el("p", "Outcome: ", "outcome");
  outcome.append(badge(answer.outcome));
  if (answer.reason) outcome.append(el("span", ` (${answer.reason})`));
  box.append(outcome);
  box.append(el("div", answer.text, `answer-text ${answer.outcome === "verified" ? "" : "fallback"}`));
  if (answer.fingerprint) box.append(el("p", `Fingerprint ${answer.fingerprint}`, "fingerprint"));
  box.append(factsTable(answer.facts_used || []));
}

function factsTable(facts) {
  const table = el("table");
  const head = el("tr");
  for (const title of ["Evidence id", "Source", "Values", "Citation"]) head.append(el("th", title));
  table.append(head);
  if (facts.length === 0) {
    const row = el("tr");
    const cell = el("td", "No facts used.", "muted");
    cell.colSpan = 4;
    row.append(cell);
    table.append(row);
  }
  for (const fact of facts) {
    const row = el("tr");
    const id = el("td");
    id.append(el("code", fact.id));
    row.append(id, el("td", fact.source), valuesCell(fact.values || {}), citationCell(fact.citation || {}));
    table.append(row);
  }
  return table;
}

function valuesCell(values) {
  const cell = el("td");
  for (const [key, value] of Object.entries(values)) {
    const line = el("div");
    line.append(el("span", `${key}: `, "muted"));
    if (typeof value === "string" && value in STATUS) line.append(badge(value));
    else line.append(el("span", value === null ? "—" : typeof value === "object" ? JSON.stringify(value) : value));
    cell.append(line);
  }
  return cell;
}

function citationCell(citation) {
  const cell = el("td");
  if (citation.doc) {
    const page = citation.page === undefined || citation.page === null ? "" : `, page ${citation.page}`;
    cell.append(el("div", `doc ${String(citation.doc).slice(0, 12)}${page}`));
  }
  if (citation.table) cell.append(el("div", `${citation.table} ${citation.key ?? ""}`));
  if (citation.quote) cell.append(el("div", `“${citation.quote}”`, "quote"));
  for (const [key, value] of Object.entries(citation)) {
    if (!["doc", "page", "table", "key", "quote"].includes(key)) {
      cell.append(el("div", `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`, "muted"));
    }
  }
  return cell;
}

document.addEventListener("DOMContentLoaded", () => {
  $("session-form").addEventListener("submit", createSession);
  $("close-btn").addEventListener("click", closeSession);
  $("upload-form").addEventListener("submit", upload);
  $("ask-form").addEventListener("submit", ask);
  refreshButtons();
});
