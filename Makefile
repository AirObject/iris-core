UV := UV_CACHE_DIR=.uv-cache uv

.PHONY: bootstrap format format-check lint typecheck contracts contracts-check public-api-check test test-affected sdk-test console-check console-browser package-check ci clean

bootstrap:
	$(UV) sync --group dev --frozen
	npm ci --prefix sdk/typescript
	npm ci --prefix web/console

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

public-api-check:
	$(UV) run python -m tools.check_public_api

test:
	$(UV) run pytest

test-affected:
	@test -n "$(strip $(TESTS))" || { echo 'Set TESTS to explicit affected test paths or node IDs.'; exit 2; }
	$(UV) run pytest $(TESTS) --no-cov

sdk-test:
	npm test --prefix sdk/typescript

console-check:
	npm run check --prefix web/console

console-browser: console-check
	npm run test:browser --prefix web/console

package-check:
	$(UV) run python -m tools.check_packages

ci: format-check lint typecheck contracts-check public-api-check test sdk-test console-browser package-check

clean:
	$(UV) cache clean
