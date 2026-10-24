/* Build-time renderer for bundled LINE menu images (ADR 0019). */
(function () {
  "use strict";

  var ORANGE = "#df5d28";
  var DEEP_ORANGE = "#a93622";
  var GOLD = "#f3b62f";
  var INK = "#3b302d";
  var MUTED = "#7d6d65";

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function fillRound(ctx, x, y, w, h, r, fill) {
    roundRect(ctx, x, y, w, h, r);
    ctx.fillStyle = fill;
    ctx.fill();
  }

  function strokeRound(ctx, x, y, w, h, r, stroke, width) {
    roundRect(ctx, x, y, w, h, r);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.stroke();
  }

  function splitLongWord(ctx, word, width) {
    var lines = [];
    var line = "";
    Array.from(word).forEach(function (character) {
      var next = line + character;
      if (line && ctx.measureText(next).width > width) {
        lines.push(line);
        line = character;
      } else {
        line = next;
      }
    });
    if (line) lines.push(line);
    return lines;
  }

  function wrap(ctx, text, width) {
    var words = String(text || "").trim().split(/\s+/).filter(Boolean);
    var lines = [];
    var line = "";
    words.forEach(function (word) {
      if (ctx.measureText(word).width > width) {
        if (line) {
          lines.push(line);
          line = "";
        }
        lines = lines.concat(splitLongWord(ctx, word, width));
        return;
      }
      var next = line ? line + " " + word : word;
      if (line && ctx.measureText(next).width > width) {
        lines.push(line);
        line = word;
      } else {
        line = next;
      }
    });
    if (line) lines.push(line);
    return lines.slice(0, 3);
  }

  function iconType(label) {
    if (/Meter|CT|VT/.test(label)) return "meter";
    if (/หม้อแปลง|LBS|RMU/.test(label)) return "transformer";
    if (/เครื่องมือ|อุปกรณ์/.test(label)) return "tools";
    if (/SMG|AMR/.test(label)) return "system";
    if (/ใช้ไฟฟ้า/.test(label)) return "usage";
    if (/ปัญหา|ข้อเสนอ/.test(label)) return "chat";
    if (/งาน|สั่ง/.test(label)) return "task";
    if (/ติดตาม|เรื่องของฉัน/.test(label)) return "track";
    return "grid";
  }

  function drawBolt(ctx, x, y, scale) {
    ctx.beginPath();
    ctx.moveTo(x + 8 * scale, y - 30 * scale);
    ctx.lineTo(x - 18 * scale, y + 6 * scale);
    ctx.lineTo(x - 2 * scale, y + 6 * scale);
    ctx.lineTo(x - 9 * scale, y + 35 * scale);
    ctx.lineTo(x + 23 * scale, y - 8 * scale);
    ctx.lineTo(x + 6 * scale, y - 8 * scale);
    ctx.closePath();
    ctx.fillStyle = GOLD;
    ctx.fill();
  }

  function drawIcon(ctx, type, x, y, scale) {
    var stroke = ORANGE;
    ctx.save();
    ctx.translate(x, y);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = 11 * scale;
    ctx.strokeStyle = stroke;
    ctx.fillStyle = "#fff9f3";
    if (type === "meter") {
      fillRound(ctx, -70 * scale, -62 * scale, 140 * scale, 124 * scale, 24 * scale, "#fffaf4");
      strokeRound(ctx, -70 * scale, -62 * scale, 140 * scale, 124 * scale, 24 * scale, stroke, 8 * scale);
      ctx.beginPath();
      ctx.arc(0, -15 * scale, 26 * scale, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(-35 * scale, 34 * scale);
      ctx.lineTo(35 * scale, 34 * scale);
      ctx.stroke();
      drawBolt(ctx, 0, -2 * scale, scale * 0.7);
      [-94, 94].forEach(function (offset) {
        ctx.beginPath();
        ctx.arc(offset * scale, 26 * scale, 19 * scale, 0, Math.PI * 2);
        ctx.stroke();
      });
    } else if (type === "transformer") {
      ctx.beginPath();
      ctx.moveTo(-82 * scale, 58 * scale);
      ctx.lineTo(-54 * scale, -60 * scale);
      ctx.lineTo(54 * scale, -60 * scale);
      ctx.lineTo(82 * scale, 58 * scale);
      ctx.stroke();
      [-30, 30].forEach(function (offset) {
        ctx.beginPath();
        ctx.arc(offset * scale, -2 * scale, 28 * scale, -Math.PI / 2, Math.PI / 2);
        ctx.arc(offset * scale, -2 * scale, 28 * scale, Math.PI / 2, (Math.PI * 3) / 2);
        ctx.stroke();
      });
      ctx.beginPath();
      ctx.moveTo(-95 * scale, 60 * scale);
      ctx.lineTo(95 * scale, 60 * scale);
      ctx.stroke();
      drawBolt(ctx, 0, 4 * scale, scale * 0.72);
    } else if (type === "tools") {
      ctx.beginPath();
      ctx.moveTo(-75 * scale, -68 * scale);
      ctx.arc(-75 * scale, -68 * scale, 32 * scale, 0.35, Math.PI * 1.7);
      ctx.lineTo(34 * scale, 42 * scale);
      ctx.arc(50 * scale, 58 * scale, 21 * scale, -2.4, 0.75);
      ctx.lineTo(-57 * scale, -51 * scale);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(-45 * scale, 65 * scale);
      ctx.lineTo(75 * scale, -55 * scale);
      ctx.strokeStyle = GOLD;
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(80 * scale, -60 * scale, 21 * scale, 0, Math.PI * 2);
      ctx.stroke();
    } else if (type === "system") {
      [[0, -58], [-76, 35], [76, 35]].forEach(function (point) {
        ctx.beginPath();
        ctx.arc(point[0] * scale, point[1] * scale, 23 * scale, 0, Math.PI * 2);
        ctx.fillStyle = "#fffaf4";
        ctx.fill();
        ctx.strokeStyle = stroke;
        ctx.stroke();
      });
      ctx.strokeStyle = stroke;
      ctx.beginPath();
      ctx.moveTo(0, -35 * scale);
      ctx.lineTo(-59 * scale, 20 * scale);
      ctx.moveTo(0, -35 * scale);
      ctx.lineTo(59 * scale, 20 * scale);
      ctx.moveTo(-53 * scale, 35 * scale);
      ctx.lineTo(53 * scale, 35 * scale);
      ctx.stroke();
      drawBolt(ctx, 0, 8 * scale, scale * 0.62);
    } else if (type === "usage") {
      ctx.beginPath();
      ctx.moveTo(-80 * scale, -8 * scale);
      ctx.lineTo(0, -70 * scale);
      ctx.lineTo(80 * scale, -8 * scale);
      ctx.lineTo(65 * scale, -8 * scale);
      ctx.lineTo(65 * scale, 65 * scale);
      ctx.lineTo(-65 * scale, 65 * scale);
      ctx.lineTo(-65 * scale, -8 * scale);
      ctx.closePath();
      ctx.fillStyle = "#fffaf4";
      ctx.fill();
      ctx.stroke();
      drawBolt(ctx, 0, 18 * scale, scale * 0.82);
    } else if (type === "chat") {
      fillRound(ctx, -88 * scale, -60 * scale, 176 * scale, 118 * scale, 33 * scale, "#fffaf4");
      strokeRound(ctx, -88 * scale, -60 * scale, 176 * scale, 118 * scale, 33 * scale, stroke, 9 * scale);
      ctx.beginPath();
      ctx.moveTo(-23 * scale, 55 * scale);
      ctx.lineTo(-48 * scale, 86 * scale);
      ctx.lineTo(5 * scale, 58 * scale);
      ctx.stroke();
      [-35, 0, 35].forEach(function (offset) {
        ctx.beginPath();
        ctx.arc(offset * scale, -4 * scale, 8 * scale, 0, Math.PI * 2);
        ctx.fillStyle = GOLD;
        ctx.fill();
      });
    } else if (type === "task" || type === "track") {
      fillRound(ctx, -66 * scale, -72 * scale, 132 * scale, 144 * scale, 18 * scale, "#fffaf4");
      strokeRound(ctx, -66 * scale, -72 * scale, 132 * scale, 144 * scale, 18 * scale, stroke, 9 * scale);
      [-28, 2, 32].forEach(function (offset) {
        ctx.beginPath();
        ctx.moveTo(-35 * scale, offset * scale);
        ctx.lineTo(32 * scale, offset * scale);
        ctx.stroke();
      });
      if (type === "track") {
        ctx.beginPath();
        ctx.arc(70 * scale, 55 * scale, 31 * scale, 0, Math.PI * 2);
        ctx.strokeStyle = GOLD;
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(92 * scale, 77 * scale);
        ctx.lineTo(118 * scale, 103 * scale);
        ctx.stroke();
      }
    } else {
      [-38, 38].forEach(function (xOffset) {
        [-38, 38].forEach(function (yOffset) {
          fillRound(ctx, (xOffset - 25) * scale, (yOffset - 25) * scale, 50 * scale, 50 * scale, 12 * scale, "#fffaf4");
          strokeRound(ctx, (xOffset - 25) * scale, (yOffset - 25) * scale, 50 * scale, 50 * scale, 12 * scale, stroke, 7 * scale);
        });
      });
    }
    ctx.restore();
  }

  function drawBackground(ctx, canvas) {
    var background = ctx.createLinearGradient(0, 0, canvas.width, canvas.height);
    background.addColorStop(0, "#fffdf8");
    background.addColorStop(0.52, "#fff5e9");
    background.addColorStop(1, "#fee5c5");
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.save();
    ctx.globalAlpha = 0.48;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#f39b31";
    ctx.lineWidth = 105;
    ctx.beginPath();
    ctx.arc(-125, -140, 720, 0.25, 1.2);
    ctx.stroke();
    ctx.strokeStyle = "#e75d28";
    ctx.lineWidth = 52;
    ctx.beginPath();
    ctx.arc(-125, -140, 855, 0.28, 1.19);
    ctx.stroke();
    ctx.strokeStyle = "#f7ce53";
    ctx.lineWidth = 32;
    ctx.beginPath();
    ctx.arc(-125, -140, 920, 0.28, 1.2);
    ctx.stroke();
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = 0.32;
    ctx.strokeStyle = "#f2a136";
    ctx.lineWidth = 100;
    ctx.beginPath();
    ctx.arc(canvas.width + 250, canvas.height + 180, 780, Math.PI + 0.15, Math.PI * 1.66);
    ctx.stroke();
    ctx.strokeStyle = "#e9612b";
    ctx.lineWidth = 46;
    ctx.beginPath();
    ctx.arc(canvas.width + 250, canvas.height + 180, 900, Math.PI + 0.16, Math.PI * 1.65);
    ctx.stroke();
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = "#5da6dd";
    for (var i = 0; i < 28; i += 1) {
      var width = 34 + ((i * 37) % 86);
      var height = 52 + ((i * 71) % 230);
      ctx.fillRect(i * 96, canvas.height - height - 22, width, height);
    }
    ctx.restore();

    ctx.save();
    ctx.globalAlpha = 0.55;
    ctx.fillStyle = "#ffffff";
    [[1540, 110, 8], [1770, 230, 6], [2140, 130, 11], [2400, 420, 7], [1230, 1520, 7]].forEach(function (spark) {
      ctx.beginPath();
      ctx.arc(spark[0], spark[1], spark[2], 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.restore();
  }

  function titleLines(ctx, label, width) {
    if (!/\s/.test(label)) {
      for (var singleSize = 80; singleSize >= 46; singleSize -= 4) {
        ctx.font = "700 " + singleSize + "px MenuThai, sans-serif";
        if (ctx.measureText(label).width <= width) {
          return { size: singleSize, lines: [label] };
        }
      }
    }
    for (var size = 80; size >= 46; size -= 4) {
      ctx.font = "700 " + size + "px MenuThai, sans-serif";
      var lines = wrap(ctx, label, width);
      var widest = Math.max.apply(null, lines.map(function (line) { return ctx.measureText(line).width; }));
      if (lines.length <= 2 && widest <= width) return { size: size, lines: lines };
    }
    return { size: 46, lines: wrap(ctx, label, width) };
  }

  function drawTile(ctx, s) {
    var inset = 20;
    var x = s.x + inset;
    var y = s.y + inset;
    var w = s.w - inset * 2;
    var h = s.h - inset * 2;
    var card = ctx.createLinearGradient(x, y, x + w, y + h);
    card.addColorStop(0, "#ffffff");
    card.addColorStop(1, "#fff4e7");
    ctx.save();
    ctx.shadowColor = "rgba(124, 60, 28, 0.22)";
    ctx.shadowBlur = 16;
    ctx.shadowOffsetY = 8;
    fillRound(ctx, x, y, w, h, 56, card);
    ctx.restore();
    strokeRound(ctx, x, y, w, h, 56, ORANGE, 5);
    strokeRound(ctx, x + 10, y + 10, w - 20, h - 20, 46, "#f6c54d", 2);

    ctx.save();
    ctx.globalAlpha = 0.09;
    ctx.fillStyle = "#7bc7f1";
    ctx.beginPath();
    ctx.arc(x + w * 0.88, y + h * 0.13, w * 0.24, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    var scale = Math.min(w / 850, h / 690);
    drawIcon(ctx, iconType(s.label), x + w / 2, y + h * 0.29, scale * 0.92);
    var title = titleLines(ctx, s.label, w - 110);
    var lineHeight = Math.round(title.size * 1.13);
    var note = String(s.note || "");
    var total = title.lines.length * lineHeight + (note ? 70 : 0);
    var textY = y + h * 0.68 - total / 2 + lineHeight / 2;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = INK;
    ctx.font = "700 " + title.size + "px MenuThai, sans-serif";
    title.lines.forEach(function (line) {
      ctx.fillText(line, x + w / 2, textY);
      textY += lineHeight;
    });
    if (note) {
      textY += 24;
      ctx.fillStyle = MUTED;
      ctx.font = "600 43px MenuThai, sans-serif";
      ctx.fillText(note, x + w / 2, textY);
    }
  }

  function drawBar(ctx, s) {
    var inset = 20;
    var x = s.x + inset;
    var y = s.y + inset;
    var w = s.w - inset * 2;
    var h = s.h - inset * 2;
    var fill = ctx.createLinearGradient(x, y, x + w, y + h);
    fill.addColorStop(0, "#ef762b");
    fill.addColorStop(0.55, "#d95726");
    fill.addColorStop(1, DEEP_ORANGE);
    ctx.save();
    ctx.shadowColor = "rgba(117, 46, 25, 0.26)";
    ctx.shadowBlur = 14;
    ctx.shadowOffsetY = 6;
    fillRound(ctx, x, y, w, h, 46, fill);
    ctx.restore();
    strokeRound(ctx, x, y, w, h, 46, "#f7ca55", 4);
    ctx.save();
    ctx.globalAlpha = 0.22;
    ctx.fillStyle = "#ffffff";
    ctx.beginPath();
    ctx.arc(x + w * 0.86, y + h * 0.18, h * 0.72, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    ctx.fillStyle = "#ffffff";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "700 66px MenuThai, sans-serif";
    ctx.fillText(s.label, x + w / 2, y + h / 2 + 2);
  }

  function drawTab(ctx, s) {
    var inset = 18;
    var x = s.x + inset;
    var y = s.y + 16;
    var w = s.w - inset * 2;
    var h = s.h - 30;
    var fill = s.active ? DEEP_ORANGE : "#fff8f0";
    fillRound(ctx, x, y, w, h, 36, fill);
    strokeRound(ctx, x, y, w, h, 36, s.active ? "#f6c54d" : ORANGE, s.active ? 4 : 3);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = s.active ? "#ffffff" : INK;
    ctx.font = "700 64px MenuThai, sans-serif";
    ctx.fillText(s.label, x + w / 2, y + h / 2 + 2);
  }

  function draw(canvas, shapes) {
    var ctx = canvas.getContext("2d");
    drawBackground(ctx, canvas);
    shapes.forEach(function (s) {
      if (!s.label) return;
      if (s.place === "tab") {
        drawTab(ctx, s);
      } else if (s.place === "bar") {
        drawBar(ctx, s);
      } else {
        drawTile(ctx, s);
      }
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
