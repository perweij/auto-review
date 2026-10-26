.PHONY: check check-all check-backend check-emacs check-guix lint test test-api

GUIX ?= guix
GUIX_CHANNELS ?=

check: check-backend

check-backend: lint test test-api

check-all: check-backend check-emacs

check-emacs:
	bash emacs/tests/run.sh

check-guix:
	GUIX="$(GUIX)" bash tests/check-guix.sh "$(GUIX_CHANNELS)"

lint:
	shellcheck -x tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.bats tests/*.sh
	python3 -m compileall -q lib/pr_review
	python3 -m py_compile bin/pr-review
	@for file in tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.sh; do bash -n "$$file" || exit; done

test:
	bats tests/*.bats

test-api:
	python3 -m unittest discover -s tests -p 'test_*.py' -v
