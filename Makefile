.PHONY: schema

# Export the OpenAPI schema to the repo root (../schema.yml), next to the frontend. The
# container only sees this directory (mounted at /app), so write it here and move it up.
schema:
	docker compose exec backend python manage.py spectacular --file schema.yml --validate
	mv schema.yml ../schema.yml
