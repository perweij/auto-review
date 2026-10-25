.PHONY: check check-all check-backend check-emacs lint test test-api

check: check-backend

check-backend: lint test test-api

check-all: check-backend check-emacs

check-emacs:
	bash emacs/tests/run.sh

lint:
	shellcheck -x tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.bats tests/*.sh
	python3 -m compileall -q lib/pr_review bin/pr-review
	@for file in tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.sh; do bash -n "$$file" || exit; done

test:
	bats tests/*.bats

test-api:
	python3 -m unittest discover -s tests -p 'test_*.py' -v
