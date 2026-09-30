# Export the OpenAPI schema to the repo root (../schema.yml; PowerShell equivalent of
# `make schema`). The container only sees this directory (mounted at /app), so write it
# here and move it up.
Push-Location $PSScriptRoot
try {
    docker compose exec backend python manage.py spectacular --file schema.yml --validate
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Move-Item -Force schema.yml ../schema.yml
} finally {
    Pop-Location
}
