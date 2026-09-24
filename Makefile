# atf — an extensible test framework.  Run `make help` for targets.
PY      := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
BENCH   ?= benches/lab.yaml
IMAGE   ?= atf-mgmt:latest
REQ     ?=
BOARD   ?=
VECTOR  ?=
SUITE   ?=
BACKEND ?= docker

.PHONY: help setup image list suites run report check test test-all clean new-check web

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'

setup:  ## create .venv, install (editable, with host+web extras), seed the bench secrets file
	python3 -m venv .venv
	.venv/bin/pip install -e ".[host,web]"
	@sec=$(BENCH:.yaml=.secrets.yaml); \
	  test -f $$sec || { cp $(BENCH:.yaml=.secrets.example.yaml) $$sec && echo "seeded $$sec"; }

image:  ## build the atf-mgmt container image
	docker build -f docker/atf-mgmt.Dockerfile -t $(IMAGE) .

list:  ## list registered checks
	$(PY) -m atf.cli list

suites:  ## list available suites (suites/*.yaml)
	$(PY) -m atf.cli suites

run:  ## run checks (vars: SUITE=baseline REQ=C.4 BOARD=board-1 VECTOR=mgmt BACKEND=local)
	$(PY) -m atf.cli run --bench $(BENCH) --mgmt-backend $(BACKEND) \
	  $(if $(SUITE),--suite $(SUITE)) $(if $(REQ),--req $(REQ)) \
	  $(if $(BOARD),--board $(BOARD)) $(if $(VECTOR),--vector $(VECTOR))

new-check:  ## scaffold a check (vars: ID=mgmt-tls-enum VECTOR=mgmt REQ=acme:E.3 SEV=high TITLE="...")
	$(PY) -m atf.cli new-check --id $(ID) --vector $(or $(VECTOR),host) \
	  $(if $(REQ),--req $(REQ)) $(if $(SEV),--severity $(SEV)) $(if $(TITLE),--title "$(TITLE)")

report:  ## regenerate matrix/findings from results.json
	$(PY) -m atf.cli report

web:  ## serve the read-only dashboard (http://127.0.0.1:8899)
	$(PY) -m atf.cli web --port $(or $(PORT),8899)

check:  ## verify the codebase (compile + import sanity + ruff if present)
	$(PY) -m compileall -q atf
	@$(PY) -m atf.cli list >/dev/null && echo "import chain OK"
	@command -v ruff >/dev/null 2>&1 && ruff check atf || echo "(ruff not installed; skipped)"

test:  ## run the fast regression suite (no docker); needs the .[test] extra
	$(PY) -m pytest -q -m "not integration"

test-all:  ## run everything incl. the checks-common docker integration test
	$(PY) -m pytest -q

clean:  ## remove reports/ and __pycache__
	rm -rf reports
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

cleanall: clean  ## clean + synced checkouts, tool caches and build artifacts (keeps .venv)
	rm -rf checkouts .pytest_cache .ruff_cache build dist *.egg-info
	@find . -name '*.py[cod]' -delete 2>/dev/null || true
