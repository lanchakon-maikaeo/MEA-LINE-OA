/* requirement ของกอง: the box list follows the chosen flow (ADR 0023). */
(function () {
  "use strict";
  var data = document.getElementById("req-boxes");
  if (!data) return;
  var boxes = JSON.parse(data.textContent);
  document.querySelectorAll("select[data-boxes-for]").forEach(function (flowSelect) {
    var nodeSelect = document.getElementById(flowSelect.dataset.boxesFor);
    function fill() {
      var current = nodeSelect.dataset.current || "";
      nodeSelect.innerHTML = "";
      var none = document.createElement("option");
      none.value = ""; none.textContent = flowSelect.value ? "เลือกกล่อง" : "ไม่ผูก";
      nodeSelect.append(none);
      (boxes[flowSelect.value] || []).forEach(function (b) {
        var o = document.createElement("option");
        o.value = b.id; o.textContent = b.title || b.id; o.selected = b.id === current;
        nodeSelect.append(o);
      });
      nodeSelect.disabled = !flowSelect.value;
    }
    flowSelect.addEventListener("change", function () { nodeSelect.dataset.current = ""; fill(); });
    fill();
  });
})();
