async function call(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = await res.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {}
    throw new Error(detail)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  status: () => call('GET', '/api/status'),
  emails: () => call('GET', '/api/emails'),
  samples: () => call('GET', '/api/samples'),
  addEmail: (email) => call('POST', '/api/emails', email),
  decide: (id, decision) => call('POST', `/api/emails/${encodeURIComponent(id)}/decision`, decision),
  correctTriage: (id, label) =>
    call('POST', `/api/emails/${encodeURIComponent(id)}/triage-correction`, { label }),
  feedback: (id, text) => call('POST', `/api/emails/${encodeURIComponent(id)}/feedback`, { text }),
  prompts: () => call('GET', '/api/prompts'),
  activatePrompt: (name, version) => call('POST', `/api/prompts/${name}/activate`, { version }),
  rollbackPrompt: (name) => call('POST', `/api/prompts/${name}/rollback`),
  facts: (q) => call('GET', '/api/facts' + (q ? `?q=${encodeURIComponent(q)}` : '')),
}
