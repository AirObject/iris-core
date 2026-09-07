# Contract authoring

Both HTTP planes are declared in JSON under `source/`. Generated files under
`schemas/` are outputs, including the compatibility baselines and version manifest.

For the business API, edit `source/contracts.json`:

- `paths` contains OpenAPI path items, operations, parameters and responses.
- `schemas` contains the named schemas used by OpenAPI and standalone JSON Schema.
- `schema_files` maps each standalone filename to a name in `schemas`.
- `openapi` contains document metadata and security schemes. Its version is derived
  from `contract_version`; release runtime versions come from Console `runtime_versions`.
- Capability and error-code lists retain their runtime negotiation role.

For the Console API, edit `source/console.json`. Its `operations` shorthand defines
routes, status codes, schemas and authentication; `schemas`, `fixtures` and
`compatibility` own their respective generated documents.

Run `make contracts`, review the generated diff, and run `make contracts-check
public-api-check lint`. Update the marked current counts in the Console matrix and
Phase 14 report when operations change. Historical counts in dated evidence stay
unchanged. Public interface snapshot changes require the review described in the
[public API guide](../docs/development/public-api.md).

The business generator does not define endpoints or schemas in Python. Changing
contract declarations must not mutate the source during generation.
