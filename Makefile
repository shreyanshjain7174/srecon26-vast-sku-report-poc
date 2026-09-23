.PHONY: test setup-hooks verify-hooks semgrep

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q

setup-hooks:
	git config core.hooksPath .githooks

verify-hooks:
	@test "$$(git config --get core.hooksPath)" = ".githooks"

semgrep:
	semgrep --config .semgrep.yml --error src tests scripts

