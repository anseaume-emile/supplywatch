# Supply Watch commands. Names follow <application>-<action>-<sub-action>.
# build/test/lint/integration-test are the common entry points CI uses for every application.
APP := supplywatch
IMAGE_NAME := cdds/supplywatch
TAG ?= latest
PUSH ?= false
COMPOSE := docker compose --env-file .env -f docker/docker-compose.yml

.PHONY: build test integration-test lint \
	$(APP)-install $(APP)-lock $(APP)-build $(APP)-test $(APP)-lint $(APP)-format $(APP)-typecheck \
	$(APP)-run $(APP)-docker-up $(APP)-docker-down $(APP)-docker-logs

build: $(APP)-build
test: $(APP)-test
lint: $(APP)-lint
# No integration tests yet; cross-application tests belong in the monorepo's integration test folder.
integration-test:
	@echo "$(APP): no integration tests"

$(APP)-install:
	uv sync
	uv run pre-commit install

$(APP)-lock:
	uv lock
	uv export --no-dev --no-hashes --no-emit-project -o requirements.txt

$(APP)-build:
ifeq ($(PUSH),true)
	docker build -f docker/Dockerfile -t $(IMAGE_NAME):$(TAG) --push .
else
	docker build -f docker/Dockerfile -t $(IMAGE_NAME):$(TAG) .
endif

$(APP)-test:
	uv run pytest tests

$(APP)-lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run ty check

$(APP)-format:
	uv run ruff check --fix .
	uv run ruff format .

$(APP)-typecheck:
	uv run ty check

# Local development server without Docker.
$(APP)-run:
	AUTH_MODE=none DATABASE_URL=sqlite:///./dev.db uv run uvicorn supplywatch.main:app --reload --port 8080

$(APP)-docker-up:
	$(COMPOSE) up -d --build

$(APP)-docker-down:
	$(COMPOSE) down

$(APP)-docker-logs:
	$(COMPOSE) logs -f $(APP)
