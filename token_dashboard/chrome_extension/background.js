// AI 额度看板助手：每 15 分钟借用 Chrome 里已登录的页面查一次 Gemini 和 Kimi 的用量，
// 只把用量数字 POST 给本机看板。扩展不读取任何令牌或 Cookie。

// 看板地址和访问密钥来自 config.js（由 install.sh 生成；示例见 config.example.js）
try { importScripts("config.js"); } catch (e) { /* 没有 config.js 时下面会提示 */ }
const CFG = self.AIQ_CONFIG || {};
const DASHBOARD = CFG.dashboard || "http://127.0.0.1:8787";
const KEY = CFG.key || "";
const PERIOD_MIN = 15;
const KIMI_PAGE = "https://www.kimi.com/settings/subscription";

const schedule = () => chrome.alarms.create("refresh", { periodInMinutes: PERIOD_MIN, delayInMinutes: 0.1 });
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener(a => { if (a.name === "refresh") refreshAll(); });
chrome.action.onClicked.addListener(() => refreshAll());

async function report(provider, payload, error) {
  try {
    await fetch(`${DASHBOARD}/api/ext?k=${KEY}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, payload, error: error || null }),
    });
  } catch (e) { /* 看板没开时忽略，下一轮再试 */ }
}

async function refreshAll() {
  await Promise.allSettled([refreshGemini(), refreshKimi()]);
  await chrome.storage.local.set({ lastRun: new Date().toISOString() });
}

// ---------- Gemini：用量直接写在 /usage 页面的 HTML 里，浏览器带着自己的登录去打开即可 ----------
async function refreshGemini() {
  try {
    const r = await fetch("https://gemini.google.com/usage", { credentials: "include" });
    if (r.url.includes("accounts.google.com")) throw new Error("Google 未登录");
    const html = (await r.text()).replace(/\\"/g, '"');
    const i = html.indexOf('"getUsageInfo"');
    if (i < 0) throw new Error("页面里没有用量数据（可能未登录）");
    const snippet = html.slice(i, i + 1500);
    const tier = (snippet.match(/"subscriptionTier":"(\w+)"/) || [])[1] || null;
    const pools = [...snippet.matchAll(/"poolType":"(\w+)","percentageUsed":([\d.]+)(?:,"gxu":\{"windowRefreshesAt":"([^"]+)")?/g)]
      .map(m => ({ type: m[1], percent: parseFloat(m[2]), refreshesAt: m[3] || null }));
    if (!pools.length) throw new Error("没找到额度池");
    await report("gemini", { tier, pools });
  } catch (e) {
    await report("gemini", null, String(e.message || e));
  }
}

// ---------- Kimi：打开「我的订阅」页，页面自己查用量，kimi_hook.js 旁听结果 ----------
let kimiWindowId = null;

async function refreshKimi() {
  const open = await chrome.tabs.query({ url: `${KIMI_PAGE}*` });
  if (open.length) {
    chrome.tabs.reload(open[0].id); // 用户自己开着订阅页时，刷新一下即可
  } else {
    // 开一个最小化的小窗口，拿到数据或 30 秒后自动关闭
    const win = await chrome.windows.create({ url: KIMI_PAGE, state: "minimized", focused: false });
    kimiWindowId = win.id;
    setTimeout(() => closeKimiWindow("Kimi 页面 30 秒内没有返回用量（可能未登录）"), 30000);
  }
}

function closeKimiWindow(error) {
  if (kimiWindowId === null) return;
  chrome.windows.remove(kimiWindowId).catch(() => {});
  kimiWindowId = null;
  if (error) report("kimiwork", null, error);
}

chrome.runtime.onMessage.addListener(msg => {
  if (msg && msg.type === "kimi-usage" && msg.data) {
    report("kimiwork", msg.data);
    setTimeout(() => closeKimiWindow(null), 1000);
  }
});
