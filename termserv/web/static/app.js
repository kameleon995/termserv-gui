// Small behaviours kept out of the HTML so the Content-Security-Policy can
// forbid inline scripts.
(function () {
  "use strict";

  // <form data-confirm="..."> asks before submitting.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      e.preventDefault();
      return;
    }
    // <form data-busy-text="..."> shows progress and prevents double submits.
    var busy = form.getAttribute("data-busy-text");
    if (busy) {
      var button = form.querySelector("button");
      if (button) {
        button.disabled = true;
        button.textContent = busy;
      }
    }
  }, true);

  // Port log: stick to the bottom unless the reader scrolled up.
  function initLog() {
    var log = document.getElementById("log");
    if (!log) return;
    log.scrollTop = log.scrollHeight;
    log.addEventListener("scroll", function () {
      log.dataset.follow = (log.scrollTop + log.clientHeight >= log.scrollHeight - 8) ? "1" : "0";
    });
    document.body.addEventListener("htmx:afterSwap", function (e) {
      if (e.target === log && log.dataset.follow !== "0") log.scrollTop = log.scrollHeight;
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initLog);
  } else {
    initLog();
  }
})();
