import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { validateContract } from "../src/index.js";

interface FixtureCase {
  readonly expected: "accept" | "reject";
  readonly file: string;
  readonly schema: string;
}

interface FixtureManifest {
  readonly cases: readonly FixtureCase[];
}

const fixtureRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../schemas/fixtures");

test("TypeScript SDK matches the shared fixture manifest", async () => {
  const manifest = JSON.parse(
    await readFile(resolve(fixtureRoot, "manifest.json"), "utf8"),
  ) as FixtureManifest;
  for (const fixture of manifest.cases) {
    const value: unknown = JSON.parse(
      await readFile(resolve(fixtureRoot, fixture.file), "utf8"),
    );
    const accepted = validateContract(fixture.schema, value).length === 0;
    assert.equal(accepted, fixture.expected === "accept", fixture.file);
  }
});
