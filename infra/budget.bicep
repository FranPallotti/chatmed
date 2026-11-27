// Monthly cost budget for the resource group (M5b AC14, D17): USD 25, e-mail alerts at 80 %
// and 100 % of actual cost. An alert, not a cap: Azure does not stop resources at the budget.
// Deployed once by infra/deploy-base.ps1 -BudgetEmail; the e-mail is never committed.

param budgetName string = 'chatmed-dev-monthly'
param amount int = 25

@description('Who gets the alerts. Passed at deploy time only.')
param contactEmail string

@description('First day of the current month; a budget must start on the 1st.')
param startDate string = utcNow('yyyy-MM-01')

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [contactEmail]
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [contactEmail]
      }
    }
  }
}
