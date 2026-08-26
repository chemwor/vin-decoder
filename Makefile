.PHONY: install run test lint fmt docker smoke clean

install:
	python -m pip install -r requirements-dev.txt

run:
	uvicorn app.main:app --reload --port 8000

test:
	python -m pytest

lint:
	python -m ruff check .
	python -m ruff format --check .

fmt:
	python -m ruff check --fix .
	python -m ruff format .

docker:
	docker build -t vin-decoder:local .
	docker run --rm -p 8000:8000 -v vin-cache:/data vin-decoder:local

# End-to-end check against the real vPIC API. Requires the server running.
smoke:
	./scripts/smoke.sh

clean:
	rm -f *.db *.db-wal *.db-shm *.parquet
	rm -rf .pytest_cache .ruff_cache __pycache__ app/__pycache__ tests/__pycache__
