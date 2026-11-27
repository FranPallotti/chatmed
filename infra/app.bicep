// ChatMed container app (M5b AC6, AC9, AC11, AC12; D17). Deployed after the database is
// seeded and the image is built. Run through infra/deploy-app.ps1.
//
// - The image is referenced by digest, so the app runs exactly the image that was built.
// - At most one replica: sessions live in this process's memory (D5). minReplicas 0 by
//   default (scale to zero when idle: the process stops and every open session is wiped, as
//   on any shutdown); set it to 1 before a demo (docs/deploy.md), back to 0 after.
// - Size profile: `demo` 1 vCPU / 2 GiB (MAX_SESSIONS=3, OCR_MAX_CONCURRENCY=1) or `rated`
//   2 vCPU / 4 GiB (8 and 2), the measured worst case of architecture.md (D16, D17).
// - Each tenant reader password is a Container Apps secret referencing Key Vault through the
//   user-assigned identity, exposed as PGPASSWORD_<TENANT> (AC9). No value is in this file.
// - Ingress: HTTPS only, IP allow-list with the developer's IP; everything else is rejected
//   by the platform before the app (AC12).

param location string = resourceGroup().location

param appName string
param environmentName string
param identityName string
param registryName string
param keyVaultName string
param appInsightsName string
param postgresServerName string

@description('Image by digest: <registry>.azurecr.io/chatmed@sha256:<64 hex>, from build-image.ps1.')
param image string

@description('Public IPv4 of the developer; the only address the app accepts (AC12).')
param developerIp string

@description('Tenants this deployment serves; each needs pg-reader-<tenant> in Key Vault.')
param tenants array = [
  'acme'
  'globex'
]

param azureOpenAiEndpoint string
param azureOpenAiDeployment string
param azureOpenAiApiVersion string = '2024-10-21'

@description('verify-full checks the server certificate against the image CA bundle (D17).')
@allowed([
  'verify-full'
  'require'
])
param postgresSslMode string = 'verify-full'

@description('0 = scale to zero when idle (sessions wiped on scale-in, D5); 1 = always on.')
@minValue(0)
@maxValue(1)
param minReplicas int = 0

@description('demo: 1 vCPU / 2 GiB, 3 sessions, 1 OCR page at a time. rated: 2 vCPU / 4 GiB, 8 and 2.')
@allowed([
  'demo'
  'rated'
])
param sizeProfile string = 'demo'

param tags object = {
  project: 'chatmed'
  data: 'synthetic-only'
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresServerName
}

// Memory limits scale with the container (worst case per profile: architecture.md).
var profiles = {
  demo: {
    cpu: '1.0'
    memory: '2Gi'
    maxSessions: '3'
    ocrMaxConcurrency: '1'
  }
  rated: {
    cpu: '2.0'
    memory: '4Gi'
    maxSessions: '8'
    ocrMaxConcurrency: '2'
  }
}
var size = profiles[sizeProfile]

var sslParams = postgresSslMode == 'verify-full'
  ? 'sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt'
  : 'sslmode=require'
var dsnTemplate = 'postgresql://chatmed_reader_{tenant}@${postgres.properties.fullyQualifiedDomainName}:5432/tenant_{tenant}?${sslParams}'

// Built from the name (not keyVault.properties) so the loop below is known at deploy start.
var vaultUri = 'https://${keyVaultName}${az.environment().suffixes.keyvaultDns}/'

// Key Vault and Container Apps secret names allow no underscore; tenant ids may have one.
var readerSecrets = [
  for tenant in tenants: {
    name: 'pg-reader-${replace(tenant, '_', '-')}'
    keyVaultUrl: '${vaultUri}secrets/pg-reader-${replace(tenant, '_', '-')}'
    identity: identity.id
  }
]
var readerEnv = [
  for tenant in tenants: {
    name: 'PGPASSWORD_${toUpper(tenant)}'
    secretRef: 'pg-reader-${replace(tenant, '_', '-')}'
  }
]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false // HTTP is redirected to HTTPS, never served
        ipSecurityRestrictions: [
          {
            name: 'developer'
            description: 'Only the developer IP; every other address is denied'
            ipAddressRange: '${developerIp}/32'
            action: 'Allow'
          }
        ]
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id // AcrPull; the registry admin user is disabled
        }
      ]
      secrets: concat(readerSecrets, [
        {
          name: 'appinsights-connection-string'
          value: appInsights.properties.ConnectionString
        }
      ])
    }
    template: {
      containers: [
        {
          name: 'chatmed'
          image: image
          resources: {
            cpu: json(size.cpu)
            memory: size.memory
          }
          env: concat(
            [
              {
                name: 'AZURE_CLIENT_ID' // DefaultAzureCredential picks the user-assigned identity
                value: identity.properties.clientId
              }
              {
                name: 'AZURE_OPENAI_ENDPOINT'
                value: azureOpenAiEndpoint
              }
              {
                name: 'AZURE_OPENAI_DEPLOYMENT'
                value: azureOpenAiDeployment
              }
              {
                name: 'AZURE_OPENAI_API_VERSION'
                value: azureOpenAiApiVersion
              }
              {
                name: 'TENANT_DB_DSN_TEMPLATE'
                value: dsnTemplate
              }
              {
                name: 'TENANTS'
                value: join(tenants, ',')
              }
              {
                name: 'MAX_SESSIONS'
                value: size.maxSessions
              }
              {
                name: 'OCR_MAX_CONCURRENCY'
                value: size.ocrMaxConcurrency
              }
              {
                name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
                secretRef: 'appinsights-connection-string'
              }
            ],
            readerEnv
          )
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              initialDelaySeconds: 10
              periodSeconds: 15
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas // 0 between demos (cost), 1 during one (no cold start)
        maxReplicas: 1 // in-memory sessions (D5): a second replica would not know them
      }
    }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output image string = image
output latestRevision string = app.properties.latestRevisionName
output sizeProfile string = sizeProfile
output minReplicas int = minReplicas
