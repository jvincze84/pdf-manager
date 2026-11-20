// Installed app (PWA) on Android: a link opened in a new window (target=_blank, e.g. "Open in browser")
// lands in a window without history, so Back closes the whole app. Open such links in the same
// window instead, so Back returns to the page they came from.
// (iOS keeps target=_blank: it shows the page in an overlay with a "Done" button.)
(function () {
  var android = /Android/i.test(navigator.userAgent);
  var installed = matchMedia("(display-mode: standalone), (display-mode: fullscreen), (display-mode: minimal-ui)").matches;
  if (!android || !installed) return;
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest('a[target="_blank"]');
    if (!a || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    location.href = a.href;
  });
})();
