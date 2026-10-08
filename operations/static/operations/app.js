/*
 * Small progressive enhancements for the staff web (M10). Every page works without this file;
 * there is no inline script (CSP script-src 'self').
 */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  // Polling (hx-trigger "every Ns") pauses while the tab is hidden and catches up on return.
  document.addEventListener("htmx:beforeRequest", function (event) {
    var source = event.detail && event.detail.elt;
    if (source && source.hasAttribute("data-poll") && document.hidden) {
      event.preventDefault();
    }
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden || !window.htmx) return;
    document.querySelectorAll("[data-poll]").forEach(function (el) {
      window.htmx.trigger(el, "poll");
    });
  });

  // Filter pills submit on change (a submit button remains for no-script use).
  document.addEventListener("change", function (event) {
    var form = event.target.closest && event.target.closest("form[data-autosubmit]");
    if (form) {
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    }
  });

  // One open menu at a time; click outside or Escape closes it.
  function closeMenus(except) {
    document.querySelectorAll("details.menu[open], details.drawer[open]").forEach(function (d) {
      if (d !== except) d.removeAttribute("open");
    });
  }
  document.addEventListener("toggle", function (event) {
    var target = event.target;
    if (target.matches && target.matches("details.menu, details.drawer") && target.open) {
      closeMenus(target);
    }
  }, true);
  document.addEventListener("click", function (event) {
    if (!event.target.closest("details.menu, details.drawer")) closeMenus(null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      var open = document.querySelector("details.menu[open], details.drawer[open]");
      if (open) {
        open.removeAttribute("open");
        var summary = open.querySelector("summary");
        if (summary) summary.focus();
      }
    }
    // "/" jumps to the case-number search (not while typing).
    if (event.key === "/" && !event.target.closest("input, textarea, select, [contenteditable]")) {
      var search = document.getElementById("q");
      if (search) {
        event.preventDefault();
        search.focus();
      }
    }
  });

  // Closing an action form on the case page just empties the slot.
  document.addEventListener("click", function (event) {
    var close = event.target.closest("[data-close-action]");
    var slot = document.getElementById("action-slot");
    if (close && slot && slot.contains(close) && document.querySelector(".detail")) {
      event.preventDefault();
      slot.innerHTML = "";
      var panel = document.getElementById("actions");
      if (panel) panel.scrollIntoView({ block: "nearest" });
    }
  });

  // Reload link inside the "case changed" banner keeps the scroll position.
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-reload]");
    if (link) {
      event.preventDefault();
      window.location.reload();
    }
  });
})();
