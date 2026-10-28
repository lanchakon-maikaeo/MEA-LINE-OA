/* Build-time artwork and typography only; tap bounds come from line_menu.drawing(). */
(function () {
  "use strict";
  var BLUE = "#a5dcef", ORANGE = "#e9b38a", GOLD = "#e9ce83";
  var INK = "#f5f7fb", MUTED = "#d8e2ef";

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, r);
  }

  function strokePath(ctx, path) {
    ctx.stroke(new Path2D(path));
  }

  function splitLongWord(ctx, word, width) {
    var lines = [], line = "";
    Array.from(word).forEach(function (character) {
      var next = line + character;
      if (line && ctx.measureText(next).width > width) {
        lines.push(line);
        line = character;
      } else line = next;
    });
    if (line) lines.push(line);
    return lines;
  }

  function wrap(ctx, text, width) {
    var words = String(text || "").trim().split(/\s+/).filter(Boolean);
    var lines = [], line = "";
    words.forEach(function (word) {
      if (ctx.measureText(word).width > width) {
        if (line) { lines.push(line); line = ""; }
        lines = lines.concat(splitLongWord(ctx, word, width));
        return;
      }
      var next = line ? line + " " + word : word;
      if (line && ctx.measureText(next).width > width) {
        lines.push(line);
        line = word;
      } else line = next;
    });
    if (line) lines.push(line);
    return lines.slice(0, 3);
  }

  function titleLines(ctx, label, width) {
    if (label === "หม้อแปลง LBS RMU") {
      return { size: 80, lines: ["หม้อแปลง LBS", "RMU"] };
    }
    if (!/\s/.test(label)) {
      for (var singleSize = 80; singleSize >= 46; singleSize -= 4) {
        ctx.font = "700 " + singleSize + "px MenuThai, sans-serif";
        if (ctx.measureText(label).width <= width) return { size: singleSize, lines: [label] };
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

  function drawBolt(ctx) {
    ctx.fillStyle = GOLD;
    ctx.fill(new Path2D("M 9 -40 L -25 7 H -3 L -11 43 L 29 -13 H 7 Z"));
  }

  function drawMeter(ctx) {
    strokePath(ctx, "M -52 -90 H 52 V 40 H -52 Z M -39 -76 H 39 V -33 H -39 Z");
    strokePath(ctx, "M -17 -57 H 17 M -39 0 H 39 V 25 H -39 Z M -39 40 V 89 H 39 V 40");
    strokePath(ctx, "M -36 -18 H -27");
    [8, 33].forEach(function (x) {
      ctx.beginPath();
      ctx.arc(x, -18, 8, 0, Math.PI * 2);
      ctx.stroke();
    });
  }

  function drawIcon(ctx, type, x, y, scale, accent) {
    ctx.save();
    ctx.translate(x, y);
    ctx.scale(scale, scale);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = 5;
    ctx.strokeStyle = accent;
    ctx.shadowColor = accent;
    ctx.shadowBlur = 6;
    if (type === "meter") {
      drawMeter(ctx);
    } else if (type === "transformer") {
      strokePath(ctx, "M -85 -4 H 85 V 70 H -85 Z M -85 8 H -108 V 58 H -85");
      strokePath(ctx, "M 85 8 H 108 V 58 H 85 M -46 70 V 90 M -64 90 H -28 M 46 70 V 90 M 28 90 H 64");
      [-55, 0, 55].forEach(function (offset) {
        ctx.save();
        ctx.translate(offset, 0);
        strokePath(ctx, "M 0 -100 V -4 M -12 -82 H 12 M -12 -59 H 12 M -12 -36 H 12 M 0 12 V 54");
        ctx.restore();
      });
    } else if (type === "tools") {
      strokePath(ctx, "M -58 -55 C -102 -50 -93 -100 -62 -88 L -48 -73 L -61 -60 L -78 -75");
      strokePath(ctx, "M -51 -68 L 77 61 C 95 80 73 103 56 83 L -70 -45");
      ctx.strokeStyle = GOLD;
      strokePath(ctx, "M -79 86 L 61 -55 M -84 72 L -67 89");
      ctx.beginPath();
      ctx.arc(76, -70, 20, 0, Math.PI * 2);
      ctx.stroke();
    } else if (type === "system") {
      strokePath(ctx, "M -63 -22 C -101 -22 -101 -72 -65 -78 C -48 -119 -2 -117 14 -81 C 49 -86 74 -58 68 -34 C 100 -17 80 15 56 15 H -62 C -91 15 -91 -17 -63 -22 Z");
      strokePath(ctx, "M -48 16 L -110 57 M 0 16 V 65 M 48 16 L 110 57");
      ctx.save();
      ctx.translate(-118, 93);
      ctx.scale(0.34, 0.34);
      drawMeter(ctx);
      ctx.restore();
      strokePath(ctx, "M -30 67 H 30 V 109 H -30 Z M -30 109 L -42 122 H 42 L 30 109");
      strokePath(ctx, "M 102 66 H 142 L 153 106 H 91 Z M 99 86 H 147 M 115 66 L 111 106 M 130 66 L 134 106 M 122 106 V 126 M 108 126 H 136");
      ctx.strokeStyle = BLUE;
      [17, 29, 41].forEach(function (radius) {
        ctx.beginPath();
        ctx.arc(0, -22, radius, Math.PI * 1.22, Math.PI * 1.78);
        ctx.stroke();
      });
      ctx.beginPath();
      ctx.arc(0, -22, 3, 0, Math.PI * 2);
      ctx.stroke();
    } else if (type === "usage") {
      strokePath(ctx, "M -91 -8 L 0 -85 L 91 -8 H 69 V 80 H -69 V -8 Z");
      ctx.translate(0, 16);
      drawBolt(ctx);
    } else if (type === "chat") {
      roundRect(ctx, -91, -67, 182, 124, 30);
      ctx.stroke();
      strokePath(ctx, "M -20 57 L -51 91 L 9 57");
      [-40, 0, 40].forEach(function (offset) {
        ctx.beginPath();
        ctx.arc(offset, -4, 6, 0, Math.PI * 2);
        ctx.fillStyle = accent;
        ctx.fill();
      });
    } else if (type === "task" || type === "track") {
      roundRect(ctx, -66, -82, 132, 164, 15);
      ctx.stroke();
      [-40, 0, 40].forEach(function (offset) {
        strokePath(ctx, "M -34 " + offset + " H 34");
      });
      if (type === "track") {
        ctx.beginPath();
        ctx.arc(65, 57, 31, 0, Math.PI * 2);
        ctx.stroke();
        strokePath(ctx, "M 88 80 L 112 104");
      }
    } else {
      [-42, 42].forEach(function (dx) {
        [-42, 42].forEach(function (dy) {
          roundRect(ctx, dx - 27, dy - 27, 54, 54, 10);
          ctx.stroke();
        });
      });
    }
    ctx.restore();
  }

  function artworkSlice(ctx, artwork, source, target) {
    ctx.drawImage(artwork,
      source[0] * artwork.naturalWidth, source[1] * artwork.naturalHeight,
      source[2] * artwork.naturalWidth, source[3] * artwork.naturalHeight,
      target.x, target.y, target.w, target.h);
  }

  function drawTile(ctx, s, artwork, index, columns) {
    var row = Math.floor(index / columns), column = index % columns;
    // Crop the approved text-free artwork into the unchanged tap bounds.
    var sourceY = row === 0 ? 0 : 400 / 1030;
    var sourceH = row === 0 ? 400 / 1030 : 424 / 1030;
    artworkSlice(ctx, artwork, [column / 3, sourceY, 1 / 3, sourceH], s);
    var accent = (row + column) % 2 ? ORANGE : BLUE;
    var scale = Math.min(s.w / 650, s.h / 600);
    drawIcon(ctx, iconType(s.label), s.x + s.w / 2, s.y + s.h * 0.30, scale, accent);
    var title = titleLines(ctx, s.label, s.w - 100);
    var lineHeight = Math.round(title.size * 1.13);
    var textY = s.y + s.h * 0.635 - (title.lines.length - 1) * lineHeight / 2;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = INK;
    ctx.font = "700 " + title.size + "px MenuThai, sans-serif";
    title.lines.forEach(function (line) {
      ctx.fillText(line, s.x + s.w / 2, textY);
      textY += lineHeight;
    });
    if (s.note) {
      ctx.fillStyle = MUTED;
      ctx.font = "600 " + Math.round(title.size * 0.66) + "px MenuThai, sans-serif";
      ctx.fillText(s.note, s.x + s.w / 2, s.y + s.h * 0.825);
    }
  }

  function drawBar(ctx, s, artwork, index) {
    artworkSlice(ctx, artwork, [index / 2, 824 / 1030, 1 / 2, 206 / 1030], s);
    ctx.fillStyle = INK;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "700 90px MenuThai, sans-serif";
    ctx.fillText(s.label, s.x + s.w / 2, s.y + s.h * 0.415);
  }

  function drawTab(ctx, s) {
    var fill = ctx.createLinearGradient(s.x, s.y, s.x, s.y + s.h);
    fill.addColorStop(0, s.active ? "#89502f" : "#112b42");
    fill.addColorStop(1, s.active ? "#492b20" : "#071729");
    roundRect(ctx, s.x + 18, s.y + 16, s.w - 36, s.h - 30, 30);
    ctx.fillStyle = fill;
    ctx.fill();
    ctx.lineWidth = s.active ? 5 : 3;
    ctx.strokeStyle = s.active ? ORANGE : "#5d9abb";
    ctx.stroke();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = INK;
    ctx.font = "700 64px MenuThai, sans-serif";
    ctx.fillText(s.label, s.x + s.w / 2, s.y + s.h / 2);
  }

  function draw(canvas, shapes, artwork) {
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#06172a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    var hasBar = shapes.some(function (s) { return s.place === "bar"; });
    var tileIndex = 0, barIndex = 0;
    shapes.forEach(function (s) {
      if (!s.label) return;
      if (s.place === "tab") drawTab(ctx, s);
      else if (s.place === "bar") drawBar(ctx, s, artwork, barIndex++);
      else drawTile(ctx, s, artwork, tileIndex++, hasBar ? 3 : 2);
    });
    canvas.dataset.menuRendered = "true";
  }

  function start() {
    var data = document.getElementById("menu-drawing");
    var artwork = document.getElementById("menu-artwork");
    var canvases = document.querySelectorAll("[data-menu-canvas]");
    if (!data || !artwork || !canvases.length) return;
    var pages = JSON.parse(data.textContent).pages || {};
    Promise.all([document.fonts.ready, artwork.decode()]).then(function () {
      canvases.forEach(function (canvas) {
        draw(canvas, pages[canvas.getAttribute("data-menu-canvas")] || [], artwork);
      });
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
