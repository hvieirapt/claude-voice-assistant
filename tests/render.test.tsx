import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'voz',
  component: 'Pane',
  requestId: 'voz',
  props: {
    title: 'Voz',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const BAND = {
  plugin: 'voz',
  component: 'AbovePrompt',
  props: { hasSurvey: false },
} as const

const LISTENING = {
  state: 'listening', mic: 'AirPods', tts: 'mlx/joana-pt', level: 0.3, ptt: false, muted: false, note: 'a ouvir: olá', saying: 'Olá.',
  showSettings: true,
  settings: { voice: 'mlx:ref:x.wav', sensitivity: 'media', turn_gap_ms: 1200, mode: 'auto', barge_in: true, speed: 1, language: 'pt_PT', cleanup: 'regras' },
  voices: [{ id: 'mlx:ref:x.wav', label: 'joana-pt (clone)' }],
  languages: [{ code: 'pt_PT', label: 'Português (Portugal)' }],
  search: '',
  results: [{ id: 'piper:pt_PT-tugão-medium', label: 'tugão (Piper)', lang: 'pt_PT', installed: false, size_mb: 63 }],
  meeting: { on: true, title: 'Teste', path: '/tmp/acta.md', speakers: ['Orador 1'], entries: [{ t: '10:00:00', who: 'Orador 1', text: 'Olá a todos.' }] },
}

test('sem sessão designada a voz fica passiva: a banda não se desenha', async ($, on) => {
  mock.store(on, {})
  on('session.id', () => ({ value: 'sessao-a' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  // nada desenha por baixo do plugin: este hook faz de motor
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await band.find({ type: 'Button', key: 'gear' })).toBeUndefined()
  expect(await band.find({ type: 'Text', text: 'engine' })).toBeDefined()
  await band.unmount()
})

test('na sessão designada a voz arranca: a banda e o painel desenham-se', async ($, on) => {
  mock.store(on, { 'voz.assistant': 'sessao-voz' })
  on('session.id', () => ({ value: 'sessao-voz' }))
  on('command.register', () => ({ value: { command: 'voice-assistant' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))

  // o arranque tenta lançar o daemon; sem fs/process no kit cai em estado "error", que a banda desenha
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })

  for (const surface of ['desktop', 'terminal'] as const) {
    const pane = await $.ui.mount({ ...PANE, surface })
    expect(await pane.find({ type: 'Text', text: /Fala|Voz|voz/ })).toBeDefined()
    await pane.unmount()
    const band = await $.ui.mount({ ...BAND, surface })
    expect(await band.find({ type: 'Button', key: 'gear' })).toBeDefined()
    await band.press({ key: 'gear' })
    await band.unmount()
  }
})
