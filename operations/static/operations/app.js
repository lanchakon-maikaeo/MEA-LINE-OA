/*
 * Small progressive enhancements for the staff web (M10). Every page works without this file;
 * there is no inline script (CSP script-src 'self').
 */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  // Polling (hx-trigger "every Ns") pauses while the tab is hidden and catches up on return.
  // It also waits while cases are ticked for approval, so a refresh never clears the ticks.
  document.addEventListener("htmx:beforeRequest", function (event) {
    var source = event.detail && event.detail.elt;
    if (!source || !source.hasAttribute("data-poll")) return;
    if (document.hidden || source.querySelector("input[name=case]:checked")) {
      event.preventDefault();
    }
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden || !window.htmx) return;
    document.querySelectorAll("[data-poll]").forEach(function (el) {
      window.htmx.trigger(el, "poll");
    });
  });

  // Show or hide what is typed in a key field; the value never comes back from the server.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-reveal]");
    if (!button) return;
    var input = document.getElementById(button.getAttribute("data-reveal"));
    if (!input) return;
    var shown = input.type === "password";
    input.type = shown ? "text" : "password";
    button.setAttribute("aria-pressed", shown ? "true" : "false");
    button.textContent = shown ? "ซ่อน" : "แสดง";
  });

  // Filter pills submit on change (a submit button remains for no-script use).
  document.addEventListener("change", function (event) {
    var form = event.target.closest && event.target.closest("form[data-autosubmit]");
    if (form) {
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    }
  });

  // A part of a form that belongs to one choice (29 ต.ค. 2569, ชีทที่ระบบอ่าน), e.g.
  // data-when="match=code": shown only while that choice is picked, and disabled while hidden,
  // so it is neither checked nor sent. The server renders the first state.
  function applyWhen(form) {
    form.querySelectorAll("[data-when]").forEach(function (part) {
      var rule = part.getAttribute("data-when").split("=");
      var control = form.elements[rule[0]];
      var on = !!control && control.value === rule[1];
      part.hidden = !on;
      part.disabled = !on;
    });
  }
  document.addEventListener("change", function (event) {
    var form = event.target.form;
    if (form && form.querySelector("[data-when]")) applyWhen(form);
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

  // Report trend chart: a crosshair and tooltip for the day under the pointer or focus. The same
  // numbers are in the table under the chart, so this only adds speed (values via textContent).
  function chartParts(hit) {
    var chart = hit.closest("[data-chart]");
    return chart && {
      chart: chart,
      svg: chart.querySelector("svg"),
      tip: chart.querySelector("[data-tip]"),
      cross: chart.querySelector("[data-cross]")
    };
  }
  function tipRow(tip, series, value, label) {
    var row = document.createElement("div");
    row.className = "chart-tip__row";
    var key = document.createElement("span");
    key.className = "legend__key legend__key--" + series;
    var number = document.createElement("strong");
    number.textContent = value;
    var name = document.createElement("span");
    name.textContent = label;
    row.appendChild(key);
    row.appendChild(number);
    row.appendChild(name);
    tip.appendChild(row);
  }
  function showDay(hit) {
    var parts = chartParts(hit);
    if (!parts || !parts.tip) return;
    var tip = parts.tip;
    var x = parseFloat(hit.getAttribute("data-x"));
    tip.textContent = "";
    var day = document.createElement("div");
    day.className = "chart-tip__day";
    day.textContent = hit.getAttribute("data-day");
    tip.appendChild(day);
    // Cumulative lines (15 ต.ค. 2569): the total so far, then that day's own count.
    tipRow(tip, "new", hit.getAttribute("data-new-total"), "เข้าสะสม · วันนั้น +" + hit.getAttribute("data-new"));
    tipRow(tip, "ended", hit.getAttribute("data-ended-total"), "จบสะสม · วันนั้น +" + hit.getAttribute("data-ended"));
    var gap = hit.getAttribute("data-new-total") - hit.getAttribute("data-ended-total");
    tipRow(tip, "gap", gap > 0 ? "+" + gap : gap < 0 ? "−" + -gap : "0", "ส่วนต่าง");
    if (parts.cross) {
      parts.cross.setAttribute("x1", x);
      parts.cross.setAttribute("x2", x);
      parts.cross.setAttribute("visibility", "visible");
    }
    tip.hidden = false;
    var box = parts.svg.getBoundingClientRect();
    var frame = parts.chart.getBoundingClientRect();
    var scale = box.width / parts.svg.viewBox.baseVal.width;
    // The tip scrolls with the chart on phones, so positions are in the chart's scrolled box.
    var offset = parts.chart.scrollLeft || 0;
    var left = box.left - frame.left + offset + x * scale + 12;
    if (left + tip.offsetWidth > offset + frame.width) left -= tip.offsetWidth + 24;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = "8px";
  }
  function hideDay(hit) {
    var parts = chartParts(hit);
    if (!parts || !parts.tip) return;
    parts.tip.hidden = true;
    if (parts.cross) parts.cross.setAttribute("visibility", "hidden");
  }
  ["pointerover", "focusin"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var hit = event.target.closest && event.target.closest(".chart__hit");
      if (hit) showDay(hit);
    });
  });
  ["pointerout", "focusout"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var hit = event.target.closest && event.target.closest(".chart__hit");
      if (hit && !(event.relatedTarget && hit.contains(event.relatedTarget))) hideDay(hit);
    });
  });

  // Long lists (คลังคำตอบ, ตัวเลือกระบบ, ผู้ใช้ ...): a search box and filter dropdowns over the
  // items of [data-list-items=NAME]. Without this script every item stays shown.
  function listItems(tools) {
    var box = document.querySelector('[data-list-items="' + tools.getAttribute("data-list") + '"]');
    return box ? Array.prototype.slice.call(box.querySelectorAll("[data-item]")) : [];
  }
  function applyList(tools) {
    var search = tools.querySelector("[data-list-search]");
    var words = search ? search.value.trim().toLowerCase().split(/\s+/).filter(Boolean) : [];
    var filters = Array.prototype.map.call(tools.querySelectorAll("[data-list-filter]"), function (el) {
      return [el.getAttribute("data-list-filter"), el.value];
    });
    var items = listItems(tools);
    var shown = 0;
    items.forEach(function (item) {
      var text = (item.getAttribute("data-search") || item.textContent).toLowerCase();
      var ok = words.every(function (word) { return text.indexOf(word) !== -1; }) &&
        filters.every(function (filter) {
          if (!filter[1]) return true;
          return (item.getAttribute("data-" + filter[0]) || "").split(" ").indexOf(filter[1]) !== -1;
        });
      item.hidden = !ok;
      if (ok) shown += 1;
    });
    filters.forEach(function (filter) {
      var el = tools.querySelector('[data-list-filter="' + filter[0] + '"]');
      if (el) el.classList.toggle("is-set", Boolean(filter[1]));
    });
    var count = tools.querySelector("[data-list-count]");
    var narrowed = words.length || filters.some(function (filter) { return filter[1]; });
    if (count) count.textContent = narrowed ? "พบ " + shown + " จาก " + items.length + " รายการ" : "ทั้งหมด " + items.length + " รายการ";
    var empty = document.querySelector('[data-list-empty="' + tools.getAttribute("data-list") + '"]');
    if (empty) empty.hidden = shown !== 0;
    var box = document.querySelector('[data-list-items="' + tools.getAttribute("data-list") + '"]');
    if (box) {
      // A search or filter shows its matches from the top again.
      box.removeAttribute("data-more-shown");
      applyMore(box);
    }
  }
  // 28 ต.ค. 2569 Q8: a long list shows its first rows (10, or data-more="N") and a
  // "แสดงเพิ่มเติม" button that opens 20 more at a time. data-more-end keeps the last rows
  // instead (older chat messages). Rows hidden by a search stay out of the count.
  var MORE_STEP = 20, moreKept = {};
  function moreItems(box) {
    var rows = box.querySelectorAll(":scope > [data-item], :scope > tr, :scope > li, :scope > [data-more-item]");
    return Array.prototype.filter.call(rows, function (row) { return !row.hidden; });
  }
  function applyMore(box) {
    if (!box.hasAttribute("data-more") && !box.hasAttribute("data-list-items")) return;
    var first = parseInt(box.getAttribute("data-more"), 10) || 10;
    // A list the server refreshes every few seconds (the case chat) keeps what was opened.
    var key = box.getAttribute("data-more-key");
    if (key && moreKept[key] && !box.hasAttribute("data-more-shown")) box.setAttribute("data-more-shown", moreKept[key]);
    var limit = parseInt(box.getAttribute("data-more-shown"), 10) || first;
    var fromEnd = box.hasAttribute("data-more-end");
    var rows = moreItems(box);
    if (fromEnd) rows.reverse();
    rows.forEach(function (row, n) { row.classList.toggle("is-folded", n >= limit); });
    var left = Math.max(0, rows.length - limit);
    var anchor = box.closest(".table-wrap") || box.closest("table") || box;
    var button = box.moreButton;
    if (!button) {
      // A list swapped in by htmx leaves the old list's button behind.
      var near = fromEnd ? anchor.previousElementSibling : anchor.nextElementSibling;
      if (near && near.matches(".more-button[data-more-for]")) near.remove();
      if (!left) return;
      button = document.createElement("button");
      button.setAttribute("data-more-for", "");
      button.type = "button";
      button.className = "more-button";
      button.addEventListener("click", function () {
        var now = parseInt(box.getAttribute("data-more-shown"), 10) || first;
        box.setAttribute("data-more-shown", String(now + MORE_STEP));
        if (box.getAttribute("data-more-key")) moreKept[box.getAttribute("data-more-key")] = String(now + MORE_STEP);
        applyMore(box);
      });
      anchor.parentNode.insertBefore(button, fromEnd ? anchor : anchor.nextSibling);
      box.moreButton = button;
    }
    button.hidden = left === 0;
    var noun = box.getAttribute("data-more-noun") || "รายการ";
    button.textContent = (fromEnd ? "ดูก่อนหน้า" : "แสดงเพิ่มเติม") + " (อีก " + left + " " + noun + ")";
  }
  function initMore(root) {
    (root || document).querySelectorAll("[data-more], [data-list-items]").forEach(applyMore);
  }
  document.addEventListener("htmx:afterSwap", function () { initMore(); });

  // Approve many at once (Q9): tick every waiting case on the page and count the ticks.
  // ADR 0041: the same for LINE OA users; "all" ticks only the rows the filters show.
  function bulkBoxes(form) {
    return document.querySelectorAll('input[type=checkbox][form="' + form.id + '"]');
  }
  function bulkState(form) {
    var boxes = bulkBoxes(form);
    var ticked = Array.prototype.filter.call(boxes, function (b) { return b.checked; }).length;
    // ADR 0030: "every case the filters match" counts those instead of the ticks.
    var filtered = form.querySelector("[data-bulk-filtered]");
    var count = filtered && filtered.checked ? parseInt(filtered.getAttribute("data-count"), 10) || 0 : ticked;
    var all = form.querySelector("[data-bulk-all]");
    if (all) {
      all.checked = boxes.length > 0 && ticked === boxes.length;
      all.indeterminate = ticked > 0 && ticked < boxes.length;
      all.disabled = boxes.length === 0;
    }
    form.querySelectorAll("[data-bulk-submit]").forEach(function (submit) {
      submit.disabled = count === 0;
      if (submit.hasAttribute("data-label")) submit.textContent = count ? submit.getAttribute("data-label").replace("{n}", count) : submit.getAttribute("data-empty");
    });
    var shown = form.querySelector("[data-bulk-count]");
    if (shown) shown.textContent = count ? "เลือกแล้ว " + count + " ราย" : "ยังไม่ได้เลือกผู้ใช้";
  }
  function bulkForms() {
    document.querySelectorAll("form[data-bulk]").forEach(bulkState);
  }
  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches && target.matches("[data-bulk-all]")) {
      bulkBoxes(target.closest("form")).forEach(function (b) {
        b.checked = target.checked && !b.closest("[hidden]");
      });
    }
    if (target.matches && target.matches("[data-bulk-all], [data-bulk-filtered], input[type=checkbox][form]")) bulkForms();
  });
  document.addEventListener("htmx:afterSwap", bulkForms);

  ["input", "change"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var tools = event.target.closest && event.target.closest("[data-list]");
      if (tools) applyList(tools);
    });
  });
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-list-toggle]");
    if (!button) return;
    var open = button.getAttribute("data-list-toggle") === "open";
    listItems(button.closest("[data-list]")).forEach(function (item) {
      if (item.tagName === "DETAILS" && !item.hidden) item.open = open;
    });
  });
  document.querySelectorAll("[data-list]").forEach(applyList);
  initMore();
  bulkForms();
  // A link to #key opens that item.
  if (window.location.hash) {
    var target = document.getElementById(window.location.hash.slice(1));
    var fold = target && (target.tagName === "DETAILS" ? target : target.closest("details.fold"));
    if (fold) fold.open = true;
  }

  // Reload link inside the "case changed" banner keeps the scroll position.
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-reload]");
    if (link) {
      event.preventDefault();
      window.location.reload();
    }
  });

  // ADR 0015 Q21: what was typed survives an outage. Each POST form gets a one-time key (the
  // server applies a key once) and keeps a draft in this browser for 24 hours: never
  // passwords, files or hidden fields. The draft goes when the server confirms the submit
  // (cookie faoj_form_done); a maintenance page sets no cookie, so the draft is still there.
  var DRAFT = "faoj:draft:", DAY = 864e5, store = null;
  try {
    store = window.localStorage;
    store.setItem(DRAFT + "probe", "1");
    store.removeItem(DRAFT + "probe");
  } catch (e) {
    store = null;
  }
  function newKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return Date.now().toString(16) + "-" + Math.random().toString(16).slice(2) + "-" + Math.random().toString(16).slice(2);
  }
  function draftFields(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      var type = (el.type || "").toLowerCase();
      return el.name && !el.disabled && el.name !== "csrfmiddlewaretoken" && el.name !== "_form_key" &&
        ["password", "file", "hidden", "submit", "button", "reset"].indexOf(type) < 0;
    });
  }
  function readDraft(key) {
    try {
      var draft = JSON.parse(store.getItem(key) || "null");
      return draft && Date.now() - draft.t < DAY ? draft : null;
    } catch (e) {
      return null;
    }
  }
  if (store) {
    var done = (document.cookie.match(/(?:^|; )faoj_form_done=([^;]+)/) || [])[1];
    for (var i = store.length - 1; i >= 0; i--) {
      var name = store.key(i);
      if (!name || name.indexOf(DRAFT) !== 0) continue;
      var old = readDraft(name);
      if (!old || (done && old.k === done)) store.removeItem(name);
    }
    if (done) document.cookie = "faoj_form_done=; Max-Age=0; path=/";
  }
  document.querySelectorAll("form").forEach(function (form, index) {
    if ((form.getAttribute("method") || "").toLowerCase() !== "post") return;
    var once = form.querySelector('input[name="_form_key"]');
    if (!once) {
      once = document.createElement("input");
      once.type = "hidden";
      once.name = "_form_key";
      form.appendChild(once);
    }
    once.value = newKey();
    if (!store || form.hasAttribute("data-no-draft")) return;
    var key = DRAFT + window.location.pathname + "|" + (form.getAttribute("action") || "") + "|" + index;
    var saved = readDraft(key), restored = false;
    if (saved) {
      draftFields(form).forEach(function (el) {
        if (!Object.prototype.hasOwnProperty.call(saved.v, el.name)) return;
        var value = saved.v[el.name];
        if (el.type === "checkbox" || el.type === "radio") {
          var on = Array.isArray(value) ? value.indexOf(el.value) >= 0 : value === el.value;
          if (el.checked !== on) { el.checked = on; restored = true; }
        } else if (!el.value && typeof value === "string" && value) {
          el.value = value;
          restored = true;
        }
      });
    }
    if (restored) {
      var note = document.createElement("p");
      note.className = "draft-note";
      note.setAttribute("role", "status");
      note.textContent = "เติมข้อความที่พิมพ์ค้างไว้ให้แล้ว ";
      var drop = document.createElement("button");
      drop.type = "button";
      drop.className = "btn btn--secondary btn--sm";
      drop.textContent = "ล้างข้อความนี้";
      drop.addEventListener("click", function () {
        store.removeItem(key);
        window.location.reload();
      });
      note.appendChild(drop);
      form.insertBefore(note, form.firstChild);
    }
    function save() {
      var values = {};
      draftFields(form).forEach(function (el) {
        if (el.type === "checkbox" || el.type === "radio") {
          if (!el.checked) return;
          values[el.name] = el.type === "checkbox" ? (values[el.name] || []).concat(el.value) : el.value;
        } else if (el.value) {
          values[el.name] = el.value;
        }
      });
      try {
        if (Object.keys(values).length) store.setItem(key, JSON.stringify({ t: Date.now(), k: once.value, v: values }));
        else store.removeItem(key);
      } catch (e) { /* storage full: the form still works */ }
    }
    form.addEventListener("input", save);
    form.addEventListener("change", save);
    form.addEventListener("submit", save);
  });

  // ADR 0036: คัดลอก beside the tracking number on a case card.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button || !navigator.clipboard) return;
    navigator.clipboard.writeText(button.dataset.copy).then(function () {
      var label = button.querySelector("[data-copy-label]");
      if (!label) return;
      label.textContent = "คัดลอกแล้ว";
      setTimeout(function () { label.textContent = "คัดลอก"; }, 2000);
    });
  });

  // ADR 0041 (Q7): Back returns to the same spot of a list — its search, filters, the rows
  // opened with "แสดงเพิ่มเติม" and the scroll — even when the browser loads the page again.
  var backKey = "back:" + location.pathname + location.search;
  function lists() { return document.querySelectorAll("[data-more], [data-list-items]"); }
  window.addEventListener("pagehide", function () {
    var state = { y: window.scrollY, fields: {}, more: [] };
    document.querySelectorAll("[data-list] [data-list-search], [data-list] [data-list-filter]").forEach(function (el) {
      if (el.id) state.fields[el.id] = el.value;
    });
    lists().forEach(function (box) { state.more.push(box.getAttribute("data-more-shown") || ""); });
    try { sessionStorage.setItem(backKey, JSON.stringify(state)); } catch (e) { /* private window */ }
  });
  (function restore() {
    var nav = window.performance && performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
    if (!nav || nav.type !== "back_forward") return;
    var state = null;
    try { state = JSON.parse(sessionStorage.getItem(backKey) || "null"); } catch (e) { return; }
    if (!state) return;
    Object.keys(state.fields).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.value = state.fields[id];
    });
    document.querySelectorAll("[data-list]").forEach(applyList);
    lists().forEach(function (box, n) {
      if (state.more[n]) { box.setAttribute("data-more-shown", state.more[n]); applyMore(box); }
    });
    window.scrollTo(0, state.y);
  })();
  // ADR 0044: a person's card on ผังบุคลากร picks ฝ่าย › กอง › แผนก › กลุ่มงาน from the roster's
  // own paths (#org-units), filled when the card opens; a lower level empties when a higher changes.
  var unitData = document.getElementById("org-units");
  if (unitData) {
    var unitTree = JSON.parse(unitData.textContent);
    var fillUnits = function (box, changed) {
      var node = unitTree;
      box.querySelectorAll("select[data-level]").forEach(function (select, level) {
        var keep = level > changed ? "" : select.value;
        var names = node ? Object.keys(node).sort(function (a, b) { return a.localeCompare(b, "th"); }) : [];
        if (keep && names.indexOf(keep) < 0) names.unshift(keep);
        select.textContent = "";
        if (level > 0) select.add(new Option("ไม่ระบุ", ""));
        names.forEach(function (name) { select.add(new Option(name, name)); });
        select.value = keep;
        select.disabled = !names.length;
        node = node && keep ? node[keep] : null;
      });
      box.setAttribute("data-filled", "1");
    };
    document.addEventListener("toggle", function (event) {
      var pop = event.target.matches && event.target.matches("[popover]");
      var box = pop && event.newState === "open" && event.target.querySelector("[data-cascade]:not([data-filled])");
      if (box) fillUnits(box, 99);
    }, true);
    document.addEventListener("change", function (event) {
      var select = event.target.closest && event.target.closest("[data-cascade] select[data-level]");
      if (select) fillUnits(select.closest("[data-cascade]"), Number(select.getAttribute("data-level")));
    });
  }
  // ADR 0050 Q4, Q5: the chart opens one branch per level, and the tapped card stays where it
  // was on screen (the chart scrolls by however far the card would move). The open branches and
  // the sideways scroll last for this tab, so a saved card finds the chart as it was.
  var chart = document.querySelector("[data-ot]");
  if (chart) {
    var canvas = chart.querySelector(".ot__canvas");
    var state = {};
    try { state = JSON.parse(sessionStorage.getItem("ot-chart") || "{}"); } catch (e) { state = {}; }
    var branch = function (node, on) {
      var below = node.querySelector(":scope > .ot__below");
      var toggle = node.querySelector(":scope > .ot__card > [data-ot-toggle]");
      if (!below || !toggle) return;
      below.hidden = !on;
      toggle.setAttribute("aria-expanded", on ? "true" : "false");
    };
    var save = function () {
      var open = [].map.call(chart.querySelectorAll(".ot__node[data-key] > .ot__below:not([hidden])"), function (below) {
        return below.parentNode.getAttribute("data-key");
      });
      try { sessionStorage.setItem("ot-chart", JSON.stringify({ open: open, left: chart.scrollLeft })); } catch (e) { /* private mode */ }
    };
    // Room on both sides, so the chart can always scroll to hold a card still.
    var pad = function () { canvas.style.setProperty("--ot-pad", Math.round(chart.clientWidth / 2) + "px"); };
    if (Array.isArray(state.open)) {
      chart.querySelectorAll(".ot__node[data-key]").forEach(function (node) {
        branch(node, state.open.indexOf(node.getAttribute("data-key")) !== -1);
      });
    }
    pad();
    if (typeof state.left === "number") {
      chart.scrollLeft = state.left;
    } else {
      // The whole tree in the middle of the frame, or from its left edge when it is wider.
      var pad0 = parseFloat(getComputedStyle(canvas).paddingLeft) || 0;
      var wide = canvas.scrollWidth - 2 * pad0;
      chart.scrollLeft = wide <= chart.clientWidth ? pad0 + wide / 2 - chart.clientWidth / 2 : pad0 - 16;
    }
    // ADR 0051 Q7: the account page links to a person's card (#op-<employee>); open the
    // branches down to it and the card itself.
    var person = /^#op-\d+$/.test(location.hash) && document.getElementById(location.hash.slice(1));
    if (person && chart.contains(person) && person.showPopover) {
      for (var up = person.closest(".ot__node[data-key]"); up; up = up.parentNode.closest(".ot__node[data-key]")) {
        [].forEach.call(up.parentNode.children, function (other) { if (other !== up) branch(other, false); });
        branch(up, true);
      }
      save();
      var opener = chart.querySelector('[popovertarget="' + person.id + '"]');
      if (opener) opener.scrollIntoView({ block: "center", inline: "center" });
      person.showPopover();
    }
    chart.addEventListener("click", function (event) {
      var toggle = event.target.closest("[data-ot-toggle]");
      if (!toggle) return;
      var node = toggle.closest(".ot__node"), before = toggle.getBoundingClientRect();
      var on = toggle.getAttribute("aria-expanded") !== "true";
      if (on) {
        [].forEach.call(node.parentNode.children, function (other) { if (other !== node) branch(other, false); });
      }
      branch(node, on);
      var after = toggle.getBoundingClientRect();
      chart.scrollLeft += after.left - before.left;
      if (after.top !== before.top) window.scrollBy(0, after.top - before.top);
      save();
    });
    var wait;
    chart.addEventListener("scroll", function () { clearTimeout(wait); wait = setTimeout(save, 150); }, { passive: true });
    window.addEventListener("resize", function () {
      var left = chart.scrollLeft - parseFloat(getComputedStyle(canvas).paddingLeft || 0);
      pad();
      chart.scrollLeft = left + parseFloat(getComputedStyle(canvas).paddingLeft || 0);
    });
  }
  // ADR 0050 Q10: a Push Message switch saves at once without a reload; the row, its group and
  // the people line follow. On an error the form posts as before, so the page shows the message.
  var pushRow = function (kind) {
    if (!kind) return;
    var on = function (sel) { return kind.querySelector(sel + ' .switch[aria-checked="true"]'); };
    kind.classList.toggle("is-off", !on('.push-kind__sw[data-channel="LINE"]') && !on('.push-kind__sw[data-channel="อีเมล"]'));
    var reach = kind.querySelector("[data-reach]"), rows = kind.querySelectorAll(".push-to__row");
    if (reach && rows.length) {
      var names = [].filter.call(rows, function (row) {
        return row.querySelector('.switch[aria-checked="true"]');
      }).map(function (row) { return row.firstElementChild.textContent; });
      reach.textContent = names.length ? names[0] + (names.length > 1 ? " +" + (names.length - 1) : "") : "ยังไม่มีผู้รับ";
    }
    var group = kind.closest(".push-group");
    if (group) group.querySelectorAll("[data-count]").forEach(function (count) {
      var channel = count.getAttribute("data-count");
      count.textContent = group.querySelectorAll('.push-kind__sw[data-channel="' + channel + '"] .switch[aria-checked="true"]').length;
    });
  };
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form[data-push-switch]") || !window.fetch) return;
    event.preventDefault();
    var switches = document.querySelectorAll('button[form="' + form.id + '"]');
    switches.forEach(function (b) { b.disabled = true; });
    fetch(form.action, { method: "POST", body: new FormData(form), credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (response) {
        if (!response.ok) throw new Error("save failed");
        return response.json();
      })
      .then(function (data) {
        var on = data.value === "on";
        form.elements.value.value = on ? "off" : "on";
        switches.forEach(function (b) {
          b.setAttribute("aria-checked", on ? "true" : "false");
          b.querySelector(".switch__text").textContent = on ? "ส่ง" : "ไม่ส่ง";
          var row = b.closest(".push-to__row");
          if (row) row.classList.toggle("is-off", !on);
          pushRow(b.closest(".push-kind"));
        });
      })
      .catch(function () { HTMLFormElement.prototype.submit.call(form); })
      .then(function () { switches.forEach(function (b) { b.disabled = false; }); });
  });
  document.addEventListener("click", function (event) {
    var all = event.target.closest && event.target.closest("[data-push-all]");
    if (!all) return;
    var open = all.getAttribute("data-push-all") === "open";
    document.querySelectorAll(".push-group, .push-kind__fold").forEach(function (fold) { fold.open = open; });
  });
  // Folds marked data-remember keep their open state for this tab.
  var remembered = document.querySelectorAll("details[data-remember][id]");
  if (remembered.length) {
    var folds = {};
    try { folds = JSON.parse(sessionStorage.getItem("folds:" + location.pathname) || "{}"); } catch (e) { folds = {}; }
    remembered.forEach(function (fold) {
      if (Object.prototype.hasOwnProperty.call(folds, fold.id)) fold.open = folds[fold.id];
    });
    document.addEventListener("toggle", function (event) {
      var fold = event.target;
      if (!fold.matches || !fold.matches("details[data-remember][id]")) return;
      folds[fold.id] = fold.open;
      try { sessionStorage.setItem("folds:" + location.pathname, JSON.stringify(folds)); } catch (e) { /* private mode */ }
    }, true);
  }
  // A link to the page we came from steps back instead, so that page comes back as it was.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.target || link.hasAttribute("download") || !document.referrer || history.length < 2) return;
    var from = new URL(document.referrer), to = new URL(link.href, location.href);
    if (from.origin !== location.origin || to.origin !== location.origin || to.hash) return;
    if (from.pathname + from.search !== to.pathname + to.search) return;
    event.preventDefault();
    history.back();
  });
})();
