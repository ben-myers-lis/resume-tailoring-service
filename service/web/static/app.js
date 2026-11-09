/* Resume Tailoring: single-page web app (no build step). All text is inserted with textContent. */
"use strict";

// ================= helpers =================
const $ = (sel, root = document) => root.querySelector(sel);
const S = { me: null, meta: null, timers: [] };

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else if (k === "style") el.setAttribute("style", v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(method, path, body) {
  const opt = { method, headers: { "X-Requested-With": "rts" }, credentials: "same-origin" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/") && !path.startsWith("/api/invites")) { location.hash = "#/login"; }
    const err = new Error((data && data.error) || `Request failed (${res.status})`);
    err.status = res.status; err.data = data; throw err;
  }
  return data;
}

function toast(msg, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3200);
}
const fail = (e) => toast(e.message || String(e), true);

const can = (p) => S.me && (S.me.role === "ADMIN" || S.me.permissions.includes(p));
const isAdmin = () => S.me && S.me.role === "ADMIN";
const fmtDate = (iso) => iso ? new Date(iso.length === 10 ? iso + "T12:00:00" : iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "";
const fmtDateTime = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
const fmtMonth = (ym) => { if (!ym) return "Present"; const [y, m] = ym.split("-"); return new Date(+y, +m - 1, 1).toLocaleDateString(undefined, { month: "short", year: "numeric" }); };

function badge(text, kind) { return h("span", { class: "badge " + (kind || "") }, text); }
function runBadge(r) {
  const map = { QUEUED: ["Queued", "info"], RUNNING: ["Tailoring…", "info"], DONE: ["Done", "ok"], SKIPPED: ["Skipped (low fit)", "warn"], FAILED: ["Failed", "bad"] };
  const [t, k] = map[r.status] || [r.status, ""];
  return badge(t, k);
}
function deliveryBadge(s) {
  if (!s) return "";
  const map = { READY: ["Ready", "ok"], INCOMPLETE_EVIDENCE: ["Outline (few details)", "warn"], REVIEW_NEEDED: ["Needs review", "bad"], ILLUSTRATIVE: ["Illustrative", "warn"] };
  const [t, k] = map[s] || [s, ""];
  return badge(t, k);
}
function fitBadge(score) {
  if (score === null || score === undefined) return "";
  const k = score >= 80 ? "ok" : score >= 60 ? "accent" : score >= 40 ? "warn" : "bad";
  return badge(`Fit ${score}`, k);
}
const STATUS_LABEL = { APPLIED: "Applied", INTERVIEW: "Interview", OFFER: "Offer", REJECTED: "Rejected", WITHDRAWN: "Withdrawn" };
const statusKind = { APPLIED: "info", INTERVIEW: "accent", OFFER: "ok", REJECTED: "bad", WITHDRAWN: "" };

function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }
function every(ms, fn) { const t = setInterval(fn, ms); S.timers.push(t); }

function page(title, sub, actions, ...content) {
  return [h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, title), sub ? h("div", { class: "sub" }, sub) : null),
    h("div", { class: "actions" }, actions || [])), ...content];
}
function render(...nodes) { const m = $("#main"); m.replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false)); window.scrollTo(0, 0); }
function card(title, right, ...body) {
  return h("div", { class: "card" }, (title || right) ? h("div", { class: "card-head" }, h("h2", {}, title || ""), h("div", { class: "actions" }, right || [])) : null, ...body);
}
function field(labelText, input, help, name) {
  return h("div", { class: "field", "data-field": name || "" }, h("label", {}, labelText, help ? h("span", { class: "help" }, " · " + help) : null), input, h("div", { class: "err" }));
}
function showFieldErrors(root, err) {
  root.querySelectorAll(".err").forEach((e) => (e.textContent = ""));
  for (const d of (err.data && err.data.details) || []) {
    if (!d.field) continue;
    const key = d.field.split(".")[0];
    const box = root.querySelector(`[data-field="${key}"] .err`);
    if (box) box.textContent = d.error;
  }
}

function modal(title, body, buttons) {
  const close = () => back.remove();
  const back = h("div", { class: "modal-backdrop", onclick: (e) => { if (e.target === back) close(); } },
    h("div", { class: "modal", role: "dialog", "aria-label": title },
      h("div", { class: "modal-head" }, h("h2", { style: "margin:0" }, title), h("button", { class: "link", onclick: close, "aria-label": "Close" }, "✕")),
      h("div", { class: "modal-body" }, body),
      h("div", { class: "modal-foot" }, buttons(close))));
  $("#modal-root").append(back);
  const first = back.querySelector("input, select, textarea");
  if (first) first.focus();
  return close;
}

// Simple bar chart (SVG)
function barChart(points, { labelOf, valueOf, highlightLast = true, height = 170 }) {
  const NS = "http://www.w3.org/2000/svg";
  const n = points.length, W = Math.max(900, n * 30), top = 18, bottom = 30, H = height;
  const max = Math.max(1, ...points.map(valueOf));
  const bw = (W - 20) / n;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart"); svg.setAttribute("role", "img");
  const mk = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text !== undefined) e.textContent = text; return e; };
  const step = n > 20 ? Math.ceil(n / 10) : 1;
  points.forEach((p, i) => {
    const v = valueOf(p), bh = v ? Math.max(3, (H - top - bottom) * v / max) : 2;
    const x = 10 + i * bw + bw * 0.15, y = H - bottom - bh;
    const cls = "bar" + (v === 0 ? " zero" : "") + (highlightLast && i === n - 1 && v ? " today" : "");
    const rect = mk("rect", { x, y, width: bw * 0.7, height: bh, rx: 2, class: cls });
    rect.append(mk("title", {}, `${labelOf(p, true)}: ${v}`));
    svg.append(rect);
    if (v) svg.append(mk("text", { x: x + bw * 0.35, y: y - 4, "text-anchor": "middle", class: "val" }, String(v)));
    if (i % step === 0 || i === n - 1) svg.append(mk("text", { x: x + bw * 0.35, y: H - 12, "text-anchor": "middle" }, labelOf(p, false)));
  });
  return svg;
}

// Minimal Markdown -> DOM for the resume preview (headings, bullets, **bold**, escaped chars)
function inline(text) {
  const out = []; const re = /\*\*(.+?)\*\*/g; let last = 0, m;
  const unesc = (s) => s.replace(/\\(.)/g, "$1").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
  while ((m = re.exec(text))) { out.push(unesc(text.slice(last, m.index))); out.push(h("strong", {}, unesc(m[1]))); last = re.lastIndex; }
  out.push(unesc(text.slice(last)));
  return out;
}
function resumePreview(md) {
  const root = h("div", { class: "resume" }); let ul = null; let afterName = false;
  for (const line of md.split("\n")) {
    if (line.startsWith("- ")) { if (!ul) { ul = h("ul"); root.append(ul); } ul.append(h("li", {}, inline(line.slice(2)))); continue; }
    ul = null;
    if (!line.trim()) continue;
    if (line.startsWith("### ")) root.append(h("h3", {}, inline(line.slice(4))));
    else if (line.startsWith("## ")) root.append(h("h2", {}, inline(line.slice(3))));
    else if (line.startsWith("# ")) { root.append(h("h1", {}, inline(line.slice(2)))); afterName = true; continue; }
    else root.append(h("p", { class: afterName ? "contact" : "" }, inline(line)));
    afterName = false;
  }
  return root;
}

// ================= shell =================
const NAV = [
  ["#/", "Dashboard", () => true],
  ["#/candidates", "Candidates", () => true],
  ["#/applications", "Applications", () => true],
  ["#/prompts", "Prompt Studio", () => can("prompts.manage")],
  ["#/team", "Team", () => isAdmin()],
  ["#/settings", "Settings", () => isAdmin()],
  ["#/account", "My account", () => true],
];
function shell() {
  const side = $("#sidebar");
  if (!S.me) { side.hidden = true; return; }
  side.hidden = false;
  const cur = location.hash || "#/";
  $("#nav").replaceChildren(...NAV.filter((n) => n[2]()).map(([href, label]) => {
    const active = href === "#/" ? cur === "#/" || cur === "" : cur.startsWith(href);
    return h("a", { href, class: active ? "active" : "" }, label);
  }));
  $("#me").replaceChildren(h("div", { class: "name" }, S.me.name), h("div", {}, S.me.role === "ADMIN" ? "Administrator" : "Team member"),
    h("button", { class: "small", onclick: async () => { await api("POST", "/api/auth/logout"); S.me = null; location.hash = "#/login"; } }, "Sign out"));
}

// ================= auth pages =================
function authPage(title, sub, form) {
  $("#sidebar").hidden = true;
  render(h("div", { class: "auth-wrap" }, h("div", { class: "card auth-card" },
    h("div", { class: "brand" }, h("span", { class: "logo" }, "RT"), h("span", {}, "Resume Tailoring")),
    h("h1", { style: "margin-bottom:4px" }, title), sub ? h("p", { class: "muted" }, sub) : null, form)));
}

function loginPage() {
  const email = h("input", { type: "email", autocomplete: "username", required: true });
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
  const msg = h("div");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault(); msg.replaceChildren();
    try { const r = await api("POST", "/api/auth/login", { email: email.value, password: pw.value }); S.me = r.user; location.hash = "#/"; }
    catch (err) { msg.replaceChildren(h("div", { class: "notice warn" }, err.message)); }
  } }, msg, field("Email", email), field("Password", pw), h("button", { class: "primary", type: "submit", style: "width:100%;justify-content:center" }, "Sign in"),
  h("p", { class: "muted small", style: "margin-top:14px" }, "Forgot your password? Ask your administrator to reset it."));
  authPage("Sign in", null, form);
}

function setupPage() {
  const name = h("input", { type: "text" }), email = h("input", { type: "email", autocomplete: "username" });
  const pw = h("input", { type: "password", autocomplete: "new-password" });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try { const r = await api("POST", "/api/auth/setup", { name: name.value, email: email.value, password: pw.value }); S.me = r.user; toast("Welcome! Your administrator account is ready."); location.hash = "#/"; }
    catch (err) { showFieldErrors(form, err); toast(err.message, true); }
  } }, h("div", { class: "notice" }, "First-time setup: create the administrator account. You'll have full control and can invite your team."),
  field("Your name", name, null, "name"), field("Email", email, null, "email"),
  field("Password", pw, "10+ characters, upper and lower case, a number", "password"),
  h("button", { class: "primary", type: "submit", style: "width:100%;justify-content:center" }, "Create administrator account"));
  authPage("Set up Resume Tailoring", null, form);
}

async function invitePage(token) {
  let info;
  try { info = await api("GET", `/api/invites/${encodeURIComponent(token)}`); }
  catch (err) { authPage("Invitation", null, h("div", { class: "notice warn" }, err.message)); return; }
  const name = h("input", { type: "text" }), pw = h("input", { type: "password", autocomplete: "new-password" });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try { const r = await api("POST", "/api/invites/accept", { token, name: name.value, password: pw.value }); S.me = r.user; toast("Welcome to the team!"); location.hash = "#/"; }
    catch (err) { showFieldErrors(form, err); toast(err.message, true); }
  } }, h("p", {}, "You've been invited as ", h("strong", {}, info.email), "."),
  field("Your name", name, null, "name"), field("Choose a password", pw, "10+ characters, upper and lower case, a number", "password"),
  h("button", { class: "primary", type: "submit", style: "width:100%;justify-content:center" }, "Join the team"));
  authPage("Join your team", null, form);
}

// ================= dashboard =================
async function dashboardPage() {
  const d = await api("GET", "/api/dashboard");
  const done = d.resumes_this_week.DONE || 0, skipped = d.resumes_this_week.SKIPPED || 0;
  const tabs = chartTabs(d.per_day, d.per_week, "Last 14 days", "Last 8 weeks");
  const candRows = d.candidates.slice(0, 50).map((c) => h("tr", {},
    h("td", {}, h("a", { href: `#/candidates/${c.id}` }, c.name)), h("td", { class: "num" }, c.today), h("td", { class: "num" }, c.this_week), h("td", { class: "num" }, c.total)));
  const memberRows = d.this_week_by_member.map((m) => h("tr", {}, h("td", {}, m.name), h("td", { class: "num" }, m.count)));
  render(page("Dashboard", `${isAdmin() ? "All candidates" : "Your assigned candidates"} · ${fmtDate(d.as_of)}`, [
      h("a", { class: "btn primary", href: "#/candidates" }, "Candidates"),
      h("a", { class: "btn", href: "#/applications" }, "All applications")],
    h("div", { class: "stats" },
      stat("Applied today", d.today), stat("Applied this week", d.this_week, `since ${fmtDate(d.week_start)}`),
      stat("Resumes this week", done, skipped ? `${skipped} skipped (low fit)` : null),
      stat("Candidates", d.candidate_count), stat("In queue", d.queue)),
    card("Applications", null, tabs),
    h("div", { class: "row" },
      h("div", { class: "card flush grow", style: "min-width:320px" }, h("div", { class: "card-head" }, h("h2", {}, "By candidate")),
        candRows.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Candidate"), h("th", { class: "num" }, "Today"), h("th", { class: "num" }, "This week"), h("th", { class: "num" }, "Total"))), h("tbody", {}, candRows)))
          : h("div", { class: "empty" }, isAdmin() ? "No candidates yet." : "No candidates are assigned to you yet.")),
      isAdmin() ? h("div", { class: "card flush", style: "width:300px" }, h("div", { class: "card-head" }, h("h2", {}, "This week by team member")),
        memberRows.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Member"), h("th", { class: "num" }, "Applied"))), h("tbody", {}, memberRows)) : h("div", { class: "empty" }, "No applications this week yet.")) : null)));
}
function stat(label, value, hint) { return h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null); }

function chartTabs(perDay, perWeek, dayLabel, weekLabel) {
  const box = h("div");
  const dayChart = () => barChart(perDay, { labelOf: (p, long) => long ? fmtDate(p.date) : p.date.slice(5).replace("-", "/"), valueOf: (p) => p.count });
  const weekChart = () => barChart(perWeek, { labelOf: (p, long) => long ? `Week of ${fmtDate(p.week_start)}` : p.week_start.slice(5).replace("-", "/"), valueOf: (p) => p.count });
  const table = (rows, kind) => h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, kind === "day" ? "Date" : "Week"), h("th", { class: "num" }, "Applications"))),
    h("tbody", {}, rows.slice().reverse().map((p) => h("tr", {}, h("td", {}, kind === "day" ? fmtDate(p.date) : `${fmtDate(p.week_start)} – ${fmtDate(p.week_end)}`), h("td", { class: "num" }, p.count))))));
  const views = { day: () => dayChart(), week: () => weekChart(), daytable: () => table(perDay, "day"), weektable: () => table(perWeek, "week") };
  const btns = {};
  const show = (k) => { Object.entries(btns).forEach(([kk, b]) => b.classList.toggle("active", kk === k)); box.replaceChildren(views[k]()); };
  const tabs = h("div", { class: "tabs" }, [["day", `Per day · ${dayLabel}`], ["week", `Per week · ${weekLabel}`], ["daytable", "Daily table"], ["weektable", "Weekly table"]]
    .map(([k, l]) => (btns[k] = h("button", { onclick: () => show(k) }, l))));
  show("day");
  return h("div", {}, tabs, box);
}

// ================= candidates =================
async function candidatesPage() {
  const list = await api("GET", "/api/candidates");
  const q = h("input", { type: "search", placeholder: "Search by name, title or company…", style: "max-width:360px" });
  const tbody = h("tbody");
  const draw = () => {
    const term = q.value.toLowerCase();
    const rows = list.filter((c) => !term || [c.name, ...c.employment.map((r) => r.title + " " + r.company)].join(" ").toLowerCase().includes(term));
    tbody.replaceChildren(...(rows.length ? rows.map((c) => h("tr", {},
      h("td", {}, h("a", { href: `#/candidates/${c.id}` }, h("strong", {}, c.name)), h("div", { class: "muted small" }, c.email || "")),
      h("td", {}, c.employment[0] ? `${c.employment[0].title}` : "", h("div", { class: "muted small" }, c.employment[0] ? c.employment[0].company : "")),
      h("td", {}, c.assignees.length ? c.assignees.map((a) => h("span", { class: "chip" }, a.name)) : h("span", { class: "muted small" }, isAdmin() ? "Unassigned" : "")),
      h("td", { class: "num" }, c.applications.today), h("td", { class: "num" }, c.applications.this_week), h("td", { class: "num" }, c.applications.total)))
      : [h("tr", {}, h("td", { colspan: 6, class: "empty" }, list.length ? "No matches." : (isAdmin() ? "No candidates yet. Add your first one." : "No candidates are assigned to you yet.")))]));
  };
  q.addEventListener("input", draw); draw();
  render(page("Candidates", `${list.length} candidate${list.length === 1 ? "" : "s"}${isAdmin() ? "" : " assigned to you"}`,
    [isAdmin() ? h("a", { class: "btn primary", href: "#/candidates/new" }, "+ Add candidate") : null],
    h("div", { class: "card", style: "padding:12px" }, q),
    h("div", { class: "card flush" }, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Candidate"), h("th", {}, "Current role"), h("th", {}, "Assigned to"), h("th", { class: "num" }, "Applied today"), h("th", { class: "num" }, "This week"), h("th", { class: "num" }, "Total"))), tbody)))));
}

function roleBlock(r = {}, onRemove) {
  const current = h("input", { type: "checkbox", checked: !r.end_date && !!r.start_date || (!r.start_date && !r.company) });
  const end = h("input", { type: "month", value: r.end_date || "" });
  const sync = () => { end.disabled = current.checked; if (current.checked) end.value = ""; };
  current.addEventListener("change", sync);
  const el = h("div", { class: "role-block" },
    h("div", { class: "role-top" }, h("strong", { class: "small" }, "Job"), h("button", { type: "button", class: "small danger", onclick: () => onRemove(el) }, "Remove")),
    h("div", { class: "grid2" },
      field("Job title", h("input", { type: "text", name: "title", value: r.title || "" })),
      field("Company", h("input", { type: "text", name: "company", value: r.company || "" }))),
    h("div", { class: "grid3" },
      field("Start", h("input", { type: "month", name: "start_date", value: r.start_date || "" })),
      field("End", end),
      h("div", { class: "field" }, h("label", {}, " "), h("label", { class: "check" }, current, "Current job"))),
    field("Location", h("input", { type: "text", name: "location", value: r.location || "" }), "optional"),
    field("What they did here", h("textarea", { name: "notes", rows: 3, placeholder: "Optional. In the candidate's own words. Leave blank if you only have the title and dates." }, r.notes || ""), "optional; enables real experience bullets"));
  el._read = () => ({ title: $("[name=title]", el).value.trim(), company: $("[name=company]", el).value.trim(),
    start_date: $("[name=start_date]", el).value || "", end_date: current.checked ? null : (end.value || null),
    location: $("[name=location]", el).value.trim() || null, notes: $("[name=notes]", el).value.trim() || null });
  sync();
  return el;
}

async function candidateFormPage(id) {
  const c = id ? await api("GET", `/api/candidates/${id}`) : null;
  const name = h("input", { type: "text", value: c ? c.name : "" });
  const email = h("input", { type: "email", value: c ? c.email || "" : "" });
  const linkedin = h("input", { type: "text", value: c ? c.linkedin || "" : "", placeholder: "linkedin.com/in/…" });
  const headline = h("input", { type: "text", value: c ? c.headline || "" : "", placeholder: "Optional, e.g. Machine Learning Engineer" });
  const roles = h("div");
  const remove = (el) => { if (roles.children.length > 1) el.remove(); else toast("At least one job is required.", true); };
  (c ? c.employment : [{}]).forEach((r) => roles.append(roleBlock(r, remove)));
  const education = h("textarea", { rows: 3, placeholder: "One per line, e.g. B.S. Computer Science, State University, 2019" }, c ? c.education.join("\n") : "");
  const certs = h("textarea", { rows: 2, placeholder: "One per line" }, c ? c.certifications.join("\n") : "");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim(), email: email.value.trim() || null, linkedin: linkedin.value.trim() || null, headline: headline.value.trim() || null,
      employment: [...roles.children].map((r) => r._read()),
      education: education.value.split("\n").map((s) => s.trim()).filter(Boolean),
      certifications: certs.value.split("\n").map((s) => s.trim()).filter(Boolean), sources: c ? c.sources : [] };
    try {
      const saved = await api(id ? "PUT" : "POST", id ? `/api/candidates/${id}` : "/api/candidates", body);
      toast(id ? "Candidate updated." : "Candidate added."); location.hash = `#/candidates/${saved.id}`;
    } catch (err) {
      showFieldErrors(form, err);
      const roleErr = ((err.data && err.data.details) || []).filter((d) => d.field.startsWith("employment"));
      toast(roleErr.length ? `Job ${+roleErr[0].field.split(".")[1] + 1}: ${roleErr[0].error}` : err.message, true);
    }
  } },
  card("Contact", null, field("Full name", name, null, "name"), h("div", { class: "grid2" }, field("Email", email, null, "email"), field("LinkedIn", linkedin, null, "linkedin")), field("Headline", headline, null, "headline")),
  card("Work history", h("button", { type: "button", onclick: () => roles.append(roleBlock({}, remove)) }, "+ Add job"),
    h("p", { class: "muted small", style: "margin-top:-6px" }, "Most recent job first. Title, company and dates are enough."), roles),
  card("Education & certifications", null, field("Education", education), field("Certifications", certs)),
  h("div", { class: "actions" }, h("button", { class: "primary", type: "submit" }, id ? "Save changes" : "Add candidate"), h("a", { class: "btn", href: id ? `#/candidates/${id}` : "#/candidates" }, "Cancel")));
  render(page(id ? `Edit ${c.name}` : "Add candidate", id ? `Profile version ${c.profile_version}; saving creates version ${c.profile_version + 1}` : null, null, form));
}

async function candidatePage(id) {
  const [c, stats, apps, runs, jobs] = await Promise.all([api("GET", `/api/candidates/${id}`), api("GET", `/api/candidates/${id}/stats?days=30&weeks=12`),
    api("GET", `/api/applications?candidate_id=${id}`), api("GET", `/api/runs?candidate_id=${id}&limit=100`), api("GET", `/api/jobs?candidate_id=${id}`)]);
  const history = h("div", {}, c.employment.map((r) => h("div", { style: "margin-bottom:8px" },
    h("strong", {}, r.title), " · ", r.company, h("div", { class: "muted small" }, `${fmtMonth(r.start_date)} – ${fmtMonth(r.end_date)}${r.location ? " · " + r.location : ""}`))),
    c.education.length ? h("div", { class: "muted small", style: "margin-top:10px" }, "Education: ", c.education.join("; ")) : null,
    c.certifications.length ? h("div", { class: "muted small" }, "Certifications: ", c.certifications.join("; ")) : null);
  const assigned = h("div", {}, c.assignees.length ? c.assignees.map((a) => h("span", { class: "chip" }, a.name)) : h("span", { class: "muted" }, "Not assigned to a team member"));
  render(page(c.name, [c.email, c.linkedin].filter(Boolean).join(" · "), [
      h("button", { onclick: () => applicationModal({ candidate: c, jobs }) }, "+ Log application"),
      isAdmin() ? h("a", { class: "btn", href: `#/candidates/${id}/edit` }, "Edit") : null,
      isAdmin() ? h("button", { class: "danger", onclick: () => deleteCandidate(c) }, "Delete") : null],
    can("resumes.tailor") ? tailorBox(c) : null,
    h("div", { class: "stats" }, stat("Applied today", stats.today), stat("Applied this week", stats.this_week), stat("Total applications", stats.total),
      stat("Resumes tailored", runs.filter((r) => r.status === "DONE").length)),
    card("Applications over time", null, chartTabs(stats.per_day, stats.per_week, "last 30 days", "last 12 weeks")),
    applicationsCard(apps, { showCandidate: false, reload: () => candidatePage(id) }),
    h("div", { class: "row" },
      h("div", { class: "grow", style: "min-width:340px" }, runsCard(runs, { reload: () => candidatePage(id), candidate: c, jobs, title: "Jobs & tailored resumes" })),
      h("div", { style: "width:340px" }, card("Work history", null, history),
        card("Assigned to", isAdmin() ? h("button", { class: "small", onclick: () => assignModal(c) }, "Change") : null, assigned)))));
}

async function deleteCandidate(c) {
  modal("Delete candidate", h("p", {}, `Delete ${c.name}, all of their tailored resumes and application records? This can't be undone.`), (close) => [
    h("button", { onclick: close }, "Cancel"),
    h("button", { class: "danger", onclick: async () => { try { await api("DELETE", `/api/candidates/${c.id}`); close(); toast("Candidate deleted."); location.hash = "#/candidates"; } catch (e) { fail(e); } } }, "Delete")]);
}

async function assignModal(c) {
  const team = await api("GET", "/api/team");
  const members = team.members.filter((m) => m.role === "MEMBER");
  const chosen = new Set(c.assignees.map((a) => a.id));
  const body = members.length ? h("div", { class: "checklist" }, members.map((m) => h("label", { class: "check" },
    h("input", { type: "checkbox", checked: chosen.has(m.id), onchange: (e) => e.target.checked ? chosen.add(m.id) : chosen.delete(m.id) }),
    m.name, h("span", { class: "muted small" }, m.active ? "" : " (inactive)"))))
    : h("p", { class: "muted" }, "No team members yet. Invite people from the Team page.");
  modal(`Assign ${c.name}`, body, (close) => [h("button", { onclick: close }, "Cancel"),
    h("button", { class: "primary", disabled: !members.length, onclick: async () => {
      try { await api("PUT", `/api/candidates/${c.id}/assignees`, { user_ids: [...chosen] }); close(); toast("Assignments saved."); candidatePage(c.id); } catch (e) { fail(e); } } }, "Save")]);
}

function applicationsCard(apps, { showCandidate, reload, title = "Applications" }) {
  const statuses = (S.meta && S.meta.application_statuses) || Object.keys(STATUS_LABEL);
  const rows = apps.map((a) => {
    const sel = h("select", { style: "width:auto;padding:4px 8px", onchange: async (e) => {
      try { await api("PATCH", `/api/applications/${a.id}`, { status: e.target.value }); toast("Status updated."); } catch (err) { fail(err); }
    } }, statuses.map((s) => h("option", { value: s, selected: s === a.status ? "" : null }, STATUS_LABEL[s] || s)));
    sel.value = a.status;
    const canDelete = isAdmin() || a.applied_by === S.me.id;
    return h("tr", {},
      h("td", {}, fmtDate(a.applied_on)),
      showCandidate ? h("td", {}, h("a", { href: `#/candidates/${a.candidate_id}` }, a.candidate_name)) : null,
      h("td", {}, h("strong", {}, a.role), h("div", { class: "muted small" }, a.company || "")),
      h("td", {}, a.job_url ? h("a", { href: a.job_url, target: "_blank", rel: "noopener noreferrer" }, "Posting ↗") : h("span", { class: "muted" }, "—")),
      h("td", {}, sel), h("td", {}, a.applied_by_name),
      h("td", { class: "small" }, a.notes || ""),
      h("td", {}, canDelete ? h("button", { class: "small danger", onclick: () => modal("Delete application record", h("p", {}, `Remove the record for ${a.role}${a.company ? " at " + a.company : ""}?`), (close) => [
        h("button", { onclick: close }, "Cancel"), h("button", { class: "danger", onclick: async () => { try { await api("DELETE", `/api/applications/${a.id}`); close(); toast("Record removed."); reload(); } catch (e) { fail(e); } } }, "Remove")]) }, "Remove") : null));
  });
  return h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, `${title} (${apps.length})`)),
    rows.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Date"), showCandidate ? h("th", {}, "Candidate") : null, h("th", {}, "Job"), h("th", {}, "Link"), h("th", {}, "Status"), h("th", {}, "Applied by"), h("th", {}, "Notes"), h("th", {}, ""))), h("tbody", {}, rows)))
      : h("div", { class: "empty" }, "No applications recorded yet."));
}

function runsCard(runs, { reload, candidate, jobs, showCandidate = false, title = "Tailored resumes" }) {
  const rows = runs.map((r) => h("tr", {},
    showCandidate ? h("td", {}, h("a", { href: `#/candidates/${r.candidate_id}` }, r.candidate_name)) : null,
    h("td", {}, h("a", { href: `#/runs/${r.id}` }, r.job_role), h("div", { class: "muted small" }, r.job_company || "")),
    h("td", {}, fitBadge(r.fit_score)), h("td", {}, runBadge(r), " ", deliveryBadge(r.delivery_status)),
    h("td", { class: "small muted" }, fmtDateTime(r.created_at)),
    h("td", {}, h("div", { class: "actions" },
      r.drive_link && can("resumes.tailor") ? h("button", { class: "small", title: r.drive_link, onclick: () => copy(r.drive_link, "Drive link copied.") }, "Copy link") : null,
      r.applied ? badge("Applied", "ok") : (r.status === "DONE" ? h("button", { class: "small", onclick: () => applicationModal({ run: r, reload }) }, "Mark as applied") : null)))));
  return h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, title)),
    rows.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, showCandidate ? h("th", {}, "Candidate") : null, h("th", {}, "Job"), h("th", {}, "Fit"), h("th", {}, "Status"), h("th", {}, "Created"), h("th", {}, ""))), h("tbody", {}, rows)))
      : h("div", { class: "empty" }, "No tailored resumes yet."));
}

function tailorBox(c) {
  const text = h("textarea", { rows: 9, placeholder: "Paste the full job description here, exactly as posted (plain text is fine)." });
  const title = h("input", { type: "text", placeholder: "Detected automatically" });
  const company = h("input", { type: "text", placeholder: "Detected automatically" });
  const url = h("input", { type: "url", placeholder: "https://… (optional)" });
  const force = h("input", { type: "checkbox" });
  const btn = h("button", { class: "primary", type: "submit" }, "Tailor resume");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    if (text.value.trim().length < 40) return toast("Paste the full job description first.", true);
    btn.disabled = true; btn.textContent = "Starting…";
    try {
      const r = await api("POST", `/api/candidates/${c.id}/tailor`, { jd_text: text.value, title: title.value.trim() || null,
        company: company.value.trim() || null, url: url.value.trim() || null, force: force.checked });
      location.hash = `#/runs/${r.run.id}`;
    } catch (err) { showFieldErrors(form, err); fail(err); btn.disabled = false; btn.textContent = "Tailor resume"; }
  } },
  h("div", { "data-field": "jd_text" }, text, h("div", { class: "err" })),
  h("details", { style: "margin:10px 0" }, h("summary", { class: "small muted", style: "cursor:pointer" }, "Job title, company and link (optional; detected from the text)"),
    h("div", { class: "grid3", style: "margin-top:10px" }, field("Job title", title), field("Company", company), field("Link to posting", url))),
  h("div", { class: "actions", style: "justify-content:space-between" },
    h("label", { class: "check small" }, force, "Tailor even if the fit looks low"), btn));
  return card(`Tailor ${c.name.split(" ")[0]}'s resume for a job`, null,
    h("p", { class: "small muted", style: "margin-top:-6px" }, "The resume is built only from this candidate's profile; nothing is invented. You'll see it as soon as it's ready."), form);
}

function applicationModal({ candidate, jobs, run, reload }) {
  const today = (S.meta && S.meta.today) || new Date().toISOString().slice(0, 10);
  const date = h("input", { type: "date", value: today, max: today });
  const status = h("select", {}, Object.entries(STATUS_LABEL).map(([k, v]) => h("option", { value: k }, v)));
  const notes = h("textarea", { rows: 2, placeholder: "Optional" });
  const url = h("input", { type: "url", placeholder: "https://…", value: run && run.job_url ? run.job_url : "" });
  let jobSel, company, role;
  const parts = [];
  if (run) {
    parts.push(h("p", {}, "Resume for ", h("strong", {}, run.job_role), run.job_company ? ` at ${run.job_company}` : "", ` · ${run.candidate_name || ""}`));
  } else {
    jobSel = h("select", {}, h("option", { value: "" }, "A job not tailored here (type it below)"), (jobs || []).map((j) => h("option", { value: j.id }, `${j.role}${j.company ? " · " + j.company : ""}`)));
    company = h("input", { type: "text" }); role = h("input", { type: "text" });
    jobSel.addEventListener("change", () => { const manual = !jobSel.value; company.disabled = role.disabled = !manual; });
    parts.push(field("Job", jobSel), h("div", { class: "grid2" }, field("Job title", role, null, "role"), field("Company", company)));
  }
  const form = h("div", {}, parts, h("div", { class: "grid2" }, field("Date applied", date, null, "applied_on"), field("Status", status)), field("Link to posting", url, "optional", "job_url"), field("Notes", notes));
  modal("Mark as applied", form, (close) => [h("button", { onclick: close }, "Cancel"), h("button", { class: "primary", onclick: async () => {
    const body = { candidate_id: run ? run.candidate_id : candidate.id, applied_on: date.value || null, status: status.value, notes: notes.value.trim() || null, job_url: url.value.trim() || null };
    if (run) body.run_id = run.id;
    else if (jobSel.value) body.job_id = jobSel.value;
    else { body.role = role.value.trim(); body.company = company.value.trim() || null; }
    try { await api("POST", "/api/applications", body); close(); toast("Application recorded."); if (reload) reload(); else router(); }
    catch (e) { showFieldErrors(form, e); fail(e); }
  } }, "Save")]);
}

function editJobModal(job, done) {
  const role = h("input", { type: "text", value: job.role }), company = h("input", { type: "text", value: job.company || "" });
  const url = h("input", { type: "url", value: job.url || "" });
  const body = h("div", {}, field("Job title", role, null, "role"), field("Company", company, null, "company"), field("Link to posting", url, "optional", "url"),
    h("p", { class: "small muted" }, "Application records for this job are updated too."));
  modal("Edit job details", body, (close) => [h("button", { onclick: close }, "Cancel"), h("button", { class: "primary", onclick: async () => {
    try { await api("PATCH", `/api/jobs/${job.id}`, { role: role.value.trim(), company: company.value.trim() || null, url: url.value.trim() || null }); close(); toast("Job details saved."); done(); }
    catch (e) { showFieldErrors(body, e); fail(e); } } }, "Save")]);
}

// ================= applications (all) =================
async function applicationsPage() {
  const today = (S.meta && S.meta.today) || new Date().toISOString().slice(0, 10);
  const from = h("input", { type: "date" }), to = h("input", { type: "date", value: today });
  const d = new Date(today + "T12:00:00"); d.setDate(d.getDate() - 29); from.value = d.toISOString().slice(0, 10);
  const holder = h("div");
  const load = async () => {
    try { const apps = await api("GET", `/api/applications?from=${from.value}&to=${to.value}`); holder.replaceChildren(applicationsCard(apps, { showCandidate: true, reload: load, title: "Applications" })); }
    catch (e) { fail(e); }
  };
  const csv = h("button", { onclick: async () => {
    const apps = await api("GET", `/api/applications?from=${from.value}&to=${to.value}`);
    const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const lines = [["Date", "Candidate", "Job title", "Company", "Status", "Applied by", "Link", "Notes"].join(",")]
      .concat(apps.map((a) => [a.applied_on, a.candidate_name, a.role, a.company, STATUS_LABEL[a.status], a.applied_by_name, a.job_url, a.notes].map(esc).join(",")));
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const link = h("a", { href: URL.createObjectURL(blob), download: `applications_${from.value}_${to.value}.csv` }); document.body.append(link); link.click(); link.remove();
  } }, "Export CSV");
  render(page("Applications", isAdmin() ? "Everything your team has applied for" : "Applications for your assigned candidates", [csv],
    h("div", { class: "card filters" }, field("From", from), field("To", to), h("button", { class: "primary", onclick: load }, "Show")), holder));
  load();
}

// ================= resume (run) page =================
async function runPage(id) {
  const r = await api("GET", `/api/runs/${id}`);
  const fit = r.fit || {};
  const parts = [];
  if (r.status === "QUEUED" || r.status === "RUNNING") { parts.push(card(null, null, h("p", {}, "Tailoring in progress… this page refreshes automatically."))); every(2000, () => location.hash === `#/runs/${id}` && runPage(id)); }
  if (r.status === "SKIPPED") parts.push(h("div", { class: "notice warn" }, `Skipped: fit score ${r.fit_score} is below the threshold. `, fit.reason || "",
    can("resumes.tailor") ? h("button", { class: "small", style: "margin-left:8px", onclick: async () => { try { const n = await api("POST", "/api/runs", { candidate_id: r.candidate_id, job_id: r.job_id, force: true }); location.hash = `#/runs/${n.id}`; } catch (e) { fail(e); } } }, "Tailor anyway") : null));
  if (r.status === "FAILED") parts.push(h("div", { class: "notice warn" }, "This run failed and nothing was delivered: ", r.error || ""));
  const sideParts = () => h("div", { style: "width:320px" },
    card("Fit check", null, h("div", {}, fitBadge(r.fit_score), " ", h("span", { class: "small" }, fit.reason || "")),
      fit.met && fit.met.length ? h("div", { class: "small", style: "margin-top:8px" }, h("strong", {}, "Met"), h("ul", {}, fit.met.map((x) => h("li", {}, x)))) : null,
      fit.missing && fit.missing.length ? h("div", { class: "small" }, h("strong", {}, "Missing"), h("ul", {}, fit.missing.map((x) => h("li", {}, x)))) : null,
      fit.unknown ? h("div", { class: "small muted" }, "Unknown: ", fit.unknown.join("; ")) : null),
    r.questions_for_candidate && r.questions_for_candidate.length ? card("Ask the candidate", null, h("p", { class: "small muted" }, "Answers let the next resume include real experience bullets."), h("ul", { class: "small" }, r.questions_for_candidate.map((q) => h("li", {}, q)))) : null,
    r.gaps && r.gaps.length ? card("Gaps for this job", null, h("ul", { class: "small" }, r.gaps.map((g) => h("li", {}, g)))) : null,
    r.applications.length ? card("Applied", null, r.applications.map((a) => h("div", { class: "small" }, `${fmtDate(a.applied_on)} by ${a.applied_by_name} · `, badge(STATUS_LABEL[a.status], statusKind[a.status])))) : null,
    jobCard);
  const dl = { pdf: "Download PDF", docx: "Download Word" };
  const files = ["pdf", "docx"].filter((k) => (r.files || {})[k]).map((k) => h("a", { class: "btn", href: r.files[k] }, dl[k]));
  const d = r.drive || {};
  let driveBtn = null;
  if (d.link) driveBtn = h("button", { class: "primary", title: d.link, onclick: () => copy(d.link, "Google Drive link copied.") }, "Copy link");
  else if (d.pending) { driveBtn = h("button", { disabled: true }, "Uploading to Google Drive…"); every(2000, () => location.hash === `#/runs/${id}` && runPage(id)); }
  else if (d.error) driveBtn = h("button", { title: d.error, onclick: async (e) => {
    e.target.disabled = true; e.target.textContent = "Uploading…";
    try { await api("POST", `/api/runs/${id}/drive`); toast("Uploaded to Google Drive."); runPage(id); } catch (err) { fail(err); e.target.disabled = false; e.target.textContent = "Retry upload to Drive"; }
  } }, "Retry upload to Drive");
  let job = null;
  try { job = await api("GET", `/api/jobs/${r.job_id}`); } catch (_) { /* job deleted */ }
  const jobCard = job ? card("Job description", can("resumes.tailor") ? h("button", { class: "small", onclick: () => editJobModal(job, () => runPage(id)) }, "Edit title / company") : null,
    job.url ? h("p", { class: "small" }, h("a", { href: job.url, target: "_blank", rel: "noopener noreferrer" }, "Open posting ↗")) : null,
    h("details", {}, h("summary", { class: "small muted", style: "cursor:pointer" }, "Show the pasted text"), h("div", { class: "small", style: "white-space:pre-wrap;margin-top:8px;max-height:420px;overflow:auto" }, job.text))) : null;
  render(page(`${r.candidate_name} → ${r.job_role}`, [r.job_company, fmtDateTime(r.created_at)].filter(Boolean).join(" · "), [
      driveBtn, ...files,
      r.status === "DONE" && !r.applications.length ? h("button", { class: d.link ? "" : "primary", onclick: () => applicationModal({ run: r, reload: () => runPage(id) }) }, "Mark as applied") : null,
      h("a", { class: "btn", href: `#/candidates/${r.candidate_id}` }, `Back to ${r.candidate_name}`)],
    d.error ? h("div", { class: "notice warn small" }, "Google Drive upload failed: ", d.error, ". The resume is fine; use Retry upload or Download.") : null,
    parts, h("div", { class: "row" },
      h("div", { class: "grow", style: "min-width:340px" }, h("div", { class: "actions", style: "margin-bottom:10px" }, runBadge(r), deliveryBadge(r.delivery_status),
        r.render && r.render.pages ? badge(`${r.render.pages} page${r.render.pages === 1 ? "" : "s"}`, r.render.layout_status === "VERIFIED_WITHIN_LIMIT" ? "ok" : "warn") : null),
        r.resume_markdown ? resumePreview(r.resume_markdown) : (r.status === "DONE" ? h("div", { class: "notice warn" }, "Resume withheld: the safety checks found unresolved problems.") : null)),
      sideParts())));
}

// ================= team =================
async function teamPage() {
  const [team, cands] = await Promise.all([api("GET", "/api/team"), api("GET", "/api/candidates")]);
  const perms = team.permissions;
  const memberRows = team.members.map((m) => {
    if (m.role === "ADMIN") return h("tr", {}, h("td", {}, h("strong", {}, m.name), h("div", { class: "muted small" }, m.email)), h("td", {}, badge("Administrator", "accent")), h("td", { colspan: 3, class: "muted small" }, "Full access to everything"));
    const permBoxes = Object.entries(perms).map(([k, label]) => h("label", { class: "check small" }, h("input", { type: "checkbox", checked: m.permissions.includes(k), onchange: async (e) => {
      const next = new Set(m.permissions); e.target.checked ? next.add(k) : next.delete(k);
      try { const u = await api("PATCH", `/api/team/members/${m.id}`, { permissions: [...next] }); m.permissions = u.permissions; toast("Permissions saved."); } catch (err) { fail(err); e.target.checked = !e.target.checked; }
    } }), label));
    return h("tr", {},
      h("td", {}, h("strong", {}, m.name), h("div", { class: "muted small" }, m.email), h("div", { class: "muted small" }, m.last_login_at ? `Last sign-in ${fmtDateTime(m.last_login_at)}` : "Never signed in")),
      h("td", {}, m.active ? badge("Active", "ok") : badge("Deactivated", "bad")),
      h("td", {}, permBoxes, h("div", { class: "muted small" }, "Always: view assigned candidates, mark as applied")),
      h("td", {}, h("div", {}, `${m.candidate_ids.length} candidate${m.candidate_ids.length === 1 ? "" : "s"}`), h("button", { class: "small", onclick: () => memberCandidatesModal(m, cands) }, "Choose candidates")),
      h("td", {}, h("button", { class: "small " + (m.active ? "danger" : ""), onclick: async () => {
        try { await api("PATCH", `/api/team/members/${m.id}`, { active: !m.active }); toast(m.active ? "Member deactivated and signed out." : "Member reactivated."); teamPage(); } catch (e) { fail(e); } } }, m.active ? "Deactivate" : "Reactivate")));
  });
  const inviteRows = team.invites.map((i) => h("tr", {}, h("td", {}, i.email), h("td", { class: "small" }, i.permissions.map((p) => perms[p]).join(", ") || "—"),
    h("td", { class: "small" }, `${i.candidate_ids.length} candidate(s)`), h("td", { class: "small muted" }, `Expires ${fmtDate(i.expires_at)}`),
    h("td", {}, h("div", { class: "actions" }, h("button", { class: "small", onclick: () => copy(i.link) }, "Copy link"),
      h("button", { class: "small danger", onclick: async () => { try { await api("DELETE", `/api/team/invites/${i.id}`); toast("Invitation revoked."); teamPage(); } catch (e) { fail(e); } } }, "Revoke")))));
  render(page("Team", "Invite people, choose what they can do, and assign candidates", [h("button", { class: "primary", onclick: () => inviteModal(perms, cands) }, "+ Invite team member")],
    h("div", { class: "card flush" }, h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Person"), h("th", {}, "Status"), h("th", {}, "Permissions"), h("th", {}, "Assigned"), h("th", {}, ""))), h("tbody", {}, memberRows)))),
    inviteRows.length ? h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, "Pending invitations")),
      h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Email"), h("th", {}, "Permissions"), h("th", {}, "Candidates"), h("th", {}, ""), h("th", {}, ""))), h("tbody", {}, inviteRows)))) : null));
}

function copy(text, message) {
  const fallback = () => {               // older browsers / non-HTTPS pages
    const ta = h("textarea", { style: "position:fixed;opacity:0" }, text); document.body.append(ta); ta.select();
    let okc = false; try { okc = document.execCommand("copy"); } catch (_) { /* ignore */ }
    ta.remove(); return okc ? Promise.resolve() : Promise.reject();
  };
  (navigator.clipboard && window.isSecureContext ? navigator.clipboard.writeText(text) : fallback())
    .then(() => toast(message || "Link copied."), () => modal("Copy this link", h("input", { type: "text", value: text, readonly: true, onclick: (e) => e.target.select() }), (c) => [h("button", { onclick: c }, "Close")]));
}

function candidateChecklist(cands, chosen) {
  const q = h("input", { type: "search", placeholder: "Filter candidates…", style: "margin-bottom:6px" });
  const list = h("div", { class: "checklist" });
  const draw = () => list.replaceChildren(...(cands.length ? cands.filter((c) => c.name.toLowerCase().includes(q.value.toLowerCase())).map((c) =>
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: chosen.has(c.id), onchange: (e) => e.target.checked ? chosen.add(c.id) : chosen.delete(c.id) }), c.name)) : [h("div", { class: "muted small" }, "No candidates yet.")]));
  q.addEventListener("input", draw); draw();
  return h("div", {}, q, list);
}

function inviteModal(perms, cands) {
  const email = h("input", { type: "email" });
  const chosenP = new Set(["resumes.tailor"]), chosenC = new Set();
  const body = h("div", {}, field("Email", email, null, "email"),
    h("div", { class: "field" }, h("label", {}, "Permissions"), Object.entries(perms).map(([k, l]) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: chosenP.has(k), onchange: (e) => e.target.checked ? chosenP.add(k) : chosenP.delete(k) }), l)),
      h("div", { class: "muted small" }, "Every member can view their assigned candidates and mark jobs as applied.")),
    h("div", { class: "field" }, h("label", {}, "Assign candidates"), candidateChecklist(cands, chosenC)));
  modal("Invite team member", body, (close) => [h("button", { onclick: close }, "Cancel"), h("button", { class: "primary", onclick: async () => {
    try {
      const inv = await api("POST", "/api/team/invites", { email: email.value.trim(), permissions: [...chosenP], candidate_ids: [...chosenC] });
      close();
      const linkBox = h("input", { type: "text", value: inv.link, readonly: true, onclick: (e) => e.target.select() });
      modal("Invitation created", h("div", {}, h("p", {}, "Send this link to ", h("strong", {}, inv.email), ". It works once and expires in 7 days."),
        h("div", { class: "copybox" }, linkBox, h("button", { onclick: () => copy(inv.link) }, "Copy"))), (c2) => [h("button", { class: "primary", onclick: () => { c2(); teamPage(); } }, "Done")]);
    } catch (e) { showFieldErrors(body, e); fail(e); }
  } }, "Create invitation")]);
}

function memberCandidatesModal(m, cands) {
  const chosen = new Set(m.candidate_ids);
  modal(`Candidates for ${m.name}`, candidateChecklist(cands, chosen), (close) => [h("button", { onclick: close }, "Cancel"), h("button", { class: "primary", onclick: async () => {
    try { await api("PATCH", `/api/team/members/${m.id}`, { candidate_ids: [...chosen] }); close(); toast("Assignments saved."); teamPage(); } catch (e) { fail(e); } } }, "Save")]);
}

// ================= account =================
function accountPage() {
  const cur = h("input", { type: "password", autocomplete: "current-password" }), nw = h("input", { type: "password", autocomplete: "new-password" });
  const pwForm = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    try { await api("POST", "/api/me/password", { current_password: cur.value, new_password: nw.value }); toast("Password changed. Please sign in again."); S.me = null; location.hash = "#/login"; }
    catch (err) { showFieldErrors(pwForm, err); fail(err); }
  } }, field("Current password", cur, null, "current_password"), field("New password", nw, "10+ characters, upper and lower case, a number", "new_password"), h("button", { class: "primary", type: "submit" }, "Change password"));
  const tokenBox = h("div");
  render(page("My account", `${S.me.name} · ${S.me.email}`, null,
    h("div", { class: "row" },
      h("div", { class: "grow", style: "min-width:300px" }, card("Change password", null, pwForm)),
      h("div", { class: "grow", style: "min-width:300px" }, card("Your access", null,
        h("p", {}, S.me.role === "ADMIN" ? "Administrator: full access." : "Team member"),
        S.me.role !== "ADMIN" ? h("ul", { class: "small" }, (S.me.permissions.length ? S.me.permissions.map((p) => h("li", {}, S.meta.permissions[p])) : [h("li", {}, "No extra permissions")]), h("li", {}, "View assigned candidates and mark as applied")) : null),
        card("API token", null, h("p", { class: "small muted" }, "For scripts and integrations. Creating a new token replaces the old one."), tokenBox,
          h("button", { onclick: async () => { try { const t = await api("POST", "/api/me/api-token"); const inp = h("input", { type: "text", value: t.api_token, readonly: true }); tokenBox.replaceChildren(h("div", { class: "copybox", style: "margin-bottom:10px" }, inp, h("button", { onclick: () => copy(t.api_token) }, "Copy"))); } catch (e) { fail(e); } } }, "Create token"))))));
}

// ================= prompt studio =================
const VSTATUS = { ACTIVE: ["Active", "ok"], DRAFT: ["Draft", "info"], ARCHIVED: ["Earlier version", ""] };
const vBadge = (s) => badge(...(VSTATUS[s] || [s, ""]));
const ROUND_STATE = { RUNNING: ["Testing…", "info"], PASSED: ["Passed checks", "ok"], FAILED: ["Has failures", "bad"] };

async function promptsPage() {
  const [home, cands] = await Promise.all([api("GET", "/api/prompts"), api("GET", "/api/candidates")]);
  const active = home.versions.find((v) => v.status === "ACTIVE");
  const newDraft = async (based_on) => { try { const v = await api("POST", "/api/prompts/versions", based_on ? { based_on } : {}); toast(`Draft v${v.number} created.`); location.hash = `#/prompts/${v.id}`; } catch (e) { fail(e); } };
  const rows = home.versions.map((v) => h("tr", {},
    h("td", {}, h("a", { href: `#/prompts/${v.id}` }, h("strong", {}, `v${v.number}`))), h("td", {}, vBadge(v.status)),
    h("td", {}, v.note || h("span", { class: "muted" }, "—")),
    h("td", { class: "small" }, v.created_by_name || "System", h("div", { class: "muted" }, fmtDateTime(v.created_at))),
    h("td", { class: "small" }, v.published_at ? [v.published_by_name || "System", h("div", { class: "muted" }, fmtDateTime(v.published_at))] : h("span", { class: "muted" }, "—")),
    h("td", {}, h("div", { class: "actions" }, h("a", { class: "btn small", href: `#/prompts/${v.id}` }, v.status === "DRAFT" ? "Open" : "View"),
      v.status !== "DRAFT" ? h("button", { class: "small", onclick: () => newDraft(v.id) }, "New draft from this") : null))));

  const candSel = h("select", {}, h("option", { value: "" }, "Choose a candidate…"), cands.map((c) => h("option", { value: c.id }, c.name)));
  const jobSel = h("select", { disabled: true }, h("option", { value: "" }, "Choose a candidate first"));
  candSel.addEventListener("change", async () => {
    jobSel.replaceChildren(h("option", { value: "" }, "Loading…")); jobSel.disabled = true;
    if (!candSel.value) { jobSel.replaceChildren(h("option", { value: "" }, "Choose a candidate first")); return; }
    try {
      const jobs = await api("GET", `/api/jobs?candidate_id=${candSel.value}`);
      jobSel.replaceChildren(h("option", { value: "" }, jobs.length ? "Choose one of their jobs…" : "No jobs yet: tailor one on their page first"),
        ...jobs.map((j) => h("option", { value: j.id }, `${j.role}${j.company ? " · " + j.company : ""} (${fmtDate(j.created_at)})`)));
      jobSel.disabled = !jobs.length;
    } catch (e) { fail(e); }
  });
  const note = h("input", { type: "text", placeholder: "Optional, e.g. 'senior profile, long history'" });
  const caseRows = home.test_cases.map((t) => h("tr", {}, h("td", {}, t.candidate_name), h("td", {}, t.job_role, h("div", { class: "muted small" }, t.job_company || "")),
    h("td", { class: "small" }, t.note || ""), h("td", {}, h("button", { class: "small danger", onclick: async () => { try { await api("DELETE", `/api/prompts/test-cases/${t.id}`); promptsPage(); } catch (e) { fail(e); } } }, "Remove"))));

  render(page("Prompt Studio", active ? `Live resumes use v${active.number}${active.published_at ? `, published ${fmtDate(active.published_at)}` : ""}` : "", [
      h("button", { class: "primary", onclick: () => newDraft(null) }, "+ New draft from active version")],
    h("div", { class: "row" },
      h("div", { class: "grow", style: "min-width:420px" },
        h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, "Versions")),
          h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Version"), h("th", {}, "Status"), h("th", {}, "Note"), h("th", {}, "Created"), h("th", {}, "Published"), h("th", {}, ""))), h("tbody", {}, rows)))),
        h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, `Test set (${home.test_cases.length})`)),
          caseRows.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Candidate"), h("th", {}, "Job"), h("th", {}, "Note"), h("th", {}, ""))), h("tbody", {}, caseRows)))
            : h("div", { class: "empty" }, "No test cases yet. Add 5–10 typical candidate + job pairs below."),
          h("div", { style: "padding:14px 18px;border-top:1px solid var(--line)" }, h("div", { class: "grid3" }, field("Candidate", candSel), field("Job", jobSel), field("Note", note)),
            h("button", { onclick: async () => {
              if (!candSel.value || !jobSel.value) return toast("Choose a candidate and a job.", true);
              try { await api("POST", "/api/prompts/test-cases", { candidate_id: candSel.value, job_id: jobSel.value, note: note.value.trim() || null }); toast("Test case added."); promptsPage(); } catch (e) { fail(e); }
            } }, "+ Add test case")))),
      h("div", { style: "width:340px" },
        card("How it works", null, h("ol", { class: "steps small" },
          h("li", {}, "Create a ", h("strong", {}, "draft"), ". Live resumes keep using the active version."),
          h("li", {}, h("strong", {}, "Improve with AI"), ": describe what to change; review each proposed edit. Or edit files by hand."),
          h("li", {}, h("strong", {}, "Test"), ": run your test set and compare the draft's resumes with the active version's."),
          h("li", {}, h("strong", {}, "Publish"), " (administrator only). Every resume records its version; earlier versions can be restored.")),
          home.provider === "mock" ? h("div", { class: "notice warn small", style: "margin-top:10px" }, "Mock AI is on: proposals and test resumes are placeholders. Switch to Claude to judge real quality.") : null),
        card("Protected safety rules", null, h("p", { class: "small muted" }, "Changes here are flagged and need explicit confirmation to publish."),
          h("ul", { class: "small" }, Object.entries(home.protected_rules).map(([k, l]) => h("li", {}, `${k}: ${l}`)), Object.values(home.protected_files).map((l) => h("li", {}, l))),
          h("p", { class: "small muted" }, "Output formats and the code-level checks can't be changed from here, so every resume is still checked the same way."))))));
}

async function promptVersionPage(id, tab) {
  const v = await api("GET", `/api/prompts/versions/${id}`);
  const isDraft = v.status === "DRAFT";
  const go = (t) => { location.hash = `#/prompts/${id}?tab=${t}`; };
  const tabs = [["improve", "Improve with AI", isDraft], ["test", "Test", true], ["changes", `Changes${v.diff.length ? " (" + v.diff.length + ")" : ""}`, true],
    ["files", isDraft ? "Edit files" : "Files", true], ["publish", "Publish", isDraft]].filter((t) => t[2]);
  tab = tabs.some((t) => t[0] === tab) ? tab : tabs[0][0];
  const body = { improve: improveTab, test: testTab, changes: changesTab, files: filesTab, publish: publishTab }[tab](v);
  const canDelete = isDraft && (isAdmin() || v.created_by === S.me.id);
  render(page(`Prompt version v${v.number}`, [v.note, v.compare_to ? `compared with v${v.compare_to.number}` : null].filter(Boolean).join(" · "), [
      vBadge(v.status),
      !isDraft ? h("button", { onclick: async () => { try { const n = await api("POST", "/api/prompts/versions", { based_on: v.id }); location.hash = `#/prompts/${n.id}`; } catch (e) { fail(e); } } }, "New draft from this") : null,
      v.status === "ARCHIVED" && isAdmin() ? h("button", { class: "primary", onclick: () => modal(`Restore v${v.number}`, h("p", {}, `Make v${v.number} the active version again? New resumes will use it immediately.`), (close) => [
        h("button", { onclick: close }, "Cancel"), h("button", { class: "primary", onclick: async () => { try { await api("POST", `/api/prompts/versions/${v.id}/restore`); close(); toast(`v${v.number} is active again.`); promptVersionPage(id, tab); } catch (e) { fail(e); } } }, "Restore")]) }, "Restore this version") : null,
      canDelete ? h("button", { class: "danger", onclick: async () => { try { await api("DELETE", `/api/prompts/versions/${v.id}`); toast("Draft deleted."); location.hash = "#/prompts"; } catch (e) { fail(e); } } }, "Delete draft") : null,
      h("a", { class: "btn", href: "#/prompts" }, "All versions")],
    h("div", { class: "tabs" }, tabs.map(([k, l]) => h("button", { class: k === tab ? "active" : "", onclick: () => go(k) }, l))), body));
}

function improveTab(v) {
  const instr = h("textarea", { rows: 5, placeholder: "Describe what should change, e.g. \"Summaries are too long: keep them under 50 words. For senior roles, lead with leadership and ownership.\"" });
  const useTests = h("input", { type: "checkbox", checked: true });
  const btn = h("button", { class: "primary" }, "Propose changes");
  btn.addEventListener("click", async () => {
    if (instr.value.trim().length < 5) return toast("Describe what you'd like to change.", true);
    btn.disabled = true; btn.textContent = "Asking the AI… (up to a minute)";
    try { await api("POST", `/api/prompts/versions/${v.id}/proposals`, { instruction: instr.value.trim(), use_test_resumes: useTests.checked }); promptVersionPage(v.id, "improve"); }
    catch (e) { fail(e); btn.disabled = false; btn.textContent = "Propose changes"; }
  });
  return h("div", {},
    card("Describe the change", null, h("p", { class: "small muted", style: "margin-top:-6px" }, "Write as if briefing an editor. Point to specific problems you saw in test resumes. The AI proposes small edits; nothing changes until you accept them."),
      instr, h("label", { class: "check small", style: "margin:8px 0" }, useTests, "Show the AI resumes from the latest test round"), btn),
    v.proposals.length ? v.proposals.map((p) => proposalCard(v, p)) : h("div", { class: "empty card" }, "No proposals yet."));
}

function proposalCard(v, p) {
  const reviewed = p.status !== "PROPOSED";
  const choices = p.edits.map((e) => ({ accept: e.status === "PENDING" && !(e.protected && e.protected.length), replace: null }));
  const editCards = p.edits.map((e, i) => {
    const box = h("input", { type: "checkbox", checked: choices[i].accept, disabled: reviewed || e.status === "INVALID", onchange: (ev) => (choices[i].accept = ev.target.checked) });
    const newBox = h("div", { class: "snippet new mono" }, e.replace);
    const editBtn = (!reviewed && e.status === "PENDING") ? h("button", { class: "small link", onclick: () => {
      const ta = h("textarea", { class: "mono", rows: Math.min(14, e.replace.split("\n").length + 2) }, e.replace);
      ta.addEventListener("input", () => (choices[i].replace = ta.value));
      newBox.replaceWith(ta);
    } }, "Adjust text") : null;
    const statusTag = reviewed ? badge({ APPLIED: "Applied", REJECTED: "Rejected", FAILED: "Couldn't apply", INVALID: "Invalid", PENDING: "Not applied" }[e.status] || e.status,
      { APPLIED: "ok", FAILED: "bad", INVALID: "bad" }[e.status] || "") : (e.status === "INVALID" ? badge("Can't apply", "bad") : null);
    return h("div", { class: "edit-card" + (e.protected && e.protected.length ? " protected" : "") },
      h("div", { class: "edit-top" }, h("label", { class: "check" }, box, h("strong", {}, v.labels[e.file] || e.file)),
        h("div", { class: "actions" }, (e.protected || []).map((x) => badge(`Protected: ${x}`, "bad")), statusTag, editBtn)),
      h("div", { class: "small", style: "margin:6px 0" }, e.reason),
      e.problem ? h("div", { class: "notice warn small" }, e.problem) : null,
      h("div", { class: "small muted" }, "Replace"), h("div", { class: "snippet old mono" }, e.find),
      h("div", { class: "small muted" }, "With"), newBox);
  });
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", { style: "margin:0" }, "“" + (p.instruction.length > 120 ? p.instruction.slice(0, 120) + "…" : p.instruction) + "”"),
      h("div", { class: "muted small" }, `${p.created_by_name || ""} · ${fmtDateTime(p.created_at)} · ${p.provider || ""}${p.cost_usd ? ` · $${p.cost_usd.toFixed(3)}` : ""}`)),
      badge({ PROPOSED: "Awaiting review", APPLIED: "Applied", REJECTED: "Rejected" }[p.status] || p.status, { PROPOSED: "info", APPLIED: "ok" }[p.status] || "")),
    p.summary ? h("p", {}, p.summary) : null,
    p.warnings.length ? h("div", { class: "notice warn small" }, h("ul", { style: "margin:0;padding-left:18px" }, p.warnings.map((w) => h("li", {}, w)))) : null,
    editCards.length ? editCards : h("p", { class: "muted" }, "The AI didn't propose any edits."),
    !reviewed ? h("div", { class: "actions" },
      h("button", { class: "primary", onclick: async () => {
        try {
          const r = await api("POST", `/api/prompts/proposals/${p.id}/apply`, { decisions: choices.map((c, i) => ({ index: i, accept: c.accept, replace: c.replace })) });
          const failed = r.results.filter((x) => x.status === "FAILED").length;
          toast(`${r.applied} edit${r.applied === 1 ? "" : "s"} applied${failed ? `, ${failed} couldn't be applied` : ""}. Run a test round next.`, !!failed);
          promptVersionPage(v.id, "improve");
        } catch (e) { fail(e); }
      } }, "Apply selected edits"),
      h("button", { onclick: async () => { try { await api("POST", `/api/prompts/proposals/${p.id}/apply`, { decisions: [] }); promptVersionPage(v.id, "improve"); } catch (e) { fail(e); } } }, "Reject all")) : null);
}

function testTab(v) {
  const compare = h("input", { type: "checkbox", checked: v.status !== "ACTIVE" });
  const run = h("button", { class: "primary" }, "Run test round");
  run.addEventListener("click", async () => {
    run.disabled = true;
    try { await api("POST", `/api/prompts/versions/${v.id}/tests`, { compare: compare.checked }); toast("Test round started."); promptVersionPage(v.id, "test"); }
    catch (e) { fail(e); run.disabled = false; }
  });
  const rounds = v.rounds;
  if (rounds.some((r) => r.state === "RUNNING")) every(2500, () => location.hash.startsWith(`#/prompts/${v.id}`) && promptVersionPage(v.id, "test"));
  const latest = rounds[0];
  const current = latest && latest.content_hash === v.checks.content_hash;
  return h("div", {},
    card("Run your test set", null,
      h("p", { class: "small muted", style: "margin-top:-6px" }, "Each test case is tailored with this version", v.status !== "ACTIVE" ? " and, if ticked, with the active version for comparison" : "", ". Test resumes don't appear in candidates' records or statistics."),
      h("div", { class: "actions" }, v.status !== "ACTIVE" ? h("label", { class: "check" }, compare, "Compare side by side with the active version") : null, run, h("a", { href: "#/prompts", class: "small" }, "Edit test set"))),
    latest ? h("div", {},
      latest && !current && v.status === "DRAFT" ? h("div", { class: "notice warn" }, "This draft changed after the latest test round. Run a new round before publishing.") : null,
      roundView(latest, v),
      rounds.length > 1 ? card("Earlier rounds", null, rounds.slice(1).map((r) => h("div", { class: "small", style: "padding:4px 0" }, `${fmtDateTime(r.created_at)} · ${r.created_by_name || ""} · `, badge(...ROUND_STATE[r.state]), ` · ${r.cases.length} case(s)`))) : null)
      : h("div", { class: "empty card" }, "No test rounds yet."));
}

function roundView(r, v) {
  const hasCompare = !!r.compare_version_id;
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", { style: "margin:0" }, `Test round · ${fmtDateTime(r.created_at)}`),
      h("div", { class: "actions" }, badge(...ROUND_STATE[r.state]), r.cost_usd ? h("span", { class: "small muted" }, `$${r.cost_usd.toFixed(3)}`) : null)),
    r.cases.map((c) => h("div", { style: "margin-bottom:22px" },
      h("h3", {}, `${c.candidate_name} → ${c.job_role}`, c.job_company ? h("span", { class: "muted" }, ` · ${c.job_company}`) : null),
      h("div", { class: hasCompare ? "compare" : "" },
        hasCompare ? runColumn(c.compare, "Active version") : null,
        runColumn(c.draft, `This version (v${v.number})`)))));
}

function runColumn(run, title) {
  if (!run) return h("div", { class: "muted" }, "—");
  const findings = run.findings || [];
  return h("div", {},
    h("div", { class: "actions", style: "margin-bottom:6px" }, h("strong", { class: "small" }, title), runBadge(run), deliveryBadge(run.delivery_status),
      run.seconds ? h("span", { class: "small muted" }, `${run.seconds}s`) : null, run.cost_usd ? h("span", { class: "small muted" }, `$${run.cost_usd.toFixed(3)}`) : null),
    run.error ? h("div", { class: "notice warn small" }, run.error) : null,
    run.resume_markdown ? resumePreview(run.resume_markdown) : (run.status === "DONE" ? h("div", { class: "notice warn small" }, "Withheld by the safety checks.") : null),
    findings.length ? h("details", { class: "small", style: "margin-top:6px" }, h("summary", {}, `${findings.length} open finding${findings.length === 1 ? "" : "s"} from the fact-check`),
      h("ul", {}, findings.map((f) => h("li", {}, `[${f.severity}] ${f.issue}`)))) : null);
}

function changesTab(v) {
  if (!v.compare_to) return h("div", { class: "empty card" }, "Nothing to compare with.");
  if (!v.diff.length) return h("div", { class: "empty card" }, `No differences from v${v.compare_to.number}.`);
  return h("div", {}, h("p", { class: "muted" }, `Differences from v${v.compare_to.number} (${v.compare_to.status === "ACTIVE" ? "active" : "the version this was copied from"}).`),
    v.diff.map((d) => h("div", { class: "diff" }, h("div", { class: "diff-head" }, h("strong", {}, d.label), h("span", { class: "small muted mono" }, `${d.file} · +${d.added} −${d.removed}`)),
      h("div", { class: "mono" }, d.ops.map((o) => o.type === "gap" ? h("div", { class: "diff-line gap" }, "⋯") : h("div", { class: `diff-line ${o.type}` }, o.text || " "))))));
}

function filesTab(v) {
  const isDraft = v.status === "DRAFT";
  const sel = h("select", { style: "max-width:420px" }, v.editable.map((f) => h("option", { value: f }, v.labels[f])));
  const ta = h("textarea", { class: "mono editor", spellcheck: "false", readonly: !isDraft });
  const status = h("span", { class: "small muted" });
  let current = v.editable[0], dirty = false;
  const load = () => { ta.value = v.files[current]; dirty = false; status.textContent = `${current} · ${v.files[current].length.toLocaleString()} characters`; };
  ta.addEventListener("input", () => { dirty = true; status.textContent = "Unsaved changes"; });
  sel.addEventListener("change", () => {
    if (dirty && !confirm("Discard unsaved changes to this file?")) { sel.value = current; return; }
    current = sel.value; load();
  });
  load();
  const save = isDraft ? h("button", { class: "primary", onclick: async () => {
    try { const r = await api("PUT", `/api/prompts/versions/${v.id}/files`, { files: { [current]: ta.value } }); v.files[current] = ta.value; dirty = false;
      status.textContent = "Saved."; toast(r.checks.structural.length ? `Saved, but: ${r.checks.structural[0]}` : "Saved. Run a test round before publishing.", !!r.checks.structural.length); }
    catch (e) { fail(e); }
  } }, "Save file") : null;
  return card(null, null, h("div", { class: "actions", style: "margin-bottom:10px;justify-content:space-between" }, h("div", { class: "actions" }, sel, status), save),
    !isDraft ? h("div", { class: "notice small" }, "Published versions are read-only. Create a new draft to make changes.") : null, ta);
}

function publishTab(v) {
  const c = v.checks;
  const row = (ok, title, detail, kind) => h("div", { class: "checkrow" }, h("span", { class: "icon " + (kind || (ok ? "pass" : "fail")) }, ok ? "✓" : kind === "warn" ? "!" : "✕"), h("div", {}, h("strong", {}, title), detail ? h("div", { class: "small muted" }, detail) : null));
  const testText = { PASSED: "The latest test round on this exact draft finished without failures.", FAILED: "The latest test round has failed resumes.",
    RUNNING: "A test round is still running.", STALE: "The draft changed after the last test round.", NONE: "No test round yet." }[c.test.state];
  const confirmBox = h("input", { type: "checkbox" });
  const note = h("input", { type: "text", value: v.note || "", placeholder: "What changed, e.g. 'Shorter summaries; leadership first for senior roles'" });
  const publish = h("button", { class: "primary", disabled: !c.can_publish || !isAdmin() }, `Publish v${v.number}`);
  publish.addEventListener("click", async () => {
    if (c.protected.length && !confirmBox.checked) return toast("Confirm the protected-rule changes first.", true);
    try { await api("POST", `/api/prompts/versions/${v.id}/publish`, { confirm_protected: confirmBox.checked, note: note.value.trim() || null }); toast(`v${v.number} is now live.`); location.hash = "#/prompts"; }
    catch (e) { fail(e); }
  });
  return h("div", { class: "row" }, h("div", { class: "grow", style: "min-width:380px" },
    card("Checks", null,
      row(!c.structural.length, "Structure", c.structural.length ? c.structural.join("; ") : "Every step prompt is present and well-formed; keyword catalog is valid."),
      row(c.test.state === "PASSED", "Tested", testText),
      row(!c.protected.length, "Protected safety rules", c.protected.length ? "Changed: " + c.protected.map((p) => p.label).join("; ") : "Unchanged.", c.protected.length ? "warn" : null)),
    card("Publish", null,
      !isAdmin() ? h("div", { class: "notice" }, "Only the administrator can publish. Your draft and its test results are ready for review.") : null,
      field("Version note", note),
      c.protected.length ? h("label", { class: "check", style: "margin-bottom:12px" }, confirmBox, "I've reviewed the changes to protected safety rules and want to publish them.") : null,
      h("div", { class: "actions" }, publish, h("span", { class: "small muted" }, "New resumes use the published version immediately. You can restore an earlier version at any time.")))));
}

// ================= settings (administrator) =================
async function settingsPage(params) {
  const s = await api("GET", "/api/settings");
  const d = s.drive;
  const notices = [];
  if (params.get("drive") === "connected") notices.push(h("div", { class: "notice" }, "Google Drive connected. New resumes will be uploaded automatically."));
  if (params.get("drive_error")) notices.push(h("div", { class: "notice warn" }, "Couldn't connect Google Drive: ", params.get("drive_error")));
  let driveCard;
  if (!d.configured) {
    driveCard = card("Google Drive", badge("Not set up", ""),
      h("p", {}, "Connect your Google Drive so every tailored resume is uploaded automatically and your team gets a ", h("strong", {}, "Copy link"), " button."),
      h("h3", {}, "One-time setup (about 10 minutes)"),
      h("ol", { class: "steps small" },
        h("li", {}, "Open ", h("a", { href: "https://console.cloud.google.com/", target: "_blank", rel: "noopener noreferrer" }, "Google Cloud Console ↗"), " with your Google account and create a project (e.g. \"Resume Tailoring\")."),
        h("li", {}, "APIs & Services → Library → enable ", h("strong", {}, "Google Drive API"), "."),
        h("li", {}, "OAuth consent screen / Google Auth Platform: app name, your email; audience ", h("strong", {}, "External"), " (or Internal for Google Workspace). Then ", h("strong", {}, "publish the app"), " (\"In production\"); in Testing mode Google signs you out every 7 days."),
        h("li", {}, "Credentials → Create credentials → ", h("strong", {}, "OAuth client ID"), " → type ", h("strong", {}, "Web application"), ". Add this ", h("strong", {}, "Authorized redirect URI"), ":",
          h("div", { class: "copybox", style: "margin:6px 0" }, h("input", { type: "text", readonly: true, value: d.redirect_uri, onclick: (e) => e.target.select() }), h("button", { type: "button", onclick: () => copy(d.redirect_uri, "Copied.") }, "Copy"))),
        h("li", {}, "Copy the Client ID and Client secret into the ", h("code", {}, ".env"), " file, then restart the app:",
          h("pre", { class: "mono small", style: "background:#f4f5f7;padding:8px 10px;border-radius:6px" }, "RTS_GOOGLE_CLIENT_ID=…apps.googleusercontent.com\nRTS_GOOGLE_CLIENT_SECRET=…")),
        h("li", {}, "Come back here and click ", h("strong", {}, "Connect Google Drive"), ".")));
  } else if (!d.connected) {
    driveCard = card("Google Drive", badge("Not connected", "warn"),
      h("p", {}, "Sign in with the Google account whose Drive should store the resumes. The app can only see files it creates; the rest of your Drive stays private."),
      h("a", { class: "btn primary", href: "/api/google/connect" }, "Connect Google Drive"),
      h("p", { class: "small muted", style: "margin-top:10px" }, "Redirect URI registered in Google Cloud must be exactly: ", h("code", {}, d.redirect_uri)));
  } else {
    const o = d.options;
    const auto = h("input", { type: "checkbox", checked: o.auto_upload });
    const anyone = h("input", { type: "radio", name: "share", checked: o.share_mode === "anyone" });
    const team = h("input", { type: "radio", name: "share", checked: o.share_mode === "team" });
    const pdf = h("input", { type: "checkbox", checked: o.formats.includes("pdf") });
    const docx = h("input", { type: "checkbox", checked: o.formats.includes("docx") });
    const trash = h("input", { type: "checkbox", checked: o.trash_on_delete });
    driveCard = card("Google Drive", badge("Connected", "ok"),
      h("p", {}, "Connected as ", h("strong", {}, d.account_email || "your Google account"), ". ",
        d.folder_link ? h("a", { href: d.folder_link, target: "_blank", rel: "noopener noreferrer" }, "Open the Resume Tailoring folder ↗") : null),
      h("p", { class: "small muted" }, "Each candidate gets a subfolder; files are named Candidate_Company_Job.pdf."),
      h("div", { class: "field" }, h("label", { class: "check" }, auto, "Upload every finished resume automatically")),
      h("div", { class: "field" }, h("label", {}, "Who can open the links"),
        h("label", { class: "check" }, anyone, "Anyone with the link can view ", h("span", { class: "muted small" }, "(works for your team, recruiters and job sites)")),
        h("label", { class: "check" }, team, "Only the candidate's assigned team members ", h("span", { class: "muted small" }, "(they must sign in to Google with the same email they use here)")),
        h("div", { class: "muted small" }, "Applies to new uploads.")),
      h("div", { class: "field" }, h("label", {}, "File types to upload"), h("label", { class: "check" }, pdf, "PDF"), h("label", { class: "check" }, docx, "Word (.docx)"),
        h("div", { class: "muted small" }, "Copy link uses the PDF when both are uploaded.")),
      h("div", { class: "field" }, h("label", { class: "check" }, trash, "When I delete a candidate, move their Drive folder to the trash")),
      h("div", { class: "actions" },
        h("button", { class: "primary", onclick: async () => {
          const formats = [pdf.checked && "pdf", docx.checked && "docx"].filter(Boolean);
          if (!formats.length) return toast("Choose at least one file type.", true);
          try { await api("PUT", "/api/settings/drive", { auto_upload: auto.checked, share_mode: anyone.checked ? "anyone" : "team", formats, trash_on_delete: trash.checked }); toast("Saved."); } catch (e) { fail(e); }
        } }, "Save"),
        h("button", { class: "danger", onclick: () => modal("Disconnect Google Drive", h("p", {}, "New resumes won't be uploaded until you connect again. Files already in Drive stay there, and existing links keep working."), (close) => [
          h("button", { onclick: close }, "Cancel"),
          h("button", { class: "danger", onclick: async () => { try { await api("POST", "/api/settings/drive/disconnect"); close(); toast("Disconnected."); settingsPage(new URLSearchParams()); } catch (e) { fail(e); } } }, "Disconnect")]) }, "Disconnect")));
  }
  render(page("Settings", "Administrator only", null, notices, driveCard,
    card("App", null, h("p", { class: "small" }, `AI: ${s.provider === "mock" ? "mock (simulated, free)" : "Claude"} · Fit threshold: ${s.min_fit} · Time zone: ${s.time_zone}`),
      h("p", { class: "small muted" }, "These are set in the .env file; see the README."))));
}

// ================= router =================
async function router() {
  clearTimers();
  document.querySelectorAll(".modal-backdrop").forEach((m) => m.remove());
  const raw = (location.hash || "#/").slice(1);
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const seg = path.split("/").filter(Boolean);
  try {
    if (seg[0] === "invite" && seg[1]) { if (!S.me) return invitePage(seg[1]); }
    if (!S.me) {
      const st = await api("GET", "/api/auth/state");
      if (st.needs_setup) return setupPage();
      if (!st.user) return loginPage();
      S.me = st.user;
    }
    if (seg[0] === "login" || seg[0] === "invite") { location.hash = "#/"; return; }
    if (!S.meta) S.meta = await api("GET", "/api/meta");
    shell();
    if (!seg.length) return await dashboardPage();
    if (seg[0] === "candidates") {
      if ((seg[1] === "new" || seg[2] === "edit") && !isAdmin()) {   // candidate profiles: administrator only
        location.hash = seg[1] === "new" ? "#/candidates" : `#/candidates/${seg[1]}`; return;
      }
      if (seg[1] === "new") return candidateFormPage(null);
      if (seg[1] && seg[2] === "edit") return await candidateFormPage(seg[1]);
      if (seg[1]) return await candidatePage(seg[1]);
      return await candidatesPage();
    }
    if (seg[0] === "jobs" || seg[0] === "tailor") { location.hash = "#/candidates"; return; }
    if (seg[0] === "applications") return await applicationsPage();
    if (seg[0] === "runs" && seg[1]) return await runPage(seg[1]);
    if (seg[0] === "team") return await teamPage();
    if (seg[0] === "settings") return isAdmin() ? await settingsPage(params) : (location.hash = "#/");
    if (seg[0] === "prompts") return seg[1] ? await promptVersionPage(seg[1], params.get("tab")) : await promptsPage();
    if (seg[0] === "account") return accountPage();
    render(page("Not found", null, null, h("p", {}, "That page doesn't exist. ", h("a", { href: "#/" }, "Go to the dashboard"))));
  } catch (e) {
    if (e.status === 401) { S.me = null; S.meta = null; return loginPage(); }
    render(page("Something went wrong", null, null, h("div", { class: "notice warn" }, e.message)));
  }
}
window.addEventListener("hashchange", () => { if (location.hash === "#/login") { S.me = null; S.meta = null; } router(); });
router();
