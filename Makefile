UV := UV_CACHE_DIR=.uv-cache uv

.PHONY: bootstrap format format-check lint typecheck contracts contracts-check test sdk-test ci clean

bootstrap:
	$(UV) sync --group dev --frozen
	npm ci --prefix sdk/typescript

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .
	$(UV) run python -m tools.check_import_boundaries
	$(UV) run python -m tools.check_docs

typecheck:
	$(UV) run mypy
	npm run typecheck --prefix sdk/typescript

contracts:
	$(UV) run python -m tools.generate_contracts

contracts-check:
	$(UV) run python -m tools.generate_contracts --check
	$(UV) run python -m tools.check_compatibility

test:
	$(UV) run pytest

sdk-test:
	npm test --prefix sdk/typescript

ci: format-check lint typecheck contracts-check test sdk-test

clean:
	$(UV) cache clean
