// AI 额度 · 小组件加载器（Scriptable）
// 只装这一次：每次刷新都从 Gist 下载最新的小组件代码再运行，
// 以后 Mac 上改了界面，手机会自动跟上，不用再隔空投送。没网时用上次下载的版本。
// 本文件由 make_loader.py 生成，含你的访问密钥，不要分享给别人。

globalThis.TW_KEY = "{{KEY}}";            // 访问密钥，只用于连家里的 Mac（Gist 上的代码不含密钥）
globalThis.TW_SERVER = "{{SERVER}}";      // Mac 的局域网地址
globalThis.TW_DATA_URL = "{{DATA_URL}}";  // Gist 上的额度数据
const CODE_URL = "{{CODE_URL}}";          // Gist 上的小组件代码

const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), "ai-quota-widget-core.js");

let code = null;
try {
  const req = new Request(`${CODE_URL}?t=${Date.now()}`);
  req.timeoutInterval = 10;
  const fetched = await req.loadString();
  if (!fetched.includes("pizzaImage")) throw new Error("下载内容不对");
  fm.writeString(cachePath, fetched);
  code = fetched;
} catch (e) {
  if (fm.fileExists(cachePath)) code = fm.readString(cachePath);
}

if (code) {
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  await new AsyncFunction(code)();
} else {
  const w = new ListWidget();
  const t = w.addText("AI 额度：第一次运行需要联网");
  t.font = Font.mediumSystemFont(13);
  Script.setWidget(w);
  Script.complete();
}
