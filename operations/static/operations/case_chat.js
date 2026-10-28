/* หน้าต่างแชตกับผู้แจ้งในหน้าเรื่อง (grill กคว. 27 ต.ค. 2569 Q15): a floating window that stays
 * open while the card scrolls, opens by itself when the requester wrote something not yet seen,
 * and refreshes every 10 seconds (htmx). What was seen is remembered in this browser only. */
(function () {
  "use strict";
  var win = document.getElementById("chat");
  if (!win || !win.classList.contains("chatwin")) return;
  var panel = win.querySelector(".chatwin__panel");
  var toggle = win.querySelector(".chatwin__toggle");
  var dot = win.querySelector(".chatwin__dot");
  var body = win.querySelector(".chatwin__body");
  var key = "chat-seen-" + win.dataset.case;
  function seen() { try { return localStorage.getItem(key) || ""; } catch (e) { return ""; } }
  function remember(id) { try { if (id) localStorage.setItem(key, id); } catch (e) { /* private window */ } }
  function last() { var list = body.querySelector(".chat__list"); return list ? list.dataset.last : ""; }
  function bottom() { body.scrollTop = body.scrollHeight; }
  function open(state) {
    panel.hidden = !state;
    toggle.setAttribute("aria-expanded", state ? "true" : "false");
    win.classList.toggle("is-open", state);
    if (state) { remember(last()); dot.hidden = true; bottom(); }
  }
  toggle.addEventListener("click", function () { open(panel.hidden); });
  win.querySelector(".chatwin__close").addEventListener("click", function () { open(false); });
  body.addEventListener("htmx:afterSwap", function () {
    var list = body.querySelector(".chat__list");
    var hint = win.querySelector("[data-hint]");
    if (hint && list) hint.textContent = list.dataset.fresh ? "ผู้แจ้งเพิ่งพิมพ์มา ส่งตอนนี้ถึงทันที" : "ข้อความจะถึงผู้แจ้งตอนเขาทักมาครั้งถัดไป";
    if (!panel.hidden) { remember(last()); bottom(); } else if (last() && last() !== seen()) { dot.hidden = false; }
  });
  var text = document.getElementById("chat-text");
  if (text) text.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); if (text.value.trim()) text.form.requestSubmit(); }
  });
  var unread = last() && last() !== seen();
  open(Boolean(unread) || location.hash === "#chat");
})();
