import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const playwrightPath = process.env.PLAYWRIGHT_NODE_PATH || "/opt/homebrew/lib/node_modules/playwright";
const { chromium } = require(playwrightPath);

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index === -1 || !process.argv[index + 1]) throw new Error(`missing ${name}`);
  return process.argv[index + 1];
}

const outputDir = resolve(argument("--output-dir"));
const expected = {
  instanceId: argument("--instance-id"),
  label: argument("--label"),
  nonce: argument("--nonce"),
};
const fixtureUrl = pathToFileURL(resolve("browser/report-fixture/index.html")).href;
let browser;
try {
  await mkdir(outputDir, { recursive: true });
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1024, height: 720 } });
  const requests = [];
  page.on("request", (request) => {
    if (!request.url().startsWith("file:")) requests.push(request.url());
  });
  await page.goto(fixtureUrl, { waitUntil: "load" });
  const target = page.locator("[data-instance]");
  const actual = {
    instanceId: await target.getAttribute("data-instance-id"),
    label: await target.getAttribute("data-label"),
    nonce: await target.getAttribute("data-nonce"),
  };
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error("target mismatch; refusing report click");
  }
  const visible = {
    instanceId: await page.locator("[data-field=instance-id]").textContent(),
    label: await page.locator("[data-field=label]").textContent(),
    nonce: await page.locator("[data-field=nonce]").textContent(),
  };
  if (JSON.stringify(visible) !== JSON.stringify(expected)) {
    throw new Error("visible target mismatch; refusing report click");
  }
  await page.screenshot({ path: resolve(outputDir, "before.png") });
  await page.locator("[data-action=report]").click();
  await page.locator("[data-report-submitted=true]").waitFor({ state: "visible", timeout: 60_000 });
  await page.screenshot({ path: resolve(outputDir, "after.png") });
  const providerRequestCount = await page.evaluate(() => window.fixture.providerRequestCount);
  if (providerRequestCount !== 0 || requests.length !== 0) throw new Error("fixture attempted an external provider request");
  await writeFile(resolve(outputDir, "receipt.json"), `${JSON.stringify({
    status: "SUBMITTED",
    instance_id: Number(expected.instanceId),
    label: expected.label,
    nonce: expected.nonce,
    provider_request_count: providerRequestCount,
    before_path: "before.png",
    after_path: "after.png",
  }, null, 2)}\n`, "utf8");
} catch (error) {
  console.error(error.message);
  process.exitCode = 2;
} finally {
  await browser?.close();
}
