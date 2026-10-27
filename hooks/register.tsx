// voz: conversa por voz nesta sessão do Claude desktop.
//
// Lança o daemon de voz (microfone → VAD → Whisper; TTS local pt-PT) como processo filho,
// submete aqui cada frase ditada como prompt, apanha o texto da resposta em streaming
// (turn.step) e manda-o ao daemon frase a frase para ser lido, e desenha um painel
// ao lado do chat com o estado, a transcrição, o que está a ser dito e as definições.

import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { MeetingEntry, Row, VozLanguage, VozSettings, VozStatus, VozVoice, VozDevice } from '../types'

const PANE = 'voz'
const MAX_ROWS = 40
const STORE_KEY = 'voz.settings'
const OWNER_KEY = 'voz.assistant' // id da sessão que é o assistente de voz (a última onde correu /voz)

// A sessão de voz é um assistente de mãos livres sobre as outras sessões, não a que faz o trabalho.
const ASSISTENTE = `You are the user's hands-free voice assistant for the Claude Code desktop app. This session exists to talk: keep every turn short and never start long work here.
The real work happens in the user's other sessions. To know what is going on, use the session tools (load them with ToolSearch first if they are deferred): mcp__ccd_session_mgmt__list_sessions lists the other sessions with title, state, branch and PR; mcp__ccd_session_mgmt__list_events reads what a session has been doing or asking; mcp__ccd_session_mgmt__search_session_transcripts finds where something was discussed; mcp__ccd_session_mgmt__send_message (or SendMessage with the session id) delivers an instruction or an answer into a session.
When asked how things are going, read the sessions and summarise each active one in a sentence: what it is doing and whether it waits for the user. Answer any question about another session (what it did, decided, changed, asked, or where it got stuck) by reading its recent transcript with list_events, paging back if needed, or by searching the transcripts when the user does not name the session; answer from what you read, in a few spoken sentences, and say so when the transcript does not tell. When a session is waiting on the user, say its question in your own words, take the user's spoken answer and forward it with send_message, then confirm. Send a message into a session only when the user asks you to. When the user asks for new work, hand it to the right session or propose a new one; do not do it here.
Permission requests from any session already reach the user by voice through this plugin; you have nothing to do about them. Refer to sessions by title and projects by folder name; never read ids, paths or URLs aloud.`

// Instruções de estilo falado, na língua escolhida (as outras línguas usam a versão inglesa + "responde em X").
const ESTILOS: Record<string, string> = {
  pt_PT: 'Estás numa conversa por voz: o utilizador dita as mensagens e cada frase tua é dita em voz alta assim que termina. Responde em prosa falada, português de Portugal, frases curtas e completas, no máximo quatro por resposta. Não uses listas, tabelas, cabeçalhos, negrito nem blocos de código na resposta; se tiveres de mostrar código ou um comando, escreve-o num ficheiro e diz onde está. Não leias caminhos nem URLs; descreve-os. Quando precisas de uma decisão, termina com uma única pergunta clara. Não afirmes decisões que o utilizador ainda não tomou. Antes de uma acção demorada diz numa frase curta o que vais fazer.',
  pt_BR: 'Você está numa conversa por voz: o usuário dita as mensagens e cada frase sua é falada em voz alta assim que termina. Responda em prosa falada, português do Brasil, frases curtas e completas, no máximo quatro por resposta. Não use listas, tabelas, títulos, negrito nem blocos de código; se precisar mostrar código ou um comando, escreva em um arquivo e diga onde está. Não leia caminhos nem URLs; descreva-os. Quando precisar de uma decisão, termine com uma única pergunta clara. Não afirme decisões que o usuário ainda não tomou. Antes de uma ação demorada, diga em uma frase curta o que vai fazer.',
  en: 'You are in a voice conversation: the user dictates messages and each sentence you write is spoken aloud as soon as it ends. Answer in spoken prose, short complete sentences, at most four per reply. No lists, tables, headings, bold or code blocks in the reply; if you must show code or a command, write it to a file and say where it is. Do not read paths or URLs aloud; describe them. When you need a decision, end with a single clear question. Do not state decisions the user has not made. Before a long action, say in one short sentence what you are about to do.',
  es: 'Estás en una conversación por voz: el usuario dicta los mensajes y cada frase tuya se lee en voz alta en cuanto termina. Responde en prosa hablada, frases cortas y completas, como máximo cuatro por respuesta. Sin listas, tablas, títulos, negrita ni bloques de código; si debes mostrar código o un comando, escríbelo en un archivo y di dónde está. No leas rutas ni URLs; descríbelas. Cuando necesites una decisión, termina con una única pregunta clara. No afirmes decisiones que el usuario aún no ha tomado. Antes de una acción larga, di en una frase corta lo que vas a hacer.',
  fr: 'Tu es dans une conversation vocale : l’utilisateur dicte ses messages et chaque phrase que tu écris est lue à voix haute dès qu’elle se termine. Réponds en prose parlée, phrases courtes et complètes, quatre au maximum par réponse. Pas de listes, tableaux, titres, gras ni blocs de code ; si tu dois montrer du code ou une commande, écris-le dans un fichier et dis où il est. Ne lis pas les chemins ni les URL ; décris-les. Quand tu as besoin d’une décision, termine par une seule question claire. N’affirme pas de décisions que l’utilisateur n’a pas prises. Avant une action longue, dis en une phrase courte ce que tu vas faire.',
  de: 'Du bist in einem Sprachgespräch: der Nutzer diktiert Nachrichten und jeder Satz von dir wird vorgelesen, sobald er endet. Antworte in gesprochener Prosa, kurze vollständige Sätze, höchstens vier pro Antwort. Keine Listen, Tabellen, Überschriften, Fettdruck oder Codeblöcke; wenn du Code oder einen Befehl zeigen musst, schreibe ihn in eine Datei und sage, wo sie liegt. Lies keine Pfade oder URLs vor; beschreibe sie. Wenn du eine Entscheidung brauchst, ende mit einer einzigen klaren Frage. Behaupte keine Entscheidungen, die der Nutzer nicht getroffen hat. Vor einer längeren Aktion sage in einem kurzen Satz, was du tun wirst.',
  it: 'Sei in una conversazione vocale: l’utente detta i messaggi e ogni tua frase viene letta ad alta voce appena termina. Rispondi in prosa parlata, frasi brevi e complete, al massimo quattro per risposta. Niente elenchi, tabelle, titoli, grassetto o blocchi di codice; se devi mostrare codice o un comando, scrivilo in un file e di’ dove si trova. Non leggere percorsi o URL; descrivili. Quando ti serve una decisione, termina con una sola domanda chiara. Non affermare decisioni che l’utente non ha preso. Prima di un’azione lunga, di’ in una frase breve cosa stai per fare.',
}
function estiloPara(lang: string, label: string): string {
  return ESTILOS[lang] ?? `${ESTILOS['en']} Always answer in ${label || lang}.`
}

// Confirmações curtas ditas enquanto o Claude trabalha: não são uma mensagem nova.
const BACKCHANNEL = new Set(['ok', 'okay', 'sim', 'hum', 'hmm', 'certo', 'está bem', 'continua', 'boa', 'exacto', 'exato', 'pois'])
const SENT_END = /(?<=[.!?…])\s+/
// Ordens que interrompem o trabalho em curso (além do botão Parar).
const STOP_WORDS = new Set(['pára', 'para', 'parar', 'pára aí', 'stop', 'cancela', 'cancelar', 'esquece', 'claude pára', 'claude para'])

const DEFAULT_SETTINGS: VozSettings = { voice: '', sensitivity: 'media', turn_gap_ms: 1200, mode: 'auto', barge_in: true, speed: 1, language: 'pt_PT', cleanup: 'regras', input_device: '', output_device: '' }
const SPEEDS = [
  { value: '0.85', label: 'lenta' },
  { value: '1', label: 'normal' },
  { value: '1.15', label: 'rápida' },
  { value: '1.3', label: 'muito rápida' },
]

const STATUS_DEFAULT: VozStatus = {
  state: 'off', mic: '', output: '', tts: '', level: 0, ptt: false, muted: false, note: '', saying: '',
  showSettings: false, settings: DEFAULT_SETTINGS, voices: [] as VozVoice[], languages: [] as VozLanguage[], search: '', results: [] as VozVoice[],
  devices: { inputs: [] as VozDevice[], outputs: [] as VozDevice[] },
  meeting: { on: false, title: '', path: '', speakers: [] as string[], entries: [] as MeetingEntry[] },
}
const status = atom({ plugin: 'voz', key: 'status' } as const, STATUS_DEFAULT)

// O estado da sessão sobrevive a recargas do mod: um estado guardado por uma versão anterior pode não
// ter campos novos. Ler sempre por aqui, com os valores por omissão a preencher o que falta.
async function readStatus($: EngineInterface): Promise<VozStatus> {
  const s = ((await read($, status)) ?? {}) as Partial<VozStatus>
  return {
    ...STATUS_DEFAULT,
    ...s,
    settings: { ...DEFAULT_SETTINGS, ...(s.settings ?? {}) },
    meeting: { ...STATUS_DEFAULT.meeting, ...(s.meeting ?? {}) },
    voices: s.voices ?? [],
    languages: s.languages ?? [],
    results: s.results ?? [],
  }
}
const rows = atom({ plugin: 'voz', key: 'rows' } as const, [] as Row[])

type DaemonEvent =
  | { type: 'ready'; mic: string; output?: string; muted?: boolean; tts: string; mode: 'auto' | 'ptt'; port: number; settings?: VozSettings; voices?: VozVoice[] }
  | { type: 'mute'; on: boolean }
  | { type: 'keyword'; action: 'skip' | 'stop_voice' | 'abort' }
  | { type: 'abort' }
  | { type: 'state'; state: string }
  | { type: 'level'; v: number }
  | { type: 'utterance'; text: string }
  | { type: 'partial'; text: string }
  | { type: 'live'; text: string }
  | { type: 'discarded'; reason: string }
  | { type: 'spoken'; text: string }
  | { type: 'mode'; mode: 'auto' | 'ptt' }
  | { type: 'note'; text: string }
  | { type: 'fatal'; error: string }
  | { type: 'devices'; mic: string; output: string }
  | { type: 'bye'; reason: string }
  | ({ type: 'settings' } & Partial<VozSettings>)
  | { type: 'meeting'; on: boolean; title: string; path: string; speakers: string[]; entries?: number }
  | { type: 'meeting_entry'; t: string; who: string; text: string; similarity: number; new: boolean; speakers: string[] }
  | { type: 'meeting_speakers'; speakers: string[]; relabeled?: number; entries?: MeetingEntry[] }

// Estado do módulo: reinicia a cada reload (o daemon é morto com o módulo).
const st = {
  vozDir: '',
  port: 8765,
  tts: 'mlx',
  running: false,
  wanted: true,
  nextId: 1,
  activeTurn: undefined as string | undefined,
  lastLevelAt: 0,
  lastBars: -1,
  lastScrollAt: 0,
  lastLiveAt: 0,
  streamBuf: '',
  mergeBuf: '',
  spokenInTurn: 0,
  liveRowId: undefined as number | undefined,
  permission: undefined as undefined | { resolve: (text: string) => void },
  summaryMinutes: 15,
  permissionMode: '' as string, // modo de permissões da sessão, visto no último PreToolUse
  lastErr: '', // última linha de stderr do daemon, para explicar uma saída inesperada
  fatal: '', // erro fatal reportado pelo daemon (evento `fatal`)
  silencedTurn: undefined as string | undefined, // turno cuja leitura foi calada por palavra-chave
  meetingTimer: undefined as undefined | { cancel: () => void },
  meetingCount: 0,
  summarizedCount: 0,
  summaryFrom: '',
  liveText: '',
  // todas as escritas nas linhas passam por esta fila, por ordem: evita que deltas
  // concorrentes criem várias caixas ou se percam uns aos outros
  chain: Promise.resolve() as Promise<unknown>,
}

function enqueue(fn: () => Promise<unknown>): Promise<unknown> {
  st.chain = st.chain.then(fn, fn)
  return st.chain
}

// ---------- daemon: comandos e eventos ----------

async function cmd($: EngineInterface, body: Record<string, unknown>): Promise<Record<string, unknown> | undefined> {
  try {
    const r = await $.http.fetch(`http://127.0.0.1:${st.port}/cmd`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    return r.ok ? (JSON.parse(r.text) as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

function setStatus($: EngineInterface, patch: Partial<VozStatus>) {
  return update($, status, s => ({ ...s, ...patch }))
}

// Mantém o painel alinhado com a última bolha (no máximo ~4 vezes por segundo).
function scrollToEnd($: EngineInterface, force = false) {
  const now = Date.now()
  if (!force && now - st.lastScrollAt < 250) return
  st.lastScrollAt = now
  void $.ui.scroll({ in: PANE, to: 'end' }).catch(() => undefined)
}

async function addRow($: EngineInterface, kind: Row['kind'], text: string, live = false) {
  const id = st.nextId++
  await enqueue(() => update($, rows, list => [...list, { id, kind, text, live }].slice(-MAX_ROWS)))
  scrollToEnd($, true)
  return id
}

async function appendLive($: EngineInterface, text: string) {
  st.liveText += text
  if (st.liveRowId === undefined) {
    // o id é reservado já, antes de qualquer await: os deltas seguintes encontram a caixa
    const id = st.nextId++
    st.liveRowId = id
    const row: Row = { id, kind: 'claude', text: st.liveText, live: true }
    await enqueue(() => update($, rows, list => [...list, row].slice(-MAX_ROWS)))
    return
  }
  const id = st.liveRowId
  const snapshot = st.liveText
  await enqueue(() => update($, rows, list => list.map(r => (r.id === id ? { ...r, text: snapshot } : r))))
  scrollToEnd($)
}

async function endLive($: EngineInterface) {
  if (st.liveRowId === undefined) return
  const id = st.liveRowId
  const finalText = st.liveText
  st.liveRowId = undefined
  st.liveText = ''
  await enqueue(() => update($, rows, list => list.map(r => (r.id === id ? { ...r, text: finalText, live: false } : r))))
}

async function abortTurn($: EngineInterface) {
  if (st.activeTurn === undefined) return
  const turnId = st.activeTurn
  st.activeTurn = undefined
  await $.turn.abort({ turnId }).catch(() => undefined)
}

function say($: EngineInterface, text: string) {
  const t = text.trim()
  if (!t) return
  if (st.silencedTurn !== undefined && st.silencedTurn === st.activeTurn) return
  void cmd($, { type: 'say', text: t })
}

// A primeira frase de cada resposta sai logo (latência). As seguintes juntam-se em blocos
// que crescem (60, 120, 200… caracteres): cada bloco toca durante tempo suficiente para o
// seguinte ser gerado, e há cada vez menos cortes em pontos finais.
const MERGE_STEPS = [0, 60, 120, 200, 280]
function sayMerged($: EngineInterface, sentence: string, force = false) {
  const limit = MERGE_STEPS[Math.min(st.spokenInTurn, MERGE_STEPS.length - 1)] ?? 280
  st.mergeBuf = (st.mergeBuf + ' ' + sentence).trim()
  if (force || st.mergeBuf.length >= limit) {
    if (st.mergeBuf) {
      say($, st.mergeBuf)
      st.spokenInTurn++
    }
    st.mergeBuf = ''
  }
}

function flushStream($: EngineInterface) {
  if (st.streamBuf.trim()) sayMerged($, st.streamBuf, true)
  else if (st.mergeBuf) sayMerged($, '', true)
  st.streamBuf = ''
}

// definições: aplicadas ao daemon e guardadas entre sessões
async function applySettings($: EngineInterface, patch: Partial<VozSettings>) {
  const current = (await readStatus($)).settings
  const next = { ...current, ...patch }
  await setStatus($, { settings: next })
  await $.store.set(STORE_KEY, next)
  const r = await cmd($, { type: 'set', ...patch })
  if (r && typeof r['voice'] === 'string') {
    await setStatus($, { settings: { ...next, voice: r['voice'] as string, language: typeof r['language'] === 'string' ? (r['language'] as string) : next.language } })
  }
  if ('language' in patch || 'voice' in patch) {
    const v = await cmd($, { type: 'voices' })
    if (v && Array.isArray(v['voices'])) await setStatus($, { voices: v['voices'] as VozVoice[], results: [] })
  }
}

async function searchVoices($: EngineInterface, query: string) {
  await setStatus($, { search: query, note: 'a procurar vozes…' })
  const lang = (await readStatus($)).settings.language
  const r = await cmd($, { type: 'voices_search', query, lang, all: query.trim().length > 0 })
  const results = r && Array.isArray(r['voices']) ? (r['voices'] as VozVoice[]) : []
  await setStatus($, { results, note: results.length ? `${results.length} vozes encontradas` : 'nenhuma voz encontrada' })
}

async function installVoice($: EngineInterface, id: string) {
  await setStatus($, { note: 'a instalar e activar a voz…' })
  const r = await cmd($, { type: 'voice_install', id })
  const current = (await readStatus($)).settings
  const voice = r && typeof r['voice'] === 'string' ? (r['voice'] as string) : id
  const next = { ...current, voice }
  await setStatus($, { settings: next, voices: r && Array.isArray(r['voices']) ? (r['voices'] as VozVoice[]) : (await readStatus($)).voices })
  await $.store.set(STORE_KEY, next)
}

// microfones e saídas de som disponíveis (lista pedida ao daemon; muda quando se ligam auscultadores)
async function refreshDevices($: EngineInterface) {
  const r = await cmd($, { type: 'devices' })
  if (!r) return
  const lista = (k: string) => (Array.isArray(r[k]) ? (r[k] as VozDevice[]) : [])
  await setStatus($, {
    devices: { inputs: lista('inputs'), outputs: lista('outputs') },
    ...(typeof r['input'] === 'string' ? { mic: r['input'] as string } : {}),
    ...(typeof r['output'] === 'string' ? { output: r['output'] as string } : {}),
  })
}

async function restoreSettings($: EngineInterface) {
  const saved = (await $.store.get(STORE_KEY).catch(() => undefined)) as Partial<VozSettings> | undefined
  if (!saved) return
  const r = await cmd($, { type: 'set', ...saved, quiet: true }) // sem saudação: só repõe o que estava
  const current = (await readStatus($)).settings
  await setStatus($, { settings: { ...current, ...saved, ...(r && typeof r['voice'] === 'string' ? { voice: r['voice'] as string } : {}) } })
}

async function handleEvent($: EngineInterface, ev: DaemonEvent) {
  switch (ev.type) {
    case 'ready': {
      const settings = ev.settings ?? DEFAULT_SETTINGS
      await setStatus($, { state: 'listening', mic: ev.mic, output: ev.output ?? '', muted: ev.muted ?? false, ptt: false, tts: ev.tts, note: '', settings, voices: ev.voices ?? [] })
      await restoreSettings($)
      await refreshDevices($)
      const langs = await cmd($, { type: 'languages' })
      if (langs && Array.isArray(langs['languages'])) await setStatus($, { languages: langs['languages'] as VozLanguage[] })
      $.ui.toast(`voz: a ouvir (${ev.mic})`)
      break
    }
    case 'state':
      await setStatus($, ev.state === 'speaking' ? { state: 'speaking' } : { state: ev.state as VozStatus['state'], saying: '' })
      break
    case 'mute':
      // o daemon diz o que o microfone está mesmo a fazer; o botão segue-o
      await setStatus($, { muted: ev.on, state: ev.on ? 'muted' : 'listening' })
      break
    case 'keyword':
      if (ev.action === 'stop_voice' || ev.action === 'abort') st.silencedTurn = st.activeTurn // o resto desta resposta não é lido
      await addRow($, 'note', ev.action === 'skip' ? 'frase saltada' : ev.action === 'stop_voice' ? 'voz calada; o Claude continua' : 'parado a teu pedido')
      break
    case 'abort':
      await abortTurn($)
      break
    case 'level': {
      // Só redesenha quando o número de barras muda (8 níveis) e no máximo 5 vezes por segundo:
      // cada escrita redesenha a banda inteira, e redesenhos constantes fazem os botões piscar.
      const bars = Math.round(Math.min(1, Math.max(0, ev.v)) * 8)
      const now = Date.now()
      if (bars === st.lastBars || now - st.lastLevelAt < 200) return
      st.lastBars = bars
      st.lastLevelAt = now
      await setStatus($, { level: bars / 8 })
      break
    }
    case 'partial':
      await setStatus($, { note: `a ouvir: ${ev.text}` })
      break
    case 'live': {
      const now = Date.now()
      if (now - st.lastLiveAt < 250) return
      st.lastLiveAt = now
      await setStatus($, { note: `a ouvir: ${ev.text}` })
      break
    }
    case 'utterance': {
      const text = ev.text.trim()
      await setStatus($, { note: '' })
      if (!text) return
      if (st.permission) {
        // resposta por voz a um pedido de permissão do Claude
        const pending = st.permission
        st.permission = undefined
        await addRow($, 'me', text)
        pending.resolve(text)
        return
      }
      const clean = text.replace(/[.!?,\s]+$/g, '').toLowerCase()
      if (st.activeTurn !== undefined && BACKCHANNEL.has(clean)) {
        await addRow($, 'note', `"${text}" (confirmação; o Claude continua)`)
        return
      }
      if (STOP_WORDS.has(clean)) {
        // só uma ordem explícita interrompe o trabalho em curso
        void cmd($, { type: 'stop' })
        await abortTurn($)
        await addRow($, 'note', 'parado a teu pedido')
        return
      }
      await addRow($, 'me', text)
      // Falar por cima cala a voz (o daemon já o fez), mas não aborta o trabalho em curso:
      // a frase fica em fila e entra quando o turno acabar.
      if (st.activeTurn !== undefined) await setStatus($, { note: 'em fila até o Claude acabar o que está a fazer' })
      void $.prompt.submit({ text, asUser: true })
      break
    }
    case 'discarded':
      await setStatus($, { note: `ignorado: ${ev.reason}` })
      break
    case 'spoken':
      await setStatus($, { saying: ev.text })
      break
    case 'mode': {
      const current = (await readStatus($)).settings
      await setStatus($, { settings: { ...current, mode: ev.mode } })
      break
    }
    case 'note':
      await addRow($, 'note', ev.text)
      break
    case 'devices':
      await setStatus($, { mic: ev.mic, output: ev.output })
      break
    case 'settings': {
      // definições mudadas fora do painel (por voz, pelo Claude, pelo painel web): reflecte-as e guarda-as
      const { type: _t, ...defs } = ev
      const current = (await readStatus($)).settings
      const next = { ...current, ...defs }
      await setStatus($, { settings: next })
      await $.store.set(STORE_KEY, next).catch(() => undefined)
      if ('voice' in defs || 'language' in defs) {
        const v = await cmd($, { type: 'voices' })
        if (v && Array.isArray(v['voices'])) await setStatus($, { voices: v['voices'] as VozVoice[] })
      }
      break
    }
    case 'bye':
      if (ev.reason === 'handover') {
        st.wanted = false // outra sessão ficou com a voz: aqui apaga-se sem erro
        await addRow($, 'note', 'a voz passou para outra sessão')
      }
      break
    case 'fatal':
      st.fatal = ev.error
      await addRow($, 'note', `erro do daemon: ${ev.error}`)
      break
    case 'meeting': {
      const m = (await readStatus($)).meeting
      await setStatus($, { meeting: { ...m, on: ev.on, title: ev.title, path: ev.path, speakers: ev.speakers, entries: ev.on ? [] : m.entries } })
      if (ev.on) {
        $.ui.toast(`reunião "${ev.title}" a gravar: ${ev.path}`)
        st.meetingCount = 0
        st.summarizedCount = 0
        st.summaryFrom = horaAgora()
        st.meetingTimer?.cancel()
        if (st.summaryMinutes > 0) st.meetingTimer = $.clock.every(st.summaryMinutes * 60 * 1000, () => void partialSummary($))
      } else {
        st.meetingTimer?.cancel()
        st.meetingTimer = undefined
      }
      break
    }
    case 'meeting_entry': {
      const m = (await readStatus($)).meeting
      const entry: MeetingEntry = { t: ev.t, who: ev.who, text: ev.text }
      st.meetingCount++
      await setStatus($, { meeting: { ...m, speakers: ev.speakers, entries: [...m.entries, entry].slice(-200) }, note: ev.new ? `voz nova: ${ev.who}` : '' })
      scrollToEnd($, true)
      break
    }
    case 'meeting_speakers': {
      const m = (await readStatus($)).meeting
      await setStatus($, {
        meeting: { ...m, speakers: ev.speakers, entries: ev.entries ?? m.entries },
        note: ev.relabeled ? `oradores reagrupados: ${ev.relabeled} intervenções reatribuídas` : m.on ? '' : '',
      })
      break
    }
  }
}

function horaAgora(): string {
  const d = new Date()
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// Resumo intercalar: de N em N minutos, o Claude resume as intervenções novas num ficheiro ao lado da acta.
async function partialSummary($: EngineInterface) {
  const m = (await readStatus($)).meeting
  if (!m.on || !m.path || st.meetingCount <= st.summarizedCount) return
  const desde = st.summaryFrom
  const ate = horaAgora()
  const resumos = m.path.replace(/\.md$/, '') + '-resumos.md'
  st.summarizedCount = st.meetingCount
  st.summaryFrom = ate
  await addRow($, 'note', `resumo intercalar ${desde}–${ate} pedido ao Claude`)
  void $.prompt.submit({
    text:
      `Reunião "${m.title}" em curso. Lê as intervenções entre ${desde} e ${ate} no ficheiro ${m.path} e acrescenta ao ficheiro ${resumos} ` +
      `uma secção "## ${desde}–${ate}" com um resumo curto em prosa, as decisões tomadas e as tarefas com responsável, só a partir do que foi dito. ` +
      'Cria o ficheiro se não existir. Não digas nada em voz alta: responde apenas "Resumo gravado."',
  })
}

// Fim da reunião: pede ao Claude a acta formal a partir das intervenções e dos resumos intercalares.
async function endMeeting($: EngineInterface) {
  st.meetingTimer?.cancel()
  st.meetingTimer = undefined
  const r = await cmd($, { type: 'meeting', on: false })
  const path = r && typeof r['path'] === 'string' ? (r['path'] as string) : ''
  const entries = r && typeof r['entries'] === 'number' ? (r['entries'] as number) : 0
  if (!path || entries === 0) return path
  const acta = path.replace(/\.md$/, '') + '-acta.md'
  const resumos = path.replace(/\.md$/, '') + '-resumos.md'
  void $.prompt.submit({
    text:
      `A reunião terminou. Lê o ficheiro ${path} (intervenções com hora e orador) e, se existir, ${resumos} (resumos intercalares), ` +
      `e escreve a acta formal em ${acta}, em português de Portugal, com: título, data e hora, participantes, resumo em prosa, ` +
      'decisões tomadas, tarefas com responsável e prazo quando dito, e pontos em aberto. Não inventes nada que não esteja nas intervenções. ' +
      'Depois diz-me em duas ou três frases o essencial e onde ficou a acta.',
  })
  return path
}

async function pumpDaemon($: EngineInterface) {
  const child = $.process.spawn({
    argv: [`${st.vozDir}/.venv/bin/voz-daemon`, '--port', String(st.port), '--tts', st.tts],
    cwd: st.vozDir,
  })
  let buf = ''
  try {
    for await (const chunk of child) {
      if (chunk.stream === 'stderr') {
        $.ui.log(chunk.text, { to: 'debug' })
        const util = chunk.text.split('\n').map(l => l.trim()).filter(l => l && !/Warning|WARNING|transformers|resource_tracker|codebooks|tokenizer/.test(l)).pop()
        if (util) st.lastErr = util.slice(-200)
        continue
      }
      buf += chunk.text
      let i: number
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim()
        buf = buf.slice(i + 1)
        if (!line) continue
        try {
          await handleEvent($, JSON.parse(line) as DaemonEvent)
        } catch (err) {
          $.ui.log(`voz: evento inválido: ${String(err)}`, { to: 'debug' })
        }
      }
      if (!st.wanted) break
    }
  } catch (err) {
    await setStatus($, { state: 'error', note: `daemon: ${String(err)}` })
  } finally {
    st.running = false
    if (st.wanted) {
      // ninguém pediu para desligar: o daemon morreu; mantém a banda com o motivo em vez de a esconder
      const motivo = st.fatal || st.lastErr || 'terminou sem explicação'
      await setStatus($, { state: 'error', level: 0, saying: '', note: `o daemon de voz parou: ${motivo}. Usa /voz para voltar a ligar.` })
    } else {
      await setStatus($, { state: 'off', level: 0, saying: '', note: '' })
    }
    st.fatal = ''
  }
}

// Onde está o daemon: a opção `vozDir`, ou a pasta daemon/ dentro do próprio plugin.
// Na primeira vez sem .venv, corre `uv sync` lá (demora uns minutos: descarrega MLX, Whisper, Qwen3).
async function prepareDaemon($: EngineInterface): Promise<boolean> {
  if (!st.vozDir) st.vozDir = `${$.plugin.root}/daemon`
  const bin = `${st.vozDir}/.venv/bin/voz-daemon`
  if (await $.fs.exists(bin)) return true
  await setStatus($, { state: 'starting', note: 'primeira vez: a instalar o daemon de voz com uv (pode demorar uns minutos)…' })
  const home = (await $.env.get('HOME')) ?? ''
  for (const uv of ['uv', '/opt/homebrew/bin/uv', `${home}/.local/bin/uv`]) {
    try {
      const r = await $.process.run([uv, 'sync'], { cwd: st.vozDir, timeoutMs: 600000 })
      if (r.exitCode === 0 && (await $.fs.exists(bin))) return true
      $.ui.log(`voz: ${uv} sync: ${r.stderr.slice(-400)}`, { to: 'debug' })
    } catch (err) {
      $.ui.log(`voz: ${uv} indisponível: ${String(err)}`, { to: 'debug' })
    }
  }
  await setStatus($, { state: 'error', note: 'não consegui instalar o daemon: instala o uv (https://docs.astral.sh/uv/) e corre "uv sync" na pasta daemon/ do plugin' })
  return false
}

// ---------- que sessão tem a voz ----------
async function isOwner($: EngineInterface): Promise<boolean> {
  try {
    const me = await $.session.id()
    const owner = await $.store.get(OWNER_KEY)
    return typeof owner === 'string' && owner === me
  } catch {
    return false
  }
}

// Esta sessão passa a ser o assistente: o daemon de outra sessão (mesma porta) é mandado sair
// com motivo "handover", para o mod dela se apagar sem erro. Devolve true se houve mudança.
async function takeOver($: EngineInterface): Promise<boolean> {
  if (await isOwner($)) return false
  await cmd($, { type: 'quit', reason: 'handover' })
  try {
    await $.store.set(OWNER_KEY, await $.session.id())
  } catch {
    return false
  }
  return true
}

function startDaemon($: EngineInterface) {
  if (st.running) return
  st.running = true
  st.wanted = true
  st.lastErr = ''
  st.fatal = ''
  void setStatus($, { state: 'starting', note: 'a carregar Whisper e a voz…' })
  void (async () => {
    try {
      if (await prepareDaemon($)) await pumpDaemon($)
      else st.running = false
    } catch (err) {
      st.running = false
      await setStatus($, { state: 'error', note: `daemon: ${String(err)}` })
    }
  })()
}

async function stopDaemon($: EngineInterface) {
  st.wanted = false
  await cmd($, { type: 'quit' })
}

// ---------- permissões por voz ----------
const SIM = ['sim', 'pode', 'podes', 'permito', 'autorizo', 'força', 'avança', 'ok', 'okay', 'claro', 'aprovo', 'deixa', 'yes', 'go', 'vai']
const NAO = ['não', 'nao', 'nega', 'recusa', 'recuso', 'cancela', 'pára', 'para', 'nunca', 'no', 'nope', 'deny']

function descreverTool(tool: string, input: unknown): string {
  const i = (input && typeof input === 'object' ? (input as Record<string, unknown>) : {}) as Record<string, unknown>
  const nome = (v: unknown) => String(v ?? '').split('/').pop() ?? ''
  // Dito em voz alta: só o tipo de acção, nunca o conteúdo (o comando inteiro fica escrito no painel).
  if (tool === 'Bash') {
    const c = String(i['command'] ?? '').trim()
    const primeira = (c.match(/^[\w./-]+/) ?? [''])[0]
    const verbo = c.includes('sudo') ? 'um comando com privilégios de administrador' : primeira ? `um comando de terminal, ${primeira}` : 'um comando de terminal'
    return `correr ${verbo}`
  }
  if (tool === 'Edit' || tool === 'Write' || tool === 'MultiEdit' || tool === 'NotebookEdit') return `alterar o ficheiro ${nome(i['file_path'])}`
  if (tool === 'Read') return `ler o ficheiro ${nome(i['file_path'])}`
  if (tool === 'WebFetch') return 'consultar uma página web'
  if (tool === 'WebSearch') return 'pesquisar na web'
  if (tool === 'Agent' || tool === 'Task') return `lançar um subagente: ${String(i['description'] ?? '')}`
  return `usar a ferramenta ${tool}`
}

// Só uma resposta curta (até 6 palavras) que começa por sim/não conta como decisão;
// "não é interessante fazer X" é uma mensagem, não uma recusa.
function decisaoPorVoz(text: string): 'allow' | 'deny' | undefined {
  const t = text.toLowerCase().replace(/[.!?,;:]/g, ' ')
  const words = t.split(/\s+/).filter(Boolean)
  if (words.length === 0 || words.length > 6) return undefined
  const first = words[0] ?? ''
  const second = words[1] ?? ''
  const head = [first, second]
  if (head.some(w => NAO.includes(w))) return 'deny'
  if (head.some(w => SIM.includes(w))) return 'allow'
  return undefined
}

// Espera a próxima frase ditada (ou desiste ao fim de `ms`, pelo relógio do motor).
function esperarResposta($: EngineInterface, ms: number): Promise<string | undefined> {
  return new Promise(resolve => {
    let settled = false
    const done = (text: string) => {
      if (settled) return
      settled = true
      resolve(text)
    }
    st.permission = { resolve: done }
    $.clock.after(ms, () => {
      if (settled) return
      settled = true
      if (st.permission?.resolve === done) st.permission = undefined
      resolve(undefined)
    })
  })
}

// ---------- painel ----------

const LABEL: Record<string, string> = {
  off: 'Voz desligada', starting: 'A preparar a voz…', listening: 'A ouvir', recording: 'A gravar',
  transcribing: 'A transcrever', speaking: 'A falar', muted: 'Microfone em pausa', error: 'Erro na voz',
}
const LABEL_CURTO: Record<string, string> = {
  off: 'Desligada', starting: 'A preparar…', listening: 'A ouvir', recording: 'A gravar',
  transcribing: 'A transcrever', speaking: 'A falar', muted: 'Em pausa', error: 'Erro',
}
const COLOR: Record<string, string> = {
  listening: 'green', recording: 'red', transcribing: 'yellow', speaking: 'cyan',
  error: 'red', muted: 'gray', starting: 'yellow', off: 'gray',
}
const GAPS = [
  { value: '800', label: 'curta (0,8 s)' },
  { value: '1200', label: 'média (1,2 s)' },
  { value: '2000', label: 'longa (2 s)' },
  { value: '3000', label: 'muito longa (3 s)' },
]

export const register: Register = (on, options) => {
  st.vozDir = String(options['vozDir'] ?? '')
  st.port = Number(options['port'] ?? 8765)
  st.tts = String(options['tts'] ?? 'mlx')
  st.wanted = options['autoStart'] !== false
  st.summaryMinutes = Number(options['summaryMinutes'] ?? 15)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'voz',
      description: 'Voz: conversa por voz, painel, modo de reunião com acta (/voz on|off|ptt|auto|reuniao <título>[, N oradores]|fim)',
      argumentHint: 'on|off|ptt|auto|reuniao <título>[, N oradores]|fim',
    })
    const started = await next(e)
    // Só uma sessão tem o microfone e a voz: a designada com /voz (guardada entre sessões).
    // As outras ficam passivas: não falam, não ouvem; só as permissões por voz passam por elas (hook do plugin).
    if (st.wanted && (await isOwner($))) {
      void $.ui.open({ id: PANE, title: 'Voz' })
      startDaemon($)
    }
    return started
  })

  on('command.run', { command: 'voz' }, async ($, e) => {
    const arg = (e.args ?? '').trim().toLowerCase()
    if (arg === 'off') {
      await stopDaemon($)
      if (await isOwner($)) await $.store.delete(OWNER_KEY).catch(() => undefined)
      return { text: 'Voz desligada.' }
    }
    if (arg === 'ptt' || arg === 'auto') {
      await applySettings($, { mode: arg })
      return { text: `Voz em modo ${arg}.` }
    }
    if (arg.startsWith('reuni')) {
      // "/voz reuniao Título" ou "/voz reuniao Título, 3 oradores" (ou "3 pessoas")
      let rest = (e.args ?? '').trim().replace(/^\S+\s*/, '')
      let speakers: number | undefined
      const m = rest.match(/[,;]?\s*(\d+)\s*(oradores?|pessoas?|participantes?)\s*$/i)
      if (m) {
        speakers = Number(m[1])
        rest = rest.slice(0, m.index).trim()
      }
      const title = rest || 'Reunião'
      await $.ui.open({ id: PANE, title: 'Voz', focus: true })
      const r = await cmd($, { type: 'meeting', on: true, title, speakers })
      return { text: r ? `Modo de reunião ligado: "${title}"${speakers ? `, ${speakers} oradores` : ''}. Diz "/voz fim" para terminar e gerar a acta.` : 'A voz não está ligada.' }
    }
    if (arg === 'fim' || arg === 'terminar') {
      const path = await endMeeting($)
      return { text: path ? `Reunião terminada; intervenções em ${path}. A acta formal vem a seguir.` : 'Não havia reunião a decorrer.' }
    }
    await $.ui.open({ id: PANE, title: 'Voz', focus: true })
    const was = st.running
    const tomou = await takeOver($)
    startDaemon($)
    return { text: was ? 'Painel da voz aberto.' : tomou ? 'Esta sessão passa a ser o assistente de voz; a voz está a arrancar.' : 'Voz a arrancar.' }
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!st.running) return composed
    const s = await readStatus($)
    const label = s.languages.find(l => l.code === s.settings.language)?.label ?? ''
    return {
      ...composed,
      sections: [
        ...composed.sections,
        { id: 'voz:assistente', text: ASSISTENTE, scope: 'session' as const },
        { id: 'voz:estilo', text: estiloPara(s.settings.language, label), scope: 'session' as const },
      ],
    }
  })

  on('prompt.submit', async ($, e, next) => {
    // um prompt escrito por ti enquanto a voz fala: cala-a
    if (st.running && e.origin?.kind !== 'plugin') void cmd($, { type: 'stop' })
    return next(e)
  }).catch(($, e, next) => next(e)) // nunca refusa um prompt: se o hook falhar, o prompt segue

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      st.activeTurn = e.turnId
      st.silencedTurn = undefined
      st.streamBuf = ''
      st.mergeBuf = ''
      st.spokenInTurn = 0
      await endLive($)
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined || !st.running || (await readStatus($)).meeting.on) return yield* next(e)
    for await (const chunk of next(e)) {
      if (chunk.kind === 'text') {
        st.streamBuf += chunk.text
        void appendLive($, chunk.text)
        const parts = st.streamBuf.split(SENT_END)
        if (parts.length > 1) {
          for (const s of parts.slice(0, -1)) sayMerged($, s)
          st.streamBuf = parts[parts.length - 1] ?? ''
        }
      } else if (chunk.kind === 'tool') {
        // o Claude vai usar uma ferramenta: fecha a frase em curso; as chamadas em si não aparecem no painel
        flushStream($)
        void endLive($)
      } else if (chunk.kind === 'stop') {
        flushStream($)
        void endLive($)
      }
      yield chunk
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      st.activeTurn = undefined
      flushStream($)
      await endLive($)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await stopDaemon($)
    return next(e)
  })

  // O modo de permissões da sessão chega em cada PreToolUse: em "auto" o classificador decide sozinho e
  // a voz não deve perguntar nada; em "default"/"acceptEdits" as perguntas vão mesmo para ti.
  // O modo de permissões da sessão, visto em cada PreToolUse (informativo). A permissão por voz em si
  // é feita pelo hook de comando do plugin (hooks/voz-permission.py), o único caminho que a app respeita.
  on('classic.PreToolUse', ($, e, next) => {
    if (typeof e.permission_mode === 'string') st.permissionMode = e.permission_mode
    return next(e)
  }).catch(($, e, next) => next(e))

  // ---------- banda acima da caixa de texto: estado e controlos, sempre visível ----------
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await readStatus($)
    if (!st.running && s.state === 'off') return next(e)
    if (e.props.hasSurvey) return next(e)
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Select = 'Select' in els ? els.Select : undefined
    // três larguras: completa (≥ 96 colunas), compacta (≥ 64) e só ícones (< 64)
    const cols = e.viewport?.columns ?? 100
    const wide = cols >= 96
    const compact = cols < 64
    const bar = '▮'.repeat(Math.round(s.level * 8)).padEnd(8, '▯')
    const manual = s.settings.mode === 'ptt'
    const L = (icon: string, full: string, short: string) => (compact ? icon : wide ? `${icon} ${full}` : `${icon} ${short}`)
    const MODOS = [
      { value: 'auto', label: 'automático' },
      { value: 'ptt', label: 'manual' },
    ]
    const vozesInstaladas = s.voices.filter(v => v.installed !== false)
    const nomeCurto = (label: string) => label.replace(/\s*\(.*$/, '')

    return (
      <Box flexDirection="column">
        <Box justifyContent="space-between" alignItems="center" flexWrap="wrap" gap={2}>
          {/* esquerda: estado da gravação, detecção de voz, dispositivo */}
          <Box gap={2} alignItems="center">
            <Text color={COLOR[s.state] ?? 'white'} weight="bold">● {compact ? LABEL_CURTO[s.state] ?? s.state : LABEL[s.state] ?? s.state}</Text>
            <Text color={s.level > 0.05 ? 'green' : undefined} dimColor={s.level <= 0.05} mono>{bar}</Text>
            {wide ? <Text dimColor size="footnote">🎙 {s.mic || '—'}{s.output ? ` · 🔈 ${s.output}` : ''}</Text> : null}
          </Box>
          {/* direita: acções */}
          <Box gap={1} alignItems="center" flexWrap="wrap">
            {Select && vozesInstaladas.length ? (
              <Select
                key="voice-band"
                label={compact ? undefined : 'Voz'}
                options={vozesInstaladas.map(v => ({ value: v.id, label: nomeCurto(v.label) }))}
                value={vozesInstaladas.some(v => v.id === s.settings.voice) ? s.settings.voice : vozesInstaladas[0]?.id ?? ''}
                onSelect={value => void applySettings($, { voice: value })}
              />
            ) : null}
            {Select ? (
              <Select
                key="mode-band"
                label={compact ? undefined : 'Modo'}
                options={MODOS}
                value={manual ? 'ptt' : 'auto'}
                onSelect={value => void applySettings($, { mode: value as VozSettings['mode'] })}
              />
            ) : (
              <Button
                key="mode-toggle"
                variant="secondary"
                label={manual ? L('✋', 'Modo: manual', 'Manual') : L('〰', 'Modo: automático', 'Auto')}
                onPress={() => void applySettings($, { mode: manual ? 'auto' : 'ptt' })}
              />
            )}
            {manual ? (
              <Button
                key="ptt"
                variant={s.ptt ? 'primary' : 'secondary'}
                label={s.ptt ? L('⏺', 'Enviar', 'Enviar') : L('🎙', 'Gravar', 'Gravar')}
                onPress={async () => {
                  const down = !s.ptt
                  await setStatus($, { ptt: down })
                  void cmd($, { type: 'ptt', down })
                }}
              />
            ) : null}
            <Button
              key="pause"
              variant={s.muted ? 'primary' : 'secondary'}
              label={s.muted ? L('▶', 'Retomar microfone', 'Retomar') : L('⏸', 'Pausar microfone', 'Pausar')}
              onPress={async () => {
                const r = await cmd($, { type: 'mute', on: !s.muted })
                if (!r) await setStatus($, { note: 'a voz não respondeu ao pedido de pausa' })
              }}
            />
            <Button
              key="stop"
              variant="secondary"
              label={L('⏹', 'Interromper', 'Parar')}
              onPress={async () => {
                void cmd($, { type: 'stop' })
                await abortTurn($)
              }}
            />
            <Button
              key="pane"
              variant="secondary"
              label={L('☰', 'Conversa', 'Conversa')}
              onPress={async () => {
                await $.ui.open({ id: PANE, title: 'Voz', focus: true })
              }}
            />
            <Button
              key="gear"
              variant={s.showSettings ? 'primary' : 'secondary'}
              label="⚙"
              onPress={async () => {
                const show = !s.showSettings
                await setStatus($, { showSettings: show })
                if (show) void refreshDevices($)
                if (show) await $.ui.open({ id: PANE, title: 'Voz', focus: true })
              }}
            />
            <Button
              key="power"
              variant="secondary"
              dimColor
              label={st.running ? L('⏻', 'Desligar voz', 'Desligar') : L('⏻', 'Ligar voz', 'Ligar')}
              onPress={() => {
                if (st.running) void stopDaemon($)
                else startDaemon($)
              }}
            />
          </Box>
        </Box>
        {s.meeting.on ? (
          <Box gap={2} alignItems="center" flexWrap="wrap">
            <Text color="red" weight="bold">● Reunião "{s.meeting.title}"</Text>
            <Text dimColor size="footnote">{s.meeting.speakers.length} oradores · {s.meeting.entries.length} intervenções</Text>
            <Button key="meeting-end" variant="primary" label={compact ? 'Terminar' : 'Terminar e gerar acta'} onPress={() => void endMeeting($)} />
          </Box>
        ) : null}
        {s.saying ? <Text color="cyan" wrap="truncate">♪ {s.saying}</Text> : null}
        {s.note ? <Text dimColor wrap="truncate">{s.note}</Text> : null}
      </Box>
    )
  })

  // ---------- painel: conversa e definições ----------
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const Select = 'Select' in els ? els.Select : undefined // nem todas as superfícies têm Select
    const Input = 'Input' in els ? els.Input : undefined
    const s = await readStatus($)
    const list = await read($, rows)
    const voices = s.voices.length ? s.voices : [{ id: s.settings.voice || 'mlx', label: s.tts || 'voz' }]
    const voiceValue = voices.some(v => v.id === s.settings.voice) ? s.settings.voice : voices[0]?.id ?? ''
    const gapValue = GAPS.some(g => g.value === String(s.settings.turn_gap_ms)) ? String(s.settings.turn_gap_ms) : '1200'
    const speedValue = SPEEDS.reduce((best, o) => (Math.abs(Number(o.value) - s.settings.speed) < Math.abs(Number(best) - s.settings.speed) ? o.value : best), '1')
    const cut = (t: string, n: number) => (t.length <= n ? t : '…' + t.slice(t.length - n + 1))
    // a tua transcrição mostra-se inteira; só uma resposta longa do Claude é cortada pelo início
    const visible = list.filter(r => r.kind !== 'tool').slice(-8).map(r => (r.kind === 'claude' ? { ...r, text: cut(r.text, 1500) } : r))

    return (
      <Box flexDirection="column" gap={1}>
        {s.showSettings && !Select ? (
          <Text dimColor>Esta superfície não desenha listas de escolha; usa /voz ptt ou /voz auto.</Text>
        ) : null}
        {s.showSettings && Select ? (
          <Box flexDirection="column" gap={1} borderStyle="round" borderDimColor paddingX={1}>
            <Select
              key="language"
              label="Língua"
              options={(s.languages.length ? s.languages : [{ code: s.settings.language, label: s.settings.language }]).map(l => ({ value: l.code, label: l.label }))}
              value={s.settings.language}
              onSelect={value => void applySettings($, { language: value })}
            />
            <Select
              key="voice"
              label="Voz"
              options={voices.map(v => ({ value: v.id, label: v.label }))}
              value={voiceValue}
              onSelect={value => void applySettings($, { voice: value })}
            />
            <Select
              key="input-device"
              label="Microfone"
              options={[{ value: '', label: `por omissão do sistema${s.settings.input_device ? '' : ` (${s.mic || '?'})`}` }, ...s.devices.inputs.map(d => ({ value: d.id, label: d.label }))]}
              value={s.devices.inputs.some(d => d.id === s.settings.input_device) ? s.settings.input_device : ''}
              onSelect={value => void applySettings($, { input_device: value })}
            />
            <Select
              key="output-device"
              label="Saída de som"
              options={[{ value: '', label: `por omissão do sistema${s.settings.output_device ? '' : ` (${s.output || '?'})`}` }, ...s.devices.outputs.map(d => ({ value: d.id, label: d.label }))]}
              value={s.devices.outputs.some(d => d.id === s.settings.output_device) ? s.settings.output_device : ''}
              onSelect={value => void applySettings($, { output_device: value })}
            />
            <Select
              key="speed"
              label="Velocidade da voz"
              options={SPEEDS}
              value={speedValue}
              onSelect={value => void applySettings($, { speed: Number(value) })}
            />
            <Select
              key="sensitivity"
              label="Sensibilidade do microfone"
              options={[
                { value: 'baixa', label: 'baixa (só voz clara e próxima)' },
                { value: 'media', label: 'média' },
                { value: 'alta', label: 'alta (apanha voz mais baixa, mais ruído)' },
              ]}
              value={s.settings.sensitivity}
              onSelect={value => void applySettings($, { sensitivity: value as VozSettings['sensitivity'] })}
            />
            <Select
              key="gap"
              label="Pausa que fecha a mensagem"
              options={GAPS}
              value={gapValue}
              onSelect={value => void applySettings($, { turn_gap_ms: Number(value) })}
            />
            <Select
              key="mode"
              label="Modo"
              options={[
                { value: 'auto', label: 'automático (detecta quando falas)' },
                { value: 'ptt', label: 'manual (gravas com o botão)' },
              ]}
              value={s.settings.mode}
              onSelect={value => void applySettings($, { mode: value as VozSettings['mode'] })}
            />
            <Select
              key="cleanup"
              label="Limpeza de muletas"
              options={[
                { value: 'off', label: 'desligada (transcrição tal e qual)' },
                { value: 'regras', label: 'regras (hum, tipo, repetições)' },
                { value: 'modelo', label: 'regras + modelo local (correcções a meio da frase)' },
              ]}
              value={s.settings.cleanup}
              onSelect={value => void applySettings($, { cleanup: value as VozSettings['cleanup'] })}
            />
            <Select
              key="barge"
              label="Interromper a voz quando falas"
              options={[
                { value: 'on', label: 'sim' },
                { value: 'off', label: 'não' },
              ]}
              value={s.settings.barge_in ? 'on' : 'off'}
              onSelect={value => void applySettings($, { barge_in: value === 'on' })}
            />
            {Input ? (
              <Input
                key="voice-search"
                label="Procurar vozes"
                placeholder="nome, língua ou país (vazio = todas da língua actual)"
                value={s.search}
                submitLabel="procurar"
                onSubmit={value => void searchVoices($, value)}
              />
            ) : null}
            {s.results.slice(0, 12).map(v => (
              <Box key={`r:${v.id}`} gap={1} alignItems="center">
                <Button
                  key={`use:${v.id}`}
                  variant={v.installed ? 'secondary' : 'primary'}
                  label={v.installed ? 'Usar' : `Instalar${v.size_mb ? ` (${v.size_mb} MB)` : ''}`}
                  onPress={() => void installVoice($, v.id)}
                />
                <Text wrap="truncate">{v.label}</Text>
                <Text dimColor size="footnote">{v.lang}</Text>
              </Box>
            ))}
            <Text dimColor size="footnote">Vozes: Apple (as do sistema), Qwen3 (neural, 10 línguas, clone de 10 s de áudio) e Piper (178 vozes em 58 línguas, instaladas ao primeiro uso). Para clonar a tua voz: voz --gravar-ref.</Text>
          </Box>
        ) : null}

        {s.meeting.on && Input ? (
          <Box flexDirection="column" gap={1} borderStyle="round" borderColor="red" paddingX={1}>
            <Text weight="bold">Reunião "{s.meeting.title}"</Text>
            <Text dimColor size="footnote">{s.meeting.path}</Text>
            <Text dimColor size="footnote">Oradores: {s.meeting.speakers.join(', ') || '—'}. Para dar nome a uma voz diz "eu sou o João", ou escreve abaixo; "Orador 2 = Orador 1" funde os dois.</Text>
            <Input
              key="rename"
              label="Renomear"
              placeholder="Orador 2 = João"
              submitLabel="renomear"
              onSubmit={value => {
                const m = value.match(/^\s*(.+?)\s*=\s*(.+?)\s*$/)
                if (m) void cmd($, { type: 'meeting_rename', speaker: m[1], name: m[2] })
              }}
            />
            {s.meeting.entries.slice(-14).map((e2, i) => (
              <Text key={`m:${i}:${e2.t}`} wrap="wrap">
                <Text dimColor>{e2.t} </Text>
                <Text color="blue" weight="semibold">{e2.who}: </Text>
                {e2.text}
              </Text>
            ))}
          </Box>
        ) : null}
        <Box flexDirection="column" gap={1}>
          {visible.length === 0 && !s.meeting.on && <Text dimColor>Fala, ou escreve no chat. Cada frase da resposta é dita assim que fecha.</Text>}
          {visible.map(r =>
            r.kind === 'me' || r.kind === 'claude' ? (
              <Box
                key={String(r.id)}
                flexDirection="column"
                borderStyle="round"
                borderColor={r.kind === 'me' ? 'blue' : 'gray'}
                borderDimColor={r.kind !== 'me'}
                paddingX={1}
                alignSelf={r.kind === 'me' ? 'flex-end' : 'flex-start'}
              >
                <Text dimColor size="footnote">{r.kind === 'me' ? 'Tu' : 'Claude'}</Text>
                <Text wrap="wrap">{r.text}{r.live ? ' ▍' : ''}</Text>
              </Box>
            ) : (
              <Text key={String(r.id)} dimColor size="footnote">{r.kind === 'tool' ? `⚙ ${r.text}` : r.text}</Text>
            ),
          )}
        </Box>
      </Box>
    )
  })
}
