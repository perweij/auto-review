.PHONY: check check-all check-backend check-emacs check-guix lint test test-api

GUIX ?= guix
GUIX_CHANNELS ?=

# Checks must not populate the checkout with interpreter caches.
export PYTHONDONTWRITEBYTECODE = 1

check: check-backend

check-backend: lint test test-api

check-all: check-backend check-emacs

check-emacs:
	bash emacs/tests/run.sh

check-guix:
	GUIX="$(GUIX)" bash tests/check-guix.sh "$(GUIX_CHANNELS)"

lint:
	shellcheck -x tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.bats tests/*.sh emacs/tests/run.sh
	python3 -c 'from pathlib import Path; [compile(p.read_bytes(), str(p), "exec") for p in [Path("bin/pr-review"), Path("bin/fix-issue"), *Path("lib/pr_review").rglob("*.py")]]'
	@for file in tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.sh emacs/tests/run.sh; do bash -n "$$file" || exit; done

test:
	bats tests/*.bats

test-api:
	python3 -m unittest discover -s tests -p 'test_*.py' -v

# Opt-in: real remote fixtures. smoke also runs economical models (uses quota).
.PHONY: smoke smoke-fast smoke-extended
smoke:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke-forges.py --suite all

smoke-fast:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke-forges.py --suite smoke

smoke-extended:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/smoke-forges.py --suite extended
