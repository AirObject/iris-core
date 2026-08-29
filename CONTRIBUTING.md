# Contributing

## Before changing code

1. Read the architecture baseline and the active phase document.
2. Confirm that prerequisite phase gates are complete.
3. Add or update an ADR before changing a frozen boundary.
4. For API, Schema, error, or persistence changes, update source contracts, fixtures, migrations, compatibility expectations, tests, and affected phase documentation in the same change.

## Development workflow

```bash
make bootstrap
make format
make ci
```

The domain package may import only the Python standard library and domain-safe primitives. It must not import API, storage, indexing, provider, coordinator, SDK, or host framework packages.

Migration files are immutable after application. Use the next sequential number, include a short purpose in the filename, and test both an empty-database upgrade and an upgrade from the previously released schema.

## Change review checklist

- Tests describe behavior, failure, and compatibility semantics.
- Generated contracts have no unexplained drift.
- Public changes have forward-compatibility fixtures.
- Logs and failures do not include secrets or sensitive body text.
- Documentation contains traceability and delivery evidence.
- Known limitations and follow-up work are explicit.
