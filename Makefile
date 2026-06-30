.PHONY: ci compose-config lint test docker-build-images dev-install

ci: lint test docker-build-images

dev-install:
	python3 -m pip install -r requirements-dev.txt

compose-config:
	docker compose --env-file .env.example -f compose.yaml config >/tmp/house-sensors-compose.yaml

lint: compose-config
	python3 -m ruff check collectors tests
	sh -n management/volt-event/docker-entrypoint.sh

test:
	python3 -m pytest

docker-build-images:
	docker build -t house-sensors-environment-sensors:test collectors/environment-sensors
	docker build -t house-sensors-volt:test collectors/volt
	docker build -t house-sensors-volt-event:test management/volt-event
