// ChatMed base resources (M5b AC6, AC7, AC8; D17). Deployed first, before the database is
// seeded and the image is built. Every name is a parameter; nothing secret is in this file or
// in base.bicepparam. Run through infra/deploy-base.ps1.
//
// Least privilege (AC7): the app's user-assigned identity gets exactly AcrPull (registry),
// Key Vault Secrets User (vault) and Cognitive Services OpenAI User (Foundry resource). The
// developer gets Key Vault Secrets Officer on the vault, only to write the generated passwords.

@description('Region of every resource; eastus2, next to the Foundry resource.')
param location string = resourceGroup().location

param registryName string
param logAnalyticsName string
param appInsightsName string
param keyVaultName string
param identityName string
param environmentName string
param postgresServerName string

@description('Existing Foundry (Azure OpenAI) resource and its resource group.')
param foundryResourceName string
param foundryResourceGroup string

@description('Object id of the developer (az ad signed-in-user show --query id).')
param developerPrincipalId string

@description('Public IPv4 of the developer: Postgres firewall, needed for seeding (AC8).')
param developerIp string

param postgresAdminLogin string = 'chatmed_admin'

@secure()
@description('Generated at deploy time and written to Key Vault (pg-admin) by this template. Never passed on a command line; every redeploy rotates it (D17).')
param postgresAdminPassword string = newGuid()

param postgresVersion string = '17'
param postgresSkuName string = 'Standard_B1ms'
param postgresStorageGb int = 32

param tags object = {
  project: 'chatmed'
  data: 'synthetic-only'
}

// Built-in role definition ids (same in every tenant).
var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var roleKeyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleKeyVaultSecretsOfficer = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false // pulls use the managed identity (AcrPull), never a password
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id // workspace-based
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true // RBAC mode: no access policies
    softDeleteRetentionInDays: 7 // the minimum; purged on delete (docs/deploy.md)
    publicNetworkAccess: 'Enabled'
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresServerName
  location: location
  tags: tags
  sku: {
    name: postgresSkuName
    tier: 'Burstable'
  }
  properties: {
    version: postgresVersion
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
    storage: {
      storageSizeGB: postgresStorageGb
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled' // POC (AC8); production: VNet + private access (D17)
    }
  }
}

// A server accepts one change at a time, so the firewall rules and the setting go in order.
resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource allowDeveloper 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'developer'
  properties: {
    startIpAddress: developerIp
    endIpAddress: developerIp
  }
  dependsOn: [allowAzureServices]
}

resource requireTls 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgres
  name: 'require_secure_transport'
  properties: {
    value: 'ON'
    source: 'user-override'
  }
  dependsOn: [allowDeveloper]
}

resource adminPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'pg-admin'
  properties: {
    value: postgresAdminPassword
    contentType: 'PostgreSQL admin password (${postgresAdminLogin}); read only by infra/seed-db.ps1'
  }
  dependsOn: [postgres] // written once the server has accepted it
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    // No workloadProfiles: a Consumption-only environment.
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        // Read by ARM at deploy time; never a parameter and never an output (D17).
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, roleAcrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, roleKeyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource developerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, developerPrincipalId, roleKeyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsOfficer)
    principalId: developerPrincipalId
    principalType: 'User'
  }
}

module foundryRole 'modules/foundry-role.bicep' = {
  name: 'chatmed-foundry-role'
  scope: resourceGroup(foundryResourceGroup)
  params: {
    foundryResourceName: foundryResourceName
    principalId: identity.properties.principalId
  }
}

// Names and ids only: no key, password or connection string is ever an output.
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output keyVaultName string = keyVault.name
output identityName string = identity.name
output environmentName string = environment.name
output appInsightsName string = appInsights.name
output postgresServerName string = postgres.name
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
