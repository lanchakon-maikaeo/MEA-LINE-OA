PYTHON ?= python3
UV = $(PYTHON) -m uv
ENV_FILE = $(if $(wildcard .env),--env-file .env,)
RUN = $(UV) run --locked $(ENV_FILE)

.PHONY: bootstrap guard db migrate seed-demo dev line-worker clamav n8n n8n-import eval-ai simulate-line tunnel check test test-module test-e2e security ci build smoke down \
	deploy rollback restore-drill oci-quota-check oci-stack backup-pull backup-pull-schedule image-scan rehearsal-up rehearsal-release rehearsal-down \
	load-test acceptance-tests release-candidate
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
	$(RUN) python manage.py seed_fake_inventory
	$(RUN) python manage.py load_ai_examples

dev: db migrate
	$(RUN) python manage.py runserver 127.0.0.1:8000

# Local/staging LINE turns and file scans until n8n (M04); webhook needs a public HTTPS URL.
# The worker service (Compose) runs chat turns; this follows its log.
line-worker:
	docker compose logs -f worker

clamav:
	docker compose up -d clamav

n8n:
	docker compose up -d --wait n8n

# Re-import the Django credential and workflows/n8n/*.json, publish active ones, restart n8n.
n8n-import:
	docker compose run --rm n8n-import
	docker compose restart n8n

# Held-out AI evaluation against the real provider API (not part of make ci).
eval-ai:
	$(RUN) python manage.py eval_ai $(if $(VERSION),--config-version $(VERSION),) $(if $(LIMIT),--limit $(LIMIT),) $(if $(DATASET),--dataset $(DATASET),) $(if $(PRIMARY_ONLY),--primary-only,)

# Synthetic LINE dialogues against the real bot; everything is rolled back, nothing is sent.
simulate-line: guard
	$(RUN) python manage.py simulate_line_dialogues --count $(or $(COUNT),100) --seed $(or $(SEED),1) --fail-on-findings

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

# --- M12: hosts (run on the host as root, FAOJ_ROOT=/srv/faoj) ----------------------------------
# The release is an image digest from the Release workflow; ENV must match the host's FAOJ_ENV.
FAOJ_ROOT ?= /srv/faoj
FAOJ = FAOJ_ROOT=$(FAOJ_ROOT) deploy/server/bin/faoj
host-env = @grep -qx "FAOJ_ENV=$(ENV)" $(FAOJ_ROOT)/env/compose.env || (echo "ENV=$(ENV) is not this host's FAOJ_ENV"; exit 1)

deploy:
	@test -n "$(RELEASE)" || (echo 'make deploy ENV=staging|production RELEASE=<image@sha256:…> [APPROVAL="…"]'; exit 1)
	$(host-env)
	$(FAOJ) deploy "$(RELEASE)" $(if $(APPROVAL),--approval "$(APPROVAL)",)

rollback:
	$(host-env)
	$(FAOJ) rollback $(RELEASE)

restore-drill:
	$(FAOJ) restore-drill

oci-quota-check:
	$(FAOJ) quota-check

# Zip of deploy/oci for OCI Resource Manager (Stacks → Create stack → My configuration → .zip).
oci-stack:
	@mkdir -p reports && rm -f reports/faoj-oci-stack.zip
	cd deploy/oci && zip -qr ../../reports/faoj-oci-stack.zip . -x '.terraform/*' '*.tfstate*'
	@echo "reports/faoj-oci-stack.zip"

# --- M12: this Mac (Q13 a) ------------------------------------------------------------------------
backup-pull:
	deploy/backup-pull.sh

backup-pull-schedule:
	sed -e "s#@REPO@#$(CURDIR)#" -e "s#@HOME@#$(HOME)#" deploy/launchd/th.or.mea.faoj.backup-pull.plist \
		> $(HOME)/Library/LaunchAgents/th.or.mea.faoj.backup-pull.plist
	launchctl bootstrap gui/$$(id -u) $(HOME)/Library/LaunchAgents/th.or.mea.faoj.backup-pull.plist

# Same blocking scan as the Release workflow, on locally built images (IMAGES="ref …").
image-scan:
	@for img in $(IMAGES); do docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		aquasec/trivy@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa image --quiet \
		--scanners vuln,secret --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $$img || exit 1; done

# --- M12: laptop rehearsal of the host flow (FAOJ_ROOT = a scratch directory) --------------------
rehearsal-up:
	FAOJ_ROOT=$(FAOJ_ROOT) deploy/rehearsal.sh up

rehearsal-release:
	@FAOJ_ROOT=$(FAOJ_ROOT) deploy/rehearsal.sh release

rehearsal-down:
	FAOJ_ROOT=$(FAOJ_ROOT) deploy/rehearsal.sh down

# --- M13: release candidate (PROJECT.md M13; evidence in reports/release/$(RC)/evidence) --------
RC ?= m13-rc1
EVIDENCE = reports/release/$(RC)/evidence

# E.9 baseline through the edge with k6 on a rehearsal or staging host (never production).
load-test:
	FAOJ_ROOT=$(FAOJ_ROOT) deploy/server/bin/faoj load-test $(or $(PROFILE),smoke)

# The tests deploy/acceptance.json names, one line each, for the acceptance matrix.
acceptance-tests:
	@mkdir -p $(EVIDENCE)
	$(RUN) python manage.py test --noinput -v 2 $$($(PYTHON) deploy/release_candidate.py --list-tests) \
		> $(EVIDENCE)/acceptance-tests.log 2>&1; status=$$?; tail -n 3 $(EVIDENCE)/acceptance-tests.log; exit $$status

release-candidate:
	@test -n "$(APP_IMAGE)" || (echo 'make release-candidate RC=m13-rc1 APP_IMAGE=<ref@sha256:…>'; exit 1)
	$(RUN) python deploy/release_candidate.py --name $(RC) --app-image $(APP_IMAGE)
