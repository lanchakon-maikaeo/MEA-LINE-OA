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

  // ADR 0042: the flows as they run now (new cases enter the owner division at once, the
  // warehouse stages replace the old stock steps, the chat window is on every page), in formal
  // wording, one idea per step.
  var TOURS = {
    basics: {
      title: "รู้จักหน้าจอ",
      summary: "เมนู คิวงาน หน้าเรื่อง และการสนทนากับผู้แจ้ง ใช้เวลาประมาณ 1 นาที",
      steps: [
        { page: "any", target: "[data-tour=nav]", title: "เมนูหลัก",
          body: "ภาพรวมแสดงตัวเลขสรุปของกอง คิวงานแสดงเรื่องที่ต้องดำเนินการ และรายงานแสดงสรุปย้อนหลัง 30 วัน" },
        { page: "any", target: "[data-tour=search]", show: { focus: "#q" }, title: "ค้นหาเลขเรื่อง",
          body: "พิมพ์เลขเรื่องแล้วกด Enter หรือกดปุ่ม / ได้จากทุกหน้า ระบบค้นเฉพาะเรื่องที่บัญชีของท่านมีสิทธิ์เห็น" },
        { page: "queue", query: "tab=host", target: "[data-tour=queue-tabs]", title: "แท็บของคิวงาน",
          body: "แท็บงานของกองแสดงเรื่องที่กองของท่านดูแล แท็บอื่นแยกเรื่องที่ส่งต่อมา งานย่อย งานของท่าน และเรื่องที่รอหน่วยงานตอบ ตัวเลขปรับปรุงทุกนาที" },
        { page: "queue", query: "tab=host&kind=withdrawal", target: "[data-tour=queue-filters]", title: "ตัวกรอง",
          body: "ตัวอย่างนี้เลือกคำขอเบิกไว้ให้แล้ว รายการด้านล่างจึงแสดงเฉพาะเรื่องเบิก ท่านกรองตามระดับความเร่งด่วน สถานะ หน่วยงาน หรือเฉพาะเรื่องที่เกินกรอบเวลาได้" },
        { page: "queue", target: ".qrow", title: "หนึ่งแถวคือหนึ่งเรื่อง",
          body: "ด้านซ้ายคือระดับความเร่งด่วน ด้านขวาคือเวลาที่เหลือตามเวลาทำการ จุดสีแดงคือเรื่องที่ควรดูก่อน กด ถัดไป เพื่อเปิดเรื่องแรก" },
        { page: "case", target: "[data-tour=case-head]", title: "หัวเรื่อง",
          body: "แสดงเลขเรื่อง ระดับความเร่งด่วน สถานะ กองที่ดูแล และผู้รับผิดชอบ" },
        { page: "case", target: "[data-tour=clocks]", title: "กรอบเวลา",
          body: "แสดงกำหนดเสร็จของขั้นตอนปัจจุบัน ช่วงที่รอหน่วยงานตอบไม่นับรวม หากเกินกรอบเวลา ระบบจะแจ้งเตือนและยกระดับให้" },
        { page: "case", target: "[data-tour=actions]", title: "การดำเนินการ",
          body: "ปุ่มสีดำคือขั้นตอนถัดไปที่ควรดำเนินการ ระบบแสดงเฉพาะการดำเนินการที่บัญชีของท่านทำได้" },
        { page: "case", show: { action: "set_priority" }, title: "แบบฟอร์มในหน้าเดียวกัน",
          body: "เมื่อกดปุ่ม แบบฟอร์มจะเปิดในหน้านี้ ตัวอย่างนี้คือแบบฟอร์มกำหนดระดับความเร่งด่วน หากมีผู้บันทึกก่อน ระบบจะแจ้งและเก็บข้อความที่ท่านพิมพ์ไว้" },
        { page: "case", target: "[data-tour=proposal]", show: { open: "[data-tour=proposal] details" }, title: "ข้อเสนอจากระบบ",
          body: "กติกาและ AI เสนอกองและระดับความเร่งด่วนพร้อมที่มาของข้อมูล ข้อเสนอนี้ยังไม่ใช่การตัดสิน เจ้าหน้าที่เป็นผู้ยืนยันเสมอ" },
        { page: "case", target: "#chatdock .chatwin__toggle", title: "สนทนากับผู้แจ้ง",
          body: "ปุ่มมุมล่างขวามีในทุกหน้า ตัวเลขสีแดงคือข้อความที่ยังไม่ได้ตอบ ในหน้าเรื่องจะเปิดการสนทนาของเรื่องนั้น ผู้แจ้งได้รับข้อความตามที่พิมพ์ทันที" },
        { page: "case", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, title: "ไทม์ไลน์",
          body: "ทุกการบันทึกอยู่ในส่วนนี้ ได้แก่ ผู้ดำเนินการ สิ่งที่ดำเนินการ เวลา และเหตุผล ตรวจสอบย้อนหลังได้ตลอด" }
      ]
    },
    withdrawal: {
      title: "ติดตามใบเบิก",
      summary: "เรื่องเข้ากองทันที → มอบหมายผู้รับผิดชอบ → บันทึกขั้นงานคลัง → ปิดเรื่อง",
      stages: ["ผู้แจ้งส่งเรื่อง", "เข้ากองทันที", "กำหนดระดับและผู้รับผิดชอบ", "บันทึกขั้นงานคลัง", "ปิดเรื่อง"],
      steps: [
        { page: "any", stage: 0, title: "ขั้นตอนของเรื่องเบิก",
          body: "ผู้แจ้งส่งเลขใบเบิก SAP ทาง LINE → เรื่องเข้ากองเจ้าภาพทันที → กองกำหนดระดับความเร่งด่วนและผู้รับผิดชอบ → บันทึกขั้นงานคลังจนเขตได้รับของ → ระบบปิดเรื่อง หน้าจอจะเปลี่ยนตามแต่ละขั้น" },
        { page: "queue", kind: "withdrawal", query: "tab=host", target: ".qrow[data-kind=withdrawal]", stage: 1, title: "1. เรื่องเข้าคิวของกอง",
          body: "เรื่องที่ผู้แจ้งส่งทาง LINE เข้าคิวของกองเจ้าภาพทันที ไม่ต้องรอกดรับเรื่อง ตัวกรองเลือกคำขอเบิกไว้แล้ว กด ถัดไป เพื่อเปิดเรื่อง" },
        { page: "case", kind: "withdrawal", target: "[data-tour=lines] || [data-tour=case-head]", stage: 1, title: "2. ข้อมูลตามใบเบิก",
          body: "ส่วนนี้แสดงเลขใบเบิก SAP และรายการตามใบที่ผู้แจ้งระบุ ใช้ตรวจสอบกับเอกสารในระบบ SAP" },
        { page: "case", kind: "withdrawal", show: { action: "set_priority" }, stage: 2, title: "3. กำหนดระดับความเร่งด่วน",
          body: "ระบบเสนอระดับไว้ให้ เจ้าหน้าที่เป็นผู้ยืนยัน หากเลือกต่างจากที่ระบบเสนอ โปรดระบุเหตุผล" },
        { page: "case", kind: "withdrawal", show: { action: "assign" }, stage: 2, title: "4. มอบหมายผู้รับผิดชอบ",
          body: "หากกองกำหนดผู้รับผิดชอบประจำงานไว้ ระบบมอบหมายให้ทันที หากยังไม่มี แอดมินกองเลือกผู้รับผิดชอบจากแบบฟอร์มนี้" },
        { page: "case", kind: "withdrawal", show: { form: "stage/" }, stage: 3, title: "5. บันทึกขั้นงานคลัง",
          body: "กดปุ่มบันทึกขั้นในส่วนสิ่งที่ทำได้ แบบฟอร์มจะเปิดในหน้านี้ บันทึกตามลำดับ ได้แก่ รับใบเบิกแล้ว คีย์จ่ายแล้ว และจ่ายแล้ว เมื่อบันทึกวันจ่าย ผู้แจ้งจะได้รับข้อความแจ้งวันจ่ายทาง LINE" },
        { page: "case", kind: "withdrawal", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, stage: 4, title: "6. ปิดเรื่อง",
          body: "เมื่อบันทึกว่าเขตได้รับของแล้ว ระบบปิดเรื่องโดยอัตโนมัติ ทุกขั้นบันทึกในไทม์ไลน์พร้อมชื่อผู้ดำเนินการ" }
      ]
    },
    "return": {
      title: "ติดตามใบส่งคืน",
      summary: "เรื่องเข้ากองทันที → แจ้งวิธีส่งคืน → บันทึกขั้นงานคลัง → ปิดเรื่อง",
      stages: ["ผู้แจ้งส่งเรื่อง", "เข้ากองทันที", "แจ้งวิธีส่งคืน", "บันทึกขั้นงานคลัง", "ปิดเรื่อง"],
      steps: [
        { page: "any", stage: 0, title: "ขั้นตอนของเรื่องส่งคืน",
          body: "ผู้แจ้งส่งเลขใบส่งคืน SAP ทาง LINE → เรื่องเข้ากองเจ้าภาพทันที → กองแจ้งวิธีส่งคืน → บันทึกขั้นงานคลังเมื่อรับของ → ระบบปิดเรื่อง" },
        { page: "queue", kind: "return", query: "tab=host", target: ".qrow[data-kind=return]", stage: 1, title: "1. เรื่องส่งคืนในคิว",
          body: "ตัวกรองเลือกคำขอคืนไว้แล้ว กด ถัดไป เพื่อเปิดเรื่อง" },
        { page: "case", kind: "return", target: "[data-tour=lines] || [data-tour=case-head]", stage: 1, title: "2. ข้อมูลตามใบส่งคืน",
          body: "ส่วนนี้แสดงเลขใบส่งคืน SAP และรายการที่ผู้แจ้งระบุ" },
        { page: "case", kind: "return", show: { action: "return_instructions" }, stage: 2, title: "3. แจ้งวิธีส่งคืน",
          body: "ระบุสถานที่และช่วงวันส่งของ ระบบส่งข้อความถึงผู้แจ้งทาง LINE ให้" },
        { page: "case", kind: "return", show: { form: "stage/" }, stage: 3, title: "4. บันทึกขั้นงานคลัง",
          body: "เมื่อรับของคืน กดปุ่มบันทึกขั้นในส่วนสิ่งที่ทำได้ แล้วบันทึกตามลำดับในแบบฟอร์มนี้ ชื่อของแต่ละขั้นเป็นไปตามที่กองกำหนด" },
        { page: "case", kind: "return", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, stage: 4, title: "5. ปิดเรื่อง",
          body: "เมื่อบันทึกขั้นสุดท้ายแล้ว ระบบปิดเรื่องโดยอัตโนมัติ" }
      ]
    },
    problem: {
      title: "แจ้งปัญหา/ข้อเสนอแนะ",
      summary: "เรื่องเข้ากองทันที → ดำเนินการแก้ไข → บันทึกผล → ผู้แจ้งยอมรับหรือโต้แย้ง",
      stages: ["ผู้แจ้งส่งเรื่อง", "เข้ากองทันที", "ดำเนินการแก้ไข", "บันทึกผล", "ผู้แจ้งตอบผล", "ปิดเรื่อง"],
      steps: [
        { page: "any", stage: 0, title: "ขั้นตอนของเรื่องแจ้งปัญหา",
          body: "ผู้แจ้งแจ้งปัญหาทาง LINE → เรื่องเข้ากองเจ้าภาพทันที → กองดำเนินการแก้ไขและขอข้อมูลเพิ่มเติมหากจำเป็น → บันทึกผลการแก้ปัญหา → ผู้แจ้งยอมรับหรือโต้แย้ง → ปิดเรื่อง" },
        { page: "queue", kind: "problem", query: "tab=host", target: ".qrow[data-kind=problem]", stage: 1, title: "1. เรื่องแจ้งปัญหาในคิว",
          body: "แถบสีแดงด้านซ้ายคือเรื่องแจ้งเหตุอันตราย ซึ่งต้องดำเนินการก่อน กด ถัดไป เพื่อเปิดเรื่อง" },
        { page: "case", kind: "problem", target: "[data-tour=hazard] || [data-tour=case-head]", stage: 1, title: "2. เหตุอันตราย",
          body: "หากข้อความมีคำที่บ่งชี้อันตราย ระบบกำหนดระดับฉุกเฉิน แสดงคำแนะนำเร่งด่วน และเริ่มนับกรอบเวลาทันที" },
        { page: "case", kind: "problem", show: { action: "request_info" }, stage: 2, title: "3. ขอข้อมูลเพิ่มเติม",
          body: "ข้อความนี้ส่งถึงผู้แจ้งทาง LINE ระหว่างรอผู้แจ้งตอบ กรอบเวลาของ ฝอจ. จะหยุดนับ" },
        { page: "case", kind: "problem", show: { action: "request_child_work" }, stage: 2, title: "4. ขอให้กองอื่นช่วยดำเนินการ",
          body: "สร้างงานย่อยให้กองอื่นดำเนินการบางส่วน กองเจ้าภาพยังดูแลเรื่องหลัก" },
        { page: "case", kind: "problem", show: { action: "resolve" }, stage: 3, title: "5. บันทึกผลการแก้ปัญหา",
          body: "ข้อความถึงผู้แจ้งแยกจากบันทึกภายใน แนบรูปหรือเอกสารได้ โดยไฟล์แนบจะไม่ส่งถึงผู้แจ้ง" },
        { page: "case", kind: "problem", show: { action: "dispute_resolution" }, stage: 4, title: "6. ผู้แจ้งตอบผล",
          body: "ผู้แจ้งกดยอมรับหรือโต้แย้งในการ์ด LINE หรือเจ้าหน้าที่บันทึกแทนด้วยแบบฟอร์มนี้ หากโต้แย้ง เรื่องจะกลับไปที่กองเจ้าภาพ" },
        { page: "case", kind: "problem", target: "[data-tour=timeline]", show: { open: "[data-tour=timeline] details" }, stage: 5, title: "7. ปิดเรื่อง",
          body: "เมื่อผู้แจ้งกดยืนยัน เรื่องจะปิด หากปัญหาเกิดซ้ำภายใน 30 วัน เปิดเรื่องเดิมใหม่ได้" }
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

  function rows(want) {
    return document.querySelector(".qrow" + (want ? "[data-kind=" + want + "]" : ""));
  }

  // A queue and a case of this kind the account can open, from the server: the queue in view
  // may have none (an admin on a quiet division, a division that never handles this kind).
  function where(want) {
    var url = "/tour/where/" + (want ? "?kind=" + encodeURIComponent(want) : "");
    return fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : {}; })
      .catch(function () { return {}; });
  }

  function leave(url) {
    window.location.href = url;
    return false;
  }

  // Bring the page to the step: a queue showing cases of the step's kind (with its tab and
  // filter), or a case of that kind. Resolves false while the browser moves to another page.
  function arrive(step, state) {
    var here = page();
    if (step.page === "queue") {
      var params = wanted(step);
      var want = params.get("kind") || "";
      if (here === "queue" && hasQuery(params) && rows(want)) return Promise.resolve(true);
      if (state.tried === "queue") return Promise.resolve(true); // cannot: explain in place
      state.tried = "queue";
      save(state);
      return where(want).then(function (place) {
        if (place.queue) {
          var current = window.location.pathname + window.location.search;
          return place.queue === current ? true : leave(place.queue);
        }
        if (here === "queue" && hasQuery(params)) return true;
        var query = params.toString();
        return leave(queueUrl() + (query ? "?" + query : ""));
      });
    }
    if (step.page === "case") {
      if (here === "case" && (!step.kind || kind() === step.kind)) return Promise.resolve(true);
      var link = here === "queue" && document.querySelector(
        ".qrow" + (step.kind ? "[data-kind=" + step.kind + "]" : "") + " .qrow__link"
      );
      if (link) return Promise.resolve(leave(link.getAttribute("href")));
      if (state.tried === "case") return Promise.resolve(true);
      state.tried = "case";
      save(state);
      // Nothing of this kind in view: explain without an example if the account sees none.
      return where(step.kind || "").then(function (place) {
        return place.case ? leave(place.case) : true;
      });
    }
    return Promise.resolve(true);
  }

  // Change the screen for the step: open sections, focus a field, or show an example form.
  function prepare(step) {
    var show = step.show || {};
    var form = show.form || (show.action ? "act/" + show.action + "/" : "");
    if (!form) clearDemo();
    if (show.open) {
      document.querySelectorAll(show.open).forEach(function (node) { node.open = true; });
    }
    if (show.focus) {
      var field = document.querySelector(show.focus);
      if (field) field.focus({ preventScroll: true });
    }
    var slot = document.getElementById("action-slot");
    if (!form || page() !== "case" || !slot) return Promise.resolve();
    // ADR 0051 Q11: show.form is a step outside the action list, e.g. "stage/" (งานคลัง).
    var url = "/cases/" + encodeURIComponent(caseNumber()) + "/" + form + "?demo=1";
    return fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) { slot.innerHTML = html; })
      .catch(function () { slot.innerHTML = ""; });
  }

  function target(step) {
    var demo = step.show && (step.show.action || step.show.form);
    var selector = step.target || (demo ? "#action-slot [data-demo]" : "");
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
    arrive(step, state).then(function (arrived) {
      if (!arrived) return;
      state.tried = "";
      save(state);
      prepare(step).then(function () { draw(tour, step, state); });
    });
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
    if ((step.target || (step.show && (step.show.action || step.show.form))) && !node) {
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
    x.setAttribute("aria-label", "ปิดการสาธิต");
    x.addEventListener("click", function () { dialog.remove(); });
    head.appendChild(x);
    card.appendChild(head);
    var title = el("h2", "tour__title", "เลือกหัวข้อที่ต้องการสาธิต");
    title.id = "tour-menu-title";
    card.appendChild(title);
    card.appendChild(el("p", "tour__body", "เมื่อกด ถัดไป หน้าจอจะพาไปยังส่วนที่เกี่ยวข้องและเปิดแบบฟอร์มตัวอย่างของขั้นตอนนั้น ท่านทดลองกรอกได้ โดยระบบจะไม่บันทึกข้อมูล"));
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
