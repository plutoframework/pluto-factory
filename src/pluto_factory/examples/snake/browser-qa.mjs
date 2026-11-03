import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";

const playwrightModule = process.argv[2];
const artifacts = path.resolve(process.argv[3]);
assert.ok(playwrightModule, "Browser QA requires a Playwright module path");
const { chromium } = await import(pathToFileURL(playwrightModule));
const root = process.cwd();
const types = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };
const server = http.createServer(async (request, response) => {
  try {
    const requested = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    const filename = path.resolve(root, "." + (requested === "/" ? "/index.html" : requested));
    assert.ok(filename.startsWith(root + path.sep));
    response.setHeader("Content-Type", types[path.extname(filename)] || "application/octet-stream");
    response.end(await fs.readFile(filename));
  } catch {
    response.statusCode = 404;
    response.end("Not found");
  }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const url = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true, channel: "chrome" });
  const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(url, { waitUntil: "networkidle" });
  await page.locator("canvas#board").waitFor({ state: "visible" });
  assert.ok((await page.title()).toLowerCase().includes("snake"));
  assert.equal((await page.locator("#score").innerText()).trim(), "0");
  await page.locator("#pause").click();
  assert.ok((await page.locator("#pause").innerText()).toLowerCase().includes("resume"));
  await page.locator("#restart").click();
  assert.equal((await page.locator("#score").innerText()).trim(), "0");
  await page.locator("#pause").click();
  await fs.mkdir(artifacts, { recursive: true });
  const screenshots = [path.join(artifacts, "snake-desktop.png")];
  await page.screenshot({ path: screenshots[0], fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator("button[data-direction]").count(), 4);
  assert.ok(await page.locator("button[data-direction='up']").isVisible());
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.locator("#restart").click();
  await page.keyboard.press("Space");
  assert.ok((await page.locator("#pause").innerText()).toLowerCase().includes("resume"));
  screenshots.push(path.join(artifacts, "snake-mobile.png"));
  await page.screenshot({ path: screenshots[1], fullPage: true });
  assert.deepEqual(errors, [], "The app must not emit browser errors");
  console.log(JSON.stringify({
    passed: true,
    scenarios: ["desktop rendering", "pause and restart", "mobile controls", "keyboard pause", "responsive width", "no runtime errors"],
    screenshots,
  }));
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
