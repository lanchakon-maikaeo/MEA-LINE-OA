/*
 * สาธิตการใช้งาน: a step-by-step guide that changes the real screen with each step.
 * A step may move to the queue (with the tab and filter it talks about) or to a case of one
 * kind, open sections, and show an example of the form that step uses (fetched with ?demo=1:
 * the form can be tried but has no way to be sent). Progress is kept in sessionStorage only;
 * the tour never presses a button or saves anything.
 */
(function () {
  "use strict";

  var KEY = "faoj.tour";

  var TOURS = {
    basics: {
      title: "รู้จักหน้าจอ",
      summary: "เมนู คิวงาน และหน้าเรื่อง ใช้เวลาราว 1 นาที",
      steps: [
        { page: "any", target: "[data-tour=nav]", title: "เมนูหลัก",
          body: "ภาพรวมคือตัวเลขของกองคุณ คิวงานคือเรื่องที่ต้องทำ รายงานคือสรุปย้อนหลัง 30 วัน" },
        { page: "any", target: "[data-tour=search]", show: { focus: "#q" }, title: "ค้นหาเลขเรื่อง",
          body: "พิมพ์เลขเรื่องแล้วกด Enter หรือกดปุ่ม / ที่หน้าไหนก็ได้ ค้นเจอเฉพาะเรื่องที่บัญชีคุณเห็น" },
        { page: "queue", query: "tab=host", target: "[data-tour=queue-tabs]", title: "แท็บของคิว",
          body: "งานของกองคือเรื่องที่กองคุณดูแล แท็บอื่นแยกเรื่องที่ส่งต่อมา งานย่อย งานของคุณ และเรื่องที่รอเขตตอบ ตัวเลขอัปเดตเองทุกนาที" },
        { page: "queue", query: "tab=host&kind=withdrawal", target: "[data-tour=queue-filters]", title: "ตัวกรอง",
          body: "ตัวอย่างนี้เลือก “คำขอเบิก” ไว้ให้แล้ว รายการด้านล่างจึงเหลือแต่เรื่องเบิก กรองตามระดับ สถานะ เขต หรือเฉพาะเรื่องที่เกินเวลาก็ได้" },
        { page: "queue", target: ".qrow", title: "หนึ่งแถวคือหนึ่งเรื่อง",
          body: "ซ้ายคือระดับความเร่งด่วน ขวาคือเวลาที่เหลือ (นับเฉพาะเวลาทำการ) จุดสีแดงคือสิ่งที่ต้องรีบดู กดถัดไปเพื่อเปิดเรื่องแรก" },
        { page: "case", target: "[data-tour=case-head]", title: "หัวเรื่อง",
          body: "เลขเรื่อง ระดับ สถานะ กองที่ดูแล และผู้รับผิดชอบ อยู่ตรงนี้เสมอ" },
        { page: "case", target: "[data-tour=clocks]", title: "กรอบเวลา",
          body: "บอกว่าขั้นนี้ต้องเสร็จเมื่อไร ช่วงที่รอเขตตอบไม่นับ ถ้าเกินเวลาระบบจะเตือนและยกระดับให้เอง" },
        { page: "case", target: "[data-tour=actions]", title: "สิ่งที่ทำได้",
          body: "ปุ่มสีดำคือขั้นที่ควรทำต่อ ระบบแสดงเฉพาะสิ่งที่บัญชีคุณทำได้" },
        { page: "case", show: { action: "set_priority" }, title: "ฟอร์มเปิดในหน้าเดียวกัน",
          body: "กดปุ่มแล้วฟอร์มจะเปิดตรงนี้ ตัวอย่างนี้คือฟอร์มยืนยันระดับความเร่งด่วน ถ้ามีคนบันทึกก่อนคุณ ระบบจะบอกและเก็บข้อความที่พิมพ์ไว้" },
        { page: "case", target: "[data-tour=proposal]", show: { open: "[data-tour=proposal] details" }, title: "ข้อเสนอจากระบบ",
          body: "กติกาและ AI เสนอกองกับระดับพร้อมที่มาของข้อเท็จจริง แต่ยังไม่ใช่การตัดสิน เจ้าหน้าที่เป็นคนยืนยันเสมอ" },
        { page: "case", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, title: "ไทม์ไลน์",
          body: "ทุกการบันทึกอยู่ที่นี่ ใครทำ ทำอะไร เมื่อไร และเพราะอะไร ย้อนดูได้ตลอด" }
      ]
    },
    withdrawal: {
      title: "เรื่องเบิก",
      summary: "คัดกรอง → อนุมัติ → จ่ายของ → เขตยืนยันรับ",
      stages: ["ยื่นเรื่อง", "คัดกรอง", "อนุมัติ", "จ่ายของ", "เขตยืนยันรับ", "ปิดเรื่อง"],
      steps: [
        { page: "any", stage: 0, title: "เรื่องเบิกเดินอย่างไร",
          body: "เขตยื่นเรื่องทาง LINE → กองรับเรื่องกลางคัดกรองแล้วส่งให้กองเจ้าของอุปกรณ์ → ผู้มีอำนาจอนุมัติจำนวน → ผู้จ่ายบันทึกการจ่าย → เขตยืนยันรับ → ปิดเรื่อง ระหว่างสาธิต หน้าจอจะเปลี่ยนตามแต่ละขั้น" },
        { page: "queue", kind: "withdrawal", query: "tab=host", target: ".qrow[data-kind=withdrawal]", stage: 0, title: "1. เรื่องเข้าคิว",
          body: "เรื่องที่เขตยื่นทาง LINE หรือที่กองรับเรื่องกลางบันทึกแทน จะเข้าคิว ตัวกรองเลือก “คำขอเบิก” ไว้ให้แล้ว กดถัดไปเพื่อเปิดเรื่องนี้" },
        { page: "case", kind: "withdrawal", target: "[data-tour=proposal]", stage: 1, title: "2. คัดกรอง: ดูข้อเสนอ",
          body: "ระบบเสนอว่ากองไหนควรรับและควรเป็นระดับไหน พร้อมกติกาที่ใช้ ใช้ประกอบการตัดสินเท่านั้น" },
        { page: "case", kind: "withdrawal", show: { action: "set_priority" }, stage: 1, title: "2. คัดกรอง: ยืนยันระดับ",
          body: "เจ้าหน้าที่ยืนยันระดับความเร่งด่วน ถ้าต่างจากที่ระบบเสนอให้ใส่เหตุผล" },
        { page: "case", kind: "withdrawal", show: { action: "request_handoff" }, stage: 1, title: "2. คัดกรอง: ส่งต่อให้กองเจ้าของอุปกรณ์",
          body: "เลือกกองที่รับ ถ้าต่างจากข้อเสนอต้องเลือกเหตุผล กองปลายทางกด “รับงานนี้” แล้วจึงเป็นเจ้าภาพ" },
        { page: "case", kind: "withdrawal", show: { action: "approve" }, stage: 2, title: "3. อนุมัติ",
          body: "ผู้มีอำนาจกรอกจำนวนที่อนุมัติต่อรายการ ถ้าน้อยกว่าที่ขอต้องมีเหตุผล อนุมัติแล้วแก้ไม่ได้" },
        { page: "case", kind: "withdrawal", show: { action: "issue" }, stage: 3, title: "4. จ่ายของ",
          body: "ผู้จ่ายของกองที่รับผิดชอบรายการบันทึกผู้รับของและเลขเครื่อง จ่ายบางส่วนก่อนได้ ส่วนที่เหลือจะค้างจ่ายไว้" },
        { page: "case", kind: "withdrawal", show: { action: "confirm" }, stage: 4, title: "5. เขตยืนยันรับ",
          body: "เขตกดยืนยันในการ์ด LINE หรือกองรับเรื่องกลางบันทึกแทนด้วยฟอร์มนี้ ถ้าของไม่ตรงต้องปิดส่วนต่างก่อน" },
        { page: "case", kind: "withdrawal", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, stage: 5, title: "6. ปิดเรื่อง",
          body: "ยืนยันรับครบแล้วระบบปิดเรื่องเอง ทุกขั้นบันทึกในไทม์ไลน์พร้อมชื่อผู้ทำ" }
      ]
    },
    "return": {
      title: "เรื่องคืน",
      summary: "แจ้งวิธีส่งคืน → รับของ → ตรวจสภาพ → ปิดเอง",
      stages: ["ยื่นเรื่อง", "คัดกรอง", "แจ้งวิธีส่งคืน", "รับของคืน", "ตรวจสภาพ", "ปิดเรื่อง"],
      steps: [
        { page: "any", stage: 0, title: "เรื่องคืนเดินอย่างไร",
          body: "เขตยื่นคืนของ → คัดกรองแล้วส่งให้กองเจ้าของอุปกรณ์ → แจ้งวิธีส่งคืน → ผู้รับคืนนับของที่มาถึง → ผู้ตรวจบันทึกสภาพ → รับครบแล้วเรื่องปิดเอง" },
        { page: "queue", kind: "return", query: "tab=host", target: ".qrow[data-kind=return]", stage: 0, title: "1. เรื่องคืนในคิว",
          body: "ตัวกรองเลือก “คำขอคืน” ไว้ให้แล้ว กดถัดไปเพื่อเปิดเรื่องนี้" },
        { page: "case", kind: "return", target: "[data-tour=proposal]", stage: 1, title: "2. คัดกรอง",
          body: "ระบบเสนอกองเจ้าของอุปกรณ์ตามรายการที่ขอคืน เจ้าหน้าที่ยืนยันแล้วส่งต่อ" },
        { page: "case", kind: "return", show: { action: "return_instructions" }, stage: 2, title: "3. แจ้งวิธีส่งคืน",
          body: "บอกสถานที่และช่วงวันส่งของ ระบบส่งข้อความถึงเขตทาง LINE ให้" },
        { page: "case", kind: "return", show: { action: "receive" }, stage: 3, title: "4. รับของคืน",
          body: "ผู้รับคืนบันทึกเฉพาะของที่มาถึงจริง ของที่ไม่ตรงคำขอจะขึ้นเป็นส่วนต่างให้คนตัดสิน" },
        { page: "case", kind: "return", show: { action: "inspect" }, stage: 4, title: "5. ตรวจสภาพ",
          body: "ผู้ตรวจต้องไม่ใช่คนรับของชิ้นนั้น แบ่งผลเป็นพร้อมใช้ ต้องซ่อม หรือดำเนินการอื่น ผลขัดกันให้ผู้มีอำนาจรับรองสภาพอีกคนตัดสิน" },
        { page: "case", kind: "return", show: { action: "terminate_return" }, stage: 5, title: "6. ส่วนที่ไม่มา",
          body: "ถ้าเขตไม่ส่งของบางส่วน ผู้กำกับงานยุติพร้อมเหตุผล (สูญหายต้องมีเลขเอกสารทะเบียน)" },
        { page: "case", kind: "return", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, stage: 5, title: "6. ปิดเรื่อง",
          body: "รับและตรวจครบแล้วเรื่องปิดเอง ไม่ต้องกดปิด" }
      ]
    },
    problem: {
      title: "เรื่องแจ้งปัญหา",
      summary: "คัดกรอง → แก้ไข → เขตยอมรับหรือโต้แย้ง",
      stages: ["ยื่นเรื่อง", "คัดกรอง", "แก้ไข", "บันทึกผล", "เขตตอบผล", "ปิดเรื่อง"],
      steps: [
        { page: "any", stage: 0, title: "เรื่องแจ้งปัญหาเดินอย่างไร",
          body: "เขตแจ้งปัญหา → คัดกรองและขอข้อมูลเพิ่มถ้าจำเป็น → ส่งให้กองเจ้าของอุปกรณ์ → ผู้แก้ปัญหาบันทึกผล → เขตยอมรับหรือโต้แย้ง → ปิดเรื่อง (เปิดใหม่ได้ใน 30 วัน)" },
        { page: "queue", kind: "problem", query: "tab=host", target: ".qrow[data-kind=problem]", stage: 0, title: "1. เรื่องแจ้งปัญหาในคิว",
          body: "แถบสีแดงทางซ้ายคือเรื่องแจ้งเหตุอันตราย ต้องดูก่อน กดถัดไปเพื่อเปิดเรื่อง" },
        { page: "case", kind: "problem", target: "[data-tour=hazard] || [data-tour=case-head]", stage: 1, title: "2. เหตุอันตรายต้องรู้ทันที",
          body: "ถ้าข้อความมีคำที่บ่งชี้อันตราย ระบบตั้งระดับฉุกเฉินและแสดงคำแนะนำเร่งด่วนทันที ไม่ต้องรอ AI" },
        { page: "case", kind: "problem", show: { action: "request_info" }, stage: 1, title: "2. ขอข้อมูลเพิ่มจากเขต",
          body: "ข้อความนี้ส่งถึงผู้แจ้งทาง LINE ระหว่างรอเขตตอบ กรอบเวลาของ ฝอจ. หยุดนับ" },
        { page: "case", kind: "problem", target: "[data-tour=clocks]", stage: 2, title: "3. กรอบเวลาแก้ไข",
          body: "นับเวลาทำการ 07:30–15:30 เรื่องฉุกเฉินนับต่อเนื่อง เกินเวลาแล้วระบบเตือนและยกระดับให้ผู้กำกับงาน" },
        { page: "case", kind: "problem", show: { action: "request_child_work" }, stage: 2, title: "3. ให้กองอื่นช่วย",
          body: "สร้างงานย่อยให้กองอื่นทำบางส่วน กองเดิมยังดูแลเรื่องหลัก" },
        { page: "case", kind: "problem", show: { action: "resolve" }, stage: 3, title: "4. บันทึกผลการแก้ปัญหา",
          body: "ข้อความถึงผู้แจ้งแยกจากบันทึกภายใน แนบรูปหรือเอกสารได้ ไฟล์ไม่ถูกส่งถึงผู้แจ้ง" },
        { page: "case", kind: "problem", show: { action: "dispute_resolution" }, stage: 4, title: "5. เขตตอบผล",
          body: "เขตกดยอมรับหรือโต้แย้งในการ์ด LINE หรือกองรับเรื่องกลางบันทึกแทนด้วยฟอร์มนี้ โต้แย้งแล้วเรื่องกลับไปที่กองเดิม" },
        { page: "case", kind: "problem", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, stage: 5, title: "6. ปิดเรื่อง",
          body: "เขตยอมรับผลแล้วเรื่องปิด ถ้าปัญหากลับมาภายใน 30 วันเปิดเรื่องเดิมใหม่ได้" }
      ]
    }
  };

  function load() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (e) { return null; }
  }
  function save(state) {
    try {
      if (state) sessionStorage.setItem(KEY, JSON.stringify(state));
      else sessionStorage.removeItem(KEY);
    } catch (e) { /* private mode: the tour simply does not survive a page change */ }
  }

  function page() { return document.body.getAttribute("data-page") || ""; }
  function kind() { return document.body.getAttribute("data-kind") || ""; }
  function queueUrl() {
    var link = document.querySelector("[data-tour-link=queue]");
    return (link ? link.getAttribute("href") : "/console/").split("?")[0];
  }
  function caseNumber() {
    var match = window.location.pathname.match(/^\/cases\/([^/]+)\/$/);
    return match ? match[1] : "";
  }

  // The query a queue step wants (its tab and filter), e.g. "tab=host&kind=withdrawal".
  function wanted(step) {
    var params = new URLSearchParams(step.query || "");
    if (step.kind) params.set("kind", step.kind);
    return params;
  }
  function hasQuery(params) {
    var current = new URLSearchParams(window.location.search);
    var ok = true;
    params.forEach(function (value, key) { if (current.get(key) !== value) ok = false; });
    return ok;
  }

  var layer = null;

  function clearDemo() {
    var slot = document.getElementById("action-slot");
    if (slot && slot.querySelector("[data-demo]")) slot.innerHTML = "";
  }

  function close() {
    save(null);
    clearDemo();
    if (layer) { layer.remove(); layer = null; }
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    var start = document.querySelector("[data-tour-start]");
    if (start) start.focus();
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  // Bring the page to the step: the queue with the step's tab/filter, or a case of its kind.
  function arrive(step, state) {
    var here = page();
    if (step.page === "any") return true;
    if (step.page === "queue") {
      var params = wanted(step);
      if (here === "queue" && hasQuery(params)) return true;
      if (state.tried === "queue") return true; // cannot get there: explain in place
      state.tried = "queue";
      save(state);
      var query = params.toString();
      window.location.href = queueUrl() + (query ? "?" + query : "");
      return false;
    }
    if (step.page === "case") {
      if (here === "case" && (!step.kind || kind() === step.kind)) return true;
      if (here === "queue") {
        var link = document.querySelector(
          ".qrow" + (step.kind ? "[data-kind=" + step.kind + "]" : "") + " .qrow__link"
        );
        if (link) {
          window.location.href = link.getAttribute("href");
          return false;
        }
        if (step.kind && !hasQuery(wanted({ kind: step.kind })) && state.tried !== "case") {
          state.tried = "case";
          save(state);
          window.location.href = queueUrl() + "?kind=" + step.kind;
          return false;
        }
        return true; // nothing of this kind in the queue: explain without an example
      }
      if (state.tried !== "case") {
        state.tried = "case";
        save(state);
        window.location.href = queueUrl() + (step.kind ? "?kind=" + step.kind : "");
        return false;
      }
      return true;
    }
    return true;
  }

  // Change the screen for the step: open sections, focus a field, or show an example form.
  function prepare(step) {
    var show = step.show || {};
    if (!show.action) clearDemo();
    if (show.open) {
      document.querySelectorAll(show.open).forEach(function (node) { node.open = true; });
    }
    if (show.focus) {
      var field = document.querySelector(show.focus);
      if (field) field.focus({ preventScroll: true });
    }
    var slot = document.getElementById("action-slot");
    if (!show.action || page() !== "case" || !slot) return Promise.resolve();
    var url = "/cases/" + encodeURIComponent(caseNumber()) + "/act/" + show.action + "/?demo=1";
    return fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) { slot.innerHTML = html; })
      .catch(function () { slot.innerHTML = ""; });
  }

  function target(step) {
    var selector = step.target || (step.show && step.show.action ? "#action-slot [data-demo]" : "");
    if (!selector) return null;
    // "a || b": the first selector on the page wins (a selector list goes by document order).
    var options = selector.split("||");
    for (var i = 0; i < options.length; i++) {
      var node = document.querySelector(options[i].trim());
      if (node && node.offsetParent !== null) return node;
    }
    return null;
  }

  function place() {
    if (!layer) return;
    var spot = layer.querySelector(".tour__spot");
    var card = layer.querySelector(".tour__card");
    var node = layer._target;
    layer.classList.toggle("tour--dim", !node);
    if (!node) {
      spot.hidden = true;
      card.style.top = "";
      card.style.left = "";
      card.classList.add("tour__card--center");
      return;
    }
    var r = node.getBoundingClientRect();
    var pad = 8;
    spot.hidden = false;
    spot.style.top = (r.top - pad) + "px";
    spot.style.left = (r.left - pad) + "px";
    spot.style.width = (r.width + pad * 2) + "px";
    spot.style.height = (r.height + pad * 2) + "px";
    card.classList.remove("tour__card--center");
    if (window.innerWidth < 600) return; // a bottom sheet on phones
    var cw = card.offsetWidth, ch = card.offsetHeight;
    var room = window.innerWidth - r.right - 24;
    var top, left;
    if (room >= cw) { // beside a tall target (a form), so both stay in view
      left = r.right + 16;
      top = Math.min(Math.max(16, r.top), window.innerHeight - ch - 16);
    } else {
      top = r.bottom + 16;
      if (top + ch > window.innerHeight - 16) top = Math.max(16, r.top - ch - 16);
      left = Math.min(Math.max(16, r.left), window.innerWidth - cw - 16);
    }
    card.style.top = top + "px";
    card.style.left = left + "px";
  }

  function render(state) {
    var tour = TOURS[state.tour];
    if (!tour) { close(); return; }
    var step = tour.steps[state.step];
    if (!step) { close(); return; }
    save(state); // the step survives the page change arrive() may start
    if (!arrive(step, state)) return;
    state.tried = "";
    save(state);
    prepare(step).then(function () { draw(tour, step, state); });
  }

  function draw(tour, step, state) {
    if (!layer) {
      layer = el("div", "tour");
      layer.appendChild(el("div", "tour__spot"));
      var card = el("div", "tour__card");
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "false");
      card.setAttribute("aria-labelledby", "tour-title");
      card.tabIndex = -1;
      layer.appendChild(card);
      document.body.appendChild(layer);
      window.addEventListener("resize", place);
      window.addEventListener("scroll", place, true);
    }
    var cardNode = layer.querySelector(".tour__card");
    cardNode.innerHTML = "";
    var head = el("div", "tour__head");
    head.appendChild(el("span", "tour__count", tour.title + " · " + (state.step + 1) + " / " + tour.steps.length));
    var x = el("button", "tour__x", "✕");
    x.type = "button";
    x.setAttribute("aria-label", "ปิดการสาธิต");
    x.addEventListener("click", close);
    head.appendChild(x);
    cardNode.appendChild(head);
    if (tour.stages) {
      var stages = el("ol", "tour__stages");
      stages.setAttribute("aria-label", "ลำดับขั้น");
      tour.stages.forEach(function (name, i) {
        var item = el("li", i < step.stage ? "is-done" : i === step.stage ? "is-now" : "", name);
        if (i === step.stage) item.setAttribute("aria-current", "step");
        stages.appendChild(item);
      });
      cardNode.appendChild(stages);
    }
    var title = el("h2", "tour__title", step.title);
    title.id = "tour-title";
    cardNode.appendChild(title);
    cardNode.appendChild(el("p", "tour__body", step.body));
    var node = target(step);
    if ((step.target || (step.show && step.show.action)) && !node) {
      cardNode.appendChild(el("p", "tour__note", "เรื่องที่เปิดอยู่ยังไม่มีส่วนนี้ให้ดู หรือคิวของคุณยังไม่มีเรื่องชนิดนี้ อ่านคำอธิบายแล้วไปขั้นถัดไปได้"));
    }
    var bar = el("div", "tour__bar");
    var back = el("button", "btn btn--secondary btn--sm", "ย้อนกลับ");
    back.type = "button";
    back.disabled = state.step === 0;
    back.addEventListener("click", function () { go(state, -1); });
    var last = state.step === tour.steps.length - 1;
    var next = el("button", "btn btn--sm", last ? "จบการสาธิต" : "ถัดไป");
    next.type = "button";
    next.addEventListener("click", function () { if (last) close(); else go(state, 1); });
    bar.appendChild(back);
    bar.appendChild(next);
    cardNode.appendChild(bar);

    layer._target = node;
    if (node) {
      var tall = node.offsetHeight > window.innerHeight * 0.6;
      node.scrollIntoView({ block: tall ? "start" : "center", behavior: "smooth" });
    }
    place();
    window.setTimeout(place, 400);
    cardNode.focus({ preventScroll: true });
    next.focus({ preventScroll: true });
  }

  function go(state, delta) {
    state.step += delta;
    state.tried = "";
    render(state);
  }

  function chooser() {
    var dialog = el("div", "tour tour--menu");
    var card = el("div", "tour__card tour__card--center");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "tour-menu-title");
    var head = el("div", "tour__head");
    head.appendChild(el("span", "tour__count", "สาธิตการใช้งาน"));
    var x = el("button", "tour__x", "✕");
    x.type = "button";
    x.setAttribute("aria-label", "ปิด");
    x.addEventListener("click", function () { dialog.remove(); });
    head.appendChild(x);
    card.appendChild(head);
    var title = el("h2", "tour__title", "อยากดูขั้นตอนไหน");
    title.id = "tour-menu-title";
    card.appendChild(title);
    card.appendChild(el("p", "tour__body", "กด ถัดไป แล้วหน้าจอจะพาไปยังส่วนที่เกี่ยวข้อง และเปิดฟอร์มตัวอย่างของขั้นนั้นให้ดู ฟอร์มตัวอย่างลองกรอกได้แต่ระบบไม่บันทึก"));
    var list = el("div", "tour__list");
    Object.keys(TOURS).forEach(function (id) {
      var item = el("button", "tour__choice");
      item.type = "button";
      item.appendChild(el("strong", "", TOURS[id].title));
      item.appendChild(el("span", "", TOURS[id].summary));
      item.addEventListener("click", function () {
        dialog.remove();
        render({ tour: id, step: 0, tried: "" });
      });
      list.appendChild(item);
    });
    card.appendChild(list);
    dialog.appendChild(card);
    dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.remove(); });
    document.body.appendChild(dialog);
    var first = list.querySelector("button");
    if (first) first.focus();
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-tour-start]")) {
      event.preventDefault();
      if (layer) close();
      chooser();
    }
  });
  document.addEventListener("keydown", function (event) {
    var menu = document.querySelector(".tour--menu");
    if (event.key === "Escape") {
      if (menu) { menu.remove(); return; }
      if (layer) close();
      return;
    }
    if (!layer || event.target.closest("input, textarea, select")) return;
    var state = load();
    if (!state) return;
    if (event.key === "ArrowRight" && state.step < TOURS[state.tour].steps.length - 1) go(state, 1);
    if (event.key === "ArrowLeft" && state.step > 0) go(state, -1);
  });

  // Resume a tour that moved to another page.
  var state = load();
  if (state && TOURS[state.tour]) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { render(state); });
    } else {
      render(state);
    }
  }
})();
