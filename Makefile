.PHONY: sync lint types test fmt all requirements vendor-commons

# Build sg-commons into a wheel vendored here so the Lambda bundle (PythonFunction mounts only
# this dir) can pip-install it by relative path. Re-run after changing sg-commons.
vendor-commons:
	cd ../sg-commons && uv build --wheel
	mkdir -p vendor && rm -f vendor/sg_commons-*.whl
	cp ../sg-commons/dist/sg_commons-*.whl vendor/

requirements: vendor-commons
	uv export --frozen --no-dev --no-hashes --no-emit-project --no-emit-package sg-commons -o requirements.txt
	printf '%s\n' "./vendor/$$(cd vendor && ls sg_commons-*.whl)" >> requirements.txt

sync:
	uv sync

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

types:
	uv run mypy src

test:
	uv run pytest

all: lint types test
