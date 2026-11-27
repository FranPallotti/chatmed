// The container app (M5b). Same names as base.bicepparam. deploy-app.ps1 passes the image
// digest, the developer IP and the model endpoint/deployment on the command line.
using './app.bicep'

param location = 'eastus2'
param appName = 'ca-chatmed'
param environmentName = 'cae-chatmed-dev'
param identityName = 'id-chatmed-app'
param registryName = 'crchatmeddev01'
param keyVaultName = 'kv-chatmed-dev-01'
param appInsightsName = 'appi-chatmed-dev'
param postgresServerName = 'psql-chatmed-dev-01'
param tenants = ['acme', 'globex']
param azureOpenAiApiVersion = '2024-10-21'
param postgresSslMode = 'verify-full'
param minReplicas = 0          // scale to zero; set 1 before a demo (docs/deploy.md)
param sizeProfile = 'demo'     // or 'rated' (2 vCPU / 4 GiB, the measured worst case)

// Filled in by deploy-app.ps1.
param image = ''
param developerIp = ''
param azureOpenAiEndpoint = ''
param azureOpenAiDeployment = ''
