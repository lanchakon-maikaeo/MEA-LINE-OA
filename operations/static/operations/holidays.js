/* หน้าวันหยุด (รอบใหม่ Q11): a tap on a day fills the form and names the day, e.g.
 * "อังคาร, 20 ต.ค. 69"; a holiday's day offers to remove it. */
(function () {
  "use strict";
  var day = document.getElementById("hol-day");
  if (!day) return;
  var picked = document.getElementById("hol-picked");
  var name = document.getElementById("hol-name");
  var save = document.getElementById("hol-save");
  var remove = document.getElementById("hol-remove");
  document.querySelectorAll(".mcal__pick").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".mcal__pick.is-picked").forEach(function (x) { x.classList.remove("is-picked"); });
      b.classList.add("is-picked");
      day.value = b.dataset.day;
      picked.textContent = b.dataset.label + (b.dataset.name ? " · วันหยุด " + b.dataset.name : "");
      name.value = b.dataset.name || "";
      save.disabled = false;
      save.textContent = b.dataset.name ? "เปลี่ยนชื่อวันหยุด" : "ตั้งเป็นวันหยุด";
      remove.hidden = !b.dataset.name;
      name.focus();
    });
  });
})();
