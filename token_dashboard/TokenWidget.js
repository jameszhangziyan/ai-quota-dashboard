// AI 额度看板 · iPhone 小组件（Scriptable）
// 披萨布局：5 个账号各占一块扇形，从圆心向外按主额度用量填色，与网页看板一致。
// 支持：主屏幕 小 / 中 / 大，锁屏 矩形 / 圆形 / 单行。
// 数据来自 Mac 上运行的 server.py；手机需与 Mac 在同一网络（或同一 Tailscale）。

const SERVER = globalThis.TW_SERVER || "__SERVER__"; // Mac 的局域网地址（推送到 Gist 时自动填入）
const KEY = globalThis.TW_KEY || ""; // 访问密钥；经加载器运行时由加载器提供（Gist 上的副本不含密钥）
// Mac 每 5 分钟推送到 GitHub 私密 Gist 的数据地址；在外面（不在家里 Wi-Fi）也能读到
const GIST_URL = globalThis.TW_DATA_URL || "__DATA_URL__"; // Gist 数据地址（推送到 Gist 时自动填入）

const C = {
  bgTop: new Color("#23232b"), bgBottom: new Color("#141418"),
  text: new Color("#f4f4f6"), muted: new Color("#9a9aa6"),
  slice: new Color("#2a2a31"), hole: new Color("#16161a"),
  amber: "#e8b03a", red: "#f07a52",
};
const ORDER = ["claude", "chatgpt", "kimiwork", "workbuddy", "gemini"];
const BRAND = { claude: "#e8896a", chatgpt: "#2fc49a", kimiwork: "#6b95f2", workbuddy: "#4cc3df", gemini: "#bd84ea" };
const SHORT = { claude: "Claude", chatgpt: "ChatGPT", kimiwork: "Kimi", workbuddy: "WorkBuddy", gemini: "Gemini" };

// ---------- 数据：在线拉取，失败时用上次缓存 ----------
const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), "token-widget-cache.json");

async function fetchJSON(url, timeout) {
  const req = new Request(url);
  req.timeoutInterval = timeout;
  const d = await req.loadJSON();
  if (!d || !d.quotas) throw new Error("bad response");
  return d;
}

// 先读 Gist（任何网络都可用；加时间戳绕开 CDN 缓存），失败再试家里的 Mac，最后用上次缓存
async function getData() {
  const sources = [];
  if (GIST_URL.startsWith("https://")) sources.push(() => fetchJSON(`${GIST_URL}?t=${Date.now()}`, 10));
  sources.push(() => fetchJSON(`${SERVER}/api/widget?k=${KEY}`, 6));
  for (const get of sources) {
    try {
      const d = await get();
      fm.writeString(cachePath, JSON.stringify(d));
      return { d, offline: false };
    } catch (e) {}
  }
  if (fm.fileExists(cachePath)) return { d: JSON.parse(fm.readString(cachePath)), offline: true };
  return { d: null, offline: true };
}

// 每块扇形需要的数据，都取自服务端整理好的 card（与网页同一份）
function slices(d) {
  const by = {};
  (d.quotas || []).forEach(a => { by[a.id] = a; });
  return ORDER.map(id => {
    const a = by[id] || { id };
    const c = a.card || {};
    const p = c.primary && typeof c.primary.percent === "number" ? c.primary.percent : null;
    return {
      id, name: SHORT[id], plan: c.plan || "", pct: p,
      value: p === null ? "—" : `${Math.round(p)}%`,
      sub: c.sub || "", tokens: c.tokens || "",
      // 从没接入过的账号显示「待接入」，接入过但登录过期的才提示重新粘贴
      footer: p === null ? "待接入" : (c.footer || ""), warn: p !== null && !!c.warn, warnShort: c.warn_short || "需处理",
    };
  });
}

const fillColor = s => new Color(s.pct >= 90 ? C.red : s.pct >= 70 ? C.amber : BRAND[s.id], 0.55);

// ---------- 披萨绘制 ----------
// Scriptable 的 Path 没有圆弧命令，用折线逼近（每块 24 段，肉眼看不出棱角）
function wedgePath(cx, cy, ri, ro, a0, a1) {
  const pts = [], n = 24;
  for (let k = 0; k <= n; k++) { const a = a0 + (a1 - a0) * k / n; pts.push(new Point(cx + ro * Math.cos(a), cy + ro * Math.sin(a))); }
  for (let k = n; k >= 0; k--) { const a = a0 + (a1 - a0) * k / n; pts.push(new Point(cx + ri * Math.cos(a), cy + ri * Math.sin(a))); }
  const p = new Path(); p.addLines(pts); p.closeSubpath(); return p;
}

// mode: "tiny" 只写 %；"full" 写名字、%、副指标、本月 token、重置
function pizzaImage(list, size, mode, updated) {
  const ctx = new DrawContext();
  ctx.size = new Size(size, size); ctx.opaque = false; ctx.respectScreenScale = true;
  const cx = size / 2, cy = size / 2, R = size / 2 - 1, r0 = size * (mode === "full" ? 0.13 : 0.15);
  const gap = mode === "full" ? 0.02 : 0.03, step = 2 * Math.PI / 5;
  list.forEach((s, i) => {
    const a0 = -Math.PI / 2 - step / 2 + i * step + gap, a1 = a0 + step - 2 * gap;
    ctx.addPath(wedgePath(cx, cy, r0, R, a0, a1)); ctx.setFillColor(C.slice); ctx.fillPath();
    if (s.pct !== null) {
      const fr = r0 + (R - r0) * Math.max(s.pct, 1.5) / 100;
      ctx.addPath(wedgePath(cx, cy, r0, fr, a0, a1)); ctx.setFillColor(fillColor(s)); ctx.fillPath();
    }
    const mid = (a0 + a1) / 2, tr = R * (mode === "full" ? 0.63 : 0.6);
    const x = cx + tr * Math.cos(mid), y = cy + tr * Math.sin(mid);
    const line = (txt, dy, font, color, w, h) => {
      ctx.setFont(font); ctx.setTextColor(color); ctx.setTextAlignedCenter();
      ctx.drawTextInRect(txt, new Rect(x - w / 2, y + dy, w, h));
    };
    if (mode === "full") {
      const W = size * 0.36, H = size * 0.045;
      line(s.plan ? `${s.name} · ${s.plan}` : s.name, -size * 0.11, Font.semiboldSystemFont(size * 0.042), new Color(BRAND[s.id]), W, size * 0.06);
      line(s.value, -size * 0.065, Font.boldRoundedSystemFont(size * 0.075), C.text, W, size * 0.1);
      line(s.sub, size * 0.03, Font.mediumSystemFont(size * 0.031), C.muted, W, H);
      line(s.tokens, size * 0.068, Font.mediumSystemFont(size * 0.031), C.text, W, H);
      line(s.warn ? s.warnShort : s.footer, size * 0.106, Font.regularSystemFont(size * 0.029),
        s.warn ? new Color(C.red) : C.muted, W, H);
    } else {
      line(s.value, -size * 0.06, Font.boldRoundedSystemFont(size * 0.1),
        s.warn ? new Color(C.red) : C.text, size * 0.34, size * 0.13);
    }
  });
  if (mode === "full") {
    ctx.setFillColor(C.hole);
    ctx.fillEllipse(new Rect(cx - r0 + 3, cy - r0 + 3, 2 * r0 - 6, 2 * r0 - 6));
    ctx.setTextAlignedCenter();
    ctx.setFont(Font.semiboldSystemFont(size * 0.04)); ctx.setTextColor(C.text);
    ctx.drawTextInRect("AI 额度", new Rect(cx - r0, cy - size * 0.035, 2 * r0, size * 0.055));
    ctx.setFont(Font.regularSystemFont(size * 0.03)); ctx.setTextColor(C.muted);
    ctx.drawTextInRect(updated, new Rect(cx - r0, cy + size * 0.018, 2 * r0, size * 0.045));
  }
  return ctx.getImage();
}

// ---------- 小部件构件 ----------
function addText(stack, str, font, color, opts = {}) {
  const t = stack.addText(str);
  t.font = font; t.textColor = color || C.text; t.lineLimit = 1;
  if (opts.scale) t.minimumScaleFactor = opts.scale;
  return t;
}

function header(w, d, offline) {
  const row = w.addStack(); row.centerAlignContent();
  addText(row, "AI 额度", Font.heavyRoundedSystemFont(11), C.muted);
  row.addSpacer();
  const time = d ? d.generated.slice(11, 16) : "--:--";
  addText(row, offline ? `离线 ${time}` : time, Font.mediumSystemFont(10), offline ? new Color(C.red) : C.muted);
}

// ---------- 各尺寸布局 ----------
function smallWidget(w, d, offline) {
  w.setPadding(8, 8, 8, 8);
  const row = w.addStack(); row.addSpacer();
  row.addImage(pizzaImage(slices(d), 300, "tiny")).imageSize = new Size(140, 140);
  row.addSpacer();
  if (offline) { const t = addText(w, "离线", Font.mediumSystemFont(9), new Color(C.red)); t.centerAlignText(); }
}

function mediumWidget(w, d, offline) {
  w.setPadding(10, 12, 10, 14);
  const list = slices(d);
  const body = w.addStack(); body.centerAlignContent();
  body.addImage(pizzaImage(list, 300, "tiny")).imageSize = new Size(128, 128);
  body.addSpacer(10);
  const col = body.addStack(); col.layoutVertically();
  header(col, d, offline);
  col.addSpacer(5);
  list.forEach((s, i) => {
    if (i) col.addSpacer(3);
    const r = col.addStack(); r.centerAlignContent();
    const dot = r.addStack(); dot.size = new Size(7, 7); dot.cornerRadius = 3.5; dot.backgroundColor = new Color(BRAND[s.id]);
    r.addSpacer(5);
    addText(r, s.name, Font.semiboldSystemFont(12), C.text);
    r.addSpacer(4);
    const sub = r.addStack(); sub.size = new Size(58, 0);
    addText(sub, s.warn ? s.warnShort : (s.pct === null ? "待接入" : s.sub), Font.regularSystemFont(9),
      s.warn ? new Color(C.red) : C.muted, { scale: 0.6 });
    r.addSpacer();
    addText(r, s.value, Font.boldRoundedSystemFont(13), s.pct === null ? C.muted : C.text);
  });
}

function largeWidget(w, d, offline) {
  w.setPadding(10, 10, 10, 10);
  header(w, d, offline);
  w.addSpacer(4);
  const row = w.addStack(); row.addSpacer();
  row.addImage(pizzaImage(slices(d), 640, "full", d.generated.slice(11, 16) + " 更新")).imageSize = new Size(318, 318);
  row.addSpacer();
}

function accessoryWidget(w, d, family) {
  const list = slices(d).filter(s => s.pct !== null);
  if (family === "accessoryInline") {
    w.addText(list.slice(0, 3).map(s => `${s.name} ${s.value}`).join(" ") || "AI 额度");
    return;
  }
  if (family === "accessoryCircular") {
    const top = list.slice().sort((x, y) => y.pct - x.pct)[0];
    const s = w.addStack(); s.layoutVertically(); s.centerAlignContent();
    const t1 = s.addText(top ? top.name.slice(0, 6) : "额度"); t1.font = Font.mediumSystemFont(9); t1.centerAlignText();
    const t2 = s.addText(top ? top.value : "--"); t2.font = Font.boldRoundedSystemFont(15); t2.minimumScaleFactor = 0.5; t2.centerAlignText();
    return;
  }
  list.slice(0, 3).forEach(s => {
    const t = w.addText(`${s.name} ${s.value}  ${s.sub}`);
    t.font = Font.semiboldSystemFont(12); t.minimumScaleFactor = 0.7; t.lineLimit = 1;
  });
  if (!list.length) w.addText("暂无额度数据");
}

// ---------- 入口 ----------
const family = config.widgetFamily || "large";
const { d, offline } = await getData();
const w = new ListWidget();
// 点小组件：在 Scriptable 里重新运行本脚本，弹出大号披萨看板（数据来自 Gist，任何网络都能看）。
// 不再直接打开 192.168.x.x 的网页，那个地址离开家里 Wi-Fi 就是空白。
w.url = `scriptable:///run/${encodeURIComponent(Script.name())}`;
w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);

if (family.startsWith("accessory")) {
  if (d) accessoryWidget(w, d, family); else w.addText("AI 额度 离线");
} else {
  const g = new LinearGradient();
  g.colors = [C.bgTop, C.bgBottom]; g.locations = [0, 1];
  w.backgroundGradient = g;
  if (!d) {
    w.setPadding(14, 15, 14, 15);
    header(w, null, true);
    w.addSpacer();
    addText(w, "连不上 Mac", Font.boldRoundedSystemFont(18), C.text);
    const e = w.addText("确认手机和 Mac 在同一网络，且 Mac 上的看板服务在运行。");
    e.font = Font.regularSystemFont(11); e.textColor = C.muted;
    w.addSpacer();
  } else if (family === "small") smallWidget(w, d, offline);
  else if (family === "large" || family === "extraLarge") largeWidget(w, d, offline);
  else mediumWidget(w, d, offline);
}

if (config.runsInWidget) {
  Script.setWidget(w);
} else {
  // 从小组件点进来或在 Scriptable 里直接运行：显示大号披萨看板
  await w.presentLarge();
}
Script.complete();
