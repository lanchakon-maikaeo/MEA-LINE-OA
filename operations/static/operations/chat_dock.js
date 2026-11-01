/* แชตกับผู้แจ้งในทุกหน้า (ADR 0042 Q8): a round button with the number of messages not yet
 * answered; it lists the cases with messages in the last 7 days, opens one case's thread, and
 * sends what is typed as it is. It checks every 5 seconds and opens by itself when a new
 * message arrives. On a case page it opens that case. What was seen is kept in this browser. */
(function () {
  "use strict";
  var dock = document.getElementById("chatdock");
  if (!dock) return;
  var panel = dock.querySelector(".chatwin__panel");
  var toggle = dock.querySelector(".chatwin__toggle");
  var dot = dock.querySelector("[data-dock-dot]");
  var list = dock.querySelector("[data-dock-list]");
  var thread = dock.querySelector("[data-dock-thread]");
  var body = dock.querySelector("[data-dock-body]");
  var back = dock.querySelector("[data-dock-back]");
  var sub = dock.querySelector("[data-dock-sub]");
  var form = dock.querySelector("[data-dock-form]");
  var status = dock.querySelector("[data-dock-status]");  // none for a read-only account
  var here = document.querySelector("[data-dock-case]");
  var KEY = "chat-dock-latest";
  var current = null;

  function seen() { try { return parseInt(localStorage.getItem(KEY) || "0", 10) || 0; } catch (e) { return 0; } }
  function remember(id) { try { localStorage.setItem(KEY, String(id)); } catch (e) { /* private window */ } }
  function get(url) {
    return fetch(url, { credentials: "same-origin", headers: { "X-Dock": "1" } }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.text();
    });
  }
  function say(text) { if (status) { status.textContent = text; status.hidden = !text; } }

  function open(state) {
    panel.hidden = !state;
    toggle.setAttribute("aria-expanded", state ? "true" : "false");
    dock.classList.toggle("is-open", state);
    if (state) refresh(true);
  }
  function showList() {
    current = null;
    thread.hidden = true;
    list.hidden = false;
    back.hidden = true;
    sub.textContent = "เรื่องที่มีการสนทนาภายใน 7 วัน";
  }
  function showThread(item) {
    current = item;
    list.hidden = true;
    thread.hidden = false;
    back.hidden = false;
    sub.textContent = item.title;
    say("");
    loadThread(true);
  }
  function loadThread(toEnd) {
    if (!current) return;
    var item = current;
    get(item.thread).then(function (html) {
      if (current !== item) return;
      var atEnd = body.scrollTop + body.clientHeight >= body.scrollHeight - 8;
      body.innerHTML = html;
      if (toEnd || atEnd) body.scrollTop = body.scrollHeight;
    }).catch(function () { /* the next round tries again */ });
  }
  function loadList(first) {
    return get(dock.dataset.list).then(function (html) {
      list.innerHTML = html;
      var box = list.firstElementChild || {};
      var data = box.dataset || {};
      var total = parseInt(data.total || "0", 10) || 0;
      var latest = parseInt(data.latest || "0", 10) || 0;
      dot.hidden = !total;
      dot.textContent = total > 99 ? "99+" : String(total);
      if (latest > seen()) {
        // A message nobody here has seen yet opens the window (not on the first visit ever).
        if (seen() && panel.hidden && !first) open(true);
        remember(latest);
      }
    }).catch(function () { /* the next round tries again */ });
  }
  function refresh(first) {
    loadList(first);
    if (current && !panel.hidden) loadThread(false);
  }

  list.addEventListener("click", function (event) {
    var row = event.target.closest("[data-dock-open]");
    if (row) showThread({ thread: row.dataset.thread, post: row.dataset.post, title: row.dataset.title });
  });
  back.addEventListener("click", function () { showList(); loadList(); });
  toggle.addEventListener("click", function () { open(panel.hidden); });
  dock.querySelector(".chatwin__close").addEventListener("click", function () { open(false); });
  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!current) return;
      var text = form.elements.text;
      if (!text.value.trim()) return;
      fetch(current.post, { method: "POST", credentials: "same-origin", headers: { "X-Dock": "1" }, body: new FormData(form) })
        .then(function (r) { return r.json(); })
        .then(function (result) {
          if (result.error) { say(result.error); return; }
          say("");
          text.value = "";
          loadThread(true);
          loadList();
        })
        .catch(function () { say("ส่งข้อความไม่สำเร็จ โปรดลองอีกครั้ง"); });
    });
    form.elements.text.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }

  if (here) {
    showThread({ thread: here.dataset.thread, post: here.dataset.post, title: here.dataset.title });
    if (location.hash === "#chat") open(true);
  } else {
    showList();
  }
  loadList(true);
  setInterval(function () { if (!document.hidden) refresh(false); }, 5000);
})();
