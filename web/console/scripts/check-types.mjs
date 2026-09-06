import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
const dir = mkdtempSync(`${tmpdir()}/imc-console-types-`);
try {
  execFileSync("node", [
    "node_modules/openapi-typescript/bin/cli.js",
    "../../schemas/openapi/console.json",
    "-o",
    `${dir}/generated.d.ts`,
  ]);
  if (
    readFileSync(`${dir}/generated.d.ts`, "utf8") !==
    readFileSync("src/api/generated.d.ts", "utf8")
  )
    throw new Error("Console types drift: npm run types:generate");
  console.log("Published Console type generation is deterministic.");
} finally {
  rmSync(dir, { recursive: true });
}
