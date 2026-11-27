// Names for rg-chatmed-dev (M5b). Registry, Key Vault and Postgres names are global: change the
// "01" suffix if one is taken. No secrets, IPs or e-mail addresses here; deploy-base.ps1 passes
// developerIp, developerPrincipalId and the Foundry resource on the command line.
using './base.bicep'

param location = 'eastus2'
param registryName = 'crchatmeddev01'
param logAnalyticsName = 'log-chatmed-dev'
param appInsightsName = 'appi-chatmed-dev'
param keyVaultName = 'kv-chatmed-dev-01'
param identityName = 'id-chatmed-app'
param environmentName = 'cae-chatmed-dev'
param postgresServerName = 'psql-chatmed-dev-01'
param postgresAdminLogin = 'chatmed_admin'
param postgresVersion = '17'
param postgresSkuName = 'Standard_B1ms'
param postgresStorageGb = 32

// Filled in by deploy-base.ps1.
param foundryResourceName = ''
param foundryResourceGroup = ''
param developerPrincipalId = ''
param developerIp = ''
