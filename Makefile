.PHONY: pgbouncer_dev guvicorn_run

pgbouncer_dev:
	docker rm -f pgbouncer 2>/dev/null || true
	mkdir -p ~/pgbouncer/config ~/pgbouncer/logs
	docker run -d \
		--name pgbouncer \
		--add-host=host.docker.internal:host-gateway \
		-p 6400:5432 \
		-e AUTH_TYPE=scram-sha-256 \
		-e DB_HOST=host.docker.internal \
		-e DB_PORT=$(DATABASE_PORT) \
		-e DB_USER=$(DATABASE_USER) \
		-e DB_PASSWORD=$(DATABASE_PASSWORD) \
		-e DB_NAME=$(DATABASE_NAME) \
		-e PGBOUNCER_POOL_MODE=transaction \
		-e DEFAULT_POOL_SIZE=20 \
		-e MIN_POOL_SIZE=5 \
		-e POOL_MODE=transaction \
		-e MAX_CLIENT_CONN=100 \
		-e DEFAULT_POOL_SIZE=20 \
		edoburu/pgbouncer:latest

guvicorn_run:
	gunicorn shikshalokam_mohini.wsgi:application \
	    --workers 4 \
	    --threads 4 \
	    --worker-class gthread \
	    --bind 0.0.0.0:8000 \
	    --timeout 120 \
	    --graceful-timeout 30 \
	    --keep-alive 5 \
	    --access-logfile - \
	    --error-logfile -

gotenberg_up:
	docker run -d \
        -p 3003:3000 --rm \
        gotenberg/gotenberg:8
