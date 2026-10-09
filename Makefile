.PHONY: setup check test build native-test
setup:
	uv sync --extra test
	cd frontend && npm ci
check:
	uv run --extra test ruff check cammon tests native scripts
	cd frontend && npm run build
test:
	uv run --extra test pytest -m 'not integration' -q
build:
	docker compose build
native-test:
	docker build --target test -t cammon:test .
	docker run --rm cammon:test
