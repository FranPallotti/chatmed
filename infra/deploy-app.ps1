<#
.SYNOPSIS
Step 4 of the M5b deploy (and every image update): deploy the container app (infra/app.bicep)
with one image digest.

.DESCRIPTION
Only an image referenced by digest is accepted, so the app runs exactly the image
build-image.ps1 built (AC11). The model endpoint and deployment are the same values as in
your local .env. Prints the app URL and the image digest that is running (read back from
the app, not from this script's input).

.EXAMPLE
.\infra\deploy-app.ps1 -Image $image -DeveloperIp 203.0.113.7 -AzureOpenAiEndpoint https://my-foundry.openai.azure.com/ -AzureOpenAiDeployment my-deployment
#>
param(
    [Parameter(Mandatory)][string]$Image,
    [Parameter(Mandatory)][string]$DeveloperIp,
    [Parameter(Mandatory)][string]$AzureOpenAiEndpoint,
    [Parameter(Mandatory)][string]$AzureOpenAiDeployment,
    [string]$ResourceGroup = 'rg-chatmed-dev',
    [string]$AppName = 'ca-chatmed',
    # demo: 1 vCPU / 2 GiB, 3 sessions; rated: 2 vCPU / 4 GiB, 8 sessions (D17)
    [ValidateSet('demo', 'rated')][string]$SizeProfile = 'demo',
    # 0: scale to zero between demos (sessions wiped on scale-in); 1: always on
    [ValidateRange(0, 1)][int]$MinReplicas = 0
)
. "$PSScriptRoot/common.ps1"
Assert-Ipv4 $DeveloperIp
if ($Image -notmatch '^[a-z0-9]+\.azurecr\.io/chatmed@sha256:[0-9a-f]{64}$') {
    throw 'Image must be <registry>.azurecr.io/chatmed@sha256:<digest> (from build-image.ps1), never a tag'
}

Write-Host "Deploying $AppName..."
Invoke-Native 'app deployment' {
    az deployment group create -g $ResourceGroup -n chatmed-app `
        --parameters "$PSScriptRoot/app.bicepparam" `
        --parameters image=$Image developerIp=$DeveloperIp appName=$AppName `
            sizeProfile=$SizeProfile minReplicas=$MinReplicas `
            azureOpenAiEndpoint=$AzureOpenAiEndpoint azureOpenAiDeployment=$AzureOpenAiDeployment `
        --query properties.outputs -o jsonc
}
$running = Invoke-Native 'running image' {
    az containerapp show -g $ResourceGroup -n $AppName --query 'properties.template.containers[0].image' -o tsv
}
if ($running -ne $Image) {
    throw 'the app does not run the requested image digest'
}
Write-Host "Running image: $running"
