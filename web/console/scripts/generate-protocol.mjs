import { readFileSync, writeFileSync } from "node:fs";
const schemas = JSON.parse(
  readFileSync("../../schemas/openapi/console.json", "utf8"),
).components.schemas;
const body =
  JSON.stringify(
    {
      contract_version: schemas.Meta.properties.contract_version.const,
      reason_codes: schemas.ReasonCode.enum,
      templates: schemas.KeyIssueRequest.properties.template.enum,
    },
    null,
    2,
  ) + "\n";
if (process.argv.includes("--check")) {
  if (readFileSync("src/api/protocol.json", "utf8") !== body)
    throw new Error("Protocol enum drift");
} else writeFileSync("src/api/protocol.json", body);
