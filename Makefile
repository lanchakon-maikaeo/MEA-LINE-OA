PYTHON ?= python3
UV = $(PYTHON) -m uv
ENV_FILE = $(if $(wildcard .env),--env-file .env,)
RUN = $(UV) run --locked $(ENV_FILE)

.PHONY: bootstrap guard db migrate seed-demo dev line-worker clamav tunnel check test test-module test-e2e security ci build smoke down
bootstrap:
	$(PYTHON) scripts/bootstrap_env.py
	$(UV) sync --locked
	$(MAKE) guard
	$(RUN) playwright install chromium

guard:
	$(RUN) python manage.py check_demo_environment

db: guard
	docker compose up -d --wait db

migrate: guard
	$(RUN) python manage.py migrate --noinput

seed-demo: guard
	$(RUN) python manage.py load_districts
	$(RUN) python manage.py seed_demo_units
	$(RUN) python manage.py seed_demo_cases

dev: db migrate
	$(RUN) python manage.py runserver 127.0.0.1:8000

# Local/staging LINE turns and file scans until n8n (M04); webhook needs a public HTTPS URL.
# Runs in the web container so attachments share its private-media volume and reach clamav.
line-worker:
	docker compose exec web /app/.venv/bin/python manage.py process_line_inbox

clamav:
	docker compose up -d clamav

tunnel:
	cloudflared tunnel --url http://127.0.0.1:8000

check:
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) python manage.py check --fail-level WARNING
	$(RUN) python manage.py makemigrations --check --dry-run

test:
	$(RUN) python manage.py test --noinput

test-module:
	$(RUN) python manage.py test_module --module "$(MODULE)" --noinput

test-e2e:
	$(RUN) python scripts/e2e.py

security:
	$(RUN) python scripts/security.py

ci:
	$(PYTHON) scripts/ci.py

build: guard
	docker compose build web

smoke:
	@test -n "$(BASE_URL)" || (echo 'Specify BASE_URL, e.g. http://127.0.0.1:8000'; exit 1)
	$(RUN) python scripts/smoke.py "$(BASE_URL)"

down: guard
	docker compose down
