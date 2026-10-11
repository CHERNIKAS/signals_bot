param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDatabaseUrl
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param(
        [string]$Path = ".env"
    )

    $map = @{}
    if (-not (Test-Path $Path)) {
        return $map
    }

    foreach ($lineRaw in Get-Content $Path) {
        $line = $lineRaw.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }

        $idx = $line.IndexOf("=")
        if ($idx -lt 1) {
            continue
        }

        $key = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1)
        $map[$key] = $value
    }

    return $map
}

if ($SourceDatabaseUrl -match "\$\{\{") {
    throw "SourceDatabaseUrl contains unresolved template placeholders (`${{...}}`). Pass a real PostgreSQL URL."
}

$envMap = Read-DotEnv
$pgDb = if ($envMap.ContainsKey("POSTGRES_DB")) { $envMap["POSTGRES_DB"] } else { "signal_bot" }
$pgUser = if ($envMap.ContainsKey("POSTGRES_USER")) { $envMap["POSTGRES_USER"] } else { "signal_bot" }
$pgPassword = if ($envMap.ContainsKey("POSTGRES_PASSWORD")) { $envMap["POSTGRES_PASSWORD"] } else { "signal_bot_pass" }

$targetDatabaseUrl = "postgresql://${pgUser}:${pgPassword}@postgres:5432/${pgDb}"

Write-Host "[MIGRATE] Starting local postgres service..."
docker compose up -d postgres
if ($LASTEXITCODE -ne 0) {
    throw "Failed to start postgres service via docker compose."
}

$containerId = (docker compose ps -q postgres).Trim()
if (-not $containerId) {
    throw "Postgres container not found after startup."
}

Write-Host "[MIGRATE] Waiting for postgres healthcheck..."
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    $status = (docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}" $containerId).Trim()
    if ($status -eq "healthy") {
        $healthy = $true
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $healthy) {
    throw "Postgres did not become healthy in time."
}

$migrateCmd = 'set -e; pg_dump --no-owner --no-privileges --clean --if-exists --file=/tmp/transfer.sql "$SOURCE_DATABASE_URL"; psql "$TARGET_DATABASE_URL" --file=/tmp/transfer.sql'

Write-Host "[MIGRATE] Copying schema + data into docker postgres..."
docker compose run --rm --no-deps `
    -e SOURCE_DATABASE_URL=$SourceDatabaseUrl `
    -e TARGET_DATABASE_URL=$targetDatabaseUrl `
    postgres sh -lc $migrateCmd
if ($LASTEXITCODE -ne 0) {
    throw "Database migration failed. Check source URL reachability/credentials."
}

Write-Host "[MIGRATE] Done."
Write-Host "[MIGRATE] Target DB: $targetDatabaseUrl"
