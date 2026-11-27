# Shared helpers for the M5b infra scripts. Windows PowerShell 5.1 and PowerShell 7.
# Dot-sourced by the scripts next to it; never run on its own.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 may not offer TLS 1.2 by default; Key Vault requires it.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-Native {
    <#
    .SYNOPSIS
    Runs a native command (az, docker, git) and stops on a non-zero exit code, which
    PowerShell 5.1 does not do by itself. The error names the step, never the command line
    (it may hold a secret) or the output.
    #>
    param(
        [Parameter(Mandatory)][string]$Step,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    $output = & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE)"
    }
    return $output
}

function Assert-Ipv4 {
    param([Parameter(Mandatory)][string]$Ip)
    $parsed = $null
    if (-not ([System.Net.IPAddress]::TryParse($Ip, [ref]$parsed)) -or
        $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $Ip -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
        throw "DeveloperIp must be a single public IPv4 address, e.g. 203.0.113.7"
    }
}

function Assert-Tenant {
    # Same rule as the app (config.TENANT_ID, D13a): the tenant goes into role and DB names.
    param([Parameter(Mandatory)][string]$Tenant)
    if ($Tenant -cnotmatch '^[a-z0-9_]+$') {
        throw "tenant ids must match ^[a-z0-9_]+$"
    }
}

function Get-ReaderSecretName {
    # Key Vault secret names allow no underscore; app.bicep maps tenants the same way.
    param([Parameter(Mandatory)][string]$Tenant)
    return "pg-reader-$($Tenant.Replace('_', '-'))"
}
