/* Build-time renderer for bundled LINE menu images (ADR 0019). */
(function () {
  "use strict";

  function wrap(ctx, text, width) {
    var words = String(text || "").split(" ");
    var lines = [];
    var line = "";
    words.forEach(function (word) {
      var next = line ? line + " " + word : word;
      if (ctx.measureText(next).width > width && line) {
        lines.push(line);
        line = word;
      } else {
        line = next;
      }
    });
    if (line) lines.push(line);
    return lines.slice(0, 3);
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw(canvas, shapes) {
    var ctx = canvas.getContext("2d");
    var font = getComputedStyle(document.body).fontFamily || "sans-serif";
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    shapes.forEach(function (s) {
      if (!s.label) return;
      if (s.place === "tab") {
        // The tab strip of a staff menu: the open tab dark, the others light.
        roundRect(ctx, s.x + 10, s.y + 14, s.w - 20, s.h - 28, 36);
        ctx.fillStyle = s.active ? "#111111" : "#e5e5e5";
        ctx.fill();
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = s.active ? "#ffffff" : "#111111";
        ctx.font = "700 80px " + font;
        ctx.fillText(s.label, s.x + s.w / 2, s.y + s.h / 2);
        return;
      }
      var bar = s.place === "bar";
      var pad = 14;
      roundRect(ctx, s.x + pad, s.y + pad, s.w - 2 * pad, s.h - 2 * pad, 40);
      ctx.fillStyle = bar ? "#111111" : "#f5f5f5";
      ctx.fill();
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      var cx = s.x + s.w / 2;
      if (bar) {
        ctx.fillStyle = "#ffffff";
        ctx.font = "600 84px " + font;
        ctx.fillText(s.label, cx, s.y + s.h / 2);
        return;
      }
      // Thai words run without spaces: shrink the title until every line fits the tile.
      var size = 112;
      var title;
      for (; size >= 60; size -= 6) {
        ctx.font = "700 " + size + "px " + font;
        title = wrap(ctx, s.label, s.w - 120);
        var widest = Math.max.apply(null, title.map(function (line) {
          return ctx.measureText(line).width;
        }));
        if (widest <= s.w - 120 && title.length <= 2) break;
      }
      var step = Math.round(size * 1.16);
      ctx.font = "400 56px " + font;
      var note = wrap(ctx, s.note, s.w - 140);
      var height = title.length * step + (note.length ? 36 + note.length * 72 : 0);
      var y = s.y + s.h / 2 - height / 2 + step / 2;
      ctx.fillStyle = "#111111";
      ctx.font = "700 " + size + "px " + font;
      title.forEach(function (line) {
        ctx.fillText(line, cx, y);
        y += step;
      });
      y += 36 - step / 2 + 36;
      ctx.fillStyle = "#707072";
      ctx.font = "400 56px " + font;
      note.forEach(function (line) {
        ctx.fillText(line, cx, y);
        y += 72;
      });
    });
  }

  function start() {
    var data = document.getElementById("menu-drawing");
    var canvases = document.querySelectorAll("[data-menu-canvas]");
    if (!data || !canvases.length) return;
    var pages = JSON.parse(data.textContent).pages || {};
    var byPage = {};
    canvases.forEach(function (canvas) {
      byPage[canvas.getAttribute("data-menu-canvas")] = canvas;
    });
    var render = function () {
      Object.keys(byPage).forEach(function (page) {
        draw(byPage[page], pages[page] || []);
      });
    };
    var ready = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    ready.then(render);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
