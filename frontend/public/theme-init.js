// Apply the theme before first paint so there is no flash of the wrong one.
// Loaded as a classic blocking script from index.html (a file, not inline, so the
// Content-Security-Policy can stay script-src 'self'). Keep in sync with
// src/lib/theme.ts (same storage key and rule).
(function () {
  var dark = false;
  try {
    var stored = localStorage.getItem('teamboss.theme');
    dark = stored === 'dark' || (stored !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  } catch (e) {
    dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  if (dark) document.documentElement.classList.add('dark');
})();
