<#
.SYNOPSIS
Step 1 of the M5b deploy: base resources (infra/base.bicep) and, once, the budget.

.DESCRIPTION
Creates the resource group, then the registry, Log Analytics + App Insights, Key Vault, the
user-assigned identity and its three roles, the Container Apps environment and PostgreSQL
(AC6-AC8). The Postgres admin password is generated inside the deployment and written to Key
Vault (pg-admin); it never passes through this script. Redeploying rotates it (D17).
Prints the deployment outputs: names and the Postgres host only, no secrets.

.EXAMPLE
.\infra\deploy-base.ps1 -DeveloperIp 203.0.113.7 -FoundryResourceName my-foundry -FoundryResourceGroup rg-foundry -BudgetEmail me@example.com
#>
param(
    [Parameter(Mandatory)][string]$DeveloperIp,
    [Parameter(Mandatory)][string]$FoundryResourceName,
    [Parameter(Mandatory)][string]$FoundryResourceGroup,
    [string]$ResourceGroup = 'rg-chatmed-dev',
    [string]$Location = 'eastus2',
    # Only on the first deploy: a budget's start date cannot move afterwards.
    [string]$BudgetEmail = ''
)
. "$PSScriptRoot/common.ps1"
Assert-Ipv4 $DeveloperIp

Invoke-Native 'resource group' {
    az group create -n $ResourceGroup -l $Location --tags project=chatmed data=synthetic-only --output none
}
$developerId = Invoke-Native 'signed-in user' { az ad signed-in-user show --query id -o tsv }

Write-Host "Deploying base resources into $ResourceGroup (several minutes; Postgres is the slow part)..."
Invoke-Native 'base deployment' {
    az deployment group create -g $ResourceGroup -n chatmed-base `
        --parameters "$PSScriptRoot/base.bicepparam" `
        --parameters developerIp=$DeveloperIp developerPrincipalId=$developerId `
            foundryResourceName=$FoundryResourceName foundryResourceGroup=$FoundryResourceGroup `
        --query properties.outputs -o jsonc
}

if ($BudgetEmail) {
    Invoke-Native 'budget' {
        az deployment group create -g $ResourceGroup -n chatmed-budget `
            --template-file "$PSScriptRoot/budget.bicep" --parameters contactEmail=$BudgetEmail `
            --output none
    }
    Write-Host 'Budget: USD 25/month, e-mail alerts at 80% and 100% of actual cost.'
}
Write-Host 'Next: infra/seed-db.ps1 (role assignments can take a few minutes to apply).'
