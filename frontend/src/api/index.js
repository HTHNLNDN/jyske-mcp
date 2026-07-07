export const api = {
  getAgents: () => fetch('/agents').then(r => ({ ok: r.ok, status: r.status, data: r.ok ? r.json() : null })),
  login: (pin) => fetch('/auth/login', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ pin }) }),
  logout: () => fetch('/auth/logout', { method: 'POST' }),
  getHistory: () => fetch('/history').then(r => r.json()),
  chat: (message, agentId, history) => fetch('/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, agent_id: agentId, history }),
  }),
  feedback: (traceId, score) => fetch('/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ trace_id: traceId, score }),
  }).then(r => r.json()),
  getConsentStatus: () => fetch('/consent/status').then(async r => {
    if (!r.ok) {
      const body = await r.text().catch(() => '')
      throw new Error(`GET /consent/status ${r.status}: ${body.slice(0, 300)}`)
    }
    return r.json()
  }),
  startConsent: () => fetch('/consent/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' }).then(r => r.json()),
  getTodayTip: () => fetch('/tip/today').then(r => r.json()),
  submitTipFeedback: (tipId, reasonText) => fetch('/tip/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tip_id: tipId, reason_text: reasonText }),
  }).then(r => r.json()),
  getBudgetStatus: () => fetch('/budgets/status').then(r => r.json()),
  getBudgetBreakdown: (category) =>
    fetch(`/budgets/breakdown?category=${encodeURIComponent(category)}`).then(r => r.json()),
  getBudgetTransactions: (category, { mid, uncategorized } = {}) => {
    const p = new URLSearchParams({ category })
    if (uncategorized) p.set('uncategorized', 'true')
    else if (mid != null) p.set('mid', mid)
    return fetch(`/budgets/transactions?${p}`).then(r => r.json())
  },
  getGoals: () => fetch('/goals').then(r => r.json()),
  getAuditData: (agentId) => fetch(`/audit/data?agent_id=${agentId}`).then(r => r.json()),
  triggerSync: (monthsBack) =>
    fetch('/sync/trigger', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ months_back: monthsBack ?? null }) })
      .then(r => r.json().then(data => ({ ok: r.ok, status: r.status, data }))),
  getSyncStatus: () => fetch('/sync/status').then(r => r.json()),
  getProviders: () => fetch('/providers').then(r => r.json()),
  setProviderKey: (provider, apiKey) => fetch(`/providers/${provider}/key`, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ api_key: apiKey })
  }).then(r => r.json().then(data => ({ ok: r.ok, status: r.status, data }))),
  deleteProviderKey: (provider) => fetch(`/providers/${provider}/key`, { method: 'DELETE' }).then(r => r.json()),
  setAgentModel: (agentId, model) => fetch(`/agents/${agentId}/model`, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ model })
  }).then(r => r.json().then(data => ({ ok: r.ok, status: r.status, data }))),
  getCategoryTree: () => fetch('/budgets/categories').then(r => r.json()),
  recategorize: (transactionId, categoryTop, categoryMid) => fetch('/budgets/recategorize', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ transaction_id: transactionId, category_top: categoryTop, category_mid: categoryMid }),
  }).then(r => r.json().then(data => ({ ok: r.ok, status: r.status, data }))),
}
