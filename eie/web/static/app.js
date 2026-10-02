"use strict";

const $view = document.getElementById("view");
let CONFIG = { confidence_high: 85, confidence_medium: 50 };
// Who is signed in. With no logins configured everyone is an admin and types their name into forms.
let ME = { name: "", role: "admin", auth_enabled: false,
  can: { review_all: true, reply: true, forward: true, dismiss: true, fetch: true, admin: true } };
let TOKEN = "";

// ---------- helpers ----------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const label = (s) => String(s ?? "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const KIND = {
  review_form: "Review form", booking_approval: "Booking approval", ccp_review: "CCP review",
  possible_split: "Possible split",
};
const DEST = {
  whatsapp_feedback: "WhatsApp Feedback", escalation_system: "Escalation System",
  indecab: "IndeCab", ccp_queue: "CCP Queue",
};
const ROUTE_TO = { feedback: "WhatsApp Feedback", escalation: "Escalation System", new_booking: "IndeCab" };

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(TOKEN ? { "X-EIE-Token": TOKEN } : {}) };
  const res = await fetch(path, { ...opts, headers });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) showLogin();
  if (!res.ok) {
    const detail = body && body.detail;
    const err = new Error(typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : res.statusText);
    err.status = res.status;
    throw err;
  }
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data ?? {}) });

// A save or reply finishes after the user may have clicked elsewhere; only redraw the page they are still on.
const stillOn = (hash) => location.hash === hash;

function showLogin() {
  if (document.getElementById("login-token")) return;
  $view.innerHTML = `
    <div class="card" style="max-width:420px;margin:48px auto">
      <h2>Sign in</h2>
      <p class="muted">Enter the access token your administrator gave you.</p>
      <div class="field"><input id="login-token" type="password" placeholder="Access token" autocomplete="off"></div>
      <div class="actions"><button class="btn btn-primary" id="login-go">Sign in</button></div>
    </div>`;
  const input = document.getElementById("login-token");
  const go = async () => {
    TOKEN = input.value.trim();
    try {
      await api("/api/me");
      storageSet("eie.token", TOKEN);
      location.reload();
    } catch {
      TOKEN = "";
      toast("That token wasn't recognised", true);
    }
  };
  document.getElementById("login-go").onclick = go;
  input.onkeydown = (ev) => { if (ev.key === "Enter") go(); };
  input.focus();
}

function signOut() { storageSet("eie.token", ""); location.reload(); }

function toast(msg, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), error ? 7000 : 4000);
}

const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};
const tierOf = (n) => (n >= CONFIG.confidence_high ? "high" : n >= CONFIG.confidence_medium ? "medium" : "low");
// A classifier failure proposes no bucket (FRD 13.2), which reads differently from a low-confidence guess.
const catBadge = (c, classifier) => classifier === "error"
  ? `<span class="badge st-rejected" title="The classifier failed; no bucket was proposed">None · system error</span>`
  : (c ? `<span class="badge cat-${esc(c)}">${esc(label(c))}</span>` : `<span class="muted">—</span>`);
const statusBadge = (s) => `<span class="badge st-${esc(s)}">${esc(label(s))}</span>`;
const confCell = (n) => n == null ? `<span class="muted">—</span>` :
  `<span class="conf"><span class="conf-bar"><span class="tier-${tierOf(n)}" style="width:${Number(n)}%"></span></span>${Number(n)}</span>`;
const senderName = (s) => esc(String(s || "").replace(/\s*<[^>]*>\s*/, "") || s);
const verifiedBadge = (v) => v === 1 || v === true ? `<span class="badge st-approved">Verified</span>`
  : v === 0 || v === false ? `<span class="badge st-rejected">Not verified</span>` : `<span class="badge">Unknown</span>`;
const fmtDuration = (sec) => sec == null ? "—" : sec < 90 ? `${sec}s` : sec < 5400 ? `${Math.round(sec / 60)} min`
  : sec < 172800 ? `${(sec / 3600).toFixed(1)} h` : `${(sec / 86400).toFixed(1)} d`;
// The queue statuses of FRD 8.3: Unreviewed / In Review / Actioned / Dismissed
const queueStatus = (r) => r.status === "pending" ? (r.assigned_to ? "In review" : "Unreviewed")
  : r.status === "dismissed" ? "Dismissed" : r.status === "rejected" ? "Rejected" : "Actioned";
const sum = (obj) => Object.values(obj || {}).reduce((a, b) => a + b, 0);

function storageGet(key) { try { return localStorage.getItem(key) || ""; } catch { return ""; } }
function storageSet(key, v) { try { localStorage.setItem(key, v); } catch { /* ignore */ } }

// ---------- router ----------

const routes = [
  [/^#\/dashboard$/, renderDashboard, "Dashboard", "dashboard"],
  [/^#\/emails$/, renderEmails, "Emails", "emails"],
  [/^#\/emails\/(\d+)$/, renderEmail, "Email", "emails"],
  [/^#\/reviews$/, renderReviews, "Review Queue", "reviews"],
  [/^#\/reviews\/(\d+)$/, renderReview, "Review", "reviews"],
  [/^#\/audit$/, renderAudit, "Audit Log", "audit"],
  [/^#\/settings$/, renderSettings, "Settings", "settings"],
];

async function router() {
  const hash = location.hash || "#/dashboard";
  for (const [re, fn, title, nav] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    document.getElementById("page-title").textContent = title;
    document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
    $view.innerHTML = `<div class="empty">Loading…</div>`;
    try {
      await fn(...m.slice(1));
    } catch (e) {
      if (e.status !== 401) $view.innerHTML = `<div class="notice bad">${esc(e.message)}</div>`;
    }
    refreshBadge();
    return;
  }
  location.hash = "#/dashboard";
}

async function refreshBadge() {
  try {
    const s = await api("/api/summary");
    const n = sum(s.pending_reviews);
    const badge = document.getElementById("review-badge");
    badge.hidden = n === 0;
    badge.textContent = n;
  } catch { /* ignore */ }
}

// ---------- dashboard ----------

let kpiPeriod = "all";
async function renderDashboard() {
  const [s, audit, k] = await Promise.all([
    api("/api/summary"), api("/api/audit?limit=12"), api(`/api/kpis?period=${kpiPeriod}`)]);

  if (s.total_emails === 0) {
    $view.innerHTML = `
      <div class="card" style="text-align:center;padding:48px 24px">
        <h2 style="font-size:18px">No emails processed yet</h2>
        <p class="muted">Click <b>Load demo emails</b> to run the 5 sample emails through the engine,
        or <b>Fetch mailbox</b> once Outlook credentials are set in <code>.env</code>.</p>
      </div>`;
    return;
  }

  const cats = ["feedback", "escalation", "new_booking", "unclassified"];
  const maxCat = Math.max(1, ...cats.map((c) => s.by_category[c] || 0));
  const bars = cats.map((c) => {
    const n = s.by_category[c] || 0;
    return `<div class="bar-row"><span>${esc(label(c))}</span>
      <div class="bar-track"><div class="bar-fill cat-${c}" style="width:${(n / maxCat) * 100}%"></div></div>
      <span class="n">${n}</span></div>`;
  }).join("");

  const kinds = Object.keys(KIND).map((k) => `
    <tr class="clickable" onclick="location.hash='#/reviews'">
      <td>${esc(KIND[k])}</td><td class="nowrap" style="text-align:right"><b>${s.pending_reviews[k] || 0}</b></td>
    </tr>`).join("");

  $view.innerHTML = `
    <div class="toolbar">
      <label class="muted" for="kpi-period">Period</label>
      <select id="kpi-period" title="Days are counted in ${esc(CONFIG.timezone || "UTC")}">
        ${[["all", "All time"], ["today", "Today"], ["7", "Last 7 days"], ["30", "Last 30 days"]].map(([v, t]) =>
          `<option value="${v}" ${kpiPeriod === v ? "selected" : ""}>${t}</option>`).join("")}
      </select>
    </div>
    <div class="grid-stats">
      ${statTile("Total ingested", k.total_ingested, "emails in the period")}
      ${statTile("Auto-routed", k.auto_routed, "created without a human")}
      ${statTile("Pending confirmation", k.pending_confirmation, "quick-create forms + New Bookings")}
      ${statTile("Unclassified / unreviewed", k.unclassified_unreviewed, "waiting in the CCP queue")}
      ${statTile("Avg. time to action", fmtDuration(k.avg_time_to_action_seconds), "ingestion to record or dismissal")}
      ${statTile("Reply rate", k.reply_rate == null ? "—" : `${k.reply_rate}%`, "ingested emails with a reply")}
    </div>
    <div class="two-col">
      <div class="col">
        <div class="card"><h2>Emails by category</h2><div class="bars">${bars}</div></div>
        <div class="card"><div class="card-head"><h2>Pending reviews</h2><a href="#/reviews">Open queue →</a></div>
          <table><tbody>${kinds}</tbody></table></div>
      </div>
      <div class="card"><div class="card-head"><h2>Recent activity</h2><a href="#/audit">Full audit log →</a></div>
        ${timeline(audit.slice().reverse(), true)}</div>
    </div>`;
  document.getElementById("kpi-period").onchange = (ev) => { kpiPeriod = ev.target.value; renderDashboard(); };
}

const statTile = (lbl, value, hint) =>
  `<div class="card stat"><div class="label">${esc(lbl)}</div><div class="value">${esc(value)}</div><div class="hint">${esc(hint)}</div></div>`;

function timeline(entries, withEmailLink = false) {
  if (!entries.length) return `<div class="empty">No activity yet.</div>`;
  return `<ul class="timeline">${entries.map((a) => `
    <li><div>
      <div class="ev">${esc(label(a.event.replace(/\./g, " ")))}</div>
      <div class="ev-meta">${fmtTime(a.ts)} · ${esc(a.actor)}${withEmailLink && a.email_id ?
        ` · <a href="#/emails/${a.email_id}">email #${a.email_id}</a>` : ""}</div>
      ${eventSummary(a)}
    </div></li>`).join("")}</ul>`;
}

function eventSummary(a) {
  const d = a.details || {};
  const parts = {
    "email.ingested": () => `${d.subject || ""} — ${d.sender || ""}`,
    "email.classified": () => `${label(d.category)} · confidence ${d.confidence}`,
    "routing.decided": () => `${label(d.action)} → ${DEST[d.destination] || d.destination}. ${d.reason || ""}`,
    "destination.created": () => `${DEST[d.destination] || d.destination} ref ${d.external_ref}`,
    "review.queued": () => `${KIND[d.kind] || d.kind}: ${d.reason || ""}`,
    "review.approved": () => `Sent to ${DEST[d.destination] || d.destination} (ref ${d.external_ref})${d.notes ? ` — “${d.notes}”` : ""}`,
    "thread.appended": () => `Appended to the thread's existing record (email #${d.parent_email_id})`,
    "thread.message_appended": () => `Follow-up email #${d.child_email_id} appended to this record`,
    "thread.split_from": () => `${d.possible ? "Possible split" : "Split"} from email #${d.parent_email_id}`,
    "thread.split_off": () => `${d.possible ? "Possible related item" : "Related item"} split off: email #${d.child_email_id}`,
    "thread.linked": () => `Attached to ticket ${d.ticket_ref}`,
    "requester.checked": () => d.verified === true ? `Verified as ${d.role} on ${d.booking_id}`
      : d.verified === false ? d.reason : `Not checked: ${d.reason}`,
    "review.claimed": () => `Assigned to ${a.actor}`,
    "review.dismissed": () => `Dismissed: “${d.reason}”`,
    "review.forwarded": () => `Forwarded to ${d.team} (${d.to})${d.dry_run ? " — dry run, nothing sent" : ""}`,
    "forward.failed": () => `Forward to ${d.team} failed: ${d.error}`,
    "notification.sent": () => `Notified ${(d.recipients || []).join(", ")}${d.dry_run ? " (dry run)" : ""}`,
    "notification.failed": () => `Couldn't notify ${(d.recipients || []).join(", ")}: ${d.error}`,
    "ingestion.failed": () => `${d.title}: ${d.error}`,
    "contacts.loaded": () => `${d.count} booking contact(s) loaded`,
    "destination.retry": () => `Attempt ${d.attempt} failed, retrying: ${d.error}`,
    "destination.blocked": () => `Blocked, missing: ${(d.missing || []).join(", ")}`,
    "review.rejected": () => d.notes ? `“${d.notes}”` : "",
    "review.resolved": () => d.notes ? `Closed: “${d.notes}”` : "Closed without routing",
    "fetch.completed": () => `${d.count} unread email(s)`,
    "reply.sent": () => `${d.dry_run ? "Recorded (dry run)" : "Sent"} to ${d.to}${d.edited ? " — edited from the AI draft" : ""}`,
    "reply.failed": () => `To ${d.to}: ${d.error}`,
  }[a.event];
  const text = parts ? parts() : (d.error || "");
  return text ? `<pre>${esc(text)}</pre>` : "";
}

// ---------- emails ----------

const emailFilters = { q: "", category: "", status: "", source: "", tier: "", group: "", from: "", to: "", assignee: "" };
const SOURCE = { graph: "Mailbox (Graph)", imap: "Mailbox (IMAP)", file: "Demo sample" };
const GROUPS = { auto_routed: "Auto-routed", pending: "Pending confirmation", unclassified: "Unclassified", actioned: "Actioned" };

async function renderEmails() {
  const f = emailFilters;
  const qs = new URLSearchParams();
  for (const [key, param] of [["category", "category"], ["tier", "tier"], ["group", "status_group"],
                              ["from", "date_from"], ["to", "date_to"], ["assignee", "assignee"]]) {
    if (f[key]) qs.set(param, f[key]);
  }
  const emails = await api(`/api/emails?${qs}`);
  const statuses = [...new Set(emails.map((e) => e.status))];
  const opt = (value, current, text) => `<option value="${value}" ${current === value ? "selected" : ""}>${text}</option>`;

  $view.innerHTML = `
    <div class="toolbar">
      <input type="search" id="f-q" placeholder="Search subject or sender…" value="${esc(f.q)}">
      <select id="f-source"><option value="">All sources</option>
        ${opt("mailbox", f.source, "Mailbox only")}${opt("file", f.source, "Demo samples only")}
      </select>
      <select id="f-cat"><option value="">All buckets</option>
        ${["feedback", "escalation", "new_booking", "unclassified"].map((c) => opt(c, f.category, label(c))).join("")}
      </select>
      <select id="f-tier"><option value="">All confidence tiers</option>
        ${["high", "medium", "low"].map((t) => opt(t, f.tier, `${label(t)} confidence`)).join("")}
      </select>
      <select id="f-group"><option value="">Any outcome</option>
        ${Object.entries(GROUPS).map(([g, t]) => opt(g, f.group, t)).join("")}
      </select>
      <select id="f-status"><option value="">All statuses</option>
        ${statuses.map((st) => opt(st, f.status, esc(label(st)))).join("")}
      </select>
      <label class="muted" for="f-from">Ingested from</label>
      <input type="date" id="f-from" value="${esc(f.from)}" title="Ingested from">
      <label class="muted" for="f-to">to</label>
      <input type="date" id="f-to" value="${esc(f.to)}" title="Ingested up to">
      <input type="text" id="f-assignee" placeholder="Assigned reviewer" value="${esc(f.assignee)}" style="width:150px">
    </div>
    <div class="card table-wrap"><table>
      <thead><tr><th>#</th><th>Received</th><th>Source</th><th>Email</th><th>Category</th><th>Confidence</th><th>Requester</th><th>Routing</th><th>Assigned</th><th>Status</th></tr></thead>
      <tbody id="email-rows"></tbody>
    </table></div>`;

  const sourceMatches = (e) => !f.source || (f.source === "mailbox" ? e.source !== "file" : e.source === f.source);

  const draw = () => {
    const q = f.q.toLowerCase();
    const rows = emails.filter((e) => sourceMatches(e) && (!f.status || e.status === f.status) &&
      (!q || `${e.subject} ${e.sender}`.toLowerCase().includes(q)));
    document.getElementById("email-rows").innerHTML = rows.length ? rows.map((e) => `
      <tr class="clickable" onclick="location.hash='#/emails/${e.id}'">
        <td class="muted">${e.id}</td>
        <td class="nowrap muted">${fmtTime(e.received_at)}</td>
        <td class="nowrap"><span class="badge">${esc(SOURCE[e.source] || e.source)}</span></td>
        <td class="subject">
          <div class="cell-main" title="${esc(e.subject)}">${esc(e.subject || "(no subject)")}</div>
          <div class="cell-sub" title="${esc(e.sender)}">${senderName(e.sender)}</div>
        </td>
        <td>${catBadge(e.category, e.classifier)}</td>
        <td>${confCell(e.confidence)}</td>
        <td>${verifiedBadge(e.requester_verified)}</td>
        <td class="nowrap">
          <div>${esc(DEST[e.destination] || "—")}</div>
          <div class="cell-sub">${esc(label(e.action))}</div>
        </td>
        <td class="nowrap">${e.assigned_to ? esc(e.assigned_to) : '<span class="muted">—</span>'}</td>
        <td>${statusBadge(e.status)}</td>
      </tr>`).join("") : `<tr><td colspan="10" class="empty">No emails match.</td></tr>`;
  };
  draw();
  // Search, source and status filter the loaded rows; the rest ask the server.
  document.getElementById("f-q").oninput = (ev) => { f.q = ev.target.value; draw(); };
  document.getElementById("f-source").onchange = (ev) => { f.source = ev.target.value; draw(); };
  document.getElementById("f-status").onchange = (ev) => { f.status = ev.target.value; draw(); };
  for (const [id, key] of [["f-cat", "category"], ["f-tier", "tier"], ["f-group", "group"],
                           ["f-from", "from"], ["f-to", "to"], ["f-assignee", "assignee"]]) {
    document.getElementById(id).onchange = (ev) => { f[key] = ev.target.value.trim(); renderEmails(); };
  }
}

async function renderEmail(id) {
  const { email: e, reviews, replies, audit, thread } = await api(`/api/emails/${id}`);
  const classified = audit.find((a) => a.event === "email.classified");
  const decided = audit.find((a) => a.event === "routing.decided");

  $view.innerHTML = `
    <a class="back" href="#/emails">← All emails</a>
    <div class="detail-head">
      <h2>${esc(e.subject || "(no subject)")}</h2>
      <div class="meta"><span>${esc(e.sender)}</span><span>${fmtTime(e.received_at)}</span>
        <span>via ${esc(e.source)}</span>${statusBadge(e.status)}</div>
    </div>
    <div class="two-col">
      <div class="col">
        <div class="card"><div class="card-head"><h2>Email</h2>${bodyToggle(e)}</div>
          <div id="body-view" class="email-body">${esc(e.body)}</div>${attachmentsHtml(e)}</div>
        <div class="card">${replyCard(e, replies)}</div>
        <div class="card"><h2>Audit trail</h2>${timeline(audit)}</div>
      </div>
      <div class="col">
        <div class="card"><h2>Classification</h2>
          <dl class="kv">
            <dt>Category</dt><dd>${catBadge(e.category, e.classifier)}</dd>
            <dt>Confidence</dt><dd>${confCell(e.confidence)} <span class="muted">(${esc(e.tier || "")})</span></dd>
            <dt>Classifier</dt><dd>${esc(label(e.classifier))}</dd>
            <dt>Requester</dt><dd>${verifiedBadge(e.requester_verified)} <span class="muted">${esc(requesterNote(e))}</span></dd>
            <dt>Routing</dt><dd>${esc(label(e.action))} → ${esc(DEST[e.destination] || e.destination)}</dd>
            ${decided ? `<dt>Why</dt><dd>${esc(decided.details.reason)}</dd>` : ""}
            ${classified ? `<dt>Reasoning</dt><dd>${esc(classified.details.reasoning)}</dd>` : ""}
            ${e.external_ref ? `<dt>External ref</dt><dd><code>${esc(e.external_ref)}</code></dd>` : ""}
          </dl>
        </div>
        ${threadCard(e, thread)}
        <div class="card"><h2>Extracted information</h2>${extractedHtml(e.extracted)}</div>
        ${reviews.length ? `<div class="card"><h2>Review items</h2><table><tbody>${reviews.map((r) => `
          <tr class="clickable" onclick="location.hash='#/reviews/${r.id}'">
            <td>#${r.id}</td><td>${esc(KIND[r.kind] || r.kind)}</td><td>${statusBadge(r.status)}</td>
          </tr>`).join("")}</tbody></table></div>` : ""}
      </div>
    </div>`;
  wireBody(e);
  wireReply(e, replies, () => stillOn(`#/emails/${id}`) && renderEmail(id));
}

const addressOf = (s) => (String(s || "").match(/<([^>]+)>/) || [, String(s || "")])[1].trim();
const reSubject = (s) => (/^re:/i.test(s || "") ? s : `Re: ${s || ""}`).trim();
const REPLY_STATUS = { sent: "Sent", dry_run: "Recorded (dry run)", failed: "Failed" };

function requesterNote(e) {
  try {
    const d = JSON.parse(e.requester_detail || "{}");
    return d.role ? `${d.name || ""} (${d.role} on ${d.booking_id})` : d.reason || "";
  } catch { return ""; }
}

const bodyToggle = (e) => e.body_html
  ? `<button class="btn btn-sm" id="body-toggle" type="button" title="Shows the email as it was sent. Scripts, remote images and links are switched off.">Show original formatting</button>`
  : "";

// The HTML comes from outside the company: it is shown in a sandboxed frame with no scripts, no network and no links.
function wireBody(e) {
  const btn = document.getElementById("body-toggle");
  if (!btn) return;
  const view = document.getElementById("body-view");
  let formatted = false;
  btn.onclick = () => {
    formatted = !formatted;
    btn.textContent = formatted ? "Show plain text" : "Show original formatting";
    if (!formatted) { view.className = "email-body"; view.textContent = e.body; return; }
    const frame = document.createElement("iframe");
    frame.className = "email-html";
    frame.setAttribute("sandbox", "");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">${e.body_html}`;
    view.className = "";
    view.replaceChildren(frame);
  };
}

function attachmentsHtml(e) {
  let list = [];
  try { list = JSON.parse(e.attachments_json || "[]"); } catch { /* ignore */ }
  if (!list.length) return "";
  const kb = (n) => (n == null ? "" : n < 1024 ? `${n} B` : `${Math.round(n / 1024)} KB`);
  return `<div class="meta" style="margin-top:12px"><b>Attachments</b>${list.map((a) =>
    `<span class="badge" title="${esc(a.text ? `Text read for classification:\n${a.text.slice(0, 300)}` : a.content_type)}">${esc(a.name || "(unnamed)")} ${esc(kb(a.size))}${a.text ? " · text read" : ""}</span>`).join("")}</div>`;
}

const THREAD_ROLE = {
  appended: "Appended to the thread's existing record", split: "Split off from this thread",
  possible_split: "Possible split: waiting for a reviewer", linked: "Attached to another ticket",
  related: "Related message in the thread (not a record)",
};

function threadCard(e, thread) {
  if (!thread || (!thread.parent && !thread.children.length)) return "";
  const link = (m) => `<a href="#/emails/${m.id}">#${m.id} ${esc(m.subject || "(no subject)")}</a>
    ${m.category ? catBadge(m.category) : ""} ${m.status ? statusBadge(m.status) : ""}`;
  return `<div class="card"><h2>Thread</h2><dl class="kv">
    ${thread.parent ? `<dt>${esc(THREAD_ROLE[e.thread_role] || "Earlier in thread")}</dt><dd>${link(thread.parent)}</dd>` : ""}
    ${thread.children.length ? `<dt>Later in thread</dt><dd>${thread.children.map((c) =>
      `<div>${link(c)} <span class="muted">${esc(label(c.thread_role))}</span></div>`).join("")}</dd>` : ""}
  </dl></div>`;
}

function replyCard(e, replies = []) {
  if (!ME.can.reply) {
    return `<div class="card-head"><h2>Reply</h2></div><div class="notice">Your role can't send replies.</div>`;
  }
  const history = replies.length ? `<ul class="timeline" style="margin-top:14px">${replies.slice().reverse().map((r) => `
    <li><div>
      <div class="ev">${esc(REPLY_STATUS[r.status] || r.status)} to ${esc(r.to_addr)}${r.edited ? " · edited" : ""}</div>
      <div class="ev-meta">${fmtTime(r.created_at)} · ${esc(r.sent_by)}${r.transport ? ` · ${esc(r.transport)}` : ""}</div>
      ${r.error ? `<pre>${esc(r.error)}</pre>` : ""}
    </div></li>`).join("")}</ul>` : "";
  const sent = replies.some((r) => r.status !== "failed");
  return `<div class="card-head"><h2>Reply</h2>
      <span><button class="btn btn-sm" id="rp-reset" type="button">Reset to draft</button>
      <button class="btn btn-sm" data-copy type="button">Copy</button></span></div>
    ${sent ? `<div class="notice ok">A reply has already been sent for this email.</div>` : ""}
    <div class="form-grid">
      <div class="field"><label>To</label><input id="rp-to" value="${esc(addressOf(e.sender))}"></div>
      <div class="field"><label>Subject</label><input id="rp-subject" value="${esc(reSubject(e.subject))}"></div>
      <div class="field"><label>Message (edit before sending)</label>
        <textarea id="rp-body" rows="9">${esc(e.suggested_reply || "")}</textarea></div>
      ${ME.auth_enabled ? "" : `<div class="field"><label>Sent by</label>
        <input id="rp-user" value="${esc(storageGet("eie.reviewer"))}" placeholder="Your name"></div>`}
    </div>
    <div class="actions"><button class="btn btn-success" id="rp-send" type="button">${sent ? "Send another reply" : "Send reply"}</button></div>
    ${history}`;
}

function wireReply(e, replies, rerender) {
  const body = document.getElementById("rp-body");
  if (!body) return;
  const draft = e.suggested_reply || "";
  document.getElementById("rp-reset").onclick = () => { body.value = draft; };
  document.querySelector("[data-copy]").onclick = async () => {
    try {
      await navigator.clipboard.writeText(body.value);
      toast("Reply copied to clipboard");
    } catch { toast("Couldn't access the clipboard", true); }
  };
  const sendBtn = document.getElementById("rp-send");
  const already = replies.some((r) => r.status !== "failed");
  sendBtn.onclick = async () => {
    const user = ME.auth_enabled ? ME.name : document.getElementById("rp-user").value.trim();
    if (!user) return toast("Enter your name as the sender", true);
    if (!body.value.trim()) return toast("The reply is empty", true);
    const to = document.getElementById("rp-to").value.trim();
    if (!confirm(`Send this reply to ${to}?${CONFIG.dry_run ? "\n\n(Dry run: it will be recorded, not emailed.)" : ""}`)) return;
    storageSet("eie.reviewer", user);
    sendBtn.disabled = true;
    try {
      const res = await post(`/api/emails/${e.id}/reply`, {
        user, body: body.value, to, subject: document.getElementById("rp-subject").value, resend: already,
      });
      toast(res.status === "dry_run" ? "Reply recorded (dry run, nothing emailed)" : `Reply sent to ${res.to}`);
      await rerender();
    } catch (err) {
      toast(err.message, true);
      sendBtn.disabled = false;
    }
  };
}

function extractedHtml(x) {
  if (!x) return `<div class="muted">Nothing extracted.</div>`;
  const rows = [];
  const add = (k, v) => { if (v != null && v !== "" && !(Array.isArray(v) && !v.length)) rows.push([k, v]); };
  add("Booking IDs", (x.booking_ids || []).map((b) => `<code>${esc(b)}</code>`).join(" "));
  add("Summary", esc(x.summary));
  add("Company", esc(x.company));
  add("Sentiment", esc(label(x.sentiment)));
  add("Urgency", esc(label(x.urgency)));
  add("Passengers", (x.passengers || []).map((p) =>
    esc([p.name, p.phone, p.email].filter(Boolean).join(" · "))).filter(Boolean).join("<br>"));
  if (x.trip) {
    for (const [k, v] of Object.entries(x.trip)) add(label(k), esc(v));
  }
  if (!rows.length) return `<div class="muted">Nothing extracted.</div>`;
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

// ---------- review queue ----------

const reviewFilters = { status: "pending", kind: "" };

async function renderReviews() {
  const qs = new URLSearchParams({ status: reviewFilters.status });
  if (reviewFilters.kind) qs.set("kind", reviewFilters.kind);
  const items = await api(`/api/reviews?${qs}`);

  $view.innerHTML = `
    <div class="toolbar">
      <div class="tabs" id="status-tabs">
        ${["pending", "approved", "rejected", "resolved", "dismissed", "all"].map((s) =>
          `<button data-s="${s}" class="${reviewFilters.status === s ? "active" : ""}">${label(s)}</button>`).join("")}
      </div>
      <select id="f-kind"><option value="">All types</option>
        ${Object.entries(KIND).map(([k, v]) => `<option value="${k}" ${reviewFilters.kind === k ? "selected" : ""}>${v}</option>`).join("")}
      </select>
    </div>
    <div class="card table-wrap"><table>
      <thead><tr><th>#</th><th>Type</th><th>Email</th><th>Category</th><th>Confidence</th><th>Requester</th><th>Destination</th><th>Status</th><th>Assigned to</th><th>Created</th></tr></thead>
      <tbody>${items.length ? items.map((r) => `
        <tr class="clickable" onclick="location.hash='#/reviews/${r.id}'">
          <td class="muted">${r.id}</td>
          <td class="nowrap">${esc(KIND[r.kind] || r.kind)}</td>
          <td class="subject">
            <div class="cell-main" title="${esc(r.subject)}">${esc(r.subject)}</div>
            <div class="cell-sub" title="${esc(r.reason)}">${r.system_error ? "<b>System error.</b> " : ""}${esc(r.reason)}</div>
          </td>
          <td>${catBadge(r.category, r.classifier)}</td>
          <td>${confCell(r.confidence)}</td>
          <td>${verifiedBadge(r.requester_verified)}</td>
          <td class="nowrap">${esc(DEST[r.destination] || r.destination)}</td>
          <td><span class="badge st-${esc(r.status)}">${esc(queueStatus(r))}</span></td>
          <td class="nowrap">${r.assigned_to ? esc(r.assigned_to) : '<span class="muted">—</span>'}</td>
          <td class="nowrap muted">${fmtTime(r.created_at)}</td>
        </tr>`).join("") : `<tr><td colspan="10" class="empty">Nothing here. 🎉</td></tr>`}</tbody>
    </table></div>`;

  document.querySelectorAll("#status-tabs button").forEach((b) =>
    b.onclick = () => { reviewFilters.status = b.dataset.s; renderReviews(); });
  document.getElementById("f-kind").onchange = (ev) => { reviewFilters.kind = ev.target.value; renderReviews(); };
}

async function renderReview(id) {
  const { review: r, email: e, replies, teams } = await api(`/api/reviews/${id}`);
  const pending = r.status === "pending";
  const baseline = JSON.stringify(r.payload);
  let original = baseline;
  let payload = JSON.parse(baseline);
  const isSplit = r.kind === "possible_split";
  const isCcp = r.kind === "ccp_review" || isSplit;
  let ackGaps = false;
  let gapsCache = null;   // what's missing, as last reported by the server (null until it answers)
  let gapsTicket = 0;

  const categoryOptions = isCcp
    ? `<option value="">— Choose where to route —</option>
       ${Object.entries(ROUTE_TO).map(([c, d]) => `<option value="${c}">${label(c)} → ${d}</option>`).join("")}
       <option value="unclassified">Close without routing</option>`
    : `<option value="">Keep: ${esc(label(e.category))} → ${esc(DEST[r.destination] || r.destination)}</option>
       ${Object.entries(ROUTE_TO).filter(([c]) => c !== e.category)
         .map(([c, d]) => `<option value="${c}">Re-route: ${label(c)} → ${d}</option>`).join("")}`;

  const outcome = pending ? "" : `
    <div class="notice ${r.status === "rejected" ? "bad" : "ok"}">
      <b>${esc(label(r.status))}</b> by ${esc(r.reviewer)} on ${fmtTime(r.resolved_at)}
      ${r.external_ref ? ` · sent to ${esc(DEST[r.destination] || r.destination)} (ref <code>${esc(r.external_ref)}</code>)` : ""}
      ${r.notes ? `<br>Notes: ${esc(r.notes)}` : ""}
    </div>`;

  const resolutionBlock = pending && isSplit ? `
    <div class="field"><label>What should happen to this message?</label>
      <label class="radio-row"><input type="radio" name="rv-res" value="split" checked>
        <span>Split to a new record (choose the destination below)</span></label>
      <label class="radio-row"><input type="radio" name="rv-res" value="keep">
        <span>Keep with the thread's existing record (append, no new record)</span></label>
      <label class="radio-row"><input type="radio" name="rv-res" value="link">
        <span>Attach to a different existing ticket</span></label>
      <input id="rv-linkref" placeholder="Ticket reference" hidden>
    </div>` : "";

  $view.innerHTML = `
    <a class="back" href="#/reviews">← Review queue</a>
    <div class="detail-head">
      <h2>${esc(KIND[r.kind] || r.kind)} #${r.id}</h2>
      <div class="meta"><span>${esc(e.subject)}</span><span>${catBadge(e.category, e.classifier)} ${confCell(e.confidence)}</span>
        ${verifiedBadge(e.requester_verified)}
        ${r.assigned_to ? `<span>Assigned to ${esc(r.assigned_to)}</span>` : ""}
        <a href="#/emails/${e.id}">View email #${e.id} →</a></div>
    </div>
    <div class="two-col">
      <div class="card">
        ${outcome}
        ${pending && r.system_error ? `<div class="notice bad"><b>System error.</b> The classifier failed on this email, so no bucket
          was proposed. Choose where it belongs below; the form is pre-filled with whatever the keyword rules could read.
          <br><span style="font-size:12px">${esc(r.reason)}</span></div>`
          : pending ? `<div class="notice">${esc(r.reason)}</div>` : ""}
        <div class="form-grid">
          ${resolutionBlock}
          <div class="field" id="rv-route"><label>${isCcp ? "Route to" : "Destination"}</label>
            <select id="rv-category" ${pending ? "" : "disabled"}>${categoryOptions}</select></div>
          <div id="rv-gaps"></div>
          <div class="card-head" style="margin:6px 0 0"><h2>${isCcp ? "Details for the reviewer" : "Pre-filled form"}</h2>
            <span class="muted" style="font-size:12px">${pending ? "Edit anything before approving" : ""}</span></div>
          <div id="rv-form" class="form-grid"></div>
          ${pending ? `
          ${ME.auth_enabled ? "" : `<div class="field"><label>Reviewer name</label><input id="rv-reviewer" value="${esc(storageGet("eie.reviewer"))}" placeholder="Your name"></div>`}
          <div class="field"><label>Notes (required to dismiss)</label><textarea id="rv-notes" rows="2"></textarea></div>` : ""}
        </div>
        ${pending ? `<div class="queue-actions">
          <button class="btn btn-sm" id="rv-claim">${r.assigned_to ? "Take over" : "Assign to me"}</button>
          ${ME.can.dismiss ? `<button class="btn btn-sm" id="rv-dismiss" title="Close with no action; the notes are the reason">Dismiss</button>` : ""}
          ${isCcp && ME.can.forward && (teams || []).length ? `
            <select id="rv-team">${teams.map((t) => `<option>${esc(t)}</option>`).join("")}</select>
            <button class="btn btn-sm" id="rv-forward" title="Send this email to the team; no record is created">Forward to team</button>` : ""}
        </div>
        <div class="actions">
          <button class="btn btn-danger" id="rv-reject">Reject</button>
          <button class="btn btn-success" id="rv-approve">Approve</button>
        </div>` : ""}
      </div>
      <div class="col">
        <div class="card"><div class="card-head"><h2>Original email</h2>${bodyToggle(e)}</div>
          <div class="meta" style="margin-bottom:8px"><span>${esc(e.sender)}</span><span>${fmtTime(e.received_at)}</span></div>
          <div id="body-view" class="email-body">${esc(e.body)}</div>${attachmentsHtml(e)}</div>
        <div class="card" id="reply-card">${replyCard(e, replies)}</div>
      </div>
    </div>`;

  const formEl = document.getElementById("rv-form");
  buildForm(payload, formEl, !pending);
  wireBody(e);
  wireReply(e, replies, () => stillOn(`#/reviews/${id}`) && renderReview(id));
  if (!pending) return;

  const approveBtn = document.getElementById("rv-approve");
  const categorySel = document.getElementById("rv-category");
  const gapsEl = document.getElementById("rv-gaps");
  const resolution = () => (isSplit ? document.querySelector("input[name=rv-res]:checked").value : "");
  const destination = () => (categorySel.value && categorySel.value !== "unclassified"
    ? { feedback: "whatsapp_feedback", escalation: "escalation_system", new_booking: "indecab" }[categorySel.value]
    : isCcp ? "" : r.destination);
  const currentGaps = () => gapsCache;
  const showGaps = () => destination() === "indecab" && resolution() !== "keep" && resolution() !== "link";

  const drawGaps = () => {
    const g = currentGaps();
    if (!showGaps() || !g) { gapsEl.innerHTML = ""; return; }
    const list = (items) => `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
    gapsEl.innerHTML = [
      g.essential.length ? `<div class="notice bad"><b>Required before this can be pushed to IndeCab:</b>${list(g.essential)}</div>` : "",
      g.mandatory.length ? `<div class="notice warn"><b>Mandatory fields missing</b> (you can still push, marked as incomplete):${list(g.mandatory)}
        <label class="radio-row"><input type="checkbox" id="rv-ack" ${ackGaps ? "checked" : ""}> <span>Push with these gaps</span></label></div>` : "",
      g.semi_mandatory.length ? `<div class="notice">Nice to have: ${esc(g.semi_mandatory.join(", "))}</div>` : "",
      (g.essential.length || g.mandatory.length || g.semi_mandatory.length)
        ? `<div><button class="btn btn-sm" id="rv-missing" type="button">Request missing details</button></div>` : "",
    ].join("");
    const ack = document.getElementById("rv-ack");
    if (ack) ack.onchange = () => { ackGaps = ack.checked; syncApprove(); };
    const missingBtn = document.getElementById("rv-missing");
    if (missingBtn) missingBtn.onclick = async () => {
      try {
        const draft = await post(`/api/reviews/${r.id}/missing-details`, { payload });
        document.getElementById("rp-body").value = draft.body;
        document.getElementById("reply-card").scrollIntoView({ behavior: "smooth" });
        toast("Reply drafted: review it, then send");
      } catch (err) { toast(err.message, true); }
    };
  };

  const syncApprove = () => {
    const v = categorySel.value;
    const res = resolution();
    document.getElementById("rv-route").hidden = res === "keep" || res === "link";
    const link = document.getElementById("rv-linkref");
    if (link) link.hidden = res !== "link";
    let text, disabled = false;
    if (res === "keep") text = "Keep with existing record";
    else if (res === "link") { text = "Attach to ticket"; disabled = !link.value.trim(); }
    else {
      text = v === "unclassified" ? "Close item" :
        v ? `Approve → ${ROUTE_TO[v]}` : isCcp ? "Choose a destination" : `Approve → ${DEST[r.destination] || r.destination}`;
      disabled = isCcp && !v;
      if (showGaps()) {
        const g = currentGaps();
        if (!g) disabled = true;  // the server hasn't said what's missing yet
        else {
          if (g.essential.length || (g.mandatory.length && !ackGaps)) disabled = true;
          if (g.mandatory.length && ackGaps) text += " (with gaps)";
        }
      }
    }
    approveBtn.textContent = text;
    approveBtn.disabled = disabled;
  };

  // Asks the server which IndeCab fields are empty (it also splits a combined "25 Sept at 7am").
  const refreshGaps = async () => {
    const ticket = ++gapsTicket;
    if (destination() === "indecab") {
      try {
        const g = await post("/api/gaps", { payload });
        if (ticket !== gapsTicket) return;  // a newer edit is already on its way
        gapsCache = g;
      } catch (err) { toast(err.message, true); }
    }
    drawGaps();
    syncApprove();
  };

  const loadPayload = (obj) => {
    payload = obj;
    original = JSON.stringify(obj);
    formEl.replaceChildren();
    buildForm(payload, formEl, false);
    gapsCache = null;
    drawGaps();
    syncApprove();
    refreshGaps();
  };

  // Re-routing swaps in the form for the new destination, built from what was extracted.
  categorySel.onchange = async () => {
    const v = categorySel.value;
    if (!v || v === "unclassified") return loadPayload(JSON.parse(baseline));
    try { loadPayload((await api(`/api/reviews/${r.id}/preview?category=${v}`)).payload); }
    catch (err) { toast(err.message, true); }
  };
  document.querySelectorAll("input[name=rv-res]").forEach((el) => el.onchange = () => { refreshGaps(); });
  const linkEl = document.getElementById("rv-linkref");
  if (linkEl) linkEl.oninput = syncApprove;
  // Typing in the form (or adding/removing a row) can fill or empty a field.
  let typing = null;
  const refresh = () => { clearTimeout(typing); typing = setTimeout(refreshGaps, 250); };
  formEl.addEventListener("input", refresh);
  formEl.addEventListener("click", refresh);
  drawGaps();
  syncApprove();
  refreshGaps();

  const reviewer = () => {
    const name = ME.auth_enabled ? ME.name : document.getElementById("rv-reviewer").value.trim();
    if (!name) { toast("Enter your name as the reviewer", true); return null; }
    storageSet("eie.reviewer", name);
    return name;
  };
  const notes = () => document.getElementById("rv-notes").value.trim();

  approveBtn.onclick = async () => {
    const name = reviewer();
    if (!name) return;
    approveBtn.disabled = true;
    try {
      const edited = JSON.stringify(payload) !== original;
      const res = resolution();
      const sendsRecord = res !== "keep" && res !== "link";
      const body = {
        reviewer: name, notes: notes(), payload: edited && sendsRecord ? payload : null,
        category: sendsRecord ? categorySel.value || null : null,
        acknowledge_gaps: sendsRecord && ackGaps,
        resolution: res === "keep" || res === "link" ? res : null,
        link_ref: res === "link" ? linkEl.value.trim() : "",
      };
      const out = await post(`/api/reviews/${r.id}/approve`, body);
      toast(out.status === "resolved" ? (res ? "Message resolved" : "Item closed") :
        `Approved — sent to ${DEST[out.destination] || out.destination} (ref ${out.external_ref})`);
      location.hash = "#/reviews";
    } catch (err) {
      toast(err.message, true);
      approveBtn.disabled = false;
      if (err.status === 422 && stillOn(`#/reviews/${id}`)) await renderReview(id); // reload: a re-routed item now shows the new form
    }
  };
  document.getElementById("rv-claim").onclick = async () => {
    const name = reviewer();
    if (!name) return;
    try {
      await post(`/api/reviews/${r.id}/claim`, { reviewer: name });
      toast("Assigned to you");
      if (stillOn(`#/reviews/${id}`)) await renderReview(id);
    } catch (err) { toast(err.message, true); }
  };
  const dismissBtn = document.getElementById("rv-dismiss");
  if (dismissBtn) dismissBtn.onclick = async () => {
    const name = reviewer();
    if (!name) return;
    if (!notes()) return toast("Write the reason in the notes box first", true);
    try {
      await post(`/api/reviews/${r.id}/dismiss`, { reviewer: name, reason: notes() });
      toast("Dismissed");
      location.hash = "#/reviews";
    } catch (err) { toast(err.message, true); }
  };
  const forwardBtn = document.getElementById("rv-forward");
  if (forwardBtn) forwardBtn.onclick = async () => {
    const name = reviewer();
    if (!name) return;
    const team = document.getElementById("rv-team").value;
    if (!confirm(`Forward this email to ${team}?${CONFIG.dry_run ? "\n\n(Dry run: it will be recorded, not emailed.)" : ""}`)) return;
    try {
      const out = await post(`/api/reviews/${r.id}/forward`, { reviewer: name, team, notes: notes() });
      toast(out.transport === "dry_run" ? `Forward to ${team} recorded (dry run)` : `Forwarded to ${team}`);
      location.hash = "#/reviews";
    } catch (err) { toast(err.message, true); }
  };
  document.getElementById("rv-reject").onclick = async () => {
    const name = reviewer();
    if (!name) return;
    try {
      await post(`/api/reviews/${r.id}/reject`, { reviewer: name, notes: notes() });
      toast("Rejected");
      location.hash = "#/reviews";
    } catch (err) { toast(err.message, true); }
  };
}

// ---------- editable form built from the payload ----------

const READONLY = new Set(["source_email_id", "message_id", "confidence", "received_at", "parent_email_id",
  "parent_ref", "parent_link", "source_email_link"]);
const TEMPLATES = {
  passengers: { name: null, phone: null, email: null },
  trip: {
    pickup_location: null, drop_location: null, pickup_date: null, pickup_time: null, pickup_datetime: null,
    vehicle_type: null, trip_type: null,
    passenger_count: null, cost_centre: null, concur_id: null, flight_number: null, train_number: null, notes: null,
  },
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node[k] = v;
    else node.setAttribute(k, v);
  }
  children.forEach((c) => c && node.appendChild(c));
  return node;
}

function buildForm(obj, container, disabled) {
  for (const key of Object.keys(obj)) container.appendChild(buildField(obj, key, disabled));
}

function buildField(obj, key, disabled) {
  let v = obj[key];
  const tmpl = TEMPLATES[key];
  if (tmpl && v && typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length) v = obj[key] = { ...tmpl };
  if (key === "trip" && v == null) v = obj[key] = { ...tmpl };

  // list of objects (e.g. passengers)
  if (Array.isArray(v) && (v.length ? v[0] !== null && typeof v[0] === "object" : Boolean(tmpl))) {
    const fs = el("fieldset", {}, el("legend", { text: label(key) }));
    const list = el("div", { class: "form-grid" });
    const draw = () => {
      list.replaceChildren();
      v.forEach((item, i) => {
        const row = el("div", { class: "item-row" });
        buildForm(item, row, disabled);
        if (!disabled) {
          row.appendChild(el("div", { class: "row-actions" },
            el("button", { class: "btn btn-sm btn-danger", type: "button", text: "Remove",
              onclick: () => { v.splice(i, 1); draw(); } })));
        }
        list.appendChild(row);
      });
      if (!v.length) list.appendChild(el("div", { class: "muted", text: "None" }));
    };
    draw();
    fs.appendChild(list);
    if (!disabled) {
      fs.appendChild(el("div", {}, el("button", { class: "btn btn-sm", type: "button", text: `+ Add ${label(key).replace(/s$/, "")}`,
        onclick: () => {
          const shape = tmpl || Object.fromEntries(Object.keys(v[0] || {}).map((k) => [k, null]));
          v.push({ ...shape });
          draw();
        } })));
    }
    return fs;
  }

  // list of plain values (e.g. booking IDs)
  if (Array.isArray(v)) {
    const input = el("input", { placeholder: "Comma-separated" });
    input.value = v.join(", ");
    input.disabled = disabled;
    input.oninput = () => { obj[key] = input.value.split(",").map((s) => s.trim()).filter(Boolean); };
    return el("div", { class: "field" }, el("label", { text: label(key) }), input);
  }

  // nested object (e.g. trip)
  if (v && typeof v === "object") {
    const fs = el("fieldset", {}, el("legend", { text: label(key) }));
    buildForm(v, fs, disabled);
    return fs;
  }

  // scalar
  const long = typeof v === "string" && (v.length > 70 || v.includes("\n"));
  const input = long ? el("textarea", { rows: String(Math.min(8, Math.ceil(v.length / 70) + 1)) }) : el("input");
  input.value = v ?? "";
  input.disabled = disabled;
  if (READONLY.has(key)) input.readOnly = true;
  const numeric = typeof v === "number";
  input.oninput = () => {
    const raw = input.value;
    obj[key] = raw === "" ? null : numeric ? Number(raw) : raw;
  };
  return el("div", { class: "field" }, el("label", { text: label(key) }), input);
}

// ---------- admin settings ----------

const THRESHOLD_ROWS = [
  ["Everything else", "confidence_high", "confidence_medium"],
  ["Feedback", "confidence_high_feedback", "confidence_medium_feedback"],
  ["Escalation", "confidence_high_escalation", "confidence_medium_escalation"],
];
const TIER_LABEL = { essential: "Essential: blocks the push", mandatory: "Mandatory: needs 'push with gaps'",
  semi_mandatory: "Semi-mandatory: just a reminder" };
const BUCKET_LABEL = { feedback: "Feedback", escalation: "Escalation", new_booking: "New Booking",
  unclassified: "Unclassified (once assigned)" };

async function renderSettings() {
  const snap = await api("/api/admin/settings");
  const state = JSON.parse(JSON.stringify(snap.values));
  const overridden = new Set(snap.overridden);
  const sectionKeys = {
    thresholds: THRESHOLD_ROWS.flatMap((r) => [r[1], r[2]]),
    notify: ["notify_ccp", "notify_booking", "notify_alerts"],
    teams: ["forward_teams"],
    replies: ["reply_signature", "reply_templates"],
    missing: ["missing_templates"],
    tiers: ["indecab_tiers"],
  };
  const badge = (section) => sectionKeys[section].some((k) => overridden.has(k))
    ? `<span class="badge st-approved" title="Different from the .env / built-in default">Customised</span>` : "";
  const head = (section, title, hint) => `
    <div class="card-head"><div><h2>${title} ${badge(section)}</h2><div class="muted" style="font-size:12.5px;margin-top:2px">${hint}</div></div></div>`;
  const foot = (section) => `
    <div class="actions">
      <button class="btn btn-sm" data-reset="${section}" ${sectionKeys[section].some((k) => overridden.has(k)) ? "" : "disabled"}>Reset to default</button>
      <button class="btn btn-primary btn-sm" data-save="${section}">Save</button>
    </div>`;
  const num = (key, ph) => `<input type="number" min="0" max="100" data-num="${key}" placeholder="${ph}" value="${state[key] ?? ""}" style="width:90px">`;

  $view.innerHTML = `
    <div class="notice">Changes apply immediately and are recorded in the audit log. Times and dates use <b>${esc(snap.timezone)}</b>.</div>

    <div class="card">${head("thresholds", "Confidence thresholds",
      "Auto-create at or above <b>High</b>; a person confirms between <b>Medium</b> and High; below Medium goes to the CCP queue. Blank = use the first row. New Bookings always need a person.")}
      <table><thead><tr><th>Bucket</th><th>High ≥</th><th>Medium ≥</th></tr></thead><tbody>
        ${THRESHOLD_ROWS.map(([name, hi, med], i) => `<tr><td>${name}</td>
          <td>${num(hi, i ? "same" : "85")}</td><td>${num(med, i ? "same" : "50")}</td></tr>`).join("")}
      </tbody></table>${foot("thresholds")}</div>

    <div class="card">${head("notify", "Notifications",
      "Comma-separated email addresses. Emails go out through the same route as replies.")}
      <div class="form-grid">
        <div class="field"><label>Review queue: CCP Managers and Executives</label>
          <input data-text="notify_ccp" value="${esc(state.notify_ccp)}" placeholder="manager@example.com, exec@example.com"></div>
        <div class="field"><label>New Bookings (blank = same as the queue list)</label>
          <input data-text="notify_booking" value="${esc(state.notify_booking)}"></div>
        <div class="field"><label>Mailbox failures (blank = same as the queue list)</label>
          <input data-text="notify_alerts" value="${esc(state.notify_alerts)}"></div>
      </div>${foot("notify")}</div>

    <div class="card">${head("teams", "Forward to team",
      "The teams offered by <b>Forward to team</b> in the review queue.")}
      <div id="teams-rows" class="form-grid"></div>
      <div><button class="btn btn-sm" id="teams-add">+ Add team</button></div>${foot("teams")}</div>

    <div class="card">${head("replies", "Reply drafts",
      `Leave a bucket blank to keep the AI-written draft. You can use ${snap.placeholders.map((p) => `<code>{${p}}</code>`).join(" ")}. The signature replaces <code>[Agent Name]</code> in every draft.`)}
      <div class="form-grid">
        <div class="field"><label>Signature</label><textarea data-text="reply_signature" rows="3" placeholder="Cabi Support Team">${esc(state.reply_signature)}</textarea></div>
        ${snap.buckets.map((b) => `<div class="field"><label>${esc(BUCKET_LABEL[b] || b)} template</label>
          <textarea data-tpl="reply_templates.${b}" rows="5" placeholder="(AI-written draft)">${esc(state.reply_templates[b] || "")}</textarea></div>`).join("")}
      </div>${foot("replies")}</div>

    <div class="card">${head("missing", "New Booking: missing-details replies",
      "Wording of <b>Request missing details</b>, by how serious the gap is. Each must contain <code>{items}</code>, where the list of missing fields goes. Blank = built-in wording.")}
      <div class="form-grid">
        ${snap.tiers.map((t) => `<div class="field"><label>${esc(TIER_LABEL[t])}</label>
          <textarea data-tpl="missing_templates.${t}" rows="6" placeholder="${esc(snap.builtin_missing[t])}">${esc(state.missing_templates[t] || "")}</textarea></div>`).join("")}
      </div>${foot("missing")}</div>

    <div class="card">${head("tiers", "IndeCab field tiers",
      "Which fields block a push, which need an acknowledged gap, and which are only a reminder. A path like <code>trip.flight_number|trip.train_number</code> is satisfied by either.")}
      <datalist id="trip-fields">${snap.trip_fields.map((f) => `<option value="trip.${f}">`).join("")}</datalist>
      <div id="tier-rows" class="form-grid"></div>
      <div><button class="btn btn-sm" id="tier-add">+ Add field</button></div>${foot("tiers")}</div>`;

  // --- row editors for the two list-shaped settings
  const drawTeams = () => {
    const box = document.getElementById("teams-rows");
    box.replaceChildren();
    state.forward_teams.forEach((row, i) => {
      const line = el("div", { class: "item-row" });
      const name = el("input", { placeholder: "Team name, e.g. Invoicing" });
      name.value = row.name;
      name.oninput = () => { row.name = name.value; };
      const addr = el("input", { placeholder: "team@example.com" });
      addr.value = row.address;
      addr.oninput = () => { row.address = addr.value; };
      line.append(name, addr, el("div", { class: "row-actions" },
        el("button", { class: "btn btn-sm btn-danger", type: "button", text: "Remove",
          onclick: () => { state.forward_teams.splice(i, 1); drawTeams(); } })));
      box.appendChild(line);
    });
    if (!state.forward_teams.length) box.appendChild(el("div", { class: "muted", text: "No teams yet" }));
  };
  const drawTiers = () => {
    const box = document.getElementById("tier-rows");
    box.replaceChildren();
    const order = { essential: 0, mandatory: 1, semi_mandatory: 2 };
    state.indecab_tiers.sort((a, b) => order[a.tier] - order[b.tier]);
    state.indecab_tiers.forEach((row, i) => {
      const line = el("div", { class: "item-row" });
      const path = el("input", { list: "trip-fields", placeholder: "trip.cost_centre" });
      path.value = row.path;
      path.oninput = () => { row.path = path.value; };
      const lab = el("input", { placeholder: "Label shown to the reviewer" });
      lab.value = row.label;
      lab.oninput = () => { row.label = lab.value; };
      const tier = el("select");
      snap.tiers.forEach((t) => tier.appendChild(el("option", { value: t, text: TIER_LABEL[t].split(":")[0], ...(t === row.tier ? { selected: "" } : {}) })));
      tier.onchange = () => { row.tier = tier.value; };
      line.append(path, lab, tier, el("div", { class: "row-actions" },
        el("button", { class: "btn btn-sm btn-danger", type: "button", text: "Remove",
          onclick: () => { state.indecab_tiers.splice(i, 1); drawTiers(); } })));
      box.appendChild(line);
    });
  };
  drawTeams();
  drawTiers();
  document.getElementById("teams-add").onclick = () => { state.forward_teams.push({ name: "", address: "" }); drawTeams(); };
  document.getElementById("tier-add").onclick = () => { state.indecab_tiers.push({ path: "", label: "", tier: "mandatory" }); drawTiers(); };

  // --- read the simple inputs into the working copy, then save or reset a section
  const collect = () => {
    document.querySelectorAll("[data-num]").forEach((i) => { state[i.dataset.num] = i.value.trim() === "" ? null : Number(i.value); });
    document.querySelectorAll("[data-text]").forEach((i) => { state[i.dataset.text] = i.value; });
    document.querySelectorAll("[data-tpl]").forEach((i) => {
      const [key, name] = i.dataset.tpl.split(".");
      state[key][name] = i.value;
    });
  };
  const send = async (body, done) => {
    try {
      const out = await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) });
      toast(out.changed.length ? `${done}: ${out.changed.map(label).join(", ")}` : "Nothing changed");
      await loadConfig();
      if (stillOn("#/settings")) await renderSettings();
    } catch (err) { toast(err.message, true); }
  };
  document.querySelectorAll("[data-save]").forEach((b) => b.onclick = () => {
    collect();
    send({ changes: Object.fromEntries(sectionKeys[b.dataset.save].map((k) => [k, state[k]])) }, "Saved");
  });
  document.querySelectorAll("[data-reset]").forEach((b) => b.onclick = () => {
    if (confirm("Go back to the default for this section?")) send({ reset: sectionKeys[b.dataset.reset] }, "Reset");
  });
}

// ---------- audit log ----------

let auditQuery = "";

async function renderAudit() {
  const entries = (await api("/api/audit?limit=500")).reverse();
  $view.innerHTML = `
    <div class="toolbar"><input type="search" id="f-audit" placeholder="Filter by event, actor or details…" value="${esc(auditQuery)}">
      <span class="muted">Append-only · newest first · click a row for full details</span></div>
    <div class="card table-wrap"><table>
      <thead><tr><th>Time</th><th>Event</th><th>Actor</th><th>Email</th><th>Details</th></tr></thead>
      <tbody id="audit-rows"></tbody></table></div>`;

  const draw = () => {
    const q = auditQuery.toLowerCase();
    const rows = entries.filter((a) => !q || `${a.event} ${a.actor} ${JSON.stringify(a.details)}`.toLowerCase().includes(q));
    const tbody = document.getElementById("audit-rows");
    tbody.innerHTML = rows.length ? rows.map((a, i) => {
      const full = JSON.stringify(a.details, null, 2);
      const short = JSON.stringify(a.details);
      return `<tr class="clickable" data-i="${i}">
        <td class="nowrap muted">${fmtTime(a.ts)}</td>
        <td class="nowrap"><b>${esc(a.event)}</b></td>
        <td class="nowrap">${esc(a.actor)}</td>
        <td class="nowrap">${a.email_id ? `<a href="#/emails/${a.email_id}" onclick="event.stopPropagation()">#${a.email_id}</a>` : "—"}</td>
        <td><div class="audit-details" data-short="${esc(short.length > 160 ? short.slice(0, 157) + "…" : short)}" data-full="${esc(full)}">${esc(short.length > 160 ? short.slice(0, 157) + "…" : short)}</div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="5" class="empty">No entries.</td></tr>`;
    tbody.querySelectorAll("tr[data-i]").forEach((tr) => tr.onclick = () => {
      const d = tr.querySelector(".audit-details");
      const expanded = d.dataset.expanded === "1";
      d.textContent = expanded ? d.dataset.short : d.dataset.full;
      d.dataset.expanded = expanded ? "0" : "1";
    });
  };
  draw();
  document.getElementById("f-audit").oninput = (ev) => { auditQuery = ev.target.value; draw(); };
}

// ---------- top bar ----------

async function loadConfig() {
  CONFIG = await api("/api/config");
  const providerLabel = CONFIG.provider === "gemini" ? "Gemini" : CONFIG.provider === "anthropic" ? "Claude" : "Rules";
  const source = CONFIG.graph_configured && CONFIG.imap_configured ? "Graph + IMAP fallback"
    : CONFIG.graph_configured ? "Graph" : CONFIG.imap_configured ? "IMAP" : "Not configured";
  document.getElementById("mode-chips").innerHTML = `
    <span class="chip ${CONFIG.dry_run ? "warn" : "ok"}" title="${CONFIG.dry_run ? "Downstream systems are not called" : "Downstream systems are called"}">${CONFIG.dry_run ? "DRY RUN" : "LIVE"}</span>
    <span class="chip">${CONFIG.use_llm ? `${providerLabel} · ${esc(CONFIG.model)}` : "Rules only"}</span>
    ${ME.auth_enabled ? `<span class="chip">${esc(ME.name)} · ${esc(label(ME.role))}</span>
      <button class="btn btn-sm" onclick="signOut()">Sign out</button>` : ""}`;
  document.getElementById("config-panel").innerHTML = `
    <dl>
      <dt>Mailbox</dt><dd>${esc(source)}</dd>
      <dt>Classifier</dt><dd>${CONFIG.use_llm ? providerLabel : "Rules"}</dd>
      <dt>High ≥</dt><dd>${CONFIG.confidence_high}</dd>
      <dt>Medium ≥</dt><dd>${CONFIG.confidence_medium}</dd>
      <dt>Database</dt><dd>${esc(CONFIG.db_path)}</dd>
    </dl>`;
  if (!CONFIG.graph_configured && !CONFIG.imap_configured) {
    document.getElementById("btn-run").title = "No mailbox configured: set GRAPH_* or IMAP_* in .env";
  }
}

async function runBatch(btn, path, busyText) {
  const buttons = [document.getElementById("btn-run"), document.getElementById("btn-demo")];
  const original = btn.textContent;
  buttons.forEach((b) => b.disabled = true);
  btn.textContent = busyText;
  try {
    const { processed, deferred } = await post(path);
    if (deferred) toast(`${deferred} email(s) waiting: the AI model is busy, retrying automatically`, true);
    if (!processed.length) { if (!deferred) toast("No new emails"); }
    else {
      const auto = processed.filter((p) => p.status === "auto_created").length;
      toast(`Processed ${processed.length} email(s): ${auto} auto-created, ${processed.length - auto} sent for review`);
    }
    await router();
  } catch (err) {
    toast(err.message, true);
  } finally {
    buttons.forEach((b) => b.disabled = false);
    btn.textContent = original;
  }
}

// ---------- auto-fetch ----------

let AUTO = null;
const LIST_VIEWS = /^#\/(dashboard|emails|reviews|audit)?$/;

const ago = (iso) => {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  return s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
};

function drawAuto() {
  const btn = document.getElementById("btn-auto");
  const on = AUTO && AUTO.enabled;
  btn.textContent = !AUTO ? "Auto-fetch: Unavailable"
    : !on ? "Auto-fetch: Off"
    : AUTO.running ? "Auto-fetch: Checking…"
    : AUTO.last_error ? "Auto-fetch: Error"
    : AUTO.deferred ? `Auto-fetch: ${AUTO.deferred} waiting`
    : `Auto-fetch: On${AUTO.last_run ? ` · ${ago(AUTO.last_run)}` : ""}`;
  btn.classList.toggle("btn-success", Boolean(on && !AUTO.last_error));
  btn.classList.toggle("btn-danger", Boolean(on && AUTO.last_error));
  btn.title = !AUTO ? "Server has no auto-fetch endpoint: restart the GUI"
    : !on ? "Click to fetch new mail automatically"
    : [`Checks for new mail every ${AUTO.interval}s. Click to turn off.`,
       AUTO.last_run && `Last check: ${fmtTime(AUTO.last_run)} (${AUTO.last_count} new)`,
       AUTO.deferred && `${AUTO.deferred} email(s) left unread because the AI model is busy; retried on the next check`,
       AUTO.last_error && `Last check failed: ${AUTO.last_error}`].filter(Boolean).join("\n");
}

async function pollAuto() {
  try {
    const prev = AUTO;
    AUTO = await api("/api/autofetch");
    drawAuto();
    if (!prev) return;
    if (AUTO.last_error && AUTO.last_error !== prev.last_error) toast(`Auto-fetch failed: ${AUTO.last_error}`, true);
    if (AUTO.deferred && AUTO.deferred !== prev.deferred) {
      toast(`${AUTO.deferred} email(s) waiting: the AI model is busy, retrying automatically`, true);
    }
    const added = AUTO.processed_total - prev.processed_total;
    if (added > 0) {
      toast(`Auto-fetch: ${added} new email(s) processed`);
      // Re-render list pages only; never wipe a review form or search box the user is typing in.
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
      if (LIST_VIEWS.test(location.hash) && !typing) await router();
      else refreshBadge();
    }
  } catch {
    if (!AUTO) drawAuto(); // server unreachable or outdated; try again next tick
  }
}

document.getElementById("btn-auto").onclick = async () => {
  try {
    AUTO = await post("/api/autofetch", { enabled: !(AUTO && AUTO.enabled) });
    drawAuto();
    toast(AUTO.enabled ? "Auto-fetch turned on" : "Auto-fetch turned off");
  } catch (err) { toast(err.message, true); }
};
setInterval(() => { if (ME.can.fetch && (TOKEN || !ME.auth_enabled)) pollAuto(); }, 2000);

document.getElementById("btn-run").onclick = (ev) => runBatch(ev.currentTarget, "/api/run", "Fetching…");
document.getElementById("btn-demo").onclick = (ev) => runBatch(ev.currentTarget, "/api/demo", "Processing…");
window.addEventListener("hashchange", router);

async function start() {
  TOKEN = storageGet("eie.token");
  try {
    ME = await api("/api/me");
  } catch (err) {
    if (err.status === 401) return;  // the sign-in form is showing
    toast(err.message, true);
  }
  document.getElementById("nav-settings").hidden = !ME.can.admin;
  if (!ME.can.fetch) ["btn-run", "btn-demo", "btn-auto"].forEach((id) => { document.getElementById(id).hidden = true; });
  try { await loadConfig(); } catch (err) { toast(err.message, true); }
  router();
}
start();
