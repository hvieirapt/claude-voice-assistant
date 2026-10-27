import { expect, test } from 'claude-code/testing'

// O hook classic.PermissionRequest do mod: sem daemon a correr (st.running falso) passa a pergunta ao diálogo.
test('sem voz ligada, a permissão segue para o diálogo', async ($, on) => {
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.PermissionRequest', () => ({}))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r = await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: { command: 'ls' } } as never)
  expect(r).toBeDefined()
})
