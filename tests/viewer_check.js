// Built-in PDF viewer check (run by screenshot.sh in the ghcr.io/puppeteer/puppeteer image:
// PDF.js 6 needs CSS round() = Chrome 125+, the alpine-chrome image is older).
//   node viewer_check.js <base url>   -> /out/viewer.png (dark), /out/viewer-mobile.png (light)
// Flow: list with a search -> click a file name -> PDF rendered -> Back -> list with the same search.
const puppeteer = require("/home/pptruser/node_modules/puppeteer");
const base = (process.argv[2] || "http://localhost:8000").replace(/\/?$/, "/");

async function run(browser, { theme, viewport, shot }) {
  const page = await browser.newPage();
  await page.setViewport(viewport);
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
  page.on("pageerror", e => console.log("pageerror:", e.message));
  await page.goto(base + "?q=invoice", { waitUntil: "networkidle0" });
  await Promise.all([page.waitForNavigation(), page.click("#rows .name-link")]);
  if (!page.url().includes("viewer.html?id=")) throw new Error("not on the viewer: " + page.url());
  const frame = await (await page.waitForSelector("#viewerFrame")).contentFrame();
  await frame.waitForSelector(".page canvas", { timeout: 15000 });
  await frame.waitForFunction(() => document.querySelector(".page").getBoundingClientRect().height > 100);
  await new Promise(r => setTimeout(r, 800));
  await page.screenshot({ path: "/out/" + shot });
  await Promise.all([page.waitForNavigation(), page.click("#backBtn")]);
  const back = new URL(page.url());
  if (back.pathname.endsWith("viewer.html") || back.searchParams.get("q") !== "invoice") {
    throw new Error("Back did not return to the list: " + page.url());
  }
  console.log(`${shot}: ok (back -> ${back.pathname}${back.search})`);
  await page.close();
}

(async () => {
  const browser = await puppeteer.launch({ args: ["--no-sandbox"] }); // throw-away container
  try {
    await run(browser, { theme: "dark", viewport: { width: 1400, height: 700 }, shot: "viewer.png" });
    await run(browser, { theme: "light", viewport: { width: 390, height: 800, isMobile: true, hasTouch: true }, shot: "viewer-mobile.png" });
  } catch (e) {
    console.log("FAILED:", e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
