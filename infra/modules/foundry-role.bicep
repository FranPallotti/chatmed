// Cognitive Services OpenAI User for the app's identity on the existing Foundry resource
// (M5b AC7). A module because the Foundry resource lives in its own resource group.

param foundryResourceName string
param principalId string

var roleOpenAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryResourceName
}

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, principalId, roleOpenAiUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleOpenAiUser)
    principalId: principalId
    principalType: 'ServicePrincipal'
  }
}
