.PHONY: seed test
seed:
	docker compose run --rm --no-deps api python -m app.seed
test: seed
	docker compose run --rm --no-deps api python -m pytest -q app/tests
