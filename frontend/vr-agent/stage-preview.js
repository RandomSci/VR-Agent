/*
 * How a generated web program is shown: one place, used by the Coding Stage
 * and by the automated browser check, so both run it exactly the same way.
 *
 * The program runs in an <iframe sandbox="allow-scripts"> (no popups, no
 * navigation, no downloads, no access to the Stage) with a strict
 * Content-Security-Policy: scripts, images and data may only come from this
 * server's approved folders. Chat cannot make the stream load something from
 * the internet.
 */
(function () {
  "use strict";

  function stagePolicy(origin) {
    const libs = origin + "/stage-libs/";
    const assets = origin + "/stage-assets/";
    return [
      "default-src 'none'",
      "script-src 'unsafe-inline' 'unsafe-eval' " + libs + " blob:",
      "style-src 'unsafe-inline'",
      "img-src " + assets + " data: blob:",
      "media-src data: blob:",
      "font-src data:",
      "connect-src " + assets + " " + libs,
      "worker-src blob:",
      "frame-src 'none'",
      "object-src 'none'",
      "base-uri 'none'",
      "form-action 'none'",
    ].join("; ");
  }

  function previewDocument(source, origin) {
    origin = origin || window.location.origin;
    const html = String(source || "");
    const meta =
      '<meta http-equiv="Content-Security-Policy" content="' + stagePolicy(origin) + '">';
    // The policy goes first INSIDE <head>. Putting anything before
    // <!doctype html> would switch the page into quirks mode.
    const head = html.match(/<head(\s[^>]*)?>/i);
    if (head) {
      const at = head.index + head[0].length;
      return html.slice(0, at) + meta + html.slice(at);
    }
    const doctype = html.match(/^\s*<!doctype[^>]*>/i);
    if (doctype) {
      const at = doctype[0].length;
      return html.slice(0, at) + "<head>" + meta + "</head>" + html.slice(at);
    }
    return "<!doctype html><head>" + meta + "</head>" + html;
  }

  window.StagePreview = { stagePolicy: stagePolicy, previewDocument: previewDocument };
})();
