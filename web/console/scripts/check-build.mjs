import { readFileSync, readdirSync } from "node:fs";
import { extname } from "node:path";
const extensions = new Set([
  ".html",
  ".js",
  ".css",
  ".svg",
  ".png",
  ".ico",
  ".woff",
  ".woff2",
]);
const files = readdirSync("dist", { recursive: true, withFileTypes: true })
  .filter((e) => e.isFile())
  .map((e) => `${e.parentPath}/${e.name}`);
for (const file of files) {
  if (!extensions.has(extname(file)))
    throw new Error(`Unsupported static suffix: ${file}`);
  const body = readFileSync(file, "utf8");
  if (/MOCK-ONE-TIME|mock-csrf|模拟运营记录|mock-request-/.test(body))
    throw new Error(`Production mock leaked: ${file}`);
  if (/\beval\s*\(|new Function\s*\(/.test(body))
    throw new Error(`CSP eval: ${file}`);
  if (
    file.endsWith(".html") &&
    /\sstyle=|<style\b|<script(?![^>]*src=)[^>]*>\s*\S/.test(body)
  )
    throw new Error("Inline HTML script/style");
}
const html = readFileSync("dist/index.html", "utf8");
if (!html.includes("/console/assets/")) throw new Error("Incorrect asset base");
console.log(
  "Production assets: same-origin, no mock, no inline entry, no eval, allowed suffixes.",
);
