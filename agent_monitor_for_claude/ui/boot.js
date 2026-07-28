/* Head bootstrap: the two steps that must happen before the page renders.
   Kept in its own file, not an inline <script>, so the page's
   Content-Security-Policy can forbid inline script outright (see index.html).

   This file and boot-scripts.js are the only assets loaded without the ?v=
   cache-busting token - nothing could supply it before this code runs. They
   stay tiny and near-static for that reason, and pywebview's asset route sends
   no-store anyway. */

(function () {
    // Apply the saved / preferred theme before first paint to avoid a flash.
    try {
        var t = localStorage.getItem('amc-theme')
            || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
        document.documentElement.setAttribute('data-theme', t);
    } catch (e) {
        document.documentElement.setAttribute('data-theme', 'dark');
    }
})();

(function () {
    // Version every bundled asset with the ?v= token from the page URL
    // (see app._asset_version). document.write keeps these parser-inserted,
    // preserving load order and DOMContentLoaded timing exactly, while the
    // fresh URL stops WebView2's persistent cache serving a stale copy after
    // an app update.
    var v = new URLSearchParams(location.search).get('v');
    window.__assetQuery = v ? '?v=' + encodeURIComponent(v) : '';
    document.write('<link rel="stylesheet" href="index.css' + window.__assetQuery + '">');
})();
