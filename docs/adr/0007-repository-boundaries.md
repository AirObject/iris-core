# ADR-0007: Repository and Dependency Boundaries

- Status: Accepted
- Date: 2026-08-29
- Owners: Iris Memory Core Team
- Baseline: §3, §33

## Context

Core must remain independent of host frameworks while contracts and SDKs release together.

## Decision

Core, Schema, fixtures, migrations, and Python/TypeScript SDKs live in this monorepo. Bellis Adapter and AstrBot Bridge live in separate repositories and depend only on released SDK/Schema/HTTP/event contracts. Dependency direction is `api → application → domain`; adapters such as storage, indexing, jobs, providers, coordinator, security, and observability implement application ports. Domain imports only the standard library, relative domain modules, and explicitly approved domain-safe primitives.

## Rejected alternatives

- Putting host adapters in Core.
- Letting adapters read SQLite or FAISS files.
- Allowing Domain to import FastAPI, SQLite, Pydantic, FAISS, Provider SDKs, or host types.

## Consequences

Framework changes cannot redefine the domain. Consumer contract tests become mandatory for independent adapters.

## Migration impact

Moving a frozen boundary requires an ADR and a staged package/protocol migration; import-boundary tests reject accidental erosion.
