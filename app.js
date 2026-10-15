/* Patchouli book viewer: a port of Patchouli 1.20.1's book GUI to a 2D canvas.
   All coordinates are GUI pixels; the canvas transform scales them to device pixels. */
'use strict';

// ------------------------------------------------------------------ constants (GuiBook)
const FULL_WIDTH = 272, FULL_HEIGHT = 180, PAGE_WIDTH = 116, PAGE_HEIGHT = 156;
const TOP_PADDING = 18, LEFT_PAGE_X = 15, RIGHT_PAGE_X = 141, TEXT_LINE_HEIGHT = 9;
const MAX_BOOKMARKS = 10, ENTRIES_PER_PAGE = 13, ENTRIES_IN_FIRST_PAGE = 11;
const STATE = { UNREAD: { ord: 0, icon: true, anim: true, u: 140 }, PENDING: { ord: 1, icon: true, anim: true, u: 148 },
  NEUTRAL: { ord: 2, icon: false, anim: false, u: 0 }, COMPLETED: { ord: 3, icon: true, anim: false, u: 156 } };
const LEGACY = { '0': 0x000000, '1': 0x0000AA, '2': 0x00AA00, '3': 0x00AAAA, '4': 0xAA0000, '5': 0xAA00AA, '6': 0xFFAA00,
  '7': 0xAAAAAA, '8': 0x555555, '9': 0x5555FF, 'a': 0x55FF55, 'b': 0x55FFFF, 'c': 0xFF5555, 'd': 0xFF55FF, 'e': 0xFFFF55, 'f': 0xFFFFFF };

let DATA, MODS = { mods: [] }, IMG = {}, canvas, ctx, BOOK, BOOKS = [];
let ticks = 0, totalTicks = 0, lastTime = performance.now();
let shiftDown = false;

// ------------------------------------------------------------------ persistence
const STORE_KEY = 'patchouli-web:goety';
let SAVE = { books: {}, scale: 0, last: null, clickedVisualize: false };
try { const s = JSON.parse(localStorage.getItem(STORE_KEY)); if (s && s.books) SAVE = Object.assign(SAVE, s); } catch (e) { /* no storage */ }
function persist() { try { localStorage.setItem(STORE_KEY, JSON.stringify(SAVE)); } catch (e) { /* ignore */ } }
function bookData(book) {
  if (!SAVE.books[book.id]) SAVE.books[book.id] = { viewed: [], history: [], bookmarks: [], quests: [] };
  return SAVE.books[book.id];
}

const L = (key, ...args) => {
  let s = (DATA.lang && DATA.lang[key]) || key;
  let i = 0;
  return s.replace(/%%|%(\d+\$)?[sd]/g, (m, pos) => m === '%%' ? '%' : String(pos ? args[parseInt(pos) - 1] : args[i++]));
};

// ------------------------------------------------------------------ assets
function loadImage(src) {
  return new Promise((res, rej) => { const im = new Image(); im.onload = () => res(im); im.onerror = () => rej(new Error('Could not load ' + src)); im.src = src; });
}

// ------------------------------------------------------------------ font (Minecraft bitmap font)
const FONT = { glyphs: new Map(), sheets: [], tinted: new Map() };
function initFont() {
  DATA.font.forEach((p, pi) => {
    const img = IMG[p.file];
    const rows = p.chars.length, cols = [...p.chars[0]].length;
    const cw = img.width / cols, ch = img.height / rows;
    const scale = p.height / ch;
    const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
    const cx = c.getContext('2d'); cx.drawImage(img, 0, 0);
    const px = cx.getImageData(0, 0, img.width, img.height).data;
    // copy every glyph cell into a sheet with a 1px transparent gutter: when text is drawn scaled down
    // (overflow: resize), the browser can sample a column of the neighbouring glyph otherwise
    const sheet = document.createElement('canvas'); sheet.width = cols * (cw + 2); sheet.height = rows * (ch + 2);
    const sx = sheet.getContext('2d');
    for (let r = 0; r < rows; r++) for (let col = 0; col < cols; col++) {
      sx.drawImage(img, col * cw, r * ch, cw, ch, col * (cw + 2) + 1, r * (ch + 2) + 1, cw, ch);
    }
    FONT.sheets[pi] = sheet;
    p.chars.forEach((row, r) => {
      [...row].forEach((chr, col) => {
        if (chr === '\u0000' || FONT.glyphs.has(chr)) return;
        // BitmapProvider.getActualGlyphWidth
        let w = 0;
        for (let x = cw - 1; x >= 0 && !w; x--) {
          for (let y = 0; y < ch; y++) {
            if (px[((r * ch + y) * img.width + col * cw + x) * 4 + 3] !== 0) { w = x + 1; break; }
          }
        }
        FONT.glyphs.set(chr, { p: pi, sx: col * (cw + 2) + 1, sy: r * (ch + 2) + 1, sw: cw, sh: ch, scale, ascent: p.ascent,
          adv: Math.floor(0.5 + w * scale) + 1 });
      });
    });
  });
  FONT.glyphs.set(' ', { space: true, adv: 4 });
  FONT.glyphs.set('‌', { space: true, adv: 0 });
}
function glyph(c) { return FONT.glyphs.get(c) || FONT.glyphs.get('?'); }
function tintedSheet(pi, rgb) {
  const key = pi + ':' + rgb;
  let c = FONT.tinted.get(key);
  if (!c) {
    const img = FONT.sheets[pi];
    c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
    const cx = c.getContext('2d');
    cx.fillStyle = '#' + rgb.toString(16).padStart(6, '0'); cx.fillRect(0, 0, c.width, c.height);
    cx.globalCompositeOperation = 'destination-in'; cx.drawImage(img, 0, 0);
    FONT.tinted.set(key, c);
  }
  return c;
}
const BASE_STYLE = { color: 0x000000, bold: false, italic: false, underline: false, strike: false, obf: false };
const styleWith = (s, o) => Object.assign({}, s, o);

// iterate characters of a literal string the way StringDecomposer.iterateFormatted does (§ codes)
function iterFormatted(text, style, fn) {
  let st = style;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '§') {
      if (i + 1 >= text.length) break;
      const code = text[++i].toLowerCase();
      if (LEGACY[code] !== undefined) st = Object.assign({}, BASE_STYLE, { color: LEGACY[code] });
      else if (code === 'k') st = styleWith(st, { obf: true });
      else if (code === 'l') st = styleWith(st, { bold: true });
      else if (code === 'm') st = styleWith(st, { strike: true });
      else if (code === 'n') st = styleWith(st, { underline: true });
      else if (code === 'o') st = styleWith(st, { italic: true });
      else if (code === 'r') st = style;
      continue;
    }
    fn(c, st);
  }
}
function textWidth(text, style = BASE_STYLE) {
  let w = 0;
  iterFormatted(text, style, (c, st) => { w += glyph(c).adv + (st.bold ? 1 : 0); });
  return w;
}
// Font.drawString. color: 0xRRGGBB; alpha 0..1
function drawString(text, x, y, color, shadow = false, style = BASE_STYLE, alpha = 1) {
  const base = Object.assign({}, style, { color });
  if (shadow) drawRaw(text, x + 1, y + 1, base, alpha, true);
  return drawRaw(text, x, y, base, alpha, false);
}
function drawRaw(text, x, y, style, alpha, isShadow) {
  const startX = x;
  const prevA = ctx.globalAlpha; ctx.globalAlpha = prevA * alpha;
  iterFormatted(text, style, (c, st) => {
    let g = glyph(c);
    if (st.obf && !g.space) {
      const same = [...FONT.glyphs.entries()].filter(([k, v]) => !v.space && v.adv === g.adv && v.p === 0);
      if (same.length) g = same[(Math.random() * same.length) | 0][1];
    }
    let col = st.color;
    if (isShadow) col = (col & 0xFCFCFC) >> 2;
    const adv = g.adv + (st.bold ? 1 : 0);
    if (!g.space) {
      const sheet = tintedSheet(g.p, col);
      const top = y + 7 - g.ascent, w = g.sw * g.scale, h = g.sh * g.scale;
      const draw = (dx) => {
        if (st.italic) {
          ctx.save(); ctx.translate(x + dx, top); ctx.transform(1, 0, -0.25 / 1, 1, 0, 0);
          ctx.drawImage(sheet, g.sx, g.sy, g.sw, g.sh, 1, 0, w, h); ctx.restore();
        } else ctx.drawImage(sheet, g.sx, g.sy, g.sw, g.sh, x + dx, top, w, h);
      };
      draw(0); if (st.bold) draw(1);
    }
    if (st.strike || st.underline) {
      ctx.fillStyle = '#' + col.toString(16).padStart(6, '0');
      if (st.strike) ctx.fillRect(x - 1, y + 3.5, adv + 1, 1);
      if (st.underline) ctx.fillRect(x - 1, y + 8, adv + 1, 1);
    }
    x += adv;
  });
  ctx.globalAlpha = prevA;
  return x - startX;
}
function drawCentered(text, cx, y, color) { drawString(text, cx - Math.trunc(textWidth(text) / 2), y, color); }

// ------------------------------------------------------------------ render context
// Tracks a transform stack so hit regions and the mouse can be expressed in local coordinates.
const RC = { stack: [], ox: 0, oy: 0, s: 1, mouse: { x: -999, y: -999 }, hits: [], tooltip: null, tooltipStack: null,
  targetPage: null, recordHits: true };
function push(dx, dy, s = 1) {
  ctx.save(); ctx.translate(dx, dy); if (s !== 1) ctx.scale(s, s);
  RC.stack.push([RC.ox, RC.oy, RC.s]);
  RC.ox += dx * RC.s; RC.oy += dy * RC.s; RC.s *= s;
}
function pop() { ctx.restore(); [RC.ox, RC.oy, RC.s] = RC.stack.pop(); }
function mx() { return (RC.mouse.x - RC.ox) / RC.s; }
function my() { return (RC.mouse.y - RC.oy) / RC.s; }
// GuiBook.isMouseInRelativeRange (exclusive left/top, inclusive right/bottom)
function inRange(x, y, w, h) { const a = mx(), b = my(); return a > x && b > y && a <= x + w && b <= y + h; }
function hit(x, y, w, h, fn, opts = {}) {
  if (!RC.recordHits) return;
  RC.hits.push({ x: RC.ox + x * RC.s, y: RC.oy + y * RC.s, w: w * RC.s, h: h * RC.s, fn, ...opts });
}
function setTooltip(lines) { RC.tooltip = lines; }

function tex(name) { return IMG[name]; }
// GuiGraphics.blit with an explicit texture size
function blit(img, x, y, u, v, w, h, tw, th, dw = w, dh = h) {
  if (!img) return;
  const sx = img.width / tw, sy = img.height / th;
  ctx.drawImage(img, u * sx, v * sy, w * sx, h * sy, x, y, dw, dh);
}
function drawFromTexture(book, x, y, u, v, w, h) { blit(tex(book.texture), x, y, u, v, w, h, 512, 256); }
function fill(x1, y1, x2, y2, argb) {
  const a = ((argb >>> 24) & 255) / 255;
  ctx.fillStyle = `rgba(${(argb >> 16) & 255},${(argb >> 8) & 255},${argb & 255},${a})`;
  ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
}
function fillGradient(x1, y1, x2, y2, c1, c2) {
  if (c1 === c2) return fill(x1, y1, x2, y2, c1);
  const g = ctx.createLinearGradient(0, y1, 0, y2);
  const css = (c) => `rgba(${(c >> 16) & 255},${(c >> 8) & 255},${c & 255},${((c >>> 24) & 255) / 255})`;
  g.addColorStop(0, css(c1)); g.addColorStop(1, css(c2));
  ctx.fillStyle = g; ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
}
const hex = (s) => parseInt(s, 16);

function drawSeparator(book, x, y) {
  const w = 110, h = 3, rx = x + PAGE_WIDTH / 2 - w / 2;
  ctx.globalAlpha = 0.8; drawFromTexture(book, rx, y, 140, 180, w, h); ctx.globalAlpha = 1;
}
function drawLock(book, x, y) { drawFromTexture(book, x, y, 250, 180, 16, 16); }
function drawMarking(book, x, y, state) {
  if (!state.icon) return;
  const a = state.anim ? Math.sin(totalTicks * 0.2) * 0.3 + 0.7 : 1;
  ctx.globalAlpha = a; drawFromTexture(book, x, y, state.u, 197, 8, 8); ctx.globalAlpha = 1;
}
function drawPageFiller(book, x = RIGHT_PAGE_X, y = TOP_PADDING) {
  const img = tex(book.filler); if (img) ctx.drawImage(img, x + PAGE_WIDTH / 2 - 64, y + PAGE_HEIGHT / 2 - 74, 128, 128);
}

// ------------------------------------------------------------------ items
// item id -> [atlas image, sx, sy, size]; the main atlas is registered first, so mod packs only add icons it lacks
const ICONS = new Map();
function registerIcons(icons, img) {
  if (!icons || !img) return;
  const S = icons.size, cols = icons.cols;
  for (const [id, idx] of Object.entries(icons.index)) {
    if (!ICONS.has(id)) ICONS.set(id, [img, (idx % cols) * S, Math.floor(idx / cols) * S, S]);
  }
}
function stackId(s) { return typeof s === 'string' ? s : s && s.i; }
function stackName(s) {
  if (s && typeof s === 'object' && s.n) return s.n;
  const id = stackId(s);
  return (DATA.itemNames && DATA.itemNames[id]) || (id ? id.split(':')[1] : 'Air');
}
const RARITY = { e: 0xFFFF55, b: 0x55FFFF, d: 0xFF55FF };
function drawItem(s, x, y, hoverable = true) {
  if (!s) return;
  const icon = ICONS.get(stackId(s));
  if (icon) {
    const [atlas, sx, sy, S] = icon;
    ctx.drawImage(atlas, sx, sy, S, S, x, y, 16, 16);
    if (s.g) drawGlint(atlas, sx, sy, S, x, y);
  }
  const count = typeof s === 'object' && s.c ? s.c : 1;
  if (count !== 1) {
    const str = String(count);
    drawString(str, x + 19 - 2 - textWidth(str), y + 6 + 3, 0xFFFFFF, true);
  }
  if (hoverable && inRange(x, y, 16, 16)) setTooltipStack(s);
}
let glintCanvas = null;
function drawGlint(atlas, sx, sy, S, x, y) {
  const g = IMG['img/vanilla/glint.png']; if (!g) return;
  if (!glintCanvas) { glintCanvas = document.createElement('canvas'); glintCanvas.width = S; glintCanvas.height = S; }
  const gc = glintCanvas.getContext('2d');
  gc.globalCompositeOperation = 'source-over'; gc.clearRect(0, 0, S, S);
  const t = (performance.now() % 3000) / 3000;
  gc.save(); gc.translate(S / 2, S / 2); gc.rotate(-0.17); gc.translate(-S / 2, -S / 2);
  const off = t * S * 2;
  for (let k = -2; k < 3; k++) gc.drawImage(g, -S + off + k * S, -S, S * 2, S * 3);
  gc.restore();
  gc.globalCompositeOperation = 'destination-in';
  gc.drawImage(atlas, sx, sy, S, S, 0, 0, S, S);
  ctx.save(); ctx.globalCompositeOperation = 'lighter'; ctx.globalAlpha = 0.55;
  ctx.drawImage(glintCanvas, 0, 0, S, S, x, y, 16, 16); ctx.restore();
}
function drawIngredient(list, x, y) {
  if (!list || !list.length) return;
  drawItem(list[Math.floor(ticks / 20) % list.length], x, y);
}
function setTooltipStack(s) {
  RC.tooltip = null; RC.tooltipStack = s;
}
function itemTooltipLines(s, gui) {
  const custom = typeof s === 'object' && s.n;
  let color = (typeof s === 'object' && s.r && RARITY[s.r]) || 0xFFFFFF;
  const lines = [{ text: stackName(s), color, italic: !!custom }];
  if (typeof s === 'object' && s.l) s.l.forEach(t => lines.push({ text: t, color: 0xAAAAAA }));
  RC.targetPage = null;
  const prov = gui.book.recipeMap.get(stackId(s));
  if (prov && !(gui instanceof GuiBookEntry && gui.entry === prov[0])) {
    lines.push({ text: '(' + L('patchouli.gui.lexicon.shift_for_recipe') + ')', color: 0xFFAA00 });
    RC.targetPage = prov;
  }
  return lines;
}

// ------------------------------------------------------------------ book text (BookTextParser + TextLayouter)
function expandMacros(book, text) {
  let s = text == null ? '[ERROR]' : text;
  for (let i = 0; i < 10; i++) {
    let n = s;
    for (const [k, v] of book.macroList) n = n.split(k).join(v);
    if (n === s) break;
    s = n;
  }
  return s;
}
class SpanState {
  constructor(gui, book, base) {
    this.gui = gui; this.book = book; this.base = base;
    this.stack = [{ cur: base }];
    this.tooltip = ''; this.onClick = null; this.cluster = null;
    this.isExternalLink = false; this.endingExternal = false;
    this.lineBreaks = 0; this.spacingLeft = 0; this.spacingRight = 0;
    this.spaceWidth = textWidth(' ', base);
  }
  peek() { return this.stack[this.stack.length - 1].cur; }
  modify(o) { const f = this.stack[this.stack.length - 1]; f.cur = styleWith(f.cur, o); }
  push(o) { this.stack.push({ cur: styleWith(this.peek(), o) }); }
  pop() { if (this.stack.length <= 1) throw new Error('Underflow in style stack'); this.stack.pop(); }
  reset() {
    this.endingExternal = this.isExternalLink;
    this.stack = [{ cur: this.base }];
    this.cluster = null; this.tooltip = ''; this.onClick = null; this.isExternalLink = false;
  }
}
function makeSpan(state, text, errorStyle) {
  const sp = { text, style: errorStyle || state.peek(), onClick: errorStyle ? null : state.onClick,
    cluster: errorStyle ? null : state.cluster, tooltip: errorStyle ? '' : state.tooltip,
    lineBreaks: state.lineBreaks, spacingLeft: state.spacingLeft, spacingRight: state.spacingRight };
  sp.bold = sp.style.bold;
  state.lineBreaks = 0; state.spacingLeft = 0; state.spacingRight = 0;
  return sp;
}
const COMMANDS = {
  'br': (s) => { s.lineBreaks = 1; return ''; },
  'br2': (s) => { s.lineBreaks = 2; return ''; }, '2br': (s) => { s.lineBreaks = 2; return ''; }, 'p': (s) => { s.lineBreaks = 2; return ''; },
  '/l': (s) => { s.endingExternal = s.isExternalLink; s.pop(); s.cluster = null; s.tooltip = ''; s.onClick = null; s.isExternalLink = false; return ''; },
  '/t': (s) => { s.cluster = null; s.tooltip = ''; return ''; },
  'playername': () => 'Player',
  'k': (s) => { s.modify({ obf: true }); return ''; }, 'obf': (s) => { s.modify({ obf: true }); return ''; },
  'l': (s) => { s.modify({ bold: true }); return ''; }, 'bold': (s) => { s.modify({ bold: true }); return ''; },
  'm': (s) => { s.modify({ strike: true }); return ''; }, 'strike': (s) => { s.modify({ strike: true }); return ''; },
  'n': (s) => { s.modify({ underline: true }); return ''; }, 'underline': (s) => { s.modify({ underline: true }); return ''; },
  'o': (s) => { s.modify({ italic: true }); return ''; }, 'italic': (s) => { s.modify({ italic: true }); return ''; },
  'italics': (s) => { s.modify({ italic: true }); return ''; },
  '': (s) => { s.reset(); return ''; }, 'reset': (s) => { s.reset(); return ''; }, 'clear': (s) => { s.reset(); return ''; },
  'nocolor': (s) => { s.modify({ color: s.base.color }); return ''; },
  '/c': (s) => { s.pop(); s.cluster = null; s.tooltip = ''; s.onClick = null; return ''; },
};
const KEYS = { use: 'Right Button', attack: 'Left Button', sneak: 'Left Shift', jump: 'Space', inventory: 'E', sprint: 'Left Control',
  drop: 'Q', swapOffhand: 'F', 'key.use': 'Right Button', 'key.attack': 'Left Button', 'key.sneak': 'Left Shift' };
const FUNCTIONS = {
  'k': (param, s) => {
    const k = KEYS[param] || KEYS['key.' + param];
    if (!k) { s.tooltip = L('patchouli.gui.lexicon.keybind_missing', param); return 'N/A'; }
    s.tooltip = L('patchouli.gui.lexicon.keybind', param); return k;
  },
  'l': (param, s) => {
    s.cluster = []; s.push({ color: s.book.linkColor });
    if (/^https?:/.test(param)) {
      s.tooltip = L('patchouli.gui.lexicon.external_link'); s.isExternalLink = true;
      s.onClick = () => { window.open(param, '_blank', 'noopener'); return true; };
    } else {
      let anchor = null; const h = param.indexOf('#');
      if (h >= 0) { anchor = param.substring(h + 1); param = param.substring(0, h); }
      const href = param.includes(':') ? param : s.book.ns + ':' + param;
      const entry = s.book.entryMap.get(href), cat = s.book.catMap.get(href);
      if (entry) {
        s.tooltip = entry.name;
        let page = 0;
        if (anchor != null) {
          const ap = entry.pages.findIndex(p => p.anchor === anchor);
          if (ap >= 0) page = Math.floor(ap / 2); else s.tooltip += ' (INVALID ANCHOR:' + anchor + ')';
        }
        const gui = s.gui;
        s.onClick = () => { gui.display(new GuiBookEntry(s.book, entry, page), true); flip(); return true; };
      } else if (cat) {
        if (anchor != null) s.tooltip = 'BAD LINK: Cannot specify anchor when linking to a category';
        else { s.tooltip = cat.name; const gui = s.gui; s.onClick = () => { gui.display(new GuiBookCategory(s.book, cat), true); flip(); return true; }; }
      } else s.tooltip = 'BAD LINK: ' + param;
    }
    return '';
  },
  'tooltip': (param, s) => { s.tooltip = param; s.cluster = []; return ''; },
  't': (param, s) => { s.tooltip = param; s.cluster = []; return ''; },
  'command': (param, s) => { s.push({ color: s.book.linkColor }); s.cluster = []; s.tooltip = param.startsWith('/') ? (param.length < 20 ? param : param.substring(0, 20) + '...') : 'INVALID COMMAND (must begin with /)'; s.onClick = () => true; return ''; },
  'c': (param, s) => FUNCTIONS.command(param, s),
};
function processCommand(state, cmd) {
  state.endingExternal = false;
  let result;
  if (cmd.length === 1 && /^[0-9a-f]$/.test(cmd)) { state.modify({ color: LEGACY[cmd] }); result = ''; }
  else if (cmd.startsWith('#') && (cmd.length === 4 || cmd.length === 7)) {
    let p = cmd.substring(1); if (p.length === 3) p = p[0] + p[0] + p[1] + p[1] + p[2] + p[2];
    const v = parseInt(p, 16);
    state.modify({ color: isNaN(v) ? state.base.color : v }); result = '';
  } else if (/^li\d?$/.test(cmd)) {
    const c = cmd.length > 2 ? cmd[2] : '1';
    const dist = /\d/.test(c) ? parseInt(c) : 1;
    state.lineBreaks = 1; state.spacingLeft = dist * 4; state.spacingRight = state.spaceWidth;
    result = '§0' + (dist % 2 === 0 ? '◦' : '•');
  } else if (cmd.indexOf(':') > 0) {
    const i = cmd.indexOf(':'); const f = FUNCTIONS[cmd.substring(0, i)];
    result = f ? f(cmd.substring(i + 1), state) : '[MISSING FUNCTION: ' + cmd.substring(0, i) + ']';
  } else if (COMMANDS[cmd] !== undefined) result = COMMANDS[cmd](state);
  else result = '$(' + cmd + ')';
  if (state.endingExternal) result += '§7↪';
  return result;
}
function parseText(gui, book, text, baseColor) {
  const state = new SpanState(gui, book, styleWith(BASE_STYLE, { color: baseColor }));
  const spans = [];
  const src = expandMacros(book, text);
  const re = /\$\(([^)]*)\)/g; let m, last = 0;
  while ((m = re.exec(src))) {
    spans.push(makeSpan(state, src.substring(last, m.index)));
    last = re.lastIndex;
    try {
      const processed = processCommand(state, m[1]);
      if (processed.length) { spans.push(makeSpan(state, processed)); if (state.cluster == null) state.tooltip = ''; }
    } catch (e) { spans.push(makeSpan(state, '[ERROR]', styleWith(BASE_STYLE, { color: 0xFF5555 }))); }
  }
  spans.push(makeSpan(state, src.substring(last)));
  return spans;
}
// java.text.BreakIterator.getLineInstance, approximated: break after whitespace runs and after hyphens
function breakPoints(text) {
  const b = [0];
  for (let i = 1; i < text.length; i++) {
    const p = text[i - 1], c = text[i];
    if (/\s/.test(p) && !/\s/.test(c)) b.push(i);
    else if (p === '-' && /[A-Za-z0-9]/.test(c) && i >= 2 && /[A-Za-z0-9]/.test(text[i - 2])) b.push(i);
  }
  b.push(text.length);
  return b;
}
function charWidth(c, style) { return c === '§' ? 0 : glyph(c).adv + (style.bold ? 1 : 0); }
function layoutText(spans, pageX, pageY, lineHeight, basePageWidth, mode) {
  let words = [], y, pageWidth = basePageWidth, smallestOverstep, pending = [], lineStart = 0, widthSoFar = 0;
  let linkCluster = null, spanCluster = null, points = [];
  const tail = (span, start, cluster) => {
    const sub = span.text.substring(start);
    return { span, start, cluster, length: span.text.length - start, width: textWidth(sub, span.style) + span.spacingLeft + span.spacingRight };
  };
  const position = (t, x, yy, length) => {
    const w = { text: t.span.text.substring(t.start, t.start + length), style: t.span.style, x: x + t.span.spacingLeft, y: yy,
      width: t.width, height: 8, onClick: t.span.onClick, tooltip: t.span.tooltip, cluster: t.cluster };
    if (t.cluster) t.cluster.push(w);
    return w;
  };
  const flush = () => { let x = pageX; for (const t of pending) { words.push(position(t, x, y, t.length)); x += t.width; } pending = []; };
  const breakAt = (textOffset) => {
    let index, offset = lineStart, x = pageX;
    for (index = 0; index < pending.length; index++) {
      const t = pending[index];
      if (offset + t.length < textOffset) { words.push(position(t, x, y, t.length)); offset += t.length; x += t.width; }
      else { words.push(position(t, x, y, textOffset - offset)); pending[index] = tail(t.span, t.start + textOffset - offset, t.cluster); break; }
    }
    pending.splice(0, index);
    lineStart = textOffset; y += lineHeight;
  };
  const preceding = (off) => { let r = 0; for (const p of points) { if (p < off) r = p; else break; } return r; };
  const breakLine = () => {
    let width = 0, offset = 0;
    for (const t of pending) { width += t.width; offset += t.length; }
    const lastT = pending[pending.length - 1];
    width -= lastT.width; offset -= lastT.length;
    const chars = lastT.span.text;
    for (let i = lastT.start; i < chars.length; i++) {
      width += charWidth(chars[i], lastT.span.style);
      if (lastT.span.bold) width++;
      if (width > pageWidth) {
        smallestOverstep = Math.min(width, smallestOverstep);
        const overflowOffset = lineStart + offset + i - lastT.start;
        let breakOffset = overflowOffset + 1;
        if (!/\s/.test(chars[i])) breakOffset = preceding(breakOffset);
        if (breakOffset <= lineStart) breakOffset = overflowOffset - 1;
        breakAt(breakOffset);
        return;
      }
    }
    flush(); y += lineHeight;
  };
  const layoutSpan = (span) => {
    if (spanCluster !== span.cluster) { linkCluster = span.cluster == null ? null : []; spanCluster = span.cluster; }
    const t = tail(span, 0, linkCluster);
    widthSoFar += t.width; pending.push(t);
    let guard = 0;
    while (widthSoFar > pageWidth && guard++ < 500) {
      breakLine();
      widthSoFar = 0; for (const p of pending) widthSoFar += p.width;
    }
  };
  const layoutParagraph = (para) => {
    points = breakPoints(para.map(s => s.text).join(''));
    lineStart = 0;
    for (const s of para) layoutSpan(s);
    flush();
  };
  const overflow = () => (y + lineHeight - pageY) / (PAGE_HEIGHT - pageY);
  const scale = () => basePageWidth / pageWidth;
  let iter = 0;
  do {
    y = pageY; words = []; smallestOverstep = Infinity; widthSoFar = 0; pending = []; linkCluster = null; spanCluster = null;
    let para = [];
    for (const sp of spans) {
      if (sp.lineBreaks > 0) { layoutParagraph(para); widthSoFar = 0; y += sp.lineBreaks * lineHeight; para = []; }
      para.push(sp);
    }
    if (para.length) layoutParagraph(para);
    if (!(mode === 'resize' && overflow() * scale() > 1)) break;
    pageWidth = 1 + Math.min(smallestOverstep, Math.trunc(basePageWidth * overflow()));
  } while (++iter < 50);
  if (mode === 'truncate') words = words.filter(w => w.y + lineHeight <= PAGE_HEIGHT);
  return { words, scale: scale() };
}
class BookText {
  constructor(gui, text, x, y, width = PAGE_WIDTH, lineHeight = TEXT_LINE_HEIGHT, baseColor = null) {
    this.book = gui.book;
    const spans = parseText(gui, gui.book, text || '', baseColor == null ? gui.book.textColor : baseColor);
    const r = layoutText(spans, x, y, lineHeight, width, gui.book.overflow);
    this.words = r.words; this.scale = r.scale;
  }
  render() {
    if (!this.words.length) return;
    const f = this.words[0];
    push(f.x, f.y, this.scale); push(-f.x, -f.y);
    const hov = (w) => inRange(w.x, w.y, w.width, w.height);
    for (const w of this.words) {
      const clusterHovered = w.cluster ? w.cluster.some(hov) : hov(w);
      let style = w.style, color = style.color;
      if (clusterHovered) {
        if (w.onClick) color = this.book.linkHoverColor;
        if (w.tooltip) setTooltip([{ text: w.tooltip, color: 0xFFFFFF }]);
      }
      drawString(w.text, w.x, w.y, color, false, style);
      if (w.onClick) hit(w.x, w.y, w.width, w.height, () => w.onClick(), { link: true });
    }
    pop(); pop();
  }
}

// ------------------------------------------------------------------ book model
function prepareBook(b) {
  b.ns = b.id.split(':')[0];
  b.textColor = hex(b.textColor); b.headerColor = hex(b.headerColor); b.nameplateColor = hex(b.nameplateColor);
  b.linkColor = hex(b.linkColor); b.linkHoverColor = hex(b.linkHoverColor);
  b.progressBarColor = hex(b.progressBarColor); b.progressBarBackground = hex(b.progressBarBackground);
  const macros = { '$(list': '$(li', '/$': '$()', '<br>': '$(br)', '$(item)': '$(#b0b)', '$(thing)': '$(#490)' };
  Object.assign(macros, b.macros || {});
  b.macroList = Object.entries(macros);
  b.entryMap = new Map(b.entries.map(e => [e.id, e]));
  b.catMap = new Map(b.categories.map(c => [c.id, c]));
  b.categories.forEach(c => { c.children = []; c.entries = []; });
  b.categories.forEach(c => { if (c.parent && b.catMap.get(c.parent)) b.catMap.get(c.parent).children.push(c); });
  b.recipeMap = new Map();
  b.entries.forEach(e => {
    const cat = b.catMap.get(e.category); if (cat) { cat.entries.push(e); e.cat = cat; }
    e.searchNames = (e.stacks || []).map(([s]) => stackName(s).toLowerCase());
    for (const [s, page] of e.stacks || []) { const id = stackId(s); if (!b.recipeMap.has(id)) b.recipeMap.set(id, [e, Math.floor(page / 2)]); }
  });
  b.subtitleText = b.subtitle || L('patchouli.gui.lexicon.edition_str', ordinal(b.version));
}
function ordinal(n) { const s = ['th', 'st', 'nd', 'rd', 'th', 'th', 'th', 'th', 'th', 'th']; return (n % 100 >= 11 && n % 100 <= 13) ? n + 'th' : n + s[n % 10]; }
function entryState(book, e) {
  const d = bookData(book);
  if (!e.readByDefault && !d.viewed.includes(e.id)) return STATE.UNREAD;
  for (const p of e.pages) if (p.type === 'quest' && questDone(book, e, p)) return STATE.COMPLETED;
  return STATE.NEUTRAL;
}
function questDone(book, e, p) { return p.trigger ? true : bookData(book).quests.includes(e.id); }
function categoryState(book, c) {
  let best = STATE.NEUTRAL;
  for (const e of c.entries) { const s = entryState(book, e); if (s.ord < best.ord) best = s; }
  for (const ch of c.children) { const s = categoryState(book, ch); if (s.ord < best.ord) best = s; }
  return best;
}
const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
function compareEntries(book) {
  return (a, b) => {
    const sa = entryState(book, a), sb = entryState(book, b);
    if (sa !== sb) return sa.ord - sb.ord;
    if (!!a.priority !== !!b.priority) return a.priority ? -1 : 1;
    const s = (a.sortnum || 0) - (b.sortnum || 0);
    return s === 0 ? cmpStr(a.name, b.name) : s;
  };
}
const compareCats = (a, b) => (a.sortnum !== b.sortnum ? a.sortnum - b.sortnum : cmpStr(a.name, b.name));
function markViewed(book, e) {
  const d = bookData(book);
  if (!d.viewed.includes(e.id)) d.viewed.push(e.id);
  const i = d.history.indexOf(e.id);
  if (i !== 0) { if (i > 0) d.history.splice(i, 1); d.history.unshift(e.id); d.history.length = Math.min(d.history.length, ENTRIES_PER_PAGE); }
  persist();
}

// ------------------------------------------------------------------ icons for entries/categories
function drawIcon(icon, x, y) {
  if (!icon) return;
  if (icon.tex) { const im = tex(icon.tex); if (im) ctx.drawImage(im, x, y, 16, 16); return; }
  drawItem(icon, x, y, false);
}

// ------------------------------------------------------------------ buttons
function bookButton(gui, x, y, u, v, w, h, onPress, tooltip) {
  const hov = inRange(x, y, w, h);
  drawFromTexture(gui.book, x, y, u + (hov ? w : 0), v, w, h);
  if (hov && tooltip) setTooltip(tooltip);
  hit(x, y, w, h, () => { onPress(); flip(); });
  return hov;
}
function hoverAnim(key, hovered) {
  const m = hoverAnim.map || (hoverAnim.map = new Map());
  let t = m.get(key) || 0;
  t = hovered ? Math.min(5, t + frameDeltaTicks) : Math.max(0, t - frameDeltaTicks);
  m.set(key, t);
  return t;
}
function entryButton(gui, x, y, entry, onPress) {
  const w = PAGE_WIDTH, h = 10;
  const hov = inRange(x, y, w, h);
  const frac = hoverAnim('e:' + entry.id + ':' + x + ':' + y, hov) / 5;
  fill(x, y, x + Math.trunc(w * frac * 2) / 2, y + h, 0x22000000);
  push(x + 1, y + 1, 0.5); drawIcon(entry.icon, 0, 0); pop();
  let style = BASE_STYLE;
  if (entry.priority) style = styleWith(style, { italic: true });
  let color = entry.color ? hex(entry.color) : gui.book.textColor, alpha = 1;
  if (entry.secret) alpha = 0xAA / 255;
  drawString(entry.name, x + 12, y, color, false, style, alpha);
  drawMarking(gui.book, x + w - 5, y + 1, entryState(gui.book, entry));
  hit(x, y, w, h, () => { onPress(entry); });
}
function categoryButton(gui, x, y, icon, name, onPress, category) {
  const hov = inRange(x, y, 20, 20);
  const time = hoverAnim('c:' + name + x + ':' + y, hov);
  const transparency = 0.5 - (time / 5) * 0.5;
  drawIcon(icon, x + 2, y + 2);
  ctx.globalAlpha = transparency; drawFromTexture(gui.book, x, y, x, y, 20, 20); ctx.globalAlpha = 1;
  if (category) drawMarking(gui.book, x, y, categoryState(gui.book, category));
  if (hov) setTooltip([{ text: name, color: 0xFFFFFF }]);
  hit(x, y, 20, 20, () => { onPress(); flip(); });
}
// vanilla Button (widgets.png)
function vanillaButton(x, y, w, h, label, onPress) {
  const hov = inRange(x, y, w, h);
  const img = IMG['img/vanilla/widgets.png'];
  const v = hov ? 86 : 66;
  blit(img, x, y, 0, v, w / 2, h, 256, 256);
  blit(img, x + w / 2, y, 200 - w / 2, v, w / 2, h, 256, 256);
  drawString(label, x + w / 2 - Math.trunc(textWidth(label) / 2), y + (h - 8) / 2, hov ? 0xFFFFA0 : 0xFFFFFF, true);
  hit(x, y, w, h, () => { onPress(); });
}

// ------------------------------------------------------------------ GUIs
let current = null, guiStack = [];
function flip() { /* page-flip sound is not played on the web */ }

class GuiBook {
  constructor(book) { this.book = book; this.spread = 0; this.maxSpreads = 0; this.ticksInBook = 0; }
  display(gui, push) { openGui(gui, push); }
  canSeePageButton(left) { return left ? this.spread > 0 : this.spread + 1 < this.maxSpreads; }
  canSeeBackButton() { return guiStack.length > 0; }
  changePage(left) { if (this.canSeePageButton(left)) { this.spread += left ? -1 : 1; this.onPageChanged(); flip(); saveLocation(); } }
  onPageChanged() {}
  onFirstOpened() {}
  showsAddBookmark() { return false; }
  showsMarkRead() {
    if (this instanceof GuiBookIndex) return false;
    return this.book.entries.some(e => entryState(this.book, e) === STATE.UNREAD);
  }
  render() {
    drawFromTexture(this.book, 0, 0, 0, 0, FULL_WIDTH, FULL_HEIGHT);
    this.drawForeground();
    // back + arrows (GuiBook.init)
    if (this.canSeeBackButton() && !LAYOUT.stacked) this.drawBackButton(FULL_WIDTH / 2 - 9, FULL_HEIGHT - 5);
    if (this.canSeePageButton(true)) bookButton(this, -4, FULL_HEIGHT - 6, 272, 10, 18, 10, () => this.changePage(true), [{ text: L('patchouli.gui.lexicon.button.prev_page'), color: 0xFFFFFF }]);
    if (this.canSeePageButton(false)) bookButton(this, FULL_WIDTH - 14, FULL_HEIGHT - 6, 272, 0, 18, 10, () => this.changePage(false), [{ text: L('patchouli.gui.lexicon.button.next_page'), color: 0xFFFFFF }]);
    this.drawBookmarks();
  }
  drawBackButton(x, y) {
    bookButton(this, x, y, 308, 0, 18, 9, () => back(false), [{ text: L('patchouli.gui.lexicon.button.back'), color: 0xFFFFFF },
      { text: L('patchouli.gui.lexicon.button.back.info'), color: 0xAAAAAA }]);
  }
  drawBookmarks() {
    const d = bookData(this.book);
    let y = 0;
    for (const bm of d.bookmarks) {
      const entry = this.book.entryMap.get(bm.entry);
      const x = FULL_WIDTH, by = TOP_PADDING + y;
      const hov = inRange(x, by, 13, 10);
      drawFromTexture(this.book, x, by, 272 + (hov ? 13 : 0), 160, 13, 10);
      if (entry) {
        push(0, 0, 0.5);
        const px = x * 2 + (hov ? 6 : 2), py = by * 2 + 2;
        drawIcon(entry.icon, px, py);
        drawString(String(bm.spread + 1), px + 12, py + 10, 0xFFFFFF, true);
        pop();
        if (hov) setTooltip([{ text: entry.name, color: 0xFFFFFF }, { text: L('patchouli.gui.lexicon.remove_bookmark'), color: 0xAAAAAA }]);
      }
      hit(x, by, 13, 10, (ev) => {
        if (ev.shift) { d.bookmarks.splice(d.bookmarks.indexOf(bm), 1); persist(); }
        else if (entry) { this.display(new GuiBookEntry(this.book, entry, bm.spread), true); flip(); }
      });
      y += 12;
    }
    if (this.showsAddBookmark() && d.bookmarks.length <= MAX_BOOKMARKS) {
      const x = FULL_WIDTH, by = TOP_PADDING + y;
      const hov = inRange(x, by, 13, 10);
      drawFromTexture(this.book, x, by, 272 + (hov ? 13 : 0), 170, 13, 10);
      if (hov) setTooltip([{ text: L('patchouli.gui.lexicon.add_bookmark'), color: 0xFFFFFF }]);
      hit(x, by, 13, 10, () => this.bookmarkThis());
    }
    if (this.showsMarkRead()) {
      const x = FULL_WIDTH, by = TOP_PADDING + PAGE_HEIGHT - 10;
      const hov = inRange(x, by, 11, 11);
      drawFromTexture(this.book, x, by, 285, 160, 13, 10);
      drawFromTexture(this.book, x + 1, by + 0.5, 308, 31, 11, 11);
      if (hov) {
        drawFromTexture(this.book, x + 1, by + 0.5, 319, 31, 11, 11);
        setTooltip([{ text: L(guiStack.length ? 'patchouli.gui.lexicon.button.mark_category_read' : 'patchouli.gui.lexicon.button.mark_all_read'), color: 0xFFFFFF }]);
      }
      drawString('+', x + 1, by + 0.5, 0x00FF01, true);
      hit(x, by, 11, 11, () => this.markRead());
    }
  }
  markRead() {
    const d = bookData(this.book);
    const inCat = (e, cat) => { let c = e.cat; while (c) { if (c === cat) return true; c = this.book.catMap.get(c.parent); } return false; };
    for (const e of this.book.entries) {
      if (!guiStack.length || (this.category && inCat(e, this.category))) if (!d.viewed.includes(e.id)) d.viewed.push(e.id);
    }
    persist();
  }
  drawProgressBar(filter) {
    if (!this.book.showProgress || !this.book.advancementsEnabled) return;
    const barLeft = 19, barTop = FULL_HEIGHT - 36, barWidth = PAGE_WIDTH - 10, barHeight = 12;
    let total = 0, unlocked = 0;
    for (const e of this.book.entries) if (filter(e) && !e.secret) { total++; unlocked++; }
    const frac = unlocked / Math.max(1, total);
    const pw = Math.trunc((barWidth - 1) * frac);
    fill(barLeft, barTop, barLeft + barWidth, barTop + barHeight, 0xFF000000 | this.book.headerColor);
    const grad = (x1, y1, x2, y2, c) => {
      const r = (c >> 16) & 255, g = (c >> 8) & 255, b = c & 255;
      const dk = (Math.trunc(r * 0.7) << 16) | (Math.trunc(g * 0.7) << 8) | Math.trunc(b * 0.7);
      fillGradient(x1, y1, x2, y2, 0xFF000000 | c, 0xFF000000 | dk);
    };
    grad(barLeft + 1, barTop + 1, barLeft + barWidth - 1, barTop + barHeight - 1, this.book.progressBarBackground);
    grad(barLeft + 1, barTop + 1, barLeft + pw, barTop + barHeight - 1, this.book.progressBarColor);
    drawString(L('patchouli.gui.lexicon.progress_meter'), barLeft, barTop - 9, this.book.headerColor);
    if (inRange(barLeft, barTop, barWidth, barHeight)) setTooltip([{ text: L('patchouli.gui.lexicon.progress_tooltip', unlocked, total), color: 0xFFFFFF }]);
  }
  key() { return false; }
  char() { return false; }
}

class GuiBookLanding extends GuiBook {
  constructor(book) {
    super(book);
    this.text = new BookText(this, book.landing, LEFT_PAGE_X, TOP_PADDING + 25);
    this.cats = book.categories.filter(c => !c.parent).sort(compareCats);
    this.maxSpreads = 1;
  }
  drawForeground() {
    const b = this.book;
    this.text.render();
    const loaded = this.cats.length + 1;
    const topSep = TOP_PADDING + 12, bottomSep = topSep + 25 + 24 * (Math.floor((loaded - 1) / 4) + 1);
    // header
    drawFromTexture(b, -8, 12, 0, 180, 140, 31);
    drawString(b.name, 13, 16, b.nameplateColor);
    drawString(b.subtitleText, 24, 24, b.nameplateColor);
    drawCentered(L('patchouli.gui.lexicon.categories'), RIGHT_PAGE_X + PAGE_WIDTH / 2, TOP_PADDING, b.headerColor);
    drawSeparator(b, RIGHT_PAGE_X, topSep);
    if (loaded <= 16) drawSeparator(b, RIGHT_PAGE_X, bottomSep);
    this.drawProgressBar(() => true);
    this.cats.forEach((c, i) => {
      categoryButton(this, RIGHT_PAGE_X + 10 + (i % 4) * 24, TOP_PADDING + 25 + Math.floor(i / 4) * 24, c.icon, c.name,
        () => this.display(new GuiBookCategory(b, c), true), c);
    });
    const i = this.cats.length;
    categoryButton(this, RIGHT_PAGE_X + 10 + (i % 4) * 24, TOP_PADDING + 25 + Math.floor(i / 4) * 24, b.icon,
      L('patchouli.gui.lexicon.index'), () => this.display(new GuiBookIndex(b), true));
    // bottom-left buttons: resize, history
    const disableBar = !b.showProgress || !b.advancementsEnabled;
    const x = disableBar ? 25 : 20, y = FULL_HEIGHT - (disableBar ? 25 : 62);
    let pos = 0;
    bookButton(this, x + (pos++) * 15, y, 330, 9, 11, 11, () => cycleScale(), [{ text: L('patchouli.gui.lexicon.button.resize'), color: 0xFFFFFF },
      { text: SAVE.scale ? 'x' + SAVE.scale : L('patchouli.gui.lexicon.button.resize.size0'), color: 0xAAAAAA }]);
    bookButton(this, x + (pos++) * 15, y, 330, 31, 11, 11, () => this.display(new GuiBookHistory(b), true),
      [{ text: L('patchouli.gui.lexicon.button.history'), color: 0xFFFFFF }]);
  }
}

class GuiBookEntryList extends GuiBook {
  constructor(book, title, category = null) {
    super(book);
    this.category = category;
    this.subcats = category ? [...category.children].sort(compareCats) : [];
    this.title = title; this.query = '';
    this.text = new BookText(this, this.description(), LEFT_PAGE_X, TOP_PADDING + 22);
    this.all = this.getEntries();
    if (this.sorted()) this.all.sort(compareEntries(book));
    this.build();
  }
  sorted() { return true; }
  drawsProgress() { return true; }
  chapterTitle() { return L('patchouli.gui.lexicon.chapters'); }
  noEntryMessage() { return L('patchouli.gui.lexicon.no_entries'); }
  countsForProgress() { return true; }
  searchVisible() { return true; }
  build() {
    const q = this.query.toLowerCase();
    this.visible = this.all.filter(e => e.name.toLowerCase().includes(q) || e.searchNames.some(n => n.includes(q)));
    this.maxSpreads = 1;
    const count = this.visible.length - ENTRIES_IN_FIRST_PAGE;
    if (count > 0) this.maxSpreads += Math.ceil(count / (ENTRIES_PER_PAGE * 2));
    while (this.startIndex() > this.visible.length && this.spread > 0) this.spread--;
  }
  startIndex() { return this.spread === 0 ? 0 : ENTRIES_IN_FIRST_PAGE + ENTRIES_PER_PAGE * 2 * (this.spread - 1); }
  onPageChanged() { this.build(); }
  openEntry(e, ev) { displayOrBookmark(this, e, ev); }
  drawEntries(x, y, start, count) {
    for (let i = 0; i < count && i + start < this.visible.length; i++) {
      entryButton(this, x, y + i * 11, this.visible[start + i], (e) => this.openEntry(e, LAST_EVENT));
    }
  }
  drawForeground() {
    const b = this.book;
    if (this.spread === 0) {
      drawCentered(this.title, LEFT_PAGE_X + PAGE_WIDTH / 2, TOP_PADDING, b.headerColor);
      drawCentered(this.chapterTitle(), RIGHT_PAGE_X + PAGE_WIDTH / 2, TOP_PADDING, b.headerColor);
      drawSeparator(b, LEFT_PAGE_X, TOP_PADDING + 12);
      drawSeparator(b, RIGHT_PAGE_X, TOP_PADDING + 12);
      this.text.render();
      if (this.drawsProgress()) this.drawProgressBar((e) => this.countsForProgress(e));
      this.drawEntries(RIGHT_PAGE_X, TOP_PADDING + 20, 0, ENTRIES_IN_FIRST_PAGE);
      this.drawSubcategories();
    } else {
      const start = this.startIndex();
      this.drawEntries(LEFT_PAGE_X, TOP_PADDING, start, ENTRIES_PER_PAGE);
      this.drawEntries(RIGHT_PAGE_X, TOP_PADDING, start + ENTRIES_PER_PAGE, ENTRIES_PER_PAGE);
      if (this.spread % 2 === 1 && this.spread === this.maxSpreads - 1 && this.visible.length - start <= ENTRIES_PER_PAGE) drawPageFiller(b);
    }
    // search field (EditBox at 160,170, 90x12)
    if (this.searchVisible()) {
      if (this.query) {
        drawFromTexture(b, 152, 170, 140, 183, 99, 14);
        drawString(this.query + (SEARCH_FOCUSED && (totalTicks / 6 | 0) % 2 === 0 ? '_' : ''), 167, 171, b.textColor);
      } else if (SEARCH_FOCUSED) {
        drawFromTexture(b, 152, 170, 140, 183, 99, 14);
        if ((totalTicks / 6 | 0) % 2 === 0) drawString('_', 167, 171, b.textColor);
      }
      hit(152, 166, 106, 18, () => focusSearch(this), { search: true });
    }
    if (!this.visible.length) {
      if (this.query) {
        drawCentered(L('patchouli.gui.lexicon.no_results'), RIGHT_PAGE_X + PAGE_WIDTH / 2, 80, 0x333333);
        push(0, 0, 2); drawCentered(L('patchouli.gui.lexicon.sad'), RIGHT_PAGE_X / 2 + PAGE_WIDTH / 4, 47, 0x999999); pop();
      } else drawCentered(this.noEntryMessage(), RIGHT_PAGE_X + PAGE_WIDTH / 2, 80, 0x333333);
    }
  }
  drawSubcategories() {}
  char(c) {
    if (!this.searchVisible() || this.query.length >= 32) return false;
    this.query += c; this.build(); return true;
  }
  key(e) {
    if (!this.searchVisible()) return false;
    if (e.key === 'Enter') { if (this.visible.length === 1) { this.display(new GuiBookEntry(this.book, this.visible[0]), true); } return true; }
    if (e.key === 'Backspace') { this.query = this.query.slice(0, -1); this.build(); return true; }
    return false;
  }
}
class GuiBookCategory extends GuiBookEntryList {
  constructor(book, category) { super(book, category.name, category); }
  description() { return this.category.desc; }
  getEntries() { return [...this.category.entries]; }
  countsForProgress(e) { return e.cat === this.category; }
  chapterTitle() { return (!this.all.length && this.subcats.length) ? L('patchouli.gui.lexicon.categories') : L('patchouli.gui.lexicon.chapters'); }
  noEntryMessage() { return this.subcats.length ? '' : L('patchouli.gui.lexicon.no_entries'); }
  searchVisible() { return this.all.length > 0; }
  showsMarkRead() { return this.book.entries.some(e => entryState(this.book, e) === STATE.UNREAD); }
  drawSubcategories() {
    const cats = this.subcats; if (!cats.length) return;
    const rightFree = !this.all.length;
    const baseX = rightFree ? RIGHT_PAGE_X + 10 : LEFT_PAGE_X + 10;
    const baseY = rightFree ? TOP_PADDING + 25 : TOP_PADDING + PAGE_HEIGHT - Math.floor(cats.length / 4) * 20 - (!this.book.advancementsEnabled ? 38 : 64);
    cats.forEach((c, i) => categoryButton(this, baseX + (i % 4) * 24, baseY + Math.floor(i / 4) * (rightFree ? 24 : 20), c.icon, c.name,
      () => this.display(new GuiBookCategory(this.book, c), true), c));
    if (rightFree && cats.length <= 16) drawSeparator(this.book, RIGHT_PAGE_X, TOP_PADDING + 37 + 24 * (Math.floor((cats.length - 1) / 4) + 1));
  }
}
class GuiBookIndex extends GuiBookEntryList {
  constructor(book) { super(book, L('patchouli.gui.lexicon.index')); }
  description() { return L('patchouli.gui.lexicon.index.info'); }
  getEntries() { return [...this.book.entries]; }
}
class GuiBookHistory extends GuiBookEntryList {
  constructor(book) { super(book, L('patchouli.gui.lexicon.history')); }
  description() { return L('patchouli.gui.lexicon.history.info'); }
  drawsProgress() { return false; }
  sorted() { return false; }
  getEntries() { return bookData(this.book).history.map(id => this.book.entryMap.get(id)).filter(Boolean); }
}

class GuiBookEntry extends GuiBook {
  constructor(book, entry, spread = 0) {
    super(book);
    this.entry = entry; this.spread = spread;
    this.maxSpreads = Math.ceil(entry.pages.length / 2);
    this.pages = entry.pages.map((p, i) => makePage(this, p, i));
  }
  onFirstOpened() { markViewed(this.book, this.entry); }
  showsAddBookmark() {
    return !bookData(this.book).bookmarks.some(b => b.entry === this.entry.id && b.spread === this.spread);
  }
  showsMarkRead() { return false; }
  bookmarkThis() { bookData(this.book).bookmarks.push({ entry: this.entry.id, spread: this.spread }); persist(); }
  drawForeground() {
    const l = this.pages[this.spread * 2], r = this.pages[this.spread * 2 + 1];
    if (l) { push(LEFT_PAGE_X, TOP_PADDING); l.render(); pop(); }
    if (r) { push(RIGHT_PAGE_X, TOP_PADDING); r.render(); pop(); }
    if (!r) drawPageFiller(this.book);
  }
}
function displayOrBookmark(gui, entry, ev) {
  const d = bookData(gui.book);
  if (ev && ev.shift) {
    const i = d.bookmarks.findIndex(b => b.entry === entry.id && b.spread === 0);
    if (i >= 0) { d.bookmarks.splice(i, 1); persist(); return; }
    if (d.bookmarks.length < MAX_BOOKMARKS) { d.bookmarks.push({ entry: entry.id, spread: 0 }); persist(); return; }
  }
  gui.display(new GuiBookEntry(gui.book, entry), true); flip();
}

function openGui(gui, pushIt, fromBack = false) {
  if (pushIt && current) guiStack.push(current);
  current = gui;
  if (!fromBack) gui.onFirstOpened();
  blurSearch();
  saveLocation();
}
function back(sfx) {
  if (!guiStack.length) return;
  if (shiftDown || (LAST_EVENT && LAST_EVENT.shift)) { guiStack = []; openGui(new GuiBookLanding(BOOK), false, true); }
  else {
    const g = guiStack.pop();
    // rebuild list screens so read markers/order refresh
    if (g instanceof GuiBookEntryList) g.build();
    openGui(g, false, true);
  }
}
function saveLocation() {
  if (!current) return;
  const enc = (g) => g instanceof GuiBookEntry ? { t: 'e', id: g.entry.id, s: g.spread }
    : g instanceof GuiBookCategory ? { t: 'c', id: g.category.id, s: g.spread }
      : g instanceof GuiBookIndex ? { t: 'i', s: g.spread } : g instanceof GuiBookHistory ? { t: 'h', s: g.spread } : { t: 'l' };
  SAVE.last = { book: BOOK.id, stack: guiStack.map(enc), cur: enc(current) };
  persist();
}
function restoreLocation() {
  const last = SAVE.last;
  if (!last || last.book !== BOOK.id) return false;
  const dec = (o) => {
    let g;
    if (o.t === 'e') { const e = BOOK.entryMap.get(o.id); if (!e) return null; g = new GuiBookEntry(BOOK, e, o.s); }
    else if (o.t === 'c') { const c = BOOK.catMap.get(o.id); if (!c) return null; g = new GuiBookCategory(BOOK, c); g.spread = o.s; g.build(); }
    else if (o.t === 'i') { g = new GuiBookIndex(BOOK); g.spread = o.s; g.build(); }
    else if (o.t === 'h') { g = new GuiBookHistory(BOOK); g.spread = o.s; g.build(); }
    else g = new GuiBookLanding(BOOK);
    if (g.spread >= g.maxSpreads) g.spread = Math.max(0, g.maxSpreads - 1);
    return g;
  };
  const stack = last.stack.map(dec);
  const cur = dec(last.cur);
  if (!cur || stack.some(g => !g)) return false;
  guiStack = stack; current = cur;
  return true;
}

// ------------------------------------------------------------------ pages
function makePage(gui, p, num) {
  const page = { gui, p, num, book: gui.book, entry: gui.entry };
  const textAt = (y) => (page.textRender = new BookText(gui, p.text || '', 0, y));
  switch (p.type) {
    case 'text': case 'link': textAt(num === 0 ? 22 : (p.title ? 12 : -4)); page.render = () => renderTextPage(page); break;
    case 'spotlight': textAt(40); page.render = () => renderSpotlight(page); break;
    case 'crafting': case 'processing': setupRecipePage(page); break;
    case 'image': textAt(120); page.index = 0; page.render = () => renderImage(page); break;
    case 'entity': textAt(115); page.render = () => renderEntity(page); break;
    case 'multiblock': textAt(115); page.render = () => renderMultiblock(page); break;
    case 'relations': {
      page.entries = (p.entries || []).map(id => gui.book.entryMap.get(id)).filter(Boolean);
      textAt(22 + page.entries.length * 11); page.render = () => renderRelations(page); break;
    }
    case 'quest': textAt(22); page.render = () => renderQuest(page); break;
    case 'template': page.render = () => renderTemplate(page); page.comps = (p.c || []).map(c => c.t === 'text'
      ? Object.assign({}, c, { tr: new BookText(gui, c.text, c.x, c.y, c.w || PAGE_WIDTH, c.lh || TEXT_LINE_HEIGHT, c.color ? hex(c.color) : null) }) : c); break;
    case 'empty': page.render = () => { if (p.filler !== false) drawPageFiller(gui.book, 0, 0); }; break;
    default: textAt(-4); page.render = () => page.textRender.render();
  }
  return page;
}
function renderTextPage(page) {
  const { p, book, gui } = page;
  page.textRender.render();
  if (page.num === 0) {
    drawCentered(gui.entry.name, PAGE_WIDTH / 2, 0, book.headerColor);
    drawSeparator(book, 0, 12);
  } else if (p.title) drawCentered(p.title, PAGE_WIDTH / 2, 0, book.headerColor);
  if (p.type === 'link') vanillaButton(PAGE_WIDTH / 2 - 50, PAGE_HEIGHT - 35, 100, 20, p.link_text || 'Link', () => window.open(p.url, '_blank', 'noopener'));
}
function renderSpotlight(page) {
  const { p, book } = page;
  const w = 66, h = 26;
  blit(tex(book.crafting), PAGE_WIDTH / 2 - w / 2, 10, 0, 128 - h, w, h, 128, 256);
  const title = p.title || stackName((p.items || [])[0]);
  drawCentered(title, PAGE_WIDTH / 2, 0, book.headerColor);
  drawIngredient(p.items, PAGE_WIDTH / 2 - 8, 15);
  page.textRender.render();
}
function setupRecipePage(page) {
  const { p, gui } = page;
  const recs = p.r || [];
  page.recipes = recs;
  const name = (r) => stackName(r.out);
  page.title1 = p.title || (recs[0] ? name(recs[0]) : 'Air');
  page.title2 = '-';
  if (recs[1]) { page.title2 = p.title ? '' : name(recs[1]); if (page.title2 === page.title1) page.title2 = ''; }
  const rh = p.type === 'crafting' ? 78 : 45;
  page.rh = rh;
  const textHeight = 4 + rh * (recs[1] ? 2 : 1) - (page.title2 === '' ? 23 : 13);
  page.textRender = new BookText(gui, p.text || '', 0, textHeight);
  page.showText = textHeight + 10 < PAGE_HEIGHT;
  page.render = () => renderRecipePage(page);
}
function renderRecipePage(page) {
  const { p, book } = page;
  const x = PAGE_WIDTH / 2 - 49, y = 4;
  const drawOne = (r, ry, second) => {
    const title = second ? page.title2 : page.title1;
    if (p.type === 'crafting') {
      blit(tex(book.crafting), x - 2, ry - 2, 0, 0, 100, 62, 128, 256);
      if (!r.shaped) {
        blit(tex(book.crafting), x + 62, ry + 2, 0, 64, 11, 11, 128, 256);
        if (inRange(x + 62, ry + 2, 11, 11)) setTooltip([{ text: L('patchouli.gui.lexicon.shapeless'), color: 0xFFFFFF }]);
      }
      drawCentered(title, PAGE_WIDTH / 2, ry - 10, book.headerColor);
      drawItem(r.out, x + 79, ry + 22);
      const wrap = r.shaped ? r.w : 3;
      r.in.forEach((ing, i) => drawIngredient(ing, x + (i % wrap) * 19 + 3, ry + Math.floor(i / wrap) * 19 + 3));
      drawItem(r.toast, x + 79, ry + 41);
    } else {
      blit(tex(book.crafting), x, ry, 11, 71, 96, 24, 128, 256);
      drawCentered(title, PAGE_WIDTH / 2, ry - 10, book.headerColor);
      drawIngredient(r.in[0], x + 4, ry + 4);
      drawItem(r.toast, x + 40, ry + 4);
      drawItem(r.out, x + 76, ry + 4);
    }
  };
  if (page.recipes[0]) {
    drawOne(page.recipes[0], y, false);
    if (page.recipes[1]) drawOne(page.recipes[1], y + page.rh - (page.title2 === '' ? 10 : 0), true);
  }
  if (page.showText) page.textRender.render();
}
function renderImage(page) {
  const { p, book, gui } = page;
  const x = PAGE_WIDTH / 2 - 53, y = 7;
  const img = tex(p.images[page.index]);
  if (img) blit(img, x + 3, y + 3, 0, 0, 200, 200, 256, 256, 100, 100);
  if (p.border) drawFromTexture(book, x, y, 405, 149, 106, 106);
  if (p.title) drawCentered(p.title, PAGE_WIDTH / 2, -3, book.headerColor);
  if (p.images.length > 1 && p.border) {
    const xs = x + 83, ys = y + 92;
    fill(xs, ys, xs + 20, ys + 11, 0x44000000); fill(xs - 1, ys - 1, xs + 20, ys + 11, 0x44000000);
  }
  if (page.index > 0) bookButton(gui, 90, 100, 272, 27, 5, 7, () => page.index--, [{ text: L('patchouli.gui.lexicon.button.prev_page'), color: 0xFFFFFF }]);
  if (page.index < p.images.length - 1) bookButton(gui, 100, 100, 272, 20, 5, 7, () => page.index++, [{ text: L('patchouli.gui.lexicon.button.next_page'), color: 0xFFFFFF }]);
  page.textRender.render();
}
function renderEntity(page) {
  const { p, book } = page;
  const x = PAGE_WIDTH / 2 - 53, y = 7;
  drawFromTexture(book, x, y, 405, 149, 106, 106);
  drawCentered(p.name, PAGE_WIDTH / 2, 0, book.headerColor);
  ENTITY3D.draw(page, 58, 60);
  page.textRender.render();
}
function renderMultiblock(page) {
  const { p, book, gui } = page;
  const x = PAGE_WIDTH / 2 - 53, y = 7;
  drawFromTexture(book, x, y, 405, 149, 106, 106);
  drawCentered(p.name || '', PAGE_WIDTH / 2, 0, book.headerColor);
  MB3D.draw(page);
  if (p.visualize) {
    bookButton(gui, 12, 97, 308, 31, 11, 11, () => { page.paused = !page.paused; SAVE.clickedVisualize = true; persist(); },
      [{ text: L('patchouli.gui.lexicon.button.visualize'), color: 0xFFFFFF }, { text: L('patchouli.gui.lexicon.button.visualize.info'), color: 0xAAAAAA }]);
    if (!SAVE.clickedVisualize && totalTicks % 20 < 10) drawString('!', 12, 97, 0xFF3333, true);
  }
  page.textRender.render();
}
function renderRelations(page) {
  const { p, book, gui } = page;
  drawCentered(p.title || L('patchouli.gui.lexicon.relations'), PAGE_WIDTH / 2, 0, book.headerColor);
  drawSeparator(book, 0, 12);
  const list = [...page.entries].sort(compareEntries(book));
  list.forEach((e, i) => entryButton(gui, 0, 20 + i * 11, e, (en) => displayOrBookmark(gui, en, LAST_EVENT)));
  page.textRender.render();
}
function renderQuest(page) {
  const { p, book, gui } = page;
  page.textRender.render();
  drawCentered(p.title || L('patchouli.gui.lexicon.objective'), PAGE_WIDTH / 2, 0, book.headerColor);
  drawSeparator(book, 0, 12);
  if (p.trigger) {
    drawSeparator(book, 0, PAGE_HEIGHT - 25);
    const done = questDone(book, gui.entry, p);
    drawCentered(L(done ? 'patchouli.gui.lexicon.complete' : 'patchouli.gui.lexicon.incomplete'), PAGE_WIDTH / 2, PAGE_HEIGHT - 17, done ? 0x008b1a : book.headerColor);
  } else {
    const d = bookData(book), done = d.quests.includes(gui.entry.id);
    vanillaButton(PAGE_WIDTH / 2 - 50, PAGE_HEIGHT - 35, 100, 20, L(done ? 'patchouli.gui.lexicon.mark_incomplete' : 'patchouli.gui.lexicon.mark_complete'), () => {
      if (done) d.quests.splice(d.quests.indexOf(gui.entry.id), 1); else d.quests.push(gui.entry.id); persist();
    });
  }
}
function renderTemplate(page) {
  const { book } = page;
  for (const c of page.comps) {
    if (c.t === 'item') {
      if (c.framed) blit(tex(book.crafting), c.x - 5, c.y - 5, 20, 102, 26, 26, 128, 256);
      drawIngredient(c.items, c.x, c.y);
    } else if (c.t === 'text') c.tr.render();
    else if (c.t === 'header') {
      push(c.x, c.y, c.scale || 1);
      const col = c.color ? hex(c.color) : book.headerColor;
      if (c.centered !== false) drawCentered(c.text, 0, 0, col); else drawString(c.text, 0, 0, col);
      pop();
    } else if (c.t === 'image') {
      push(c.x, c.y, c.scale || 1); blit(tex(c.src), 0, 0, c.u, c.v, c.w, c.h, c.tw, c.th); pop();
    } else if (c.t === 'separator') drawSeparator(book, c.x, c.y);
  }
}

// ------------------------------------------------------------------ 3D (multiblocks, entities) via three.js
const MB3D = {
  renderer: null, scenes: new Map(), atlasTex: null,
  ensure() {
    if (this.renderer || !window.THREE) return !!this.renderer;
    try {
      this.canvas = document.createElement('canvas');
      this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, alpha: true, antialias: false, preserveDrawingBuffer: true });
      this.renderer.setClearColor(0x000000, 0);
      const img = IMG['blocks.png'];
      this.atlasTex = new THREE.Texture(img); this.atlasTex.magFilter = THREE.NearestFilter; this.atlasTex.minFilter = THREE.NearestFilter;
      this.atlasTex.generateMipmaps = false; this.atlasTex.flipY = false; this.atlasTex.needsUpdate = true;
    } catch (e) { this.renderer = null; this.failed = true; }
    return !!this.renderer;
  },
  build(mb) {
    const B = DATA.blocks, cols = B.cols, rows = B.rows;
    const layers = mb.pattern, sizeY = layers.length, sizeZ = layers[0].length, sizeX = Math.max(...layers[0].map(r => r.length));
    const pos = [], uv = [], col = [], idx = [];
    const shade = { up: 1, down: 0.5, north: 0.8, south: 0.8, east: 0.6, west: 0.6 };
    let n = 0;
    const tagFallback = (s) => s;
    layers.forEach((layer, li) => {
      const yy = sizeY - 1 - li;
      layer.forEach((row, z) => {
        [...row].forEach((ch, x) => {
          if (ch === ' ' || ch === '_') return;
          const state = mb.mapping[ch];
          if (!state) return;
          const quads = B.blocks[state] || B.blocks[state.split('[')[0]] || B.blocks[tagFallback(state)];
          if (!quads) return;
          for (const q of quads) {
            const t = q.t, tu = (t % cols), tv = Math.floor(t / cols);
            const c = q.c || [1, 1, 1], sh = shade[q.f] || 1;
            for (let k = 0; k < 4; k++) {
              pos.push(x + q.v[k * 3] / 16, yy + q.v[k * 3 + 1] / 16, z + q.v[k * 3 + 2] / 16);
              uv.push((tu + q.uv[k * 2] / 16) / cols, (tv + q.uv[k * 2 + 1] / 16) / rows);
              col.push(c[0] * sh, c[1] * sh, c[2] * sh);
            }
            idx.push(n, n + 1, n + 2, n, n + 2, n + 3, n + 2, n + 1, n, n + 3, n + 2, n);
            n += 4;
          }
        });
      });
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    g.setIndex(idx);
    const mat = new THREE.MeshBasicMaterial({ map: this.atlasTex, vertexColors: true, alphaTest: 0.1, side: THREE.FrontSide, transparent: false });
    const mesh = new THREE.Mesh(g, mat);
    const scene = new THREE.Scene();
    const pivot = new THREE.Group(); scene.add(pivot);
    mesh.position.set(-sizeX / 2, -sizeY / 2, -sizeZ / 2);
    pivot.add(mesh);
    const diag = Math.sqrt(sizeX * sizeX + sizeZ * sizeZ);
    const scale = Math.min(90 / diag, 90 / sizeY);
    return { scene, pivot, scale, sizeX, sizeY, sizeZ };
  },
  draw(page) {
    const mb = page.p.mb;
    if (!mb || !this.ensure()) return;
    let s = this.scenes.get(mb);
    if (!s) { s = this.build(mb); this.scenes.set(mb, s); }
    if (!page.paused) page.rot = (page.rot || 0) + frameDeltaTicks * 0.5;
    const px = Math.max(1, Math.round(106 * LAYOUT.scale * RC.s));
    if (this.canvas.width !== px) this.renderer.setSize(px, px, false);
    const cam = new THREE.OrthographicCamera(-53, 53, 53, -53, -1000, 1000);
    cam.position.set(0, 0, 100); cam.lookAt(0, 0, 0);
    s.pivot.scale.setScalar(s.scale);
    s.pivot.rotation.set(0, 0, 0);
    s.pivot.rotateX(THREE.MathUtils.degToRad(30));
    s.pivot.rotateY(THREE.MathUtils.degToRad(-((page.rot || 0) + 45)));
    // Patchouli centres the structure at (58,60): the frame centre is (58,60) too
    this.renderer.render(s.scene, cam);
    ctx.drawImage(this.canvas, 5, 7, 106, 106);
  },
};
const ENTITY3D = {
  draw(page, cx, cy) {
    const ent = (page.p.entity || '').split('{')[0];
    if (window.ENTITY_RENDER && window.ENTITY_RENDER.draw(page, ent, cx, cy)) return;
    // fallback: the mob's spawn egg, drawn large
    const [ns, path] = ent.split(':');
    const egg = ns + ':' + path + '_spawn_egg';
    if (ICONS.has(egg)) { push(cx - 16, cy - 16, 2); drawItem(egg, 0, 0, false); pop(); }
  },
};


// ------------------------------------------------------------------ entity models (Goety's Java models, parsed at build time)
const GUI_LIGHTS = [[-0.222519, 0.17149859, 0.95972576], [-0.21501213, 0.97182532, 0.09656778]];
window.ENTITY_RENDER = {
  cache: new Map(),
  // ModelPart.Cube polygons (same layout as vanilla), in part-local pixel units
  cubeGeometry(c, tw, th, pos, uv, nrm, idx) {
    let [u, v, x, y, z, w, h, d, gx, gy, gz, mirror] = c;
    let x1 = x - gx, y1 = y - gy, z1 = z - gz, x2 = x + w + gx, y2 = y + h + gy, z2 = z + d + gz;
    if (mirror) [x1, x2] = [x2, x1];
    const V = [[x1, y1, z1], [x2, y1, z1], [x2, y2, z1], [x1, y2, z1], [x1, y1, z2], [x2, y1, z2], [x2, y2, z2], [x1, y2, z2]];
    const f7 = u, f8 = u + d, f9 = u + d + w, f10 = u + d + w + w, f11 = u + d + w + d, f12 = u + d + w + d + w;
    const f13 = v, f14 = v + d, f15 = v + d + h;
    const polys = [
      [[5, 4, 0, 1], [f8, f13, f9, f14], [0, -1, 0]], [[2, 3, 7, 6], [f9, f14, f10, f13], [0, 1, 0]],
      [[0, 4, 7, 3], [f7, f14, f8, f15], [-1, 0, 0]], [[1, 0, 3, 2], [f8, f14, f9, f15], [0, 0, -1]],
      [[5, 1, 2, 6], [f9, f14, f11, f15], [1, 0, 0]], [[4, 5, 6, 7], [f11, f14, f12, f15], [0, 0, 1]]];
    for (const [vi0, [a, b, cc, dd], n] of polys) {
      let vi = vi0, uvs = [[cc, b], [a, b], [a, dd], [cc, dd]];
      let nn = n;
      if (mirror) { vi = [...vi].reverse(); uvs = [...uvs].reverse(); nn = [-n[0], n[1], n[2]]; }
      const base = pos.length / 3;
      for (let k = 0; k < 4; k++) {
        pos.push(...V[vi[k]]);
        uv.push(uvs[k][0] / tw, uvs[k][1] / th);
        nrm.push(...(mirror && (n[0] !== 0) ? nn : n));
      }
      idx.push(base, base + 1, base + 2, base, base + 2, base + 3);
    }
  },
  build(def) {
    const r = def.region, img = IMG['entities.png'];
    const c = document.createElement('canvas'); c.width = r[2]; c.height = r[3];
    c.getContext('2d').drawImage(img, r[0], r[1], r[2], r[3], 0, 0, r[2], r[3]);
    const tex = new THREE.CanvasTexture(c);
    tex.magFilter = THREE.NearestFilter; tex.minFilter = THREE.NearestFilter; tex.generateMipmaps = false; tex.flipY = false;
    const solid = new THREE.MeshLambertMaterial({ map: tex, alphaTest: 0.1, side: THREE.DoubleSide });
    const glass = new THREE.MeshLambertMaterial({ map: tex, transparent: true, depthWrite: false, side: THREE.DoubleSide });
    const mk = (p) => {
      const g = new THREE.Group();
      g.position.set(p.p[0], p.p[1], p.p[2]);
      g.rotation.set(p.p[3], p.p[4], p.p[5], 'ZYX');
      if (p.h) g.visible = false;
      if (p.c && p.c.length) {
        const pos = [], uv = [], nrm = [], idx = [];
        p.c.forEach(cb => this.cubeGeometry(cb, def.tw, def.th, pos, uv, nrm, idx));
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
        geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
        geo.setAttribute('normal', new THREE.Float32BufferAttribute(nrm, 3));
        geo.setIndex(idx);
        const mesh = new THREE.Mesh(geo, p.t ? glass : solid);
        if (p.t) mesh.renderOrder = 1;
        g.add(mesh);
      }
      (p.k || []).forEach(ch => g.add(mk(ch)));
      return g;
    };
    const model = mk(def.root);
    const holder = new THREE.Group(); holder.matrixAutoUpdate = false; holder.add(model);
    const scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 0.4));
    GUI_LIGHTS.forEach(l => { const d = new THREE.DirectionalLight(0xffffff, 0.6); d.position.set(l[0], -l[1], l[2]); scene.add(d); });
    // GUI space is y-down; flip it here so three.js keeps correct winding and lighting
    const flipY = new THREE.Group(); flipY.scale.set(1, -1, 1); flipY.add(holder);
    scene.add(flipY);
    return { scene, holder, def };
  },
  draw(page, ent, cx, cy) {
    const def = DATA.entities && DATA.entities[ent];
    if (!def || !MB3D.ensure()) return false;
    let e = this.cache.get(ent);
    if (!e) { e = this.build(def); this.cache.set(ent, e); }
    const p = page.p;
    const scale = p.scale == null ? 1 : p.scale;
    const [bw, bh] = def.bb;
    const size = Math.max(1, Math.max(bw, bh));
    const renderScale = 100 / size * 0.8 * scale;
    const offset = Math.max(bh, size) * 0.5 + (p.offset || 0);
    const rotation = p.rotate === false ? (p.default_rotation == null ? -45 : p.default_rotation) : totalTicks;
    const M = new THREE.Matrix4(), T = new THREE.Matrix4();
    const D = THREE.MathUtils.degToRad;
    M.makeTranslation(cx, cy, 50);
    M.multiply(T.makeScale(renderScale, renderScale, renderScale));
    M.multiply(T.makeTranslation(0, offset, 0));
    M.multiply(T.makeRotationZ(Math.PI));
    M.multiply(T.makeRotationY(D(rotation)));
    M.multiply(T.makeRotationY(Math.PI));                 // LivingEntityRenderer: 180 - bodyRot
    M.multiply(T.makeScale(-1, -1, 1));
    const s = def.scale || [1, 1, 1];
    M.multiply(T.makeScale(s[0], s[1], s[2]));
    M.multiply(T.makeTranslation(0, -1.501, 0));
    M.multiply(T.makeScale(1 / 16, 1 / 16, 1 / 16));
    e.holder.matrix.copy(M); e.holder.matrixWorldNeedsUpdate = true;
    // render a region a little larger than the frame: mobs may overhang it, as in game
    const X0 = -10, Y0 = -12, W = 136, H = 176;
    const ppu = LAYOUT.scale * RC.s;
    const pw = Math.max(1, Math.round(W * ppu)), ph = Math.max(1, Math.round(H * ppu));
    const r = MB3D.renderer;
    if (MB3D.canvas.width !== pw || MB3D.canvas.height !== ph) r.setSize(pw, ph, false);
    const cam = new THREE.OrthographicCamera(X0, X0 + W, -Y0, -(Y0 + H), -2000, 2000);
    cam.position.set(0, 0, 0); cam.updateMatrixWorld();
    r.render(e.scene, cam);
    ctx.drawImage(MB3D.canvas, X0, Y0, W, H);
    return true;
  },
};

// ------------------------------------------------------------------ tooltip (GuiGraphics.renderTooltipInternal)
function drawTooltip(lines, mxs, mys) {
  if (!lines || !lines.length) return;
  let w = 0, h = lines.length === 1 ? -2 : 0;
  for (const l of lines) { w = Math.max(w, textWidth(l.text, styleWith(BASE_STYLE, { italic: !!l.italic }))); h += 10; }
  let x = mxs + 12, y = mys - 12;
  if (x + w > LAYOUT.guiW) x -= 28 + w;
  if (y + h + 6 > LAYOUT.guiH) y = LAYOUT.guiH - h - 6;
  if (x < 4) x = 4; if (y < 4) y = 4;
  const bg = 0xF0100010, b0 = 0x505000FF, b1 = 0x5028007F;
  fill(x - 3, y - 4, x + w + 3, y - 3, bg);
  fill(x - 3, y + h + 3, x + w + 3, y + h + 4, bg);
  fill(x - 3, y - 3, x + w + 3, y + h + 3, bg);
  fill(x - 4, y - 3, x - 3, y + h + 3, bg);
  fill(x + w + 3, y - 3, x + w + 4, y + h + 3, bg);
  fillGradient(x - 3, y - 3 + 1, x - 3 + 1, y + h + 3 - 1, b0, b1);
  fillGradient(x + w + 2, y - 3 + 1, x + w + 3, y + h + 3 - 1, b0, b1);
  fill(x - 3, y - 3, x + w + 3, y - 3 + 1, b0);
  fill(x - 3, y + h + 2, x + w + 3, y + h + 3, b1);
  let yy = y;
  lines.forEach((l, i) => {
    drawString(l.text, x, yy, l.color == null ? 0xFFFFFF : l.color, true, styleWith(BASE_STYLE, { italic: !!l.italic }));
    yy += 10 + (i === 0 ? 2 : 0);
  });
}

// ------------------------------------------------------------------ layout & frame loop
const LAYOUT = { scale: 4, stacked: false, guiW: 0, guiH: 0, bookLeft: 0, bookTop: 0, gap: 8 };
const STACK_LEFT = [-30, 136], STACK_RIGHT = [136, 290];
function computeLayout() {
  const dpr = window.devicePixelRatio || 1;
  const cw = canvas.clientWidth, ch = canvas.clientHeight;
  const W = Math.round(cw * dpr), H = Math.round(ch * dpr);
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const fit = (gw, gh) => { const s = Math.min(W / gw, H / gh); return s >= 2 ? Math.floor(s) : Math.max(0.5, s); };
  const side = fit(FULL_WIDTH + 64, FULL_HEIGHT + 24);          // tabs + bookmarks + arrows
  const stack = fit(136 + 30 + 22, 2 * (FULL_HEIGHT + 6) + LAYOUT.gap + 18);
  LAYOUT.stacked = stack > side * 1.15;
  let scale = LAYOUT.stacked ? stack : side;
  if (SAVE.scale && SAVE.scale < scale) scale = SAVE.scale;
  LAYOUT.maxScale = LAYOUT.stacked ? stack : side;
  LAYOUT.scale = scale;
  LAYOUT.guiW = W / scale; LAYOUT.guiH = H / scale;
  if (!LAYOUT.stacked) {
    LAYOUT.bookLeft = Math.floor(LAYOUT.guiW / 2 - FULL_WIDTH / 2 + 8);
    LAYOUT.bookTop = Math.floor(LAYOUT.guiH / 2 - FULL_HEIGHT / 2);
  } else {
    const totalH = 2 * FULL_HEIGHT + LAYOUT.gap + 14;
    LAYOUT.bookLeft = Math.floor(LAYOUT.guiW / 2 - 68 + 8);
    LAYOUT.bookTop = Math.max(14, Math.floor(LAYOUT.guiH / 2 - totalH / 2) + 10);
  }
}
function cycleScale() {
  const max = Math.floor(LAYOUT.maxScale);
  if (!SAVE.scale || SAVE.scale >= max) SAVE.scale = SAVE.scale ? 0 : Math.max(2, max - 1);
  else SAVE.scale = Math.max(2, SAVE.scale + 1);
  if (SAVE.scale >= max) SAVE.scale = 0;
  persist();
}
// screen (gui px) <-> book coords
function screenToBook(sx, sy) {
  if (!LAYOUT.stacked) return { x: sx - LAYOUT.bookLeft, y: sy - LAYOUT.bookTop };
  const topH = FULL_HEIGHT + LAYOUT.gap / 2;
  if (sy - LAYOUT.bookTop < topH) return { x: sx - LAYOUT.bookLeft, y: sy - LAYOUT.bookTop };
  return { x: sx - LAYOUT.bookLeft + 136, y: sy - LAYOUT.bookTop - FULL_HEIGHT - LAYOUT.gap };
}
let frameDeltaTicks = 0, MOUSE_SCREEN = { x: -999, y: -999 }, LAST_EVENT = null, STICKY = null;
function frame(now) {
  const dt = Math.min(250, now - lastTime); lastTime = now;
  frameDeltaTicks = dt / 50;
  ticks += frameDeltaTicks; totalTicks += frameDeltaTicks;
  computeLayout();
  const s = LAYOUT.scale;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = '#100c0e'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(s, 0, 0, s, 0, 0);
  // Minecraft's translucent screen background
  const grd = ctx.createLinearGradient(0, 0, 0, LAYOUT.guiH);
  grd.addColorStop(0, 'rgba(16,16,16,0.75)'); grd.addColorStop(1, 'rgba(16,16,16,0.82)');
  ctx.fillStyle = grd; ctx.fillRect(0, 0, LAYOUT.guiW, LAYOUT.guiH);
  RC.hits = []; RC.tooltip = null; RC.tooltipStack = null; RC.targetPage = null;
  const mpos = STICKY || MOUSE_SCREEN;
  RC.mouse = screenToBook(mpos.x, mpos.y);
  RC.ox = 0; RC.oy = 0; RC.s = 1; RC.stack = [];
  if (current) {
    if (!LAYOUT.stacked) {
      push(LAYOUT.bookLeft, LAYOUT.bookTop); RC.ox = 0; RC.oy = 0;
      current.render();
      pop();
    } else {
      for (let pass = 0; pass < 2; pass++) {
        RC.recordHits = true;
        ctx.save();
        const [x0, x1] = pass ? STACK_RIGHT : STACK_LEFT;
        const dx = pass ? -136 : 0, dy = pass ? FULL_HEIGHT + LAYOUT.gap : 0;
        ctx.translate(LAYOUT.bookLeft + dx, LAYOUT.bookTop + dy);
        ctx.beginPath(); ctx.rect(x0, -16, x1 - x0, FULL_HEIGHT + 30); ctx.clip();
        RC.ox = 0; RC.oy = 0; RC.s = 1; RC.stack = [];
        // only take hits from the half the region belongs to
        const before = RC.hits.length;
        const savedTip = [RC.tooltip, RC.tooltipStack, RC.targetPage];
        current.render();
        RC.hits = RC.hits.filter((h, i) => i < before || (pass ? h.x + h.w > 136 : h.x < 136));
        if (pass === 1 && RC.mouse.x < 136) [RC.tooltip, RC.tooltipStack, RC.targetPage] = savedTip;
        ctx.restore();
      }
      if (current.canSeeBackButton()) {
        // back button sits on the spine; in stacked mode it goes under the second page
        ctx.save(); ctx.translate(LAYOUT.bookLeft, LAYOUT.bookTop + 2 * FULL_HEIGHT + LAYOUT.gap);
        const m = RC.mouse; RC.mouse = { x: mpos.x - LAYOUT.bookLeft, y: mpos.y - (LAYOUT.bookTop + 2 * FULL_HEIGHT + LAYOUT.gap) };
        RC.ox = 0; RC.oy = 0;
        const before = RC.hits.length;
        current.drawBackButton(59, -5);
        for (let i = before; i < RC.hits.length; i++) { RC.hits[i].screen = true; RC.hits[i].x += LAYOUT.bookLeft; RC.hits[i].y += LAYOUT.bookTop + 2 * FULL_HEIGHT + LAYOUT.gap; }
        RC.mouse = m; ctx.restore();
      }
    }
  }
  // tooltip
  ctx.setTransform(s, 0, 0, s, 0, 0);
  const tipLines = RC.tooltipStack ? itemTooltipLines(RC.tooltipStack, current) : RC.tooltip;
  if (tipLines) drawTooltip(tipLines, mpos.x, mpos.y);
  requestAnimationFrame(frame);
}
function switchBook(b) {
  if (b === BOOK) return;
  BOOK = b; guiStack = [];
  if (!restoreLocation()) { current = null; openGui(new GuiBookLanding(b), false); }
  SAVE.lastBook = b.id; persist();
  updateSidebar();
}

// ------------------------------------------------------------------ sidebar (HTML): every book, grouped by mod, searchable
const SIDEBAR = { el: null, list: null, search: null, rows: new Map() };
// "@mod" words filter by mod (name or id), other words by book name
function bookMatches(b, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const modKeys = b.mod ? [b.mod.name.toLowerCase().replace(/\s+/g, ''), b.mod.id.toLowerCase()] : [];
  return words.every(w => w.startsWith('@')
    ? modKeys.some(k => k.includes(w.slice(1)))
    : b.name.toLowerCase().includes(w));
}
function iconCanvas(stack) {
  const c = document.createElement('canvas'); c.width = 32; c.height = 32;
  const icon = ICONS.get(stackId(stack));
  if (icon) {
    const g = c.getContext('2d'); g.imageSmoothingEnabled = false;
    const [atlas, sx, sy, S] = icon; g.drawImage(atlas, sx, sy, S, S, 0, 0, 32, 32);
  }
  c.setAttribute('aria-hidden', 'true');
  return c;
}
function renderBookList() {
  const q = SIDEBAR.search.value.trim();
  const list = SIDEBAR.list; list.textContent = ''; SIDEBAR.rows.clear();
  let lastMod;
  for (const b of BOOKS) {
    if (!bookMatches(b, q)) continue;
    if (b.mod !== lastMod) {
      lastMod = b.mod;
      const h = document.createElement('li'); h.className = 'mod';
      h.textContent = b.mod ? b.mod.name + ' ' : 'Other';
      if (b.mod && b.mod.authors) { const s = document.createElement('small'); s.textContent = 'by ' + b.mod.authors.join(', '); h.append(s); }
      list.append(h);
    }
    const li = document.createElement('li'), btn = document.createElement('button');
    btn.type = 'button'; btn.append(iconCanvas(b.icon), b.name);
    btn.addEventListener('click', () => { switchBook(b); closeDrawer(); canvas.focus({ preventScroll: true }); });
    li.append(btn); list.append(li); SIDEBAR.rows.set(b, btn);
  }
  if (!SIDEBAR.rows.size) { const li = document.createElement('li'); li.className = 'empty'; li.textContent = 'No books found'; list.append(li); }
  markActiveBook();
}
function markActiveBook() { for (const [b, btn] of SIDEBAR.rows) btn.setAttribute('aria-current', String(b === BOOK)); }
function updateSidebar() { if (SIDEBAR.el) markActiveBook(); }
function openDrawer() { document.body.classList.add('drawer-open'); }
function closeDrawer() { document.body.classList.remove('drawer-open'); }
function setupSidebar() {
  SIDEBAR.el = document.getElementById('sidebar');
  SIDEBAR.list = document.getElementById('book-list');
  SIDEBAR.search = document.getElementById('book-search');
  if (!SIDEBAR.el) return;
  const firstMod = BOOKS.find(b => b.mod);
  const hint = SIDEBAR.el.querySelector('.hint b');
  if (hint && firstMod) hint.textContent = '@' + firstMod.mod.id;
  SIDEBAR.search.addEventListener('input', renderBookList);
  SIDEBAR.search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && SIDEBAR.rows.size) SIDEBAR.rows.values().next().value.click();
    if (e.key === 'Escape') { SIDEBAR.search.value = ''; renderBookList(); closeDrawer(); canvas.focus({ preventScroll: true }); }
  });
  document.getElementById('sidebar-toggle').addEventListener('click', () => { openDrawer(); SIDEBAR.search.focus({ preventScroll: true }); });
  document.getElementById('sidebar-close').addEventListener('click', closeDrawer);
  canvas.addEventListener('pointerdown', closeDrawer);
  SIDEBAR.el.hidden = false;
  document.getElementById('sidebar-toggle').hidden = false;
  renderBookList();
  updateSidebar();
}

// ------------------------------------------------------------------ input
function toGui(e) {
  const r = canvas.getBoundingClientRect();
  const dpr = canvas.width / r.width;
  return { x: (e.clientX - r.left) * dpr / LAYOUT.scale, y: (e.clientY - r.top) * dpr / LAYOUT.scale };
}
function findHit(sx, sy) {
  const b = screenToBook(sx, sy);
  for (let i = RC.hits.length - 1; i >= 0; i--) {
    const h = RC.hits[i];
    const px = h.screen ? sx : b.x, py = h.screen ? sy : b.y;
    if (px > h.x && py > h.y && px <= h.x + h.w && py <= h.y + h.h) return h;
  }
  return null;
}
function clickAt(sx, sy, ev) {
  LAST_EVENT = ev;
  const h = findHit(sx, sy);
  // GuiBook: shift-click on a hovered item stack jumps to its recipe page
  if (RC.targetPage && (ev.shift || ev.itemTap)) {
    const [entry, spread] = RC.targetPage;
    current.display(new GuiBookEntry(current.book, entry, spread), true); flip();
    STICKY = null;
    return true;
  }
  if (h) { h.fn(ev); if (!h.search) blurSearch(); return true; }
  blurSearch();
  return false;
}
let pressTimer = null, pressStart = null;
function setupInput() {
  canvas.addEventListener('pointermove', (e) => {
    if (e.pointerType === 'mouse') { MOUSE_SCREEN = toGui(e); STICKY = null; }
  });
  canvas.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') MOUSE_SCREEN = { x: -999, y: -999 }; });
  canvas.addEventListener('contextmenu', (e) => e.preventDefault());
  canvas.addEventListener('pointerdown', (e) => {
    const p = toGui(e);
    if (e.pointerType === 'mouse') {
      MOUSE_SCREEN = p;
      if (e.button === 2) { back(true); return; }
      if (e.button === 3) { current && current.changePage(true); return; }
      if (e.button === 4) { current && current.changePage(false); return; }
      if (e.button === 0) clickAt(p.x, p.y, { shift: e.shiftKey || shiftDown });
      return;
    }
    // touch / pen: long press = shift-click
    pressStart = { p, t: performance.now(), long: false };
    clearTimeout(pressTimer);
    pressTimer = setTimeout(() => { pressStart.long = true; STICKY = p; clickAt(p.x, p.y, { shift: true }); }, 550);
  });
  canvas.addEventListener('pointerup', (e) => {
    if (e.pointerType === 'mouse' || !pressStart) return;
    clearTimeout(pressTimer);
    if (pressStart.long) { pressStart = null; return; }
    const p = pressStart.p; pressStart = null;
    // first tap on an item shows its tooltip; tapping the same item again opens its recipe
    const prev = STICKY;
    STICKY = p;
    RC.mouse = screenToBook(p.x, p.y);
    const h = findHit(p.x, p.y);
    const sameSpot = prev && Math.abs(prev.x - p.x) < 10 && Math.abs(prev.y - p.y) < 10;
    if (h) { STICKY = null; clickAt(p.x, p.y, { shift: false }); return; }
    if (sameSpot && RC.targetPage) { clickAt(p.x, p.y, { shift: false, itemTap: true }); STICKY = null; return; }
    // otherwise keep the sticky hover so the tooltip shows
  });
  canvas.addEventListener('pointercancel', () => { clearTimeout(pressTimer); pressStart = null; });
  let wheelAcc = 0;
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    wheelAcc += e.deltaY;
    if (Math.abs(wheelAcc) >= 40) { current && current.changePage(wheelAcc < 0); wheelAcc = 0; }
  }, { passive: false });
  // horizontal swipe on touch turns pages
  let sw = null;
  canvas.addEventListener('touchstart', (e) => { if (e.touches.length === 1) sw = { x: e.touches[0].clientX, y: e.touches[0].clientY }; }, { passive: true });
  canvas.addEventListener('touchend', (e) => {
    if (!sw || !e.changedTouches.length) return;
    const dx = e.changedTouches[0].clientX - sw.x, dy = e.changedTouches[0].clientY - sw.y;
    if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) { clearTimeout(pressTimer); pressStart = null; STICKY = null; current && current.changePage(dx > 0); }
    sw = null;
  }, { passive: true });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Shift') shiftDown = true;
    if (!current || e.target === SEARCH_INPUT || creditsOpen() || (e.target.closest && e.target.closest('#sidebar'))) return;
    if (current.key(e)) { e.preventDefault(); return; }
    if (e.key === 'Backspace' || e.key === 'Escape') { back(true); e.preventDefault(); }
    else if (e.key === 'ArrowLeft' || e.key === 'PageUp') current.changePage(true);
    else if (e.key === 'ArrowRight' || e.key === 'PageDown') current.changePage(false);
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && current.char(e.key)) e.preventDefault();
  });
  window.addEventListener('keyup', (e) => { if (e.key === 'Shift') shiftDown = false; });
  window.addEventListener('blur', () => { shiftDown = false; });
  SEARCH_INPUT.addEventListener('input', () => {
    if (current instanceof GuiBookEntryList) { current.query = SEARCH_INPUT.value.slice(0, 32); current.build(); }
  });
  SEARCH_INPUT.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && current instanceof GuiBookEntryList) { current.key(e); SEARCH_INPUT.blur(); }
    if (e.key === 'Escape') SEARCH_INPUT.blur();
  });
  SEARCH_INPUT.addEventListener('blur', () => { SEARCH_FOCUSED = false; });
}
let SEARCH_INPUT, SEARCH_FOCUSED = false;
function focusSearch(gui) {
  SEARCH_INPUT.value = gui.query; SEARCH_FOCUSED = true;
  try { SEARCH_INPUT.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
}
function blurSearch() { if (SEARCH_FOCUSED) { SEARCH_FOCUSED = false; SEARCH_INPUT.blur(); } }

// ------------------------------------------------------------------ credits
let CREDITS = null;
function setupCredits() {
  CREDITS = document.getElementById('credits');
  const btn = document.getElementById('credits-btn');
  if (!CREDITS || !btn || !CREDITS.showModal) return;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
  const link = (href, text) => { const a = el('a', null, text); a.href = href; a.target = '_blank'; a.rel = 'noopener'; return a; };
  const card = (m, books) => {
    const d = el('div', 'mod');
    const head = el('p');
    const name = m.url ? link(m.url, m.name) : el('span', null, m.name);
    name.classList.add('mod-name');
    head.append(name);
    if (m.authors) head.append(' by ' + m.authors.join(', '));
    d.append(head);
    if (books && books.length) d.append(el('p', 'meta', 'Books: ' + books.map(b => b.name).join(', ')));
    if (m.used) d.append(el('p', null, m.used));
    if (m.owner) d.append(el('p', 'meta', 'All of this belongs to ' + m.owner + ', not to this site.'));
    if (m.changes) d.append(el('p', 'meta', m.changes));
    if (m.license) {
      const p = el('p', 'meta', 'License: ');
      p.append(m.licenseUrl ? link(m.licenseUrl, m.license) : m.license);
      d.append(p);
    }
    return d;
  };
  const mods = document.getElementById('credits-mods'), other = document.getElementById('credits-other');
  [...MODS.mods].sort((a, b) => cmpStr(a.name.toLowerCase(), b.name.toLowerCase()))
    .forEach(m => mods.append(card(m, BOOKS.filter(b => b.mod === m))));
  (MODS.other || []).forEach(m => other.append(card(m)));
  btn.addEventListener('click', () => CREDITS.showModal());
  CREDITS.addEventListener('close', () => canvas.focus({ preventScroll: true }));
}
const creditsOpen = () => !!(CREDITS && CREDITS.open);

// ------------------------------------------------------------------ boot
async function boot() {
  canvas = document.getElementById('book');
  ctx = canvas.getContext('2d');
  SEARCH_INPUT = document.getElementById('search');
  const status = document.getElementById('status');
  try {
    const getJson = async (url) => { const r = await fetch(url); if (!r.ok) throw new Error(url + ': HTTP ' + r.status); return r.json(); };
    const [data, mods] = await Promise.all([getJson('data.json'), getJson('mods.json').catch(err => { console.warn(err.message); return { mods: [] }; })]);
    DATA = data; MODS = mods;
    // mod packs: books generated from other mods, each with its own icon atlas
    const packs = await Promise.all(MODS.mods.filter(m => m.pack).map(m => getJson(m.pack).catch(err => { console.warn(err.message); return null; })));
    const atlases = [{ icons: DATA.icons, file: 'items.png' }];
    for (const pack of packs.filter(Boolean)) {
      DATA.books.push(...pack.books);
      DATA.itemNames = Object.assign({}, pack.itemNames, DATA.itemNames);
      atlases.push({ icons: pack.icons, file: pack.icons.file });
    }
    const images = new Set(['blocks.png', 'entities.png', 'img/vanilla/widgets.png', 'img/vanilla/glint.png']);
    atlases.forEach(a => images.add(a.file));
    DATA.font.forEach(f => images.add(f.file));
    const collect = (o) => {
      if (!o || typeof o !== 'object') return;
      if (Array.isArray(o)) { o.forEach(collect); return; }
      for (const [k, v] of Object.entries(o)) {
        if (typeof v === 'string' && v.startsWith('img/')) images.add(v);
        else if (Array.isArray(v) && k === 'images') v.forEach(s => images.add(s));
        else collect(v);
      }
    };
    collect(DATA.books);
    await Promise.all([...images].map(src => loadImage(src).then(im => { IMG[src] = im; }).catch(err => console.warn(err.message))));
    initFont();
    atlases.forEach(a => registerIcons(a.icons, IMG[a.file]));
    BOOKS = DATA.books;
    BOOKS.forEach(prepareBook);
    BOOKS.forEach(b => { b.mod = MODS.mods.find(m => (m.namespaces || [m.id]).includes(b.ns)) || null; });
    // group books by mod, mods in alphabetical order; books keep their order within a mod
    const modName = (b) => (b.mod ? b.mod.name : '￿').toLowerCase();
    BOOKS = BOOKS.map((b, i) => [b, i]).sort(([a, i], [b, j]) => cmpStr(modName(a), modName(b)) || i - j).map(([b]) => b);
    setupCredits();
    BOOK = BOOKS.find(b => b.id === (SAVE.last && SAVE.last.book)) || BOOKS[0];
    if (!restoreLocation()) openGui(new GuiBookLanding(BOOK), false);
    setupSidebar();
    setupInput();
    status.hidden = true;
    requestAnimationFrame((t) => { lastTime = t; frame(t); });
  } catch (err) {
    status.textContent = 'The book could not be opened: ' + err.message;
    console.error(err);
  }
}
window.addEventListener('DOMContentLoaded', boot);
