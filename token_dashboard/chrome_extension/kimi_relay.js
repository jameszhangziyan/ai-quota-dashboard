// 运行在 kimi.com「我的订阅」页：直接读页面上显示的用量文字（总使用量、Code 5 小时 / 7 天、赠送额度、会员等级），
// 整理成与订阅用量接口相同的结构交给扩展后台。不接触任何登录信息。

function toISO(s) {
  // 页面时间是本地时间，形如「2026-10-23 16:32」或「10-20 10:32」（省略年份）
  const m = s.match(/(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})/);
  if (!m) return null;
  const y = m[1] ? +m[1] : new Date().getFullYear();
  return new Date(y, +m[2] - 1, +m[3], +m[4], +m[5]).toISOString();
}

function after(text, label, re) {
  const i = text.indexOf(label);
  if (i < 0) return null;
  const m = text.slice(i + label.length, i + label.length + 120).match(re);
  return m || null;
}

function scrape() {
  const t = document.body ? document.body.innerText : "";
  const total = after(t, "总使用量", /([\d.]+)%/);
  if (!total) return null;
  const pct = m => (m ? parseFloat(m[1]) / 100 : null);
  const reset = s => { const m = s && s.match(/((?:\d{4}-)?\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})\s*后重置/); return m ? toISO(m[1]) : null; };
  const seg = (label, len = 120) => { const i = t.indexOf(label); return i < 0 ? "" : t.slice(i, i + len); };
  const s5 = seg("5 小时用量"), s7 = seg("7 天用量"), sTotal = seg("总使用量", 160);
  const plan = (t.match(/我的订阅[\s\S]{0,200}?\n\s*(Moderato|Allegretto|Allegro|Vivace|Presto|Adagio|Andante|[A-Z][a-z]+)\s*\n/) || [])[1] || null;
  const data = {
    plan,
    subscriptionBalance: { amountUsedRatio: pct(total), upcomingExpiration: reset(sTotal) },
    ratelimitCode5h: { ratio: pct(s5.match(/([\d.]+)%/)), resetTime: reset(s5), enabled: !!s5 },
    ratelimitCode7d: { ratio: pct(s7.match(/([\d.]+)%/)), resetTime: reset(s7), enabled: !!s7 },
    giftBalances: [],
  };
  const g = t.match(/赠送额度\s*\n+\s*([^\n]+?)\s*\n+\s*([\d.]+)%/);
  if (g) data.giftBalances.push({ displayName: g[1].trim(), amountUsedRatio: parseFloat(g[2]) / 100 });
  return data;
}

// 页面是前端渲染的，数据加载完才有文字：每秒检查一次，最多 25 秒
let tries = 0;
const timer = setInterval(() => {
  const data = scrape();
  if (data || ++tries > 25) {
    clearInterval(timer);
    if (data) chrome.runtime.sendMessage({ type: "kimi-usage", data });
  }
}, 1000);
