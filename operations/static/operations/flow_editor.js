/* flow LINE editor (ADR 0023): boxes and lines on a canvas, saved as the draft's graph.
 * The server checks every save (no dead ends, LINE limits, every question has a field and a
 * rule) and answers with the issues, so the panel always shows what the draft needs.
 * No inline styles in markup (CSP style-src 'self'); positions are set through the CSSOM. */
(function () {
  "use strict";

  var cfgEl = document.getElementById("flow-config");
  if (!cfgEl) return;
  var CFG = JSON.parse(cfgEl.textContent);
  var KINDS = CFG.kinds, ROLES = CFG.roles, RULES = CFG.rules, LIMITS = CFG.limits;
  var EDITABLE = CFG.version.editable;
  var ICONS = { entry: "▶", message: "💬", ask: "✎", branch: "⑂", summary: "≡", create_case: "＋", find_case: "⌕", koj_tracking: "⌕", sheet_lookup: "▤", book: "▦", ai_read: "AI", handoff: "☎", web: "↗", limit: "⛔", result: "✓", note: "✱" };
  var PALETTE = [
    ["คุยกับผู้ใช้", ["message", "ask", "branch", "summary", "result"]],
    ["ระบบทำงาน", ["create_case", "find_case", "koj_tracking", "sheet_lookup", "book", "ai_read", "handoff"]],
    ["อื่น ๆ", ["entry", "web", "limit", "note"]],
  ];
  var CONFIG_FIELDS = {
    koj_tracking: [["equipment", "อุปกรณ์ (ว่างเปิดเมนู กอจ.)", "select", ":เมนู กอจ.|transformer:หม้อแปลง|lbs:LBS|rmu:RMU|metering:ตู้ metering|frtu:LBS FRTU|bushing:bushing box|vt_lbs:VT ประกอบ LBS|lv_cap:LV Cap.|other:อุปกรณ์อื่น"], ["service", "บริการ (ว่างให้ผู้ใช้เลือก)", "select", ":เลือกบริการ|withdrawal:ติดตามใบเบิก|return:ติดตามใบส่งคืน|repair:ติดตามสถานะการซ่อม|watches:รายการติดตาม กอจ."]],
    ask: [["field", "ช่องที่เก็บ", "text", "เช่น purpose, sap_document, rows.meter"], ["rule", "กฎตรวจ", "rule"], ["choices", "ปุ่มเลือกชนิดก่อนพิมพ์", "text", "คั่นด้วย | เช่น เลขที่ใบเบิก|ใบสั่งงาน SAP"], ["optional", "ข้ามได้", "flag"], ["max_rows", "จำนวนแถวสูงสุด", "text", "ว่างไว้ถ้าไม่จำกัด"]],
    create_case: [["case_kind", "ชนิดเรื่อง", "select", "withdrawal:เบิก|return:ส่งคืน|problem:แจ้งปัญหา"], ["division", "กองปลายทาง", "text", "เช่น กคว."], ["topic", "หัวข้อ", "text", "เช่น repair"]],
    find_case: [["by", "ค้นด้วย", "text", "เช่น sap_document, meter_number"]],
    book: [["calendar", "ปฏิทิน", "text", "เช่น notebook, pickup"]],
    web: [["url", "ลิงก์", "text", "https://…"]],
    sheet_lookup: [["source", "แหล่งข้อมูล", "select", CFG.sheets || ":ยังไม่มีแหล่งข้อมูล"], ["by", "ค้นด้วย", "select", ":เลข UI ที่ผู้ใช้พิมพ์|employee:รหัสพนักงานของผู้ใช้ (แสดงรายการที่ตัวเองแจ้ง)"]],
  };

  var graph = CFG.graph && CFG.graph.nodes ? CFG.graph : { nodes: [], edges: [] };
  var checks = CFG.checks || [];
  var revision = CFG.version.revision;
  var view = { x: 40, y: 40, z: 0.8 };
  var sel = { node: null, edge: null };
  var undoStack = [], redoStack = [];
  var dirty = false, saving = false, saveTimer = null;
  var tab = "node";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var el = function (tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  var canvas = $("#fe-canvas"), world = $("#fe-world"), svg = $("#fe-edges"), stateEl = $("#fe-state");
  var uid = function (p) { return p + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); };
  var nodeById = function (id) { return graph.nodes.find(function (n) { return n.id === id; }); };
  var issuesOf = function (id) { return checks.filter(function (c) { return c.node === id; }); };
  var reqsOf = function (id) { return CFG.requirements.filter(function (r) { return r.node === id; }); };

  /* ---------- history & saving ---------- */
  function snapshot() { return JSON.stringify(graph); }
  function change(fn) {
    if (!EDITABLE) return;
    undoStack.push(snapshot()); if (undoStack.length > 120) undoStack.shift(); redoStack.length = 0;
    fn(); markDirty(); render();
  }
  function undo() { if (!undoStack.length) return; redoStack.push(snapshot()); graph = JSON.parse(undoStack.pop()); fixSel(); markDirty(); render(); }
  function redo() { if (!redoStack.length) return; undoStack.push(snapshot()); graph = JSON.parse(redoStack.pop()); fixSel(); markDirty(); render(); }
  function fixSel() { if (sel.node && !nodeById(sel.node)) sel.node = null; if (sel.edge && !graph.edges.some(function (e) { return e.id === sel.edge; })) sel.edge = null; }
  function setState(text, error) { stateEl.textContent = text; stateEl.classList.toggle("is-error", !!error); }
  function markDirty() { dirty = true; setState("มีการแก้ กำลังบันทึก…"); clearTimeout(saveTimer); saveTimer = setTimeout(save, 1200); }
  function csrf() { var i = document.querySelector("[name=csrfmiddlewaretoken]"); return i ? i.value : ""; }
  function save() {
    if (!EDITABLE || !dirty) return;
    if (saving) { clearTimeout(saveTimer); saveTimer = setTimeout(save, 800); return; }
    saving = true; dirty = false;
    fetch(CFG.saveUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify({ graph: graph, revision: revision }) })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .then(function (res) {
        saving = false;
        if (!res.ok) { dirty = true; setState("บันทึกไม่ได้: " + (res.body.error || "ลองใหม่"), true); return; }
        revision = res.body.revision; checks = res.body.checks;
        var t = new Date();
        setState("บันทึกแล้ว " + String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0"));
        render();
      })
      .catch(function () { saving = false; dirty = true; setState("บันทึกไม่ได้ ตรวจการเชื่อมต่อแล้วลองใหม่", true); });
  }
  window.addEventListener("beforeunload", function (e) { if (dirty || saving) { e.preventDefault(); e.returnValue = ""; } });

  /* ---------- view ---------- */
  function applyView() { world.style.transform = "translate(" + view.x + "px," + view.y + "px) scale(" + view.z + ")"; }
  function toWorld(cx, cy) { var r = canvas.getBoundingClientRect(); return { x: (cx - r.left - view.x) / view.z, y: (cy - r.top - view.y) / view.z }; }
  function zoomAt(f, cx, cy) {
    var r = canvas.getBoundingClientRect(); var px = cx - r.left, py = cy - r.top;
    var z = Math.min(2, Math.max(0.2, view.z * f)); var k = z / view.z;
    view.x = px - (px - view.x) * k; view.y = py - (py - view.y) * k; view.z = z; applyView();
  }
  function fit() {
    if (!graph.nodes.length) return;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    graph.nodes.forEach(function (n) { var e = world.querySelector('[data-id="' + n.id + '"]'); var h = e ? e.offsetHeight : 120; minX = Math.min(minX, n.x); minY = Math.min(minY, n.y); maxX = Math.max(maxX, n.x + n.w); maxY = Math.max(maxY, n.y + h); });
    var r = canvas.getBoundingClientRect();
    view.z = Math.min(1.1, Math.max(0.2, Math.min((r.width - 60) / (maxX - minX), (r.height - 60) / (maxY - minY))));
    view.x = 30 - minX * view.z; view.y = 30 - minY * view.z; applyView();
  }
  function reveal(id) {
    var n = nodeById(id); if (!n) return;
    var r = canvas.getBoundingClientRect(); view.z = Math.max(view.z, 0.8);
    view.x = r.width / 2 - (n.x + n.w / 2) * view.z; view.y = r.height / 3 - n.y * view.z; applyView();
    var e = world.querySelector('[data-id="' + id + '"]'); if (e) { e.classList.remove("is-focus"); void e.offsetWidth; e.classList.add("is-focus"); }
  }

  /* ---------- render ---------- */
  function render() { renderNodes(); renderEdges(); renderSide(); }
  function renderNodes() {
    world.querySelectorAll(".fn").forEach(function (e) { e.remove(); });
    graph.nodes.forEach(function (n) {
      var box = el("div", "fn" + (n.kind === "note" ? " fn--note" : "") + (sel.node === n.id ? " is-selected" : ""));
      box.dataset.id = n.id; box.style.left = n.x + "px"; box.style.top = n.y + "px"; box.style.width = n.w + "px";
      var head = el("div", "fn__head"); head.dataset.drag = "1";
      var icon = el("span", "fn__icon k-" + n.kind, ICONS[n.kind] || "•");
      var titles = el("div"); titles.append(el("div", "fn__kind", KINDS[n.kind] || n.kind), el("div", "fn__title", n.title || "(ยังไม่มีชื่อ)"));
      head.append(icon, titles); box.append(head);
      if (n.text) box.append(el("div", "fn__text", n.text));
      var chips = el("div", "fn__chips");
      var c = n.config || {};
      if (n.kind === "ask") { if (c.field) chips.append(el("span", "fn__chip", "ช่อง " + c.field)); if (c.rule) chips.append(el("span", "fn__chip", RULES[c.rule] || c.rule)); if (c.choices) chips.append(el("span", "fn__chip", "เลือก: " + c.choices.split("|").join(" / "))); }
      if (n.kind === "create_case") { chips.append(el("span", "fn__chip", [c.case_kind, c.division, c.topic].filter(Boolean).join(" · "))); }
      if (n.kind === "koj_tracking") { chips.append(el("span", "fn__chip", [c.equipment, c.service].filter(Boolean).join(" · ") || "เมนู กอจ.")); }
      if (n.kind === "sheet_lookup") chips.append(el("span", "fn__chip", (c.source ? "ชีท " + c.source : "ยังไม่เลือกชีท") + (c.by === "employee" ? " · รหัสพนักงาน" : " · เลข UI")));
      if (n.kind === "book" && c.calendar) chips.append(el("span", "fn__chip", "ปฏิทิน " + c.calendar));
      if (n.kind === "branch" && c.condition) chips.append(el("span", "fn__chip", "ดูจาก " + c.condition));
      var reqs = reqsOf(n.id); if (reqs.length) { var rc = el("span", "fn__chip fn__chip--req", "สิ่งที่กองต้องการ " + reqs.length + " ข้อ"); rc.dataset.req = "1"; rc.title = reqs.map(function (r) { return r.text; }).join("\n"); chips.append(rc); }
      var errs = issuesOf(n.id).filter(function (i) { return i.level === "error"; }); if (errs.length) { var ec = el("span", "fn__chip fn__chip--err", "ต้องแก้ " + errs.length); ec.title = errs.map(function (i) { return i.message; }).join("\n"); chips.append(ec); }
      if (chips.childNodes.length) box.append(chips);
      if (n.buttons && n.buttons.length) {
        var bs = el("div", "fn__buttons");
        n.buttons.forEach(function (b) {
          var be = el("div", "fn__button" + (b.label.length > LIMITS.label ? " is-long" : ""), b.label || "(ปุ่มไม่มีชื่อ)");
          be.title = b.full ? "ชื่อเต็มในแชต: " + b.full : "";
          if (b.note && !graph.edges.some(function (e) { return e.from === n.id && e.port === b.id; })) be.append(el("span", "fn__note", "→ " + b.note));
          if (b.set) be.append(el("span", "fn__set", (n.kind === "branch" ? "เมื่อ " : "ตั้ง ") + b.set));
          var p = el("span", "fn__port fn__port--out"); p.dataset.port = b.id; p.title = "ลากไปหากล่องที่ปุ่มนี้พาไป"; be.append(p);
          bs.append(be);
        });
        box.append(bs);
      }
      if (n.kind !== "note" && n.kind !== "entry") box.append(el("span", "fn__port fn__port--in"));
      if (ROLES[n.kind] !== "end") { var mp = el("span", "fn__port fn__port--out"); mp.dataset.port = ""; mp.title = n.kind === "note" ? "ลากเพื่อชี้โน้ตไปที่กล่อง" : "ลากไปหากล่องถัดไป"; box.append(mp); }
      world.append(box);
    });
  }
  function portPoint(nodeId, port) {
    var box = world.querySelector('[data-id="' + nodeId + '"]'); var n = nodeById(nodeId); if (!box || !n) return { x: 0, y: 0 };
    var p = box.querySelector('.fn__port--out[data-port="' + (port || "") + '"]');
    if (!p) return { x: n.x + n.w, y: n.y + 22 };
    var br = box.getBoundingClientRect(), pr = p.getBoundingClientRect();
    return { x: n.x + (pr.left + pr.width / 2 - br.left) / view.z, y: n.y + (pr.top + pr.height / 2 - br.top) / view.z };
  }
  function inPoint(nodeId) { var n = nodeById(nodeId); return { x: n.x, y: n.y + 22 }; }
  function curve(a, b) { var dx = Math.max(40, Math.abs(b.x - a.x) / 2); return "M" + a.x + "," + a.y + " C" + (a.x + dx) + "," + a.y + " " + (b.x - dx) + "," + b.y + " " + b.x + "," + b.y; }
  function renderEdges() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var ns = "http://www.w3.org/2000/svg";
    var defs = document.createElementNS(ns, "defs"); var m = document.createElementNS(ns, "marker");
    m.setAttribute("id", "fe-arrow"); m.setAttribute("viewBox", "0 0 10 10"); m.setAttribute("refX", "9"); m.setAttribute("refY", "5"); m.setAttribute("markerWidth", "7"); m.setAttribute("markerHeight", "7"); m.setAttribute("orient", "auto-start-reverse");
    var tri = document.createElementNS(ns, "path"); tri.setAttribute("d", "M0,0 L10,5 L0,10 z"); tri.setAttribute("fill", "#9e9ea0"); m.append(tri); defs.append(m); svg.append(defs);
    graph.edges.forEach(function (e) {
      if (!nodeById(e.from) || !nodeById(e.to)) return;
      var a = portPoint(e.from, e.port), b = inPoint(e.to);
      var path = document.createElementNS(ns, "path"); path.setAttribute("d", curve(a, b)); path.setAttribute("class", "fe__edge" + (sel.edge === e.id ? " is-selected" : "")); path.setAttribute("marker-end", "url(#fe-arrow)"); path.dataset.edge = e.id; svg.append(path);
      if (e.label) { var t = document.createElementNS(ns, "text"); t.setAttribute("class", "fe__edge-label"); t.setAttribute("x", (a.x + b.x) / 2); t.setAttribute("y", (a.y + b.y) / 2 - 4); t.setAttribute("text-anchor", "middle"); t.textContent = e.label; svg.append(t); }
    });
  }

  /* ---------- side panel ---------- */
  function renderSide() {
    var errors = checks.filter(function (c) { return c.level === "error"; }).length;
    var cnt = $("#fe-issue-count"); cnt.textContent = errors ? String(errors) : ""; cnt.hidden = !errors;
    document.querySelectorAll(".fe__tab").forEach(function (t) { t.setAttribute("aria-selected", t.dataset.tab === tab ? "true" : "false"); });
    document.querySelectorAll(".fe__pane").forEach(function (p) { p.hidden = p.dataset.pane !== tab; });
    if (tab === "node") renderNodePane(); else if (tab === "issues") renderIssues(); else if (tab === "try") renderTry(); else renderPreview();
    var pub = $("#fe-publish"); if (pub && CFG.runnerReady) pub.disabled = errors > 0 || dirty;
  }
  function field(label, input, help) { var f = el("div", "fe__field"); var l = el("label", null, label); if (input.id) l.htmlFor = input.id; f.append(l, input); if (help) f.append(el("div", "fe__help", help)); return f; }
  function bindText(input, get, set) { input.value = get() || ""; input.disabled = !EDITABLE; var pushed = false; input.addEventListener("focus", function () { pushed = false; }); input.addEventListener("input", function () { if (!pushed) { undoStack.push(snapshot()); redoStack.length = 0; pushed = true; } set(input.value); markDirty(); renderNodes(); renderEdges(); }); return input; }
  function renderNodePane() {
    var pane = $('[data-pane="node"]'); pane.innerHTML = "";
    var e = sel.edge && graph.edges.find(function (x) { return x.id === sel.edge; });
    if (e) {
      pane.append(el("p", "fe__help", "เส้นจาก " + (nodeById(e.from) || {}).title + " ไปยัง " + (nodeById(e.to) || {}).title));
      var li = document.createElement("input"); li.id = "fe-elabel"; pane.append(field("ป้ายบนเส้น (ไม่บังคับ)", bindText(li, function () { return e.label; }, function (v) { e.label = v; })));
      if (EDITABLE) { var d = el("button", "fe__mini fe__mini--danger", "ลบเส้นนี้"); d.type = "button"; d.onclick = function () { change(function () { graph.edges = graph.edges.filter(function (x) { return x !== e; }); sel.edge = null; }); }; pane.append(d); }
      return;
    }
    var n = sel.node && nodeById(sel.node);
    if (!n) { pane.append(el("p", "fe__empty", EDITABLE ? "คลิกกล่องเพื่อแก้ หรือกดชนิดกล่องทางซ้ายเพื่อเพิ่ม ลากจากจุดวงกลมด้านขวาของกล่องหรือของปุ่มไปหากล่องถัดไป" : "รุ่นนี้ดูได้อย่างเดียว เริ่มร่างใหม่จากหน้ารายการเพื่อแก้")); return; }
    var kind = document.createElement("select"); kind.id = "fe-kind"; kind.disabled = !EDITABLE;
    Object.keys(KINDS).forEach(function (k) { var o = el("option", null, KINDS[k]); o.value = k; o.selected = k === n.kind; kind.append(o); });
    kind.onchange = function () { change(function () { n.kind = kind.value; }); };
    pane.append(field("ชนิดกล่อง", kind));
    var ti = document.createElement("input"); ti.id = "fe-title"; pane.append(field(n.kind === "note" ? "หัวข้อโน้ต" : "ชื่อกล่อง", bindText(ti, function () { return n.title; }, function (v) { n.title = v; })));
    var tx = document.createElement("textarea"); tx.id = "fe-text"; tx.rows = 4;
    pane.append(field(n.kind === "note" ? "รายละเอียด" : (n.kind === "ask" ? "คำถามที่บอทพิมพ์" : "ข้อความที่บอทพิมพ์"), bindText(tx, function () { return n.text; }, function (v) { n.text = v; }), n.kind === "summary" ? "ตัวอย่างสรุป ระบบสร้างสรุปจริงจากข้อมูลในร่าง" : ""));
    (CONFIG_FIELDS[n.kind] || []).forEach(function (spec) {
      n.config = n.config || {};
      var key = spec[0], input;
      if (spec[2] === "rule") { input = document.createElement("select"); input.append(el("option", null, "เลือกกฎตรวจ")); input.firstChild.value = ""; Object.keys(RULES).forEach(function (r) { var o = el("option", null, RULES[r]); o.value = r; o.selected = n.config.rule === r; input.append(o); }); }
      else if (spec[2] === "select") { input = document.createElement("select"); input.append(el("option", null, "เลือก")); input.firstChild.value = ""; spec[3].split("|").forEach(function (p) { var kv = p.split(":"); var o = el("option", null, kv[1]); o.value = kv[0]; o.selected = n.config[key] === kv[0]; input.append(o); }); }
      else if (spec[2] === "flag") { input = document.createElement("select"); [["", "ไม่ได้"], ["1", "ได้"]].forEach(function (p) { var o = el("option", null, p[1]); o.value = p[0]; o.selected = (n.config[key] || "") === p[0]; input.append(o); }); }
      else { input = document.createElement("input"); input.placeholder = spec[3] || ""; }
      input.id = "fe-c-" + key; input.disabled = !EDITABLE;
      if (input.tagName === "SELECT") { input.onchange = function () { change(function () { if (input.value) n.config[key] = input.value; else delete n.config[key]; }); }; pane.append(field(spec[1], input)); }
      else pane.append(field(spec[1], bindText(input, function () { return n.config[key]; }, function (v) { if (v) n.config[key] = v; else delete n.config[key]; })));
    });
    if (ROLES[n.kind] === "buttons" || n.kind === "ask" || (n.buttons && n.buttons.length)) {
      var wrap = el("div", "fe__field"); wrap.append(el("span", "fe__label", n.kind === "branch" ? "ทางแยก (ระบบเลือกทางแรกที่เงื่อนไขตรง ไม่ตรงเลยไปตามเส้นหลัก)" : "ปุ่มตอบกลับด่วน (ไม่เกิน " + LIMITS.buttons + " ปุ่ม ชื่อไม่เกิน " + LIMITS.label + " ตัวอักษร)"));
      (n.buttons || []).forEach(function (b, i) {
        var row = el("div", "fe__brow");
        var lab = document.createElement("input"); lab.setAttribute("aria-label", "ชื่อปุ่ม"); lab.placeholder = "ชื่อปุ่ม";
        var counter = el("span", "fe__counter");
        var upd = function () { counter.textContent = (b.label || "").length + "/" + LIMITS.label; counter.classList.toggle("is-over", (b.label || "").length > LIMITS.label); };
        bindText(lab, function () { return b.label; }, function (v) { b.label = v; upd(); }); upd();
        var full = document.createElement("input"); full.className = "fe__bfull"; full.placeholder = "ชื่อเต็มที่ขึ้นในแชต (ถ้าชื่อปุ่มย่อ)"; full.setAttribute("aria-label", "ชื่อเต็ม");
        bindText(full, function () { return b.full; }, function (v) { b.full = v; });
        var note = document.createElement("input"); note.className = "fe__bfull"; note.placeholder = "ถ้าไม่ต่อเส้น กดแล้วทำอะไร เช่น เริ่มกรอกใหม่"; note.setAttribute("aria-label", "กดแล้วทำอะไร");
        bindText(note, function () { return b.note; }, function (v) { b.note = v; });
        var set = document.createElement("input"); set.className = "fe__bfull fe__bset";
        set.placeholder = n.kind === "branch" ? "เงื่อนไข เช่น case_kind=withdrawal หรือ set_kind=ct|ct_vt" : "ตั้งค่าเมื่อกด เช่น case_kind=withdrawal; group=meter";
        set.setAttribute("aria-label", n.kind === "branch" ? "เงื่อนไขของทางนี้" : "ตั้งค่าเมื่อกด");
        bindText(set, function () { return b.set; }, function (v) { b.set = v; });
        row.append(lab, counter, full, note, set);
        if (EDITABLE) {
          var acts = el("div", "fe__bacts");
          var up = el("button", "fe__mini", "↑"); up.type = "button"; up.disabled = i === 0; up.onclick = function () { change(function () { n.buttons.splice(i - 1, 0, n.buttons.splice(i, 1)[0]); }); };
          var del = el("button", "fe__mini fe__mini--danger", "ลบปุ่ม"); del.type = "button"; del.onclick = function () { change(function () { n.buttons.splice(i, 1); graph.edges = graph.edges.filter(function (e) { return !(e.from === n.id && e.port === b.id); }); }); };
          acts.append(up, del); row.append(acts);
        }
        wrap.append(row);
      });
      if (EDITABLE) { var add = el("button", "fe__mini", "+ เพิ่มปุ่ม"); add.type = "button"; add.onclick = function () { change(function () { n.buttons = n.buttons || []; n.buttons.push({ id: uid("b"), label: n.kind === "branch" ? "ทางใหม่" : "ปุ่มใหม่", full: "", note: "", set: "" }); }); }; wrap.append(add); }
      pane.append(wrap);
    }
    var reqs = reqsOf(n.id);
    if (reqs.length) { var rw = el("div", "fe__field"); rw.append(el("span", "fe__label", "สิ่งที่กองต้องการที่ผูกกับกล่องนี้")); var rl = el("div", "fe__reqs"); reqs.forEach(function (r) { var a = el("a", "fe__req", r.division + " · " + r.status + " · " + r.text); a.href = CFG.requirementsUrl + "?division=" + encodeURIComponent(r.division) + "#r" + r.id; rl.append(a); }); rw.append(rl); pane.append(rw); }
    var issues = issuesOf(n.id); if (issues.length) { var iw = el("div", "fe__field"); iw.append(el("span", "fe__label", "สิ่งที่ต้องดูในกล่องนี้")); issues.forEach(function (i) { var d = el("div", "fe__issue"); d.append(el("span", "fe__lvl fe__lvl--" + i.level, i.level === "error" ? "ต้องแก้" : "ควรดู"), el("span", null, i.message)); iw.append(d); }); pane.append(iw); }
    if (EDITABLE) {
      var dup = el("button", "fe__mini", "ทำซ้ำกล่อง"); dup.type = "button"; dup.onclick = function () { change(function () { var c = JSON.parse(JSON.stringify(n)); c.id = uid("n"); c.x += 30; c.y += 30; c.buttons.forEach(function (b) { b.id = uid("b"); }); graph.nodes.push(c); sel.node = c.id; }); };
      var rm = el("button", "fe__mini fe__mini--danger", "ลบกล่อง"); rm.type = "button"; rm.onclick = removeSelected;
      var acts2 = el("div", "fe__bacts"); acts2.append(dup, rm); pane.append(acts2);
    }
  }
  function renderIssues() {
    var pane = $('[data-pane="issues"]'); pane.innerHTML = "";
    if (dirty || saving) pane.append(el("p", "fe__help", "กำลังบันทึก ผลตรวจจะอัปเดตเมื่อบันทึกเสร็จ"));
    if (!checks.length) { pane.append(el("p", "fe__empty", "ไม่มีจุดที่ต้องแก้ ร่างนี้ผ่านการตรวจ")); return; }
    checks.slice().sort(function (a, b) { return a.level === b.level ? 0 : a.level === "error" ? -1 : 1; }).forEach(function (i) {
      var d = el("div", "fe__issue"); d.append(el("span", "fe__lvl fe__lvl--" + i.level, i.level === "error" ? "ต้องแก้" : "ควรดู"), el("span", null, i.message));
      if (i.node) d.onclick = function () { sel.node = i.node; sel.edge = null; tab = "node"; render(); reveal(i.node); };
      pane.append(d);
    });
  }
  function renderPreview() {
    var pane = $('[data-pane="preview"]'); pane.innerHTML = "";
    var n = sel.node && nodeById(sel.node);
    if (!n || n.kind === "note") { pane.append(el("p", "fe__empty", "เลือกกล่องข้อความ ถามข้อมูล หรือสรุป เพื่อดูหน้าจอ LINE ของกล่องนั้น")); return; }
    var phone = el("div", "fe__phone"), screen = el("div", "fe__screen");
    // The button the user tapped to get here (a branch is the system's choice, not a tap).
    var into = graph.edges.find(function (e) { var f = nodeById(e.from); return e.to === n.id && e.port && f && f.kind !== "branch"; });
    if (into) { var from = nodeById(into.from); var b = from && from.buttons.find(function (x) { return x.id === into.port; }); if (b) screen.append(el("div", "fe__user", b.full || b.label)); }
    var bot = { message: 1, ask: 1, summary: 1, result: 1, limit: 1, web: 1, sheet_lookup: 1, branch: 0 };
    if (bot[n.kind]) {
      var oa = el("div", "fe__oa"); oa.append(el("span", "fe__avatar", "ฝอจ"));
      var text = n.kind === "message" || n.kind === "summary" ? [n.title, n.text].filter(Boolean).join("\n") : (n.kind === "web" && n.config && n.config.url ? [n.text, n.config.url].filter(Boolean).join("\n") : (n.text || n.title));
      oa.append(el("div", "fe__bubble", text || "(ยังไม่มีข้อความ)")); screen.append(oa);
    } else {
      screen.append(el("div", "fe__typing", "ขั้นนี้ระบบทำงานเอง ผู้ใช้ไม่เห็นข้อความของกล่องนี้: " + (KINDS[n.kind] || n.kind)));
    }
    var quick = el("div", "fe__quick");
    var labels = (n.buttons || []).map(function (b) { return b.label; });
    if (n.kind === "ask" && n.config && n.config.choices) labels = n.config.choices.split("|").concat(labels);
    if (n.kind !== "summary" && n.kind !== "branch" && bot[n.kind]) labels.push("ยกเลิกร่าง");
    if (n.kind === "branch") labels = [];
    labels.forEach(function (l) { quick.append(el("span", "fe__qr" + (l.length > LIMITS.label ? " is-long" : ""), l)); });
    if (labels.length) screen.append(quick);
    if (n.kind === "sheet_lookup" && n.config && n.config.by === "employee") screen.append(el("div", "fe__typing", "ระบบแสดงรายการที่ผู้ใช้แจ้งไว้ในชีท โดยแมพรหัสพนักงานที่ผู้ใช้ยืนยันแล้ว"));
    else if (n.kind === "sheet_lookup") screen.append(el("div", "fe__typing", "ผู้ใช้พิมพ์เลข UI ได้ครั้งละไม่เกิน 10 เลข ระบบตอบสถานะจากชีท แล้วถามต่อพร้อมปุ่ม ถามเลข UI อื่น และ จบ"));
    if (n.kind === "ask") screen.append(el("div", "fe__typing", "ผู้ใช้พิมพ์ตอบ" + (n.config && n.config.rule ? " · ระบบตรวจ: " + (RULES[n.config.rule] || n.config.rule) : "")));
    phone.append(screen); pane.append(phone);
    pane.append(el("p", "fe__help", "ปุ่มที่ชื่อยาวเกินจะเป็นสีแดง LINE ตัดชื่อปุ่มที่ยาวเกิน " + LIMITS.label + " ตัวอักษร"));
  }

  /* ---------- ลองคุย: play the draft like the chat does ---------- */
  // The same moves as flow_runner: boxes that wait for nothing run at once, a branch takes its
  // first button whose condition holds, typed text is read by the server with the real rules.
  var play = null;
  var SAP_CHOICES = { "เลขที่ใบเบิก": ["slip", "withdrawal"], "เลขที่ใบส่งคืน": ["return_slip", "return"], "ใบสั่งงานจาก SAP": ["work_order", ""], "ใบสั่งงาน SAP": ["work_order", ""], "หมายเลขเครื่องวัดฯ": ["meter", ""], "โครงการ": ["project", ""] };
  var KIND_LABELS = { slip: "เลขที่ใบเบิก", return_slip: "เลขที่ใบส่งคืน", work_order: "ใบสั่งงานจาก SAP", meter: "หมายเลขเครื่องวัดฯ", project: "โครงการ" };
  var EDIT_LABELS = { tracking: "เลขติดตาม", sap_document: "เลข SAP งานซ่อม", details: "รายละเอียด", description: "รายละเอียด", "rows.meter": "มิเตอร์", ct_size: "ขนาด CT", vt_size: "ขนาด VT", multiplier: "ตัวคูณ", mea_numbers: "MEA No." };
  function does(b, what) { var v = {}; assign(v, b.set); return v["do"] ? v["do"] === what : b.label.indexOf(what === "edit" ? "แก้ไข" : "กรอกใหม่") >= 0; }
  function follow(id, port) { var e = graph.edges.find(function (x) { return x.from === id && (x.port || null) === (port || null); }); return e ? e.to : null; }
  function assign(values, text) { String(text || "").split(";").forEach(function (p) { var i = p.indexOf("="); if (i > 0) values[p.slice(0, i).trim()] = p.slice(i + 1).trim(); }); }
  function holds(values, text) {
    var parts = String(text || "").split(";").filter(function (p) { return p.indexOf("=") > 0; });
    return parts.length > 0 && parts.every(function (p) { var i = p.indexOf("="); return p.slice(i + 1).split("|").map(function (a) { return a.trim(); }).indexOf(String(values[p.slice(0, i).trim()] || "")) >= 0; });
  }
  function say(who, text) { play.log.push({ who: who, text: text }); }
  function playStart() {
    var entry = graph.nodes.find(function (n) { return n.kind === "entry"; });
    play = { node: null, values: {}, rows: [], choice: {}, log: [], done: false, path: [], answered: [], editing: false, summary: null, menu: false };
    if (!entry) { say("bot", "flow นี้ยังไม่มีปุ่มเมนูเริ่มต้น"); play.done = true; return; }
    say("user", "(กดปุ่ม " + (entry.title || "เมนู") + " บน rich menu)");
    playGo(follow(entry.id));
  }
  function playGo(id) {
    for (var hop = 0; hop < 40; hop++) {
      var n = id && nodeById(id);
      if (!n) { say("bot", "flow นี้ยังไม่มีขั้นถัดไป (ทางตัน)"); play.done = true; return; }
      if (n.kind === "branch") { var hit = n.buttons.find(function (b) { return holds(play.values, b.set); }); id = hit ? follow(n.id, hit.id) : follow(n.id); continue; }
      if (n.kind === "entry" || n.kind === "note" || n.kind === "ai_read") { id = follow(n.id); continue; }
      if (n.kind === "create_case") {
        play.node = n.id; say("sys", "ระบบสร้างเรื่องที่ " + (n.config.division || "กองตามกฎ") + " (ลองคุยไม่สร้างเรื่องจริง)");
        id = follow(n.id); continue;
      }
      if (n.kind === "book") { say("sys", "ผู้ใช้เลือกช่วงเวลาว่างจากปฏิทิน " + (n.config.calendar || "") + " (ลองคุยข้ามขั้นนี้)"); id = follow(n.id); continue; }
      if (n.kind === "find_case") { say("bot", "(แสดงรายการ เรื่องของฉัน)"); play.done = true; return; }
      if (n.kind === "koj_tracking") { say("bot", n.config.service === "watches" ? "เปิดรายการติดตาม กอจ. (ลองคุยใช้ข้อมูลจำลอง)" : "เปิดติดตามงานต้นทาง กอจ. · " + (n.config.equipment || "เลือกอุปกรณ์") + " · " + (n.config.service || "เลือกบริการ") + " (ลองคุยไม่อ่านชีทจริงและไม่สมัครรับแจ้ง)"); play.done = true; return; }
      if (n.kind === "sheet_lookup" && n.config.by === "employee") { say("sys", "ระบบแสดงรายการที่ผู้ใช้แจ้งไว้ในชีท " + (n.config.source || "(ยังไม่เลือก)") + " แมพด้วยรหัสพนักงาน (ลองคุยไม่อ่านชีทจริง)"); play.done = true; return; }
      if (n.kind === "sheet_lookup") { say("bot", n.text || n.title); say("sys", "ผู้ใช้พิมพ์เลข UI แล้วระบบตอบสถานะจากชีท " + (n.config.source || "(ยังไม่เลือก)") + " (ลองคุยไม่อ่านชีทจริง)"); play.done = true; return; }
      play.node = n.id;
      if (n.kind === "web" && n.config.url) { say("bot", [n.text, n.config.url].filter(Boolean).join("\n")); play.done = true; return; }
      if (n.kind === "result" || n.kind === "limit" || n.kind === "web" || n.kind === "handoff") { say("bot", [n.title, n.text].filter(Boolean).join("\n")); play.done = true; return; }
      if (n.kind === "summary") { play.summary = n.id; say("bot", playSummary(n)); return; }
      if (n.kind === "ask" && n.config.choices && !play.choice[n.id]) { say("bot", n.title + "\nเลือกก่อนว่าจะติดตามด้วยอะไร"); return; }
      say("bot", [n.kind === "ask" && play.choice[n.id] ? n.title + " (" + play.choice[n.id] + ")" : n.title, n.text].filter(Boolean).join("\n"));
      return;
    }
    say("bot", "flow วนไม่จบ"); play.done = true;
  }
  function playSummary(n) {
    var v = play.values, lines = [n.title];
    var confirm = n.buttons.find(function (b) { return follow(n.id, b.id); }); var target = confirm && nodeById(follow(n.id, confirm.id));
    if (target && target.config.division) lines.push("ส่งถึง: " + target.config.division);
    if (v.set_kind) lines.push("ชุด: " + ({ meter_ct: "มิเตอร์ + CT", meter_ct_vt: "มิเตอร์ + CT + VT", ct: "CT อย่างเดียว", ct_vt: "CT + VT" })[v.set_kind]);
    if (v.tracking) lines.push((KIND_LABELS[v.sap_kind] || "เลขติดตาม") + ": " + v.tracking);
    if (v.sap_document) lines.push("เลข SAP งานซ่อม: " + v.sap_document);
    play.rows.forEach(function (r) { lines.push(r.text); });
    if (v.ct_size) lines.push("ขนาด CT: " + v.ct_size);
    if (v.vt_size) lines.push("ขนาด VT: " + v.vt_size);
    if (v.multiplier_check) lines.push("ตัวคูณ: " + v.multiplier_check.given + (v.multiplier_check.ok ? " (ตรงกับที่คำนวณ)" : " (ให้เจ้าหน้าที่ตรวจ)"));
    var names = { details: "รายละเอียด", description: "รายละเอียด", mea_numbers: "MEA No." };
    Object.keys(names).forEach(function (k) { if (v[k]) lines.push(names[k] + ": " + [].concat(v[k]).join(", ")); });
    lines.push("ข้อมูลถูกต้องกดยืนยันภายใน 30 นาที ถ้าพิมพ์ผิดกดแก้ไข");
    return lines.join("\n");
  }
  function playTap(n, b) {
    say("user", b.full || b.label);
    var target = follow(n.id, b.id);
    if (n.kind === "summary" && !target && does(b, "edit")) { play.menu = true; say("bot", "ต้องการแก้ข้อไหน"); renderTry(); return; }
    if (n.kind === "summary" && !target && does(b, "restart")) { playStart(); say("bot", "เริ่มกรอกใหม่"); renderTry(); return; }
    if (n.kind !== "summary") { assign(play.values, b.set); if (n.kind !== "ask") play.path.push(b.label); }
    if (n.kind === "ask") { delete play.values[n.config.field]; if (play.editing) { play.editing = false; playGo(play.summary); renderTry(); return; } }
    if (!target) { if (b.note) say("bot", b.note); renderTry(); return; }
    playGo(target); renderTry();
  }
  function playChoose(n, c) {
    say("user", c); play.choice[n.id] = c;
    var sk = SAP_CHOICES[c]; if (sk) { play.values.sap_kind = sk[0]; if (sk[1] && !play.values.case_kind) play.values.case_kind = sk[1]; }
    playGo(n.id); renderTry();
  }
  function playType(n, text) {
    say("user", text); renderTry();
    fetch(CFG.tryUrl, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify({ rule: n.config.rule || "text", field: n.config.field || "", text: text, kind: play.values.sap_kind || "", values: play.values }) })
      .then(function (r) { return r.json(); })
      .then(function (a) {
        if (a.error || a.problem) { say("bot", a.error || a.problem); renderTry(); return; }
        if (a.shown && a.shown !== text.trim()) say("bot", "อ่านเป็น: " + a.shown);
        var field = n.config.field || "";
        if (field.indexOf("rows.") === 0) { var g = field.slice(5); if (play.editing) play.rows = play.rows.filter(function (r) { return r.group !== g; }); play.rows = play.rows.concat(a.rows || []); }
        else if (n.config.rule === "multiplier") { play.values.multiplier_check = { given: a.value, ok: a.ok }; if (!a.ok) say("bot", "ตัวคูณไม่ตรงกับที่คำนวณจาก CT และ VT (" + (a.computed || "คำนวณไม่ได้") + ") ส่งต่อได้ ระบบจะบอกเจ้าหน้าที่ให้ตรวจ"); }
        else play.values[field] = a.value;
        play.answered = play.answered.filter(function (x) { return x !== n.id; }).concat([n.id]);
        if (play.editing) { play.editing = false; playGo(play.summary); } else playGo(follow(n.id));
        renderTry();
      })
      .catch(function () { say("bot", "ตรวจข้อความไม่ได้ ตรวจการเชื่อมต่อแล้วลองใหม่"); renderTry(); });
  }
  function renderTry() {
    var pane = $('[data-pane="try"]'); pane.innerHTML = "";
    if (!play) playStart();
    var bar = el("div", "fe__bacts"); var again = el("button", "fe__mini", "เริ่มคุยใหม่"); again.type = "button"; again.onclick = function () { play = null; renderTry(); };
    bar.append(again); pane.append(bar);
    var phone = el("div", "fe__phone"), screen = el("div", "fe__screen fe__screen--try");
    play.log.forEach(function (m) {
      if (m.who === "user") screen.append(el("div", "fe__user", m.text));
      else if (m.who === "sys") screen.append(el("div", "fe__typing", m.text));
      else { var oa = el("div", "fe__oa"); oa.append(el("span", "fe__avatar", "ฝอจ"), el("div", "fe__bubble", m.text)); screen.append(oa); }
    });
    var n = !play.done && play.node && nodeById(play.node);
    if (n) {
      var quick = el("div", "fe__quick");
      var picking = n.kind === "ask" && n.config.choices && !play.choice[n.id];
      if (play.menu) {
        var seen = {};
        play.answered.forEach(function (id) { var a = nodeById(id); if (!a || a.kind !== "ask") return; var l = EDIT_LABELS[a.config.field] || a.title.slice(0, 20); if (seen[l]) return; seen[l] = 1;
          var q = el("button", "fe__qr", l); q.type = "button"; q.onclick = function () { say("user", "แก้" + l); play.menu = false; play.editing = true; play.node = a.id; delete play.choice[a.id]; say("bot", "แก้ " + a.title + (a.config.choices ? "" : "\n" + (a.text || ""))); renderTry(); }; quick.append(q); });
        var back = el("button", "fe__qr", "กลับไปที่สรุป"); back.type = "button"; back.onclick = function () { play.menu = false; playGo(play.summary); renderTry(); }; quick.append(back);
      } else if (picking) n.config.choices.split("|").forEach(function (c) { c = c.trim(); if (!c) return; var q = el("button", "fe__qr", c); q.type = "button"; q.onclick = function () { playChoose(n, c); }; quick.append(q); });
      else n.buttons.forEach(function (b) { var q = el("button", "fe__qr" + (b.label.length > LIMITS.label ? " is-long" : ""), b.label); q.type = "button"; q.onclick = function () { playTap(n, b); }; quick.append(q); });
      if (quick.childNodes.length) screen.append(quick);
      if (n.kind === "ask" && !picking) {
        var form = el("form", "fe__say"); var input = document.createElement("textarea"); input.rows = 2; input.placeholder = "พิมพ์ตอบเหมือนในแชต (Enter ส่ง, Shift+Enter ขึ้นบรรทัด)"; input.setAttribute("aria-label", "ข้อความที่ผู้ใช้พิมพ์");
        var send = el("button", "fe__mini", "ส่ง"); send.type = "submit";
        input.addEventListener("keydown", function (ev) { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); } });
        form.onsubmit = function (ev) { ev.preventDefault(); if (input.value.trim()) playType(n, input.value); };
        form.append(input, send); screen.append(form);
        requestAnimationFrame(function () { input.focus(); });
      }
    }
    phone.append(screen); pane.append(phone);
    pane.append(el("p", "fe__help", "ลองคุยตามร่างที่บันทึกล่าสุด ข้อความที่พิมพ์ตรวจด้วยกฎเดียวกับแชตจริง แต่ไม่สร้างเรื่องและไม่ส่งข้อความถึงใคร"));
    screen.scrollTop = screen.scrollHeight;
  }

  /* ---------- editing ---------- */
  function addNode(kind, at) {
    change(function () {
      var r = canvas.getBoundingClientRect(); var p = at || toWorld(r.left + r.width / 2 - 130, r.top + r.height / 3);
      var n = { id: uid("n"), kind: kind, title: KINDS[kind], text: "", buttons: [], config: {}, x: Math.round(p.x / 10) * 10, y: Math.round(p.y / 10) * 10, w: 260 };
      if (kind === "message") n.buttons = [{ id: uid("b"), label: "ปุ่ม 1", full: "", note: "" }];
      if (kind === "summary") n.buttons = [{ id: uid("b"), label: "ยืนยันส่งเรื่อง", full: "", note: "" }, { id: uid("b"), label: "กรอกใหม่", full: "", note: "เริ่มกรอกใหม่ตั้งแต่ขั้นแรก" }];
      if (kind === "ask") n.config = { rule: "text" };
      graph.nodes.push(n); sel.node = n.id; sel.edge = null; tab = "node";
    });
  }
  function removeSelected() {
    if (!EDITABLE) return;
    if (sel.edge) { change(function () { graph.edges = graph.edges.filter(function (e) { return e.id !== sel.edge; }); sel.edge = null; }); return; }
    if (!sel.node) return;
    var id = sel.node;
    change(function () { graph.nodes = graph.nodes.filter(function (n) { return n.id !== id; }); graph.edges = graph.edges.filter(function (e) { return e.from !== id && e.to !== id; }); sel.node = null; });
  }
  function arrange() {
    change(function () {
      var start = graph.nodes.find(function (n) { return n.kind === "entry"; }); if (!start) return;
      var out = {}; graph.edges.forEach(function (e) { (out[e.from] = out[e.from] || []).push(e.to); });
      var depth = {}; depth[start.id] = 0; var q = [start.id];
      while (q.length) { var a = q.shift(); (out[a] || []).forEach(function (b) { var bn = nodeById(b); if (bn && bn.kind !== "note" && depth[b] === undefined) { depth[b] = depth[a] + 1; q.push(b); } }); }
      var cols = {}, maxY = 0;
      graph.nodes.forEach(function (n) { if (n.kind === "note") return; var d = depth[n.id] === undefined ? 0 : depth[n.id]; (cols[d] = cols[d] || []).push(n); });
      Object.keys(cols).forEach(function (d) { var y = 40; cols[d].forEach(function (n) { n.x = 40 + d * 330; n.y = y; var e = world.querySelector('[data-id="' + n.id + '"]'); y += (e ? e.offsetHeight / 1 : 160) + 40; maxY = Math.max(maxY, y); }); });
      var x = 40; graph.nodes.filter(function (n) { return n.kind === "note"; }).forEach(function (n) { n.x = x; n.y = maxY + 60; x += n.w + 40; });
    });
    setTimeout(fit, 30);
  }

  /* ---------- pointer interactions ---------- */
  var drag = null;
  canvas.addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0) return;
    var t = ev.target;
    var port = t.closest(".fn__port--out"), box = t.closest(".fn"), edge = t.closest("[data-edge]");
    if (port && box && EDITABLE) { ev.preventDefault(); drag = { kind: "connect", from: box.dataset.id, port: port.dataset.port || null, temp: null }; port.classList.add("is-active"); canvas.setPointerCapture(ev.pointerId); return; }
    if (t.closest("[data-req]") && box) { sel.node = box.dataset.id; sel.edge = null; tab = "node"; render(); return; }
    if (box) {
      var id = box.dataset.id; sel.node = id; sel.edge = null; if (tab !== "preview") tab = "node"; markSel(); renderSide();
      if (EDITABLE && (t.closest("[data-drag]") || !t.closest(".fn__button"))) { var n = nodeById(id); var p = toWorld(ev.clientX, ev.clientY); drag = { kind: "move", node: n, dx: p.x - n.x, dy: p.y - n.y, moved: false }; canvas.setPointerCapture(ev.pointerId); }
      return;
    }
    if (edge) { sel.edge = edge.dataset.edge; sel.node = null; tab = "node"; render(); return; }
    sel.node = null; sel.edge = null; markSel(); renderSide();
    drag = { kind: "pan", sx: ev.clientX, sy: ev.clientY, vx: view.x, vy: view.y }; canvas.classList.add("is-panning"); canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener("pointermove", function (ev) {
    if (!drag) return;
    if (drag.kind === "pan") { view.x = drag.vx + ev.clientX - drag.sx; view.y = drag.vy + ev.clientY - drag.sy; applyView(); return; }
    var p = toWorld(ev.clientX, ev.clientY);
    if (drag.kind === "move") {
      if (!drag.moved) { undoStack.push(snapshot()); redoStack.length = 0; drag.moved = true; }
      drag.node.x = Math.round((p.x - drag.dx) / 10) * 10; drag.node.y = Math.round((p.y - drag.dy) / 10) * 10;
      var e = world.querySelector('[data-id="' + drag.node.id + '"]'); e.style.left = drag.node.x + "px"; e.style.top = drag.node.y + "px"; renderEdges(); return;
    }
    if (drag.kind === "connect") {
      var a = portPoint(drag.from, drag.port);
      if (!drag.temp) { drag.temp = document.createElementNS("http://www.w3.org/2000/svg", "path"); drag.temp.setAttribute("class", "fe__edge is-temp"); svg.append(drag.temp); }
      drag.temp.setAttribute("d", curve(a, p));
    }
  });
  canvas.addEventListener("pointerup", function (ev) {
    if (!drag) return;
    var d = drag; drag = null; canvas.classList.remove("is-panning");
    if (d.kind === "move" && d.moved) markDirty();
    if (d.kind === "connect") {
      world.querySelectorAll(".fn__port.is-active").forEach(function (p) { p.classList.remove("is-active"); });
      if (d.temp) d.temp.remove();
      var target = document.elementFromPoint(ev.clientX, ev.clientY); var box = target && target.closest(".fn");
      if (box && box.dataset.id !== d.from && nodeById(box.dataset.id).kind !== "entry") {
        change(function () {
          var from = nodeById(d.from);
          // A button leads to one box; the box's own next line may fan out (e.g. by a branch label).
          if (d.port) graph.edges = graph.edges.filter(function (e) { return !(e.from === d.from && e.port === d.port); });
          if (from.kind === "note") graph.edges = graph.edges.filter(function (e) { return e.from !== d.from; });
          graph.edges.push({ id: uid("e"), from: d.from, port: d.port, to: box.dataset.id, label: "" });
        });
      } else renderEdges();
    }
  });
  canvas.addEventListener("wheel", function (ev) { ev.preventDefault(); if (ev.ctrlKey || ev.metaKey || Math.abs(ev.deltaY) > 0 && !ev.shiftKey) zoomAt(ev.deltaY < 0 ? 1.1 : 1 / 1.1, ev.clientX, ev.clientY); }, { passive: false });
  canvas.addEventListener("dragover", function (ev) { ev.preventDefault(); });
  canvas.addEventListener("drop", function (ev) { ev.preventDefault(); var k = ev.dataTransfer.getData("text/kind"); if (k && KINDS[k]) addNode(k, toWorld(ev.clientX - 20, ev.clientY - 20)); });
  function markSel() { world.querySelectorAll(".fn").forEach(function (e) { e.classList.toggle("is-selected", e.dataset.id === sel.node); }); renderEdges(); }

  document.addEventListener("keydown", function (ev) {
    var typing = /INPUT|TEXTAREA|SELECT/.test((ev.target.tagName || ""));
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); clearTimeout(saveTimer); save(); return; }
    if (typing) return;
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); return; }
    if (ev.key === "Delete" || ev.key === "Backspace") { ev.preventDefault(); removeSelected(); }
  });

  /* ---------- toolbar & palette ---------- */
  var pal = $("#fe-palette");
  PALETTE.forEach(function (group) {
    pal.append(el("h3", null, group[0]));
    group[1].forEach(function (k) {
      var b = el("button", "fe__kind"); b.type = "button"; b.disabled = !EDITABLE; b.draggable = EDITABLE;
      var dot = el("span", "fe__dot k-" + k, ICONS[k]); b.append(dot, el("span", null, KINDS[k]));
      b.onclick = function () { addNode(k); };
      b.addEventListener("dragstart", function (ev) { ev.dataTransfer.setData("text/kind", k); });
      pal.append(b);
    });
  });
  $("#fe-undo").onclick = undo; $("#fe-redo").onclick = redo;
  $("#fe-arrange").onclick = arrange; $("#fe-fit").onclick = fit;
  $("#fe-zoom-in").onclick = function () { var r = canvas.getBoundingClientRect(); zoomAt(1.15, r.left + r.width / 2, r.top + r.height / 2); };
  $("#fe-zoom-out").onclick = function () { var r = canvas.getBoundingClientRect(); zoomAt(1 / 1.15, r.left + r.width / 2, r.top + r.height / 2); };
  document.querySelectorAll(".fe__tab").forEach(function (t) { t.onclick = function () { tab = t.dataset.tab; renderSide(); }; });
  if (!EDITABLE) setState("ดูได้อย่างเดียว");

  render(); applyView();
  requestAnimationFrame(function () { renderEdges(); fit(); if (CFG.focus && nodeById(CFG.focus)) { sel.node = CFG.focus; render(); reveal(CFG.focus); } });
})();
