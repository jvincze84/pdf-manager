"use strict";

// All URLs are relative so the app also works behind a reverse proxy under a sub-path.
const API = "api/";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  q: "", name: "", folder: "", tags: [], ocr: "", sort: "", order: "",
  items: [], total: 0, selected: new Set(),
};

// ------------------------------------------------------------------ utils

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtSize(b) {
  if (b == null) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? b : b.toFixed(b < 10 ? 1 : 0)) + " " + u[i];
}
function fmtDate(iso, withTime = false) {
  if (!iso) return "";
  const d = new Date(iso);
  const p = n => String(n).padStart(2, "0");
  const s = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  return withTime ? `${s} ${p(d.getHours())}:${p(d.getMinutes())}` : s;
}
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(API + path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  return r.json();
}
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function splitTags(s) {
  return s.split(",").map(t => t.trim()).filter(Boolean);
}

const ICON = {
  pdf: '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M6 2h8l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M14 2v5h5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
  download: '<svg viewBox="0 0 24 24" width="17" height="17"><path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M4 20h16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  external: '<svg viewBox="0 0 24 24" width="17" height="17"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  edit: '<svg viewBox="0 0 24 24" width="17" height="17"><path d="M4 20h4L19 9l-4-4L4 16v4z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
  trash: '<svg viewBox="0 0 24 24" width="17" height="17"><path d="M4 7h16M9 7V4h6v3m-8 0l1 13h8l1-13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

// ------------------------------------------------------------------ URL state

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = p.get("q") || "";
  state.name = p.get("name") || "";
  state.folder = p.get("folder") || "";
  state.tags = p.getAll("tag");
  state.ocr = p.get("ocr") || "";
  state.sort = p.get("sort") || "";
  state.order = p.get("order") || "";
}
function writeUrl() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.name) p.set("name", state.name);
  if (state.folder) p.set("folder", state.folder);
  state.tags.forEach(t => p.append("tag", t));
  if (state.ocr) p.set("ocr", state.ocr);
  if (state.sort) { p.set("sort", state.sort); p.set("order", state.order); }
  const qs = p.toString();
  history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
}

// ------------------------------------------------------------------ list

// The list is loaded incrementally: the first FIRST_BATCH rows, then NEXT_BATCH more whenever
// the user scrolls near the bottom. The browser never has to render the whole library.
const FIRST_BATCH = 100, NEXT_BATCH = 20, MAX_RELOAD = 1000;
let loadSeq = 0, loadingMore = false;

function listParams() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.name) p.set("name", state.name);
  if (state.folder) p.set("folder", state.folder);
  state.tags.forEach(t => p.append("tag", t));
  if (state.ocr) p.set("ocr", state.ocr);
  if (state.sort) { p.set("sort", state.sort); p.set("order", state.order); }
  return p;
}

// Filters / search / sort changed: start again from the top.
function load() {
  window.scrollTo({ top: 0 });
  return fetchRange(0, FIRST_BATCH, "replace");
}

// Data changed on the server: re-fetch the rows already loaded, keeping the scroll position.
function reload() {
  return fetchRange(0, Math.min(Math.max(FIRST_BATCH, state.items.length), MAX_RELOAD), "replace");
}

async function loadMore() {
  if (loadingMore || state.items.length >= state.total) return;
  loadingMore = true;
  renderFooter();
  try { await fetchRange(state.items.length, NEXT_BATCH, "append"); } finally { loadingMore = false; renderFooter(); }
  maybeLoadMore();
}

async function fetchRange(offset, limit, mode) {
  writeUrl();
  const p = listParams();
  p.set("offset", offset);
  p.set("limit", limit);
  const seq = mode === "append" ? loadSeq : ++loadSeq;
  try {
    const data = await api("documents?" + p);
    if (seq !== loadSeq) return; // a newer load replaced the list meanwhile
    state.total = data.total;
    if (data.query_error) toast(data.query_error, "error");
    if (mode === "append") {
      const known = new Set(state.items.map(d => d.id));
      const fresh = data.items.filter(d => !known.has(d.id));
      state.items.push(...fresh);
      $("#rows").insertAdjacentHTML("beforeend", fresh.map(rowHtml).join(""));
      renderChrome();
    } else {
      state.items = data.items;
      render();
      maybeLoadMore();
    }
  } catch (e) {
    toast("Failed to load documents: " + e.message, "error");
  }
}

// IntersectionObserver only fires on changes, so after each load check whether the
// end of the list is still on screen (tall window / short rows) and keep filling.
function maybeLoadMore() {
  const r = $("#sentinel").getBoundingClientRect();
  if (r.top < window.innerHeight + 600) loadMore();
}

// Re-fetch only some rows (e.g. OCR status changes) and patch them in place.
async function refreshRows(ids) {
  const loaded = new Set(state.items.map(d => d.id));
  ids = [...new Set(ids)].filter(id => loaded.has(id));
  if (!ids.length) return;
  try {
    const { items } = await api("documents/batch", { json: { ids } });
    const byId = new Map(items.map(d => [d.id, d]));
    for (const id of ids) {
      const idx = state.items.findIndex(d => d.id === id);
      if (idx < 0) continue;
      const tr = $(`#rows tr[data-id="${id}"]`);
      const fresh = byId.get(id);
      if (!fresh) { // deleted meanwhile
        state.items.splice(idx, 1);
        state.total = Math.max(0, state.total - 1);
        tr?.remove();
        continue;
      }
      if (state.items[idx].snippet) fresh.snippet = state.items[idx].snippet;
      state.items[idx] = fresh;
      if (tr) tr.outerHTML = rowHtml(fresh);
    }
    renderChrome();
  } catch { /* next poll will retry */ }
}

function ocrCell(d) {
  let badge, btn;
  if (d.ocr_status === "done") {
    badge = `<span class="badge ok" title="OCR done ${esc(fmtDate(d.ocr_at, true))}">OCR</span>`;
    btn = `<button class="btn small ghost" data-act="ocr" title="Run OCR again">Re-run</button>`;
  } else if (d.ocr_status === "queued") {
    badge = `<span class="badge busy" title="Waiting in OCR queue">OCR · queued</span>`;
    btn = `<button class="btn small ghost" data-act="ocr-cancel">Cancel</button>`;
  } else if (d.ocr_status === "running") {
    const pr = d.ocr_progress ? ` ${d.ocr_progress.done}/${d.ocr_progress.total}` : "";
    badge = `<span class="badge busy" title="OCR in progress">OCR${pr}</span>`;
    btn = `<button class="btn small ghost" data-act="ocr-cancel">Cancel</button>`;
  } else {
    const title = d.ocr_status === "error" ? "OCR failed: " + d.ocr_error : "Not OCR'd yet";
    badge = `<span class="badge bad" title="${esc(title)}">OCR${d.ocr_status === "error" ? " · error" : ""}</span>`;
    btn = `<button class="btn small" data-act="ocr">Run OCR</button>`;
  }
  return badge + btn;
}

function snippetHtml(s) {
  return esc(s).replace(/\u0002/g, "<mark>").replace(/\u0003/g, "</mark>").replace(/\s+/g, " ");
}

function rowHtml(d) {
    let sub = "";
    if (d.text_status === "pending") sub = `<div class="sub">Reading text…</div>`;
    else if (d.text_status === "error") sub = `<div class="sub warn-text" title="${esc(d.text_error)}">Could not read PDF: ${esc(d.text_error)}</div>`;
    else if (!d.has_text && d.ocr_status !== "done") sub = `<div class="sub warn-text">No text layer – run OCR to make it searchable</div>`;
    const date = d.file_date || d.added_at;
    return `
    <tr data-id="${d.id}" class="${state.selected.has(d.id) ? "selected" : ""}">
      <td class="col-check"><input type="checkbox" class="row-check" ${state.selected.has(d.id) ? "checked" : ""}></td>
      <td class="name-cell">
        <a class="name-link" href="viewer.html?id=${d.id}">${ICON.pdf}<span>${esc(d.filename)}</span></a>
        ${d.snippet ? `<div class="snippet">${snippetHtml(d.snippet)}</div>` : ""}
        ${sub}
      </td>
      <td><span class="folder-link" data-folder="${esc(d.folder || "/")}">${esc(d.folder ? d.folder : "/")}</span></td>
      <td class="num nowrap">${fmtSize(d.size)}</td>
      <td class="num">${d.pages ?? ""}</td>
      <td class="nowrap" title="File date: ${esc(fmtDate(date, true))}\nAdded: ${esc(fmtDate(d.added_at, true))}">${fmtDate(date)}</td>
      <td>${d.tags.map(t => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("")}</td>
      <td class="ocr-cell">${ocrCell(d)}</td>
      <td class="col-actions"><div class="row-actions">
        <a class="icon-btn" href="${API}documents/${d.id}/file" target="_blank" rel="noopener" title="Open in browser">${ICON.external}</a>
        <a class="icon-btn" href="${API}documents/${d.id}/file?download=1" title="Download">${ICON.download}</a>
        <button class="icon-btn" data-act="edit" title="Edit name, folder, tags">${ICON.edit}</button>
        <button class="icon-btn danger" data-act="delete" title="Delete">${ICON.trash}</button>
      </div></td>
    </tr>`;
}

function render() {
  $("#rows").innerHTML = state.items.map(rowHtml).join("");
  renderChrome();
}

// Everything around the rows: empty state, sort arrows, footer, filters, bulk bar.
function renderChrome() {
  const empty = $("#empty");
  const filtered = hasFilters();
  $("#clearFiltersBtn").hidden = !filtered;
  if (state.items.length === 0) {
    empty.innerHTML = filtered
      ? `No documents match your search. <button class="btn small" type="button" data-clear-filters>Clear all filters &amp; search</button>`
      : "No documents yet. Click <b>Upload</b> or drop PDF files here.";
    empty.hidden = false;
  } else empty.hidden = true;

  $$("th.sortable").forEach(th => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort);
    th.classList.toggle("asc", th.dataset.sort === state.sort && state.order === "asc");
  });
  $("#checkAll").checked = state.items.length > 0 && state.items.every(d => state.selected.has(d.id));
  renderFooter();
  renderActiveTags();
  renderFolderActions();
  renderBulkbar();
}

function renderFooter() {
  const n = state.items.length, t = state.total;
  let more = "";
  if (n < t) more = loadingMore ? `<span class="spinner"></span> Loading more…` : `Scroll down to load more`;
  else if (t > FIRST_BATCH) more = "All documents loaded";
  $("#pager").innerHTML = `
    <span>Showing ${n} of ${t} ${state.q || state.name ? "matching " : ""}document${t === 1 ? "" : "s"}</span>
    <span class="more">${more}</span>`;
}

function renderActiveTags() {
  $("#activeTags").innerHTML = state.tags.map(t =>
    `<span class="tag filter" data-remove-tag="${esc(t)}" title="Remove filter">${esc(t)} <span class="x">×</span></span>`).join("");
}

function renderFolderActions() {
  $("#folderActions").hidden = !state.folder;
  $("#folderActionsName").textContent = state.folder;
}

async function ocrFolder(all) {
  const f = state.folder;
  if (!f) return;
  if (all && !confirm(`Run OCR on ALL documents in "${f}" (and subfolders), including already processed ones?`)) return;
  try {
    const r = await api(`ocr/${all ? "all" : "unprocessed"}?folder=${encodeURIComponent(f)}`, { method: "POST" });
    toast(r.queued ? `${r.queued} documents in "${f}" queued for OCR` : `Nothing to do in "${f}"`, "ok");
    reload(); pollStatusSoon();
  } catch (e) { toast(e.message, "error"); }
}

function renderBulkbar() {
  const n = state.selected.size;
  $("#bulkbar").hidden = n === 0;
  $("#bulkCount").textContent = `${n} selected`;
}

// ------------------------------------------------------------------ filters / meta

async function loadMeta() {
  try {
    const [folders, tags] = await Promise.all([api("folders"), api("tags")]);
    const fsel = $("#folderFilter");
    const folderOpts = [`<option value="">All folders</option>`];
    if (folders.root) folderOpts.push(`<option value="/">/ (root only) (${folders.root})</option>`);
    folders.folders.forEach(f => {
      const depth = f.path.split("/").length - 1;
      folderOpts.push(`<option value="${esc(f.path)}">${"  ".repeat(depth)}${esc(f.path.split("/").pop())} (${f.count})</option>`);
    });
    if (state.folder && state.folder !== "/" && !folders.folders.some(f => f.path === state.folder))
      folderOpts.push(`<option value="${esc(state.folder)}">${esc(state.folder)}</option>`);
    fsel.innerHTML = folderOpts.join("");
    fsel.value = state.folder;
    $("#folderList").innerHTML = folders.folders.map(f => `<option value="${esc(f.path)}">`).join("");

    const tsel = $("#tagFilter");
    tsel.innerHTML = `<option value="">${state.tags.length ? "Add tag filter…" : "All tags"}</option>` +
      tags.filter(t => !state.tags.includes(t.name))
        .map(t => `<option value="${esc(t.name)}">${esc(t.name)} (${t.count})</option>`).join("");
    $("#tagList").innerHTML = tags.map(t => `<option value="${esc(t.name)}">`).join("");
  } catch (e) {
    toast("Failed to load folders/tags: " + e.message, "error");
  }
}

let changeSeq = null;
async function pollStatus() {
  let active = false;
  try {
    const s = await api("status?since=" + (changeSeq ?? 0));
    active = s.ocr_queued + s.ocr_running + s.text_pending > 0;
    $("#stats").innerHTML =
      `<b>${s.total}</b> documents · ${fmtSize(s.total_size)} · ` +
      `<span style="color:var(--ok)">OCR done: ${s.ocr_done}</span> · ` +
      `<span style="color:var(--bad)">not OCR'd: ${s.ocr_not_done}</span>` +
      (s.ocr_error ? ` (${s.ocr_error} failed)` : "");
    const parts = [];
    if (s.text_pending) parts.push(`Reading text: ${s.text_pending} left`);
    if (s.ocr_running || s.ocr_queued) {
      const n = s.ocr_running;
      const running = n ? `${n} document${n === 1 ? "" : "s"} running (up to ${s.ocr_parallel} pages in parallel)` : "starting";
      const cur = s.running.map(r =>
        esc(r.filename) + (r.progress ? ` (page ${r.progress.done}/${r.progress.total})` : "")).join(", ");
      parts.push(`OCR: ${running}, ${s.ocr_queued} queued${cur ? " — " + cur : ""}`);
    }
    $("#ocrActivity").innerHTML = parts.length ? `<span class="spinner"></span> ${parts.join(" · ")}` : "";
    const ch = s.changes;
    if (changeSeq !== null) {
      if (ch.reload) { reload(); loadMeta(); }
      else refreshRows([...ch.ids, ...s.running.map(r => r.id)]); // running rows: page progress
    }
    changeSeq = ch.seq;
  } catch { /* server unavailable: retry later */ }
  schedulePoll(active ? 2000 : 10000);
}
let pollTimer;
function schedulePoll(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(pollStatus, ms); }
function pollStatusSoon() { schedulePoll(300); }

// After an action: refresh loaded rows + folder/tag lists, keep scroll position.
function refreshAll() {
  reload();
  loadMeta();
}

function hasFilters() {
  return !!(state.q || state.name || state.folder || state.tags.length || state.ocr);
}

// "Clear all filters & search": content + file-name search, folder, tags, OCR filter (sort is kept).
function clearFilters() {
  Object.assign(state, { q: "", name: "", folder: "", tags: [], ocr: "" });
  $("#q").value = "";
  $("#nameQ").value = "";
  $("#ocrFilter").value = "";
  $("#folderFilter").value = "";
  resetAll();
}

// A filter changed: back to the top + refresh folder/tag lists.
function resetAll() {
  load();
  loadMeta();
}

// ------------------------------------------------------------------ row actions

async function docAction(act, id) {
  const d = state.items.find(x => x.id === id);
  try {
    if (act === "ocr") {
      await api(`documents/${id}/ocr`, { method: "POST" });
      toast(`OCR queued: ${d.filename}`);
      refreshRows([id]); pollStatusSoon();
    } else if (act === "ocr-cancel") {
      await api(`documents/${id}/ocr/cancel`, { method: "POST" });
      setTimeout(() => refreshRows([id]), 400);
    } else if (act === "delete") {
      if (!confirm(`Delete "${d.filename}"?\nThe file will be removed from the library.`)) return;
      await api(`documents/${id}`, { method: "DELETE" });
      state.selected.delete(id);
      toast("Deleted " + d.filename, "ok");
      refreshAll();
    } else if (act === "edit") {
      openEdit(d);
    }
  } catch (e) {
    toast(e.message, "error");
  }
}

// ------------------------------------------------------------------ edit dialog

let editDoc = null, editTags = [];
function renderTagEditor() {
  const ed = $("#tagEditor");
  $$(".tag", ed).forEach(e => e.remove());
  const input = $("#tagInput");
  editTags.forEach((t, i) => {
    const s = document.createElement("span");
    s.className = "tag";
    s.innerHTML = `${esc(t)} <span class="x" data-i="${i}">×</span>`;
    ed.insertBefore(s, input);
  });
}
function openEdit(d) {
  editDoc = d;
  editTags = [...d.tags];
  $("#editName").value = d.filename;
  $("#editFolder").value = d.folder;
  $("#tagInput").value = "";
  $("#editInfo").innerHTML =
    `${fmtSize(d.size)} · ${d.pages ?? "?"} pages · added ${esc(fmtDate(d.added_at, true))}` +
    (d.ocr_at ? ` · OCR ${esc(fmtDate(d.ocr_at, true))}` : "");
  renderTagEditor();
  $("#editDlg").showModal();
}
function commitTagInput() {
  const input = $("#tagInput");
  splitTags(input.value).forEach(t => {
    if (!editTags.some(x => x.toLowerCase() === t.toLowerCase())) editTags.push(t);
  });
  input.value = "";
  renderTagEditor();
}

// ------------------------------------------------------------------ prompt dialog

function promptDlg(title, label, value = "", datalist = "") {
  return new Promise(resolve => {
    const dlg = $("#promptDlg");
    $("#promptTitle").textContent = title;
    $("#promptLabel").textContent = label;
    const input = $("#promptInput");
    input.value = value;
    if (datalist) input.setAttribute("list", datalist); else input.removeAttribute("list");
    dlg.returnValue = "";
    dlg.onclose = () => resolve(dlg.returnValue === "ok" ? input.value : null);
    dlg.showModal();
    input.focus();
  });
}

// ------------------------------------------------------------------ bulk

async function bulk(action) {
  const ids = [...state.selected];
  if (!ids.length) return;
  const body = { ids, action };
  if (action === "add_tags" || action === "remove_tags") {
    const v = await promptDlg(action === "add_tags" ? `Add tags to ${ids.length} documents` : `Remove tags from ${ids.length} documents`,
      "Tags (comma separated)", "", "tagList");
    if (v == null) return;
    body.tags = splitTags(v);
    if (!body.tags.length) return;
  } else if (action === "move") {
    const v = await promptDlg(`Move ${ids.length} documents`, "Target folder (empty = library root)", state.folder && state.folder !== "/" ? state.folder : "", "folderList");
    if (v == null) return;
    body.folder = v;
  } else if (action === "delete") {
    if (!confirm(`Delete ${ids.length} documents? The files will be removed from the library.`)) return;
  }
  try {
    const r = await api("bulk", { json: body });
    const verb = { ocr: "queued for OCR", delete: "deleted", add_tags: "tagged", remove_tags: "updated", move: "moved" }[action];
    toast(`${r.affected} document${r.affected === 1 ? "" : "s"} ${verb}`, "ok");
    if (action === "delete" || action === "move") state.selected.clear();
    refreshAll();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ------------------------------------------------------------------ upload

const uploads = []; // {file, relpath, state, el, progress}
let uploading = false;

function addFiles(list) {
  let skipped = 0;
  for (const { file, relpath } of list) {
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") { skipped++; continue; }
    const el = document.createElement("div");
    el.className = "up-item";
    el.innerHTML = `<div class="up-name" title="${esc(relpath || file.name)}">${esc(relpath || file.name)}</div>
      <div class="up-size">${fmtSize(file.size)}</div><div class="up-state">Waiting</div>`;
    $("#uploadList").appendChild(el);
    uploads.push({ file, relpath, state: "waiting", el });
  }
  if (skipped) toast(`${skipped} non-PDF file${skipped === 1 ? "" : "s"} skipped`);
  updateUploadSummary();
}

function updateUploadSummary() {
  const c = s => uploads.filter(u => u.state === s).length;
  const total = uploads.length;
  $("#uploadSummary").textContent = total
    ? `${total} files (${fmtSize(uploads.reduce((a, u) => a + u.file.size, 0))}) · ` +
      `uploaded ${c("ok")} · duplicates ${c("dup")} · failed ${c("err")} · waiting ${c("waiting")}`
    : "";
  $("#uploadStart").disabled = uploading || c("waiting") === 0;
}

function uploadOne(u, folder, tags, skipDup) {
  return new Promise(resolve => {
    const fd = new FormData();
    fd.append("file", u.file, u.file.name);
    fd.append("folder", folder);
    fd.append("relpath", u.relpath || "");
    fd.append("tags", tags);
    fd.append("skip_duplicates", skipDup ? "true" : "false");
    if (u.file.lastModified) fd.append("last_modified", String(u.file.lastModified));
    const st = $(".up-state", u.el);
    st.innerHTML = `<div class="progress"><div></div></div>`;
    const bar = $(".progress > div", u.el);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + "documents/upload");
    xhr.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = (100 * e.loaded / e.total) + "%"; };
    xhr.onload = () => {
      let res = {};
      try { res = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (res.status === "duplicate") {
          u.state = "dup"; st.className = "up-state dup"; st.textContent = "Already exists";
          st.title = res.existing;
        } else {
          u.state = "ok"; st.className = "up-state ok"; st.textContent = "Uploaded";
        }
      } else {
        u.state = "err"; st.className = "up-state err";
        st.textContent = "Failed: " + (res.detail || xhr.statusText || xhr.status);
        st.title = st.textContent;
      }
      resolve();
    };
    xhr.onerror = () => { u.state = "err"; st.className = "up-state err"; st.textContent = "Network error"; resolve(); };
    xhr.send(fd);
  });
}

async function startUpload() {
  if (uploading) return;
  uploading = true;
  updateUploadSummary();
  const folder = $("#uploadFolder").value.trim();
  const tags = $("#uploadTags").value;
  const skipDup = $("#skipDup").checked;
  const queue = uploads.filter(u => u.state === "waiting");
  queue.forEach(u => { u.state = "uploading"; });
  const workers = Array.from({ length: 3 }, async () => {
    while (queue.length) {
      const u = queue.shift();
      await uploadOne(u, folder, tags, skipDup);
      updateUploadSummary();
    }
  });
  await Promise.all(workers);
  uploading = false;
  updateUploadSummary();
  const ok = uploads.filter(u => u.state === "ok").length;
  toast(`Upload finished: ${ok} new document${ok === 1 ? "" : "s"}`, "ok");
  refreshAll();
  pollStatusSoon();
}

async function filesFromDrop(dt) {
  const out = [];
  const items = [...(dt.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!items.length) return [...dt.files].map(f => ({ file: f, relpath: "" }));
  const readAll = reader => new Promise((res, rej) => {
    const all = [];
    const next = () => reader.readEntries(batch => { if (!batch.length) res(all); else { all.push(...batch); next(); } }, rej);
    next();
  });
  async function walk(entry, prefix) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, relpath: prefix ? prefix + "/" + file.name : "" });
    } else if (entry.isDirectory) {
      const children = await readAll(entry.createReader());
      for (const c of children) await walk(c, prefix ? prefix + "/" + entry.name : entry.name);
    }
  }
  for (const e of items) await walk(e, "");
  return out;
}

function openUpload() {
  $("#uploadDlg").showModal();
}

// ------------------------------------------------------------------ text viewer

let textData = null, textTab = "text";
async function showText(d) {
  try {
    textData = await api(`documents/${d.id}/text`);
    $("#textTitle").textContent = d.filename;
    setTextTab(textData.text && textData.text.trim() ? "text" : "ocr_text");
    $("#textDlg").showModal();
  } catch (e) { toast(e.message, "error"); }
}
function setTextTab(tab) {
  textTab = tab;
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  const v = (textData[tab] || "").trim();
  $("#textView").textContent = v || (tab === "ocr_text" ? "(no OCR text – run OCR first)" : "(no text layer in this PDF)");
}

// navigator.clipboard only exists in secure contexts (https / localhost): fall back to execCommand.
async function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall back */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  $("#textDlg").appendChild(ta); // inside the modal dialog, otherwise it can't be focused
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* ignore */ }
  ta.remove();
  return ok;
}

// ------------------------------------------------------------------ duplicates

let dupGroups = [];
async function openDuplicates() {
  $("#dupList").innerHTML = "";
  $("#dupSummary").textContent = "Searching…";
  $("#dupDelete").disabled = true;
  if (!$("#dupDlg").open) $("#dupDlg").showModal();
  try {
    const r = await api("duplicates");
    dupGroups = r.groups;
    $("#dupList").innerHTML = dupGroups.map((g, gi) => `
      <div class="dup-group" data-group="${gi}">
        <div class="dup-head">
          <b>${g.count} identical files</b> · ${fmtSize(g.size)} each
          <span class="spacer"></span>
          <label><input type="radio" name="dup-${gi}" value="all"> Keep all</label>
        </div>
        ${g.items.map((d, i) => `
          <label class="dup-item">
            <input type="radio" name="dup-${gi}" value="${d.id}" ${i === 0 ? "checked" : ""}>
            <div style="min-width:0">
              <a class="name-link" href="viewer.html?id=${d.id}">${ICON.pdf}<span class="dup-path"><span class="dup-dir">${esc(d.folder ? d.folder + "/" : "/")}</span>${esc(d.filename)}</span></a>
              <div class="dup-meta">added ${fmtDate(d.added_at, true)}${d.tags.length ? " · " + d.tags.map(esc).join(", ") : ""}</div>
            </div>
            <span class="badge ${d.ocr_status === "done" ? "ok" : "bad"}">OCR</span>
            <span class="dup-state"></span>
          </label>`).join("")}
      </div>`).join("");
    if (!dupGroups.length) {
      $("#dupSummary").textContent = "No duplicates found – every file in the library is unique.";
    }
    updateDupState();
  } catch (e) {
    $("#dupSummary").textContent = "";
    toast("Failed to find duplicates: " + e.message, "error");
  }
}
function dupKeepIds() {
  return dupGroups.map((g, gi) => $(`input[name="dup-${gi}"]:checked`)?.value).filter(v => v && v !== "all").map(Number);
}
function updateDupState() {
  let n = 0, bytes = 0;
  dupGroups.forEach((g, gi) => {
    const keep = $(`input[name="dup-${gi}"]:checked`)?.value;
    $$(`[data-group="${gi}"] .dup-item`).forEach(el => {
      const v = $("input", el).value;
      const del = keep !== "all" && v !== keep;
      el.classList.toggle("del", del);
      el.classList.toggle("keep", !del);
      $(".dup-state", el).textContent = keep === "all" ? "" : del ? "delete" : "keep";
      if (del) { n++; bytes += g.size; }
    });
  });
  if (dupGroups.length) {
    const total = dupGroups.reduce((a, g) => a + g.count - 1, 0);
    $("#dupSummary").textContent = `${dupGroups.length} groups, ${total} duplicate files. Selected for deletion: ${n} (${fmtSize(bytes)}).`;
  }
  $("#dupDelete").disabled = n === 0;
  $("#dupDelete").textContent = n ? `Delete ${n} duplicate${n === 1 ? "" : "s"}` : "Delete duplicates";
}
async function deleteDuplicates() {
  const keep = dupKeepIds();
  const n = $$("#dupList .dup-item.del").length;
  if (!n || !confirm(`Delete ${n} duplicate files? One copy of each is kept.`)) return;
  try {
    const r = await api("duplicates/delete", { json: { keep } });
    toast(`${r.deleted} duplicates deleted` + (r.skipped ? `, ${r.skipped} skipped (content differs on disk)` : ""), r.skipped ? "" : "ok");
    refreshAll();
    openDuplicates();
  } catch (e) { toast(e.message, "error"); }
}

// ------------------------------------------------------------------ wiring

function init() {
  const openDupDialog = location.hash === "#duplicates"; // direct link to the duplicates dialog
  readUrl();
  $("#q").value = state.q;
  $("#nameQ").value = state.name;
  $("#ocrFilter").value = state.ocr;

  const onSearch = debounce(() => {
    state.q = $("#q").value.trim();
    load();
  }, 300);
  $("#q").addEventListener("input", onSearch);
  $("#q").addEventListener("keydown", e => { if (e.key === "Escape") { $("#q").value = ""; onSearch(); } });

  const onNameSearch = debounce(() => { state.name = $("#nameQ").value.trim(); load(); }, 300);
  $("#nameQ").addEventListener("input", onNameSearch);
  $("#nameQ").addEventListener("keydown", e => { if (e.key === "Escape") { $("#nameQ").value = ""; onNameSearch(); } });
  $("#ocrFolderUnprocessedBtn").onclick = () => ocrFolder(false);
  $("#ocrFolderAllBtn").onclick = () => ocrFolder(true);

  $("#searchHelpBtn").onclick = e => { e.stopPropagation(); $("#searchHelp").hidden = !$("#searchHelp").hidden; };
  $("#moreBtn").onclick = e => { e.stopPropagation(); $("#moreMenu").hidden = !$("#moreMenu").hidden; };
  document.addEventListener("click", e => {
    if (!e.target.closest("#searchHelp")) $("#searchHelp").hidden = true;
    if (!e.target.closest("#moreMenu")) $("#moreMenu").hidden = true;
  });

  $("#themeBtn").onclick = () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    localStorage.setItem("theme", t);
  };

  $("#folderFilter").onchange = e => { state.folder = e.target.value; load(); };
  $("#tagFilter").onchange = e => {
    if (e.target.value && !state.tags.includes(e.target.value)) state.tags.push(e.target.value);
    resetAll();
  };
  $("#clearFiltersBtn").onclick = clearFilters;
  $("#empty").addEventListener("click", e => { if (e.target.closest("[data-clear-filters]")) clearFilters(); });
  $("#ocrFilter").onchange = e => { state.ocr = e.target.value; load(); };
  $("#activeTags").onclick = e => {
    const t = e.target.closest("[data-remove-tag]");
    if (!t) return;
    state.tags = state.tags.filter(x => x !== t.dataset.removeTag);
    resetAll();
  };

  $("thead").onclick = e => {
    const th = e.target.closest("th.sortable");
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sort === key) {
      if (state.order === "desc") state.order = "asc";
      else { state.sort = ""; state.order = ""; } // third click: back to default (relevance / newest)
    } else { state.sort = key; state.order = ["name", "folder"].includes(key) ? "asc" : "desc"; }
    load();
  };

  $("#checkAll").onchange = e => {
    state.items.forEach(d => e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id));
    render();
  };

  $("#rows").addEventListener("click", e => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const id = +tr.dataset.id;
    if (e.target.classList.contains("row-check")) {
      e.target.checked ? state.selected.add(id) : state.selected.delete(id);
      tr.classList.toggle("selected", e.target.checked);
      $("#checkAll").checked = state.items.every(d => state.selected.has(d.id));
      renderBulkbar();
      return;
    }
    const act = e.target.closest("[data-act]");
    if (act) { docAction(act.dataset.act, id); return; }
    const tag = e.target.closest("[data-tag]");
    if (tag) {
      if (!state.tags.includes(tag.dataset.tag)) state.tags.push(tag.dataset.tag);
      resetAll(); return;
    }
    const folder = e.target.closest("[data-folder]");
    if (folder) { state.folder = folder.dataset.folder; $("#folderFilter").value = state.folder; load(); }
  });

  new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) loadMore();
  }, { rootMargin: "600px" }).observe($("#sentinel"));

  // global OCR buttons
  $("#ocrUnprocessedBtn").onclick = async () => {
    try {
      const r = await api("ocr/unprocessed", { method: "POST" });
      toast(r.queued ? `${r.queued} documents queued for OCR` : "Nothing to do – every document has been OCR'd", "ok");
      reload(); pollStatusSoon();
    } catch (e) { toast(e.message, "error"); }
  };
  $("#ocrAllBtn").onclick = async () => {
    if (!confirm("Run OCR on ALL documents, including the ones already processed?\nThis can take a long time.")) return;
    try {
      const r = await api("ocr/all", { method: "POST" });
      toast(`${r.queued} documents queued for OCR`, "ok");
      reload(); pollStatusSoon();
    } catch (e) { toast(e.message, "error"); }
  };
  $("#cancelOcrBtn").onclick = async () => {
    $("#moreMenu").hidden = true;
    try {
      const r = await api("ocr/cancel", { method: "POST" });
      toast(`${r.cancelled} OCR jobs cancelled`, "ok");
      setTimeout(reload, 500);
    } catch (e) { toast(e.message, "error"); }
  };
  $("#dupBtn").onclick = () => { $("#moreMenu").hidden = true; openDuplicates(); };
  $("#dupList").addEventListener("change", updateDupState);
  $("#dupDelete").onclick = deleteDuplicates;
  $("#dupKeepAll").onclick = () => { $$('#dupList input[value="all"]').forEach(i => { i.checked = true; }); updateDupState(); };
  $("#dupKeepSuggested").onclick = () => { $$("#dupList .dup-group").forEach(g => { $(".dup-item input", g).checked = true; }); updateDupState(); };
  $("#rescanBtn").onclick = async () => {
    $("#moreMenu").hidden = true;
    toast("Scanning library folder…");
    try {
      const r = await api("rescan", { method: "POST" });
      toast(`Rescan done: ${r.added} added, ${r.removed} removed`, "ok");
      refreshAll();
    } catch (e) { toast(e.message, "error"); }
  };

  // bulk bar
  $$("[data-bulk]").forEach(b => { b.onclick = () => bulk(b.dataset.bulk); });
  $("#bulkClear").onclick = () => { state.selected.clear(); render(); };

  // upload dialog
  $("#uploadBtn").onclick = openUpload;
  $("#fileInput").onchange = e => { addFiles([...e.target.files].map(f => ({ file: f, relpath: "" }))); e.target.value = ""; };
  $("#dirInput").onchange = e => { addFiles([...e.target.files].map(f => ({ file: f, relpath: f.webkitRelativePath || "" }))); e.target.value = ""; };
  $("#uploadStart").onclick = startUpload;
  $("#uploadClear").onclick = () => {
    if (uploading) return;
    uploads.length = 0;
    $("#uploadList").innerHTML = "";
    updateUploadSummary();
  };
  const dz = $("#dropzone");
  dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", async e => {
    e.preventDefault(); dz.classList.remove("dragover");
    addFiles(await filesFromDrop(e.dataTransfer));
  });
  // dropping on the main table opens the upload dialog with the files
  const dt = $("#dropTarget");
  dt.addEventListener("dragover", e => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); dt.classList.add("dragover"); } });
  dt.addEventListener("dragleave", e => { if (!dt.contains(e.relatedTarget)) dt.classList.remove("dragover"); });
  dt.addEventListener("drop", async e => {
    e.preventDefault(); dt.classList.remove("dragover");
    const files = await filesFromDrop(e.dataTransfer);
    openUpload();
    addFiles(files);
  });

  // edit dialog
  $("#tagInput").addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commitTagInput(); }
    else if (e.key === "Backspace" && !e.target.value && editTags.length) { editTags.pop(); renderTagEditor(); }
  });
  $("#tagInput").addEventListener("change", commitTagInput);
  $("#tagEditor").onclick = e => {
    if (e.target.classList.contains("x")) { editTags.splice(+e.target.dataset.i, 1); renderTagEditor(); }
    else $("#tagInput").focus();
  };
  $("#editSave").onclick = async () => {
    commitTagInput();
    const name = $("#editName").value.trim();
    if (!name) { $("#editName").focus(); return; }
    try {
      await api(`documents/${editDoc.id}`, {
        method: "PATCH",
        json: { tags: editTags, filename: name, folder: $("#editFolder").value.trim() },
      });
      $("#editDlg").close();
      toast("Saved", "ok");
      refreshAll();
    } catch (e) { toast(e.message, "error"); }
  };
  $("#viewTextBtn").onclick = () => showText(editDoc);
  $$(".tab").forEach(t => { t.onclick = () => setTextTab(t.dataset.tab); });
  $("#copyTextBtn").onclick = async () => {
    const text = (textData?.[textTab] || "").trim();
    if (!text) { toast("Nothing to copy"); return; }
    if (await copyToClipboard(text)) toast(`Copied ${text.length.toLocaleString()} characters`, "ok");
    else toast("Copy failed – select the text and press Ctrl+C", "error");
  };

  // keyboard: "/" focuses search
  document.addEventListener("keydown", e => {
    if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && !document.querySelector("dialog[open]")) {
      e.preventDefault(); $("#q").focus();
    }
  });

  refreshAll();
  pollStatus();
  if (openDupDialog) openDuplicates();
}

init();

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("sw.js").catch(e => console.warn("Service worker registration failed", e));
}
