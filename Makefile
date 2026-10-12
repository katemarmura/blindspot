# Local development checks, aligned with .github/workflows/ci.yml.
#
#   make verify
#
# Runs the Python checks with `uv run --locked` (uses the uv-managed environment and
# fails instead of rewriting uv.lock) and the UI checks with `node`; never formats
# or auto-fixes, and makes
# no live API calls (the test suite blocks real Anthropic clients). All checks
# run even if one fails; the failing ones are listed and the exit code is 1.

UV_RUN := uv run --locked
# The answering screen's pure logic, tested with Node's built-in runner (no npm
# packages). Files are listed explicitly: `node --test` doesn't accept a directory.
UI_TESTS := tests/ui/state.test.mjs

.PHONY: verify
verify:
	@failed=""; \
	run() { \
		name="$$1"; shift; \
		printf '\n==> %s: %s\n' "$$name" "$(UV_RUN) $$*"; \
		if $(UV_RUN) "$$@"; then printf '==> %s: passed\n' "$$name"; \
		else printf '==> %s: FAILED\n' "$$name"; failed="$$failed $$name;"; fi; \
	}; \
	run "Ruff lint" ruff check .; \
	run "Ruff format check" ruff format --check .; \
	run "mypy" mypy; \
	run "pytest" pytest; \
	node_run() { \
		name="$$1"; shift; \
		printf '\n==> %s: %s\n' "$$name" "$$*"; \
		if "$$@"; then printf '==> %s: passed\n' "$$name"; \
		else printf '==> %s: FAILED\n' "$$name"; failed="$$failed $$name;"; fi; \
	}; \
	node_run "UI syntax" node --check src/blindspot/web/assets/app.mjs; \
	node_run "UI tests" node --test $(UI_TESTS); \
	if [ -n "$$failed" ]; then printf '\nverify FAILED:%s\n' "$$failed"; exit 1; fi; \
	printf '\nverify: all checks passed\n'
