/* End-of-body bootstrap: pulls in the UI scripts with the same ?v= token
   boot.js derived, so an app update cannot be served a stale copy.

   Sits at the end of <body> - exactly where the inline <script> it replaces
   sat - so load order and DOMContentLoaded timing are unchanged. It is a
   separate file for the same reason as boot.js: the page's
   Content-Security-Policy forbids inline script. */

(function () {
    var q = window.__assetQuery || '';
    document.write('<script src="logic.js' + q + '"><\/script>');
    document.write('<script src="index.js' + q + '"><\/script>');
})();
