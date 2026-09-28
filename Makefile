# Local development checks, aligned with .github/workflows/ci.yml.
#
#   make verify
#
# Runs every check with `uv run --locked` (uses the uv-managed environment and
# fails instead of rewriting uv.lock), never formats or auto-fixes, and makes
# no live API calls (the test suite blocks real Anthropic clients). All checks
# run even if one fails; the failing ones are listed and the exit code is 1.

UV_RUN := uv run --locked

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
	if [ -n "$$failed" ]; then printf '\nverify FAILED:%s\n' "$$failed"; exit 1; fi; \
	printf '\nverify: all checks passed\n'
