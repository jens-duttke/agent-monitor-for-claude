/* End-of-body bootstrap: pulls in the UI scripts with the same ?v= token
   boot.js derived, so an app update cannot be served a stale copy.

   Sits at the end of <body> - exactly where the inline <script> it replaces
   sat - so load order and DOMContentLoaded timing are unchanged. It is a
   separate file for the same reason as boot.js: the page's
   Content-Security-Policy forbids inline script.

   The controller scripts share one global scope, so their order here is
   their dependency order: core.js first, index.js - which starts everything -
   last. tests/js/global-scope.test.js reads this list and loads the files in
   exactly this order. */

(function () {
    var q = window.__assetQuery || '';
    var scripts = [
        'logic.js',
        'core.js',
        'tooltip.js',
        'header.js',
        'menu.js',
        'proc-panel.js',
        'dialogs.js',
        'settings.js',
        'filters.js',
        'search.js',
        'render.js',
        'detail.js',
        'actions.js',
        'index.js',
    ];
    for (var i = 0; i < scripts.length; i++) {
        document.write('<script src="' + scripts[i] + q + '"><\/script>');
    }
})();
