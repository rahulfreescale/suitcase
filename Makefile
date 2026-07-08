# Use a configurable Python so this works whether your interpreter is `python3`,
# `python`, or inside a virtualenv. Override like:  make install PY=python3.11
PY ?= python3

.PHONY: help venv up up-obs down install index ingest run eval health simulate eval-live test-injection test-email batch

help:
	@echo "make venv        - create a .venv virtual environment"
	@echo "make install     - install Python requirements (uses \$$PY -m pip)"
	@echo "make up          - start core infra (opensearch, dynamo, postgres, minio)"
	@echo "make up-obs      - start core + langfuse"
	@echo "make down        - stop all containers"
	@echo "make index       - create OpenSearch index + DynamoDB tables"
	@echo "make ingest      - extract, chunk, embed, index sample reports + load studies"
	@echo "make run         - start the FastAPI app on :8080"
	@echo "make eval        - run the RAGAS dataset evaluation (on change)"
	@echo "make simulate    - simulate production traffic"
	@echo "make eval-live   - daily live-traffic evaluation (reference-free)"
	@echo "make health      - check that every dependency is reachable"

venv:
	$(PY) -m venv .venv
	@echo "Created .venv — now run:  source .venv/bin/activate  (Windows: .venv\\Scripts\\activate)"

install:    ; $(PY) -m pip install -r requirements.txt
up:         ; docker compose --profile core up -d
up-obs:     ; docker compose --profile core --profile observability up -d
down:       ; docker compose --profile core --profile observability down
index:      ; $(PY) -m scripts.create_opensearch_index && $(PY) -m scripts.create_dynamo_table
ingest:     ; $(PY) -m ingest.run_ingest
run:        ; $(PY) -m uvicorn app.api.main:app --host 0.0.0.0 --port 8080 --reload
eval:       ; $(PY) -m eval.run_ragas
health:     ; $(PY) -m scripts.healthcheck
simulate:   ; $(PY) -m eval.simulate_traffic --n 30 --concurrency 4
eval-live:  ; $(PY) -m eval.live_traffic_eval
test-injection: ; $(PY) -m eval.test_injection
test-email: ; $(PY) -m eval.test_email_security
batch:      ; ./scripts/run_daily_batch.sh

simulate-users:  ## emulate multi-user production traffic (5 users x 3 turns)
	python3 -m eval.simulate_users --users 5 --per-user 3 --sleep 1

demo-memory:  ## multi-turn demo proving conversation memory resolves references
	python3 -m eval.demo_memory

worker:  ## start a distributed worker (run several in separate terminals)
	python3 -m app.worker

demo-distributed:  ## fire concurrent jobs to prove the worker pool parallelizes
	python3 -m eval.demo_distributed --n 9

demo-cache:  ## prove the semantic cache: paraphrases hit, different questions miss
	python3 -m eval.demo_cache

cache-index:  ## create the OpenSearch semantic_cache index
	python3 -c "from app.stores.cache import create_cache_index; create_cache_index()"

demo-stream:  ## watch live SSE streaming (needs API + a worker running)
	python3 -m eval.demo_stream

demo-gateway:  ## show model routing + A/B assignment
	python3 -m eval.demo_gateway

sim-ab:  ## dry-run an A/B experiment (scenario=model|prompt)
	python3 -m eval.simulate_ab --scenario $(scenario)

smoke:  ## end-to-end smoke test (API must be running)
	python3 -m eval.smoke_test

test:  ## run unit tests (fast, no services needed)
	python3 -m pytest tests/ -q

test-verbose:  ## run unit tests verbosely
	python3 -m pytest tests/ -v
