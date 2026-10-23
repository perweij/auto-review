.PHONY: check lint test

check: lint test

lint:
	shellcheck -x bin/pr-review lib/*.sh tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.bats tests/*.sh
	bash -n bin/pr-review
	@for file in lib/*.sh tests/helpers/mock-cli tests/helpers/fixture.bash tests/*.sh; do bash -n "$$file" || exit; done

test:
	bats tests/*.bats
