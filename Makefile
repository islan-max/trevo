test:
	pytest tests/ -v --cov=app --cov-report=term-missing

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

lint:
	ruff check .

format:
	ruff format .

run-dev:
	uvicorn main:app --reload --port 8000
