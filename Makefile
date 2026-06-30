.PHONY: ci compose-config firmware-check lint test docker-build-images dev-install

ci: lint test docker-build-images

dev-install:
	python3 -m pip install -r requirements-dev.txt

compose-config:
	docker compose --env-file .env.example -f compose.yaml config >/tmp/house-sensors-compose.yaml

lint: compose-config
	python3 -m ruff check collectors jobs tests
	$(MAKE) firmware-check
	sh -n management/volt-event/docker-entrypoint.sh

firmware-check:
	python3 -c "from pathlib import Path; path = Path('firmware/atoms3u-env3/main.py'); compile(path.read_text(), str(path), 'exec')"

test:
	python3 -m pytest

docker-build-images:
	docker build -t house-sensors-environment-sensors:test collectors/environment-sensors
	docker build -t house-sensors-volt:test collectors/volt
	docker build -t house-sensors-volt-event:test management/volt-event
	docker build -t house-sensors-downsampling:test jobs/downsampling
