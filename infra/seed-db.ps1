<#
.SYNOPSIS
Step 2 of the M5b deploy: create and seed one database and one read-only role per tenant on
the Azure PostgreSQL server (AC9, AC10), with the same db/schema and db/seed files as locally.

.DESCRIPTION
For each tenant:
  1. stops for that tenant if tenant_<t> already exists ("already seeded", nothing changed);
  2. generates a random reader password in memory and writes it straight to Key Vault as
     pg-reader-<t> (REST call from this process, nothing printed);
  3. creates (or resets) the role chatmed_reader_<t> with that password, creates the database,
     lets only that role connect, and applies db/schema + db/seed in one transaction;
  4. connects as the reader over the same TLS mode to check the password and TLS work.

psql runs from the postgres:17 image plus ca-certificates (built here once, from no build
context), so nothing is installed on Windows. Secrets reach the container as environment
variables named on the docker command line without a value (docker copies them from this
process), psql reads the reader password with \getenv, and Key Vault is written over its REST
API from this process: no secret is ever on a command line, printed, or written to a file.
psql error output is terse (no statement text).

-SslMode verify-full (default) checks the server certificate against Debian's CA bundle. If it
fails, the error is printed; rerun with -SslMode require and record the error in D17.

.EXAMPLE
.\infra\seed-db.ps1
#>
param(
    [string]$ResourceGroup = 'rg-chatmed-dev',
    [string]$ServerName = 'psql-chatmed-dev-01',
    [string]$KeyVaultName = 'kv-chatmed-dev-01',
    [string]$AdminLogin = 'chatmed_admin',
    [string[]]$Tenants = @('acme', 'globex'),
    [ValidateSet('verify-full', 'require')][string]$SslMode = 'verify-full'
)
. "$PSScriptRoot/common.ps1"

$psqlImage = 'chatmed-psql:17'
$dbDir = (Resolve-Path "$PSScriptRoot/../db").Path
foreach ($tenant in $Tenants) {
    Assert-Tenant $tenant
    if (-not (Test-Path "$dbDir/seed/tenant_$tenant.sql")) {
        throw "no seed file db/seed/tenant_$tenant.sql"
    }
}

function New-ReaderPassword {
    # 32 random bytes as hex: 256 bits, no characters that need quoting anywhere.
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

function Set-KeyVaultSecret {
    <#
    Writes a secret with the Key Vault REST API from this process. `az keyvault secret set
    --value` would put the value on a command line (visible in the process list) and print it
    back. The token and the value stay in memory; the response is discarded.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value,
        [string]$ContentType = ''
    )
    $token = Invoke-Native 'Key Vault token' {
        az account get-access-token --resource https://vault.azure.net --query accessToken -o tsv
    }
    $body = @{ value = $Value; contentType = $ContentType } | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod -Method Put -ContentType 'application/json' -Body $body `
            -Uri "https://$KeyVaultName.vault.azure.net/secrets/${Name}?api-version=7.4" `
            -Headers @{ Authorization = "Bearer $token" } | Out-Null
    } catch {
        # The status only: the exception text could quote the request.
        throw "writing Key Vault secret $Name failed (HTTP $($_.Exception.Response.StatusCode.value__)); a new role assignment can take a few minutes, then rerun"
    }
}

function Invoke-Psql {
    <#
    Runs psql in the helper image against $Database. SQL comes on stdin, so nothing (and no
    quoting) passes through the Windows command line. Credentials come from this process's
    environment: PGUSER, PGPASSWORD, READER_PW are named, never valued, in the docker args.
    #>
    param(
        [Parameter(Mandatory)][string]$Database,
        [string]$Sql = '',
        [string[]]$PsqlArgs = @()
    )
    $dockerArgs = @(
        'run', '--rm', '-i',
        '-e', 'PGHOST', '-e', 'PGUSER', '-e', 'PGPASSWORD', '-e', 'READER_PW',
        '-e', 'PGSSLMODE', '-e', 'PGSSLROOTCERT', '-e', 'PGCONNECT_TIMEOUT',
        '-v', "${dbDir}:/db:ro",
        $psqlImage,
        'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=terse',
        '-v', 'SHOW_CONTEXT=never', '-d', $Database
    ) + $PsqlArgs
    # LF only: a CR would end up inside a psql backslash-command argument.
    $output = ($Sql -replace "`r", '') | & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "psql on $Database failed (exit code $LASTEXITCODE); see the psql error above"
    }
    return $output
}

Write-Host "Building $psqlImage (postgres:17 + ca-certificates, no build context)..."
$psqlDockerfile = "FROM postgres:17`nRUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*`n"
Invoke-Native 'psql helper image' { $psqlDockerfile | docker build -q -t $psqlImage - } | Out-Null

$fqdn = Invoke-Native 'server host' {
    az postgres flexible-server show -g $ResourceGroup -n $ServerName --query fullyQualifiedDomainName -o tsv
}

$env:PGHOST = $fqdn
$env:PGUSER = $AdminLogin
$env:PGSSLMODE = $SslMode
$env:PGSSLROOTCERT = '/etc/ssl/certs/ca-certificates.crt'
$env:PGCONNECT_TIMEOUT = '15'
try {
    # Admin password: Key Vault -> this process's environment only.
    $env:PGPASSWORD = Invoke-Native 'admin password from Key Vault' {
        az keyvault secret show --vault-name $KeyVaultName --name pg-admin --query value -o tsv
    }

    foreach ($tenant in $Tenants) {
        $db = "tenant_$tenant"
        $role = "chatmed_reader_$tenant"
        $exists = Invoke-Psql -Database 'postgres' -PsqlArgs @('-tA', '-v', "db=$db") `
            -Sql "SELECT 1 FROM pg_database WHERE datname = :'db';"
        if ("$exists".Trim() -eq '1') {
            Write-Host "${db}: already seeded, nothing changed. To seed it again, drop it first (docs/deploy.md)."
            continue
        }

        Write-Host "${db}: generating the reader password and writing it to Key Vault..."
        $env:READER_PW = New-ReaderPassword
        Set-KeyVaultSecret -Name (Get-ReaderSecretName $tenant) -Value $env:READER_PW `
            -ContentType "password of $role"

        Write-Host "${db}: creating the role and the database..."
        $setup = @'
\getenv pw READER_PW
SELECT format(CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role')
                   THEN 'ALTER ROLE %I LOGIN PASSWORD %L'
                   ELSE 'CREATE ROLE %I LOGIN PASSWORD %L' END, :'role', :'pw') \gexec
\unset pw
CREATE DATABASE :"db";
REVOKE CONNECT, TEMPORARY ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"role";
'@
        Invoke-Psql -Database 'postgres' -PsqlArgs @('-v', "db=$db", '-v', "role=$role") -Sql $setup | Out-Null

        Write-Host "${db}: applying db/schema and db/seed in one transaction..."
        Invoke-Psql -Database $db -PsqlArgs @(
            '-1', '-v', "reader=$role",
            '-f', '/db/schema/tenant_schema.sql', '-f', "/db/seed/$db.sql"
        ) | Out-Null

        # The app's path: the reader role, its own password, the same TLS mode.
        $adminPassword = $env:PGPASSWORD
        $env:PGUSER = $role
        $env:PGPASSWORD = $env:READER_PW
        try {
            $count = Invoke-Psql -Database $db -PsqlArgs @('-tA') `
                -Sql 'SELECT count(*) FROM field_definitions;'
        } finally {
            $env:PGUSER = $AdminLogin
            $env:PGPASSWORD = $adminPassword
            $adminPassword = $null
            Remove-Item Env:READER_PW -ErrorAction SilentlyContinue
        }
        Write-Host "${db}: seeded; $role reads $("$count".Trim()) field definitions over sslmode=$SslMode."
    }
} finally {
    foreach ($name in 'PGPASSWORD', 'READER_PW', 'PGHOST', 'PGUSER', 'PGSSLMODE', 'PGSSLROOTCERT', 'PGCONNECT_TIMEOUT') {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
Write-Host 'Next: infra/build-image.ps1'
