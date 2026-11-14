/* หน้างานคลังและตารางกำหนดจ่าย (28 ต.ค. 2569 Q3, Q5). Without this file every field and both
 * parts of the schedule show, and the selection bar stays at the foot of the page. */
(function () {
  "use strict";

  // The issue date field shows only when the stage is คีย์จ่ายแล้ว.
  function planned(select) {
    var form = select.closest("form");
    var field = form && form.querySelector("[data-planned]");
    if (field) field.hidden = select.value !== "keyed";
  }
  document.querySelectorAll("[data-stage-select]").forEach(function (select) {
    planned(select);
    select.addEventListener("change", function () { planned(select); });
  });

  // The selection bar appears once a slip is ticked and counts the ticks.
  var bar = document.querySelector("[data-wh-bar]");
  if (bar) {
    var count = bar.querySelector("[data-pick-count]");
    var update = function () {
      var ticked = document.querySelectorAll('input[name=case][form="wh-bar"]:checked').length;
      bar.classList.toggle("is-empty", ticked === 0);
      count.textContent = "เลือก " + ticked + " ใบ";
    };
    document.addEventListener("change", function (event) {
      if (event.target.matches('input[name=case][form="wh-bar"]')) update();
    });
    update();
  }

  // Schedule: กำหนดจ่าย, วันส่งรถ and ชื่อขั้น as tabs (#truck or #stages opens the others).
  var tabs = document.querySelectorAll("[data-sc-tab]");
  function show(part) {
    tabs.forEach(function (tab) {
      if (tab.dataset.scTab === part) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-sc-part]").forEach(function (el) {
      el.hidden = el.dataset.scPart !== part;
    });
  }
  if (tabs.length) {
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        show(tab.dataset.scTab);
        history.replaceState(null, "", tab.dataset.scTab === "sla" ? location.pathname + location.search : "#" + tab.dataset.scTab);
      });
    });
    var opened = location.hash.slice(1);
    show(document.querySelector('[data-sc-tab="' + opened + '"]') ? opened : tabs[0].dataset.scTab);
  }

  // A size row is added from the group's blank row and removed by its own button.
  document.querySelectorAll("[data-sc-group]").forEach(function (group) {
    var sizes = group.querySelector("[data-sc-sizes]");
    var blank = sizes.querySelector("[data-sc-blank]");
    var model = blank.cloneNode(true);
    model.removeAttribute("data-sc-blank");
    blank.remove();
    group.querySelector("[data-sc-add]").addEventListener("click", function () {
      var row = model.cloneNode(true);
      sizes.appendChild(row);
      row.querySelector("input[type=text]").focus();
    });
    sizes.addEventListener("click", function (event) {
      var remove = event.target.closest("[data-sc-remove]");
      if (remove) remove.closest(".sc-row").remove();
    });
  });

  // วันส่งรถ: the same days for every district in one go.
  var apply = document.querySelector("[data-sc-apply]");
  if (apply) {
    apply.addEventListener("click", function () {
      var days = Array.prototype.map.call(document.querySelectorAll("[data-sc-all]:checked"), function (b) { return b.value; });
      document.querySelectorAll('.sc-trucks input[type=checkbox]').forEach(function (b) {
        b.checked = days.indexOf(b.value) !== -1;
      });
    });
  }
})();
