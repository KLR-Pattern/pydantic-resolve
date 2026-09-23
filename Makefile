.PHONY: test lint

test:
	uv run pytest tests/ -q

lint:
	uv run ruff check --fix .

# ---- Python version matrix (mirrors .github/workflows/ci.yml) ----
VENV312 := .venvs/3.12/bin/python
VENV313 := .venvs/3.13/bin/python
VENV314 := .venvs/3.14/bin/python

.PHONY: envs test-312 test-313 test-314 test-matrix

envs: ## Create the 3.12/3.13/3.14 venvs matching the CI matrix (run once; UV_INDEX_URL is honored)
	uv venv --python 3.12 .venvs/3.12
	uv pip install --python $(VENV312) -e ".[dev,mcp]"
	uv pip install --python $(VENV312) "pydantic==2.11.7"
	uv venv --python 3.13 .venvs/3.13
	uv pip install --python $(VENV313) -e ".[dev,mcp]"
	uv pip install --python $(VENV313) "pydantic==2.11.7"
	uv venv --python 3.14 .venvs/3.14
	uv pip install --python $(VENV314) -e ".[dev,mcp]"
	uv pip install --python $(VENV314) -U "pytest>=9" "pytest-asyncio>=1.4"

test-312:
	$(VENV312) -m pytest tests/ demo/ -q

test-313:
	$(VENV313) -m pytest tests/ demo/ -q

test-314:
	$(VENV314) -m pytest tests/ demo/ -q

test-matrix: test-312 test-313 test-314 ## Run tests on 3.12/3.13/3.14 (like CI)
