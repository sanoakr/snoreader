HOST ?= $(shell cat .host 2>/dev/null)

.PHONY: dev backend frontend build cert prod

dev:
	@echo "Starting backend and frontend..."
	$(MAKE) -j2 backend frontend

backend:
	cd backend && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd frontend && npm run dev -- --host

build:
	cd frontend && npm run build

cert:
	@test -n "$(HOST)" || { echo "Usage: make cert HOST=your-host.ts.net"; exit 1; }
	mkdir -p certs
	cd certs && tailscale cert $(HOST)
	echo "$(HOST)" > .host
	@echo "Certificate saved to certs/. Hostname stored in .host"

prod:
	@test -n "$(HOST)" || { echo "Set HOST or run 'make cert HOST=...' first"; exit 1; }
	$(MAKE) build
	cd backend && .venv/bin/uvicorn app.main:app \
		--host 0.0.0.0 --port 443 \
		--ssl-certfile ../certs/$(HOST).crt \
		--ssl-keyfile ../certs/$(HOST).key
