// Built-in PDF viewer page: viewer.html?id=<document id>
// A thin bar (back, file name, open in browser, download) above PDF.js (served at pdfjs/).
"use strict";

const $ = s => document.querySelector(s);
const id = Number(new URLSearchParams(location.search).get("id"));
const fileUrl = `api/documents/${id}/file`;

function showError(msg) {
  $("#viewerMsg").textContent = msg;
  $("#viewerMsg").hidden = false;
  $("#viewerFrame").hidden = true;
}

// Back: return to the list as it was (history) when we came from the app, else open the list.
$("#backBtn").addEventListener("click", e => {
  let fromApp = false;
  try { fromApp = !!document.referrer && new URL(document.referrer).origin === location.origin; } catch (_) { /* ignore */ }
  if (fromApp && history.length > 1) {
    e.preventDefault();
    history.back();
  }
});

if (!id) {
  showError("No document selected.");
} else {
  $("#browserBtn").href = fileUrl;
  $("#downloadBtn").href = fileUrl + "?download=1";

  fetch(`api/documents/${id}`)
    .then(r => {
      if (!r.ok) throw new Error(r.status === 404 ? "Document not found." : `Error ${r.status}`);
      return r.json();
    })
    .then(d => {
      document.title = d.filename;
      $("#docDir").textContent = d.folder ? d.folder + "/" : "";
      $("#docName").textContent = d.filename;
      $("#docTitle").title = (d.folder ? d.folder + "/" : "") + d.filename;
    })
    .catch(e => showError(e.message));

  // PDF.js gets an absolute URL so it also works when the app runs under a proxy sub-path.
  const frame = $("#viewerFrame");
  const absFile = new URL(fileUrl, location.href).href;
  frame.addEventListener("load", () => {
    // follow the app's light/dark theme (PDF.js styles use light-dark())
    try { frame.contentDocument.documentElement.style.colorScheme = document.documentElement.dataset.theme; } catch (_) { /* ignore */ }
  });
  frame.src = "pdfjs/web/viewer.html?file=" + encodeURIComponent(absFile);
}
