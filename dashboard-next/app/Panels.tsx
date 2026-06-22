'use client'

import { useState, useEffect, useRef, type CSSProperties } from 'react'
import type { Activity } from '@/lib/activity'

const PANEL_BG = 'rgba(7,17,13,0.90)'
const BORDER = '#173026'
const MOSS = '#3d6b48'
const EM = '#10b981'
const GOLD = '#c89b3c'
const EMB = '#7af2c0'
const DIM = 'rgba(207,234,224,0.45)'
const CONV_BTN: CSSProperties = { background: 'transparent', border: `1px solid ${BORDER}`, borderRadius: 6, color: MOSS, padding: '4px 9px', fontSize: 11, cursor: 'pointer', fontFamily: 'ui-monospace, monospace' }

type Health = { services: { name: string; port: number; up: boolean }[]; up: number; total: number } | null
type GProp = { id: string; title: string; category: string; state: string; verdict: string | null; description?: string; action?: string; evidence?: string; reasoning?: string; comment?: string }
type Proposals = { proposals: GProp[]; count: number } | null
type Ledger = { tiers: Record<string, any>; paused: boolean | null; min_clean: number; max_false_block?: number } | null

const SVC_NAMES = ['gateway', 'orchestrator', 'coder', 'worker', 'memory', 'rag_api', 'voice', 'dashboard']
function activeServices(activity: Activity | null): Set<string> {
  const active = new Set<string>()
  if (!activity) return active
  for (const f of activity.recent || []) {
    const g = (f.node || '').split('/')[0]
    active.add(SVC_NAMES.includes(g) ? g : g === 'shared' ? 'cron' : 'orchestrator')
  }
  if (activity.status && activity.status !== 'idle' && activity.agent) {
    active.add(SVC_NAMES.includes(activity.agent) ? activity.agent : 'orchestrator')
  }
  return active
}

function Systems({ health, activity }: { health: Health; activity: Activity | null }) {
  if (!health) return <div style={{ color: DIM }}>…</div>
  const active = activeServices(activity)
  return (
    <div>
      <div style={{ color: health.up === health.total ? EM : GOLD, marginBottom: 12, letterSpacing: '0.08em' }}>
        {health.up}/{health.total} services up{active.size ? `  ·  ${active.size} firing` : ''}
      </div>
      {health.services.map((s) => {
        const firing = s.up && active.has(s.name)
        return (
          <div key={s.name} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0' }}>
            <span
              style={
                firing
                  ? { width: 8, height: 8, borderRadius: 99, animation: 'svcFlicker 1.1s ease-in-out infinite' }
                  : { width: 8, height: 8, borderRadius: 99, background: s.up ? EM : '#8b3a3a', boxShadow: s.up ? `0 0 7px ${EM}` : 'none' }
              }
            />
            <span style={{ color: firing ? '#7af2c0' : s.up ? '#cfeae0' : DIM, flex: 1 }}>{s.name}</span>
            <span style={{ color: DIM, fontSize: 11 }}>:{s.port}</span>
          </div>
        )
      })}
    </div>
  )
}

function ActivityList({ activity }: { activity: Activity | null }) {
  if (!activity) return <div style={{ color: DIM }}>…</div>
  const working = activity.status !== 'idle'
  const recent = [...(activity.recent || [])].reverse()
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ width: 9, height: 9, borderRadius: 99, background: working ? EM : MOSS, boxShadow: working ? `0 0 9px ${EM}` : 'none' }} />
        <span style={{ color: working ? EMB : MOSS, letterSpacing: '0.08em' }}>{activity.status}</span>
      </div>
      <div style={{ color: DIM, marginBottom: 14, fontSize: 11.5, lineHeight: 1.4 }}>{activity.thought}</div>
      <div style={{ color: MOSS, fontSize: 10, letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 6 }}>recent firings</div>
      {recent.length === 0 && <div style={{ color: DIM }}>quiet…</div>}
      {recent.map((f, i) => (
        <div key={f.key + i} style={{ display: 'flex', gap: 8, padding: '3px 0', fontSize: 11.5 }}>
          <span style={{ color: MOSS, minWidth: 56 }}>{f.t}</span>
          <span style={{ color: '#cfeae0' }}>{f.job}</span>
          <span style={{ color: MOSS }}>→</span>
          <span style={{ color: EMB }}>{f.node.split('/').pop()}</span>
        </div>
      ))}
    </div>
  )
}

function Detail({ label, text }: { label: string; text: string }) {
  return (
    <div style={{ marginBottom: 6 }}>
      <span style={{ color: MOSS, fontSize: 9.5, letterSpacing: '0.1em', textTransform: 'uppercase' }}>{label}</span>
      <div style={{ color: '#cfeae0', whiteSpace: 'pre-wrap', marginTop: 2 }}>{text.length > 700 ? text.slice(0, 700) + '…' : text}</div>
    </div>
  )
}

const gateBtn = (color: string): CSSProperties => ({
  flex: 1, background: 'transparent', border: `1px solid ${color}`, borderRadius: 6, color,
  padding: '6px 0', fontSize: 11, cursor: 'pointer', fontFamily: 'ui-monospace, monospace',
})

function Gate({ proposals, reload }: { proposals: Proposals; reload: () => void }) {
  const [open, setOpen] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  if (!proposals) return <div style={{ color: DIM }}>…</div>

  const act = async (id: string, action: 'approve' | 'dismiss') => {
    if (busy) return
    setBusy(true)
    try {
      await fetch('/api/proposals', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ id, action, reason: action === 'dismiss' ? reason : undefined }),
      })
    } catch {}
    setBusy(false)
    setOpen(null)
    setReason('')
    reload()
  }

  return (
    <div>
      <div style={{ color: EM, marginBottom: 12, letterSpacing: '0.08em' }}>{proposals.count} active in queue</div>
      {proposals.count === 0 && <div style={{ color: DIM }}>queue empty</div>}
      {proposals.proposals.map((p) => {
        const expanded = open === p.id
        return (
          <div key={p.id} style={{ padding: '8px 0', borderBottom: `1px solid ${BORDER}` }}>
            <div onClick={() => { setOpen(expanded ? null : p.id); setReason('') }} style={{ cursor: 'pointer' }}>
              <div style={{ display: 'flex', gap: 8, marginBottom: 3, alignItems: 'center' }}>
                <span style={{ color: GOLD, fontSize: 10, letterSpacing: '0.06em', textTransform: 'uppercase' }}>{p.category}</span>
                <span style={{ color: p.verdict === 'pass' ? EM : p.verdict === 'fail' ? '#8b3a3a' : MOSS, fontSize: 10 }}>{p.verdict || p.state}</span>
                <span style={{ color: MOSS, fontSize: 10, marginLeft: 'auto' }}>{expanded ? '▾' : '▸'}</span>
              </div>
              <div style={{ color: '#cfeae0', fontSize: 12, lineHeight: 1.35 }}>{p.title}</div>
            </div>
            {expanded && (
              <div style={{ marginTop: 8, fontSize: 11.5, color: DIM, lineHeight: 1.5 }}>
                {p.description && <Detail label="what" text={p.description} />}
                {p.action && <Detail label="action" text={p.action} />}
                {p.evidence && <Detail label="evidence" text={p.evidence} />}
                {p.reasoning && <Detail label="her reasoning" text={p.reasoning} />}
                {p.comment && <Detail label="gate verdict" text={p.comment} />}
                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="why? (dismiss reason → teaches Roux)"
                  style={{ width: '100%', marginTop: 8, background: 'rgba(0,0,0,0.35)', border: `1px solid ${BORDER}`, borderRadius: 6, color: '#cfeae0', padding: '6px 8px', fontSize: 11, fontFamily: 'ui-monospace, monospace', outline: 'none', boxSizing: 'border-box' }}
                />
                <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                  <button onClick={() => act(p.id, 'approve')} disabled={busy} style={gateBtn(EM)}>✓ approve</button>
                  <button onClick={() => act(p.id, 'dismiss')} disabled={busy} style={gateBtn('#8b3a3a')}>✕ dismiss</button>
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function NodeLogPanel({ node, onClose }: { node: string; onClose: () => void }) {
  const [pos, setPos] = useState({ x: 360, y: 90 })
  const [lines, setLines] = useState<string[]>([])
  const [source, setSource] = useState('')
  const drag = useRef<{ dx: number; dy: number } | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    const poll = () =>
      fetch(`/api/node-logs?node=${encodeURIComponent(node)}`)
        .then((r) => r.json())
        .then((d) => { if (alive) { setLines(d.lines || []); setSource(d.source || '') } })
        .catch(() => {})
    poll()
    const id = setInterval(poll, 2500)
    return () => { alive = false; clearInterval(id) }
  }, [node])
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }) }, [lines])
  useEffect(() => {
    const move = (e: MouseEvent) => { if (drag.current) setPos({ x: e.clientX - drag.current.dx, y: e.clientY - drag.current.dy }) }
    const up = () => { drag.current = null }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
  }, [])

  return (
    <div style={{ position: 'fixed', left: pos.x, top: pos.y, width: 460, height: 320, zIndex: 30, background: 'rgba(4,9,7,0.96)', border: `1px solid ${EM}`, borderRadius: 9, boxShadow: '0 0 26px rgba(16,185,129,0.22)', display: 'flex', flexDirection: 'column', fontFamily: 'ui-monospace, monospace' }}>
      <div
        onMouseDown={(e: any) => { drag.current = { dx: e.clientX - pos.x, dy: e.clientY - pos.y }; e.preventDefault() }}
        style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 11px', borderBottom: `1px solid ${BORDER}`, cursor: 'move', background: 'rgba(16,185,129,0.06)', borderRadius: '9px 9px 0 0' }}
      >
        <span style={{ color: EM, fontSize: 9, letterSpacing: '0.12em', textTransform: 'uppercase' }}>terminal</span>
        <span style={{ color: '#cfeae0', fontSize: 12 }}>{node}</span>
        <span style={{ color: DIM, fontSize: 9, marginLeft: 'auto', maxWidth: 150, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{source}</span>
        <button onClick={onClose} style={{ background: 'transparent', border: 'none', color: MOSS, cursor: 'pointer', fontSize: 14, padding: 0, lineHeight: 1 }}>✕</button>
      </div>
      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '8px 11px', fontSize: 10.5, color: '#8fdcc0', lineHeight: 1.45, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {lines.length === 0 ? <span style={{ color: DIM }}>no recent output…</span> : lines.map((l, i) => <div key={i}>{l}</div>)}
      </div>
    </div>
  )
}

function LedgerPanel({ ledger }: { ledger: Ledger }) {
  if (!ledger) return <div style={{ color: DIM }}>…</div>
  const tiers = Object.entries(ledger.tiers || {})
  const target = ledger.min_clean || 25
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
        <span style={{ width: 9, height: 9, borderRadius: 99, background: ledger.paused ? '#8b3a3a' : EM, boxShadow: ledger.paused ? 'none' : `0 0 9px ${EM}` }} />
        <span style={{ color: ledger.paused ? '#c98b8b' : EMB, letterSpacing: '0.1em', textTransform: 'uppercase', fontSize: 11 }}>
          autonomy {ledger.paused ? 'paused' : 'live'}
        </span>
        <span style={{ marginLeft: 'auto', color: DIM, fontSize: 10 }}>graduate at {target} clean · 0 false-pass</span>
      </div>
      {tiers.length === 0 && <div style={{ color: DIM }}>no ledger events yet</div>}
      {tiers.map(([tier, s]: [string, any]) => {
        const correct = s.correct || 0
        const pct = Math.min(100, (correct / target) * 100)
        const ready = s.graduation_ready
        const disq = (s.false_pass || 0) > 0
        return (
          <div key={tier} style={{ padding: '10px 0', borderBottom: `1px solid ${BORDER}` }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
              <span style={{ color: '#cfeae0', fontSize: 12 }}>{tier}</span>
              <span style={{ marginLeft: 'auto', fontSize: 9.5, color: ready ? EM : disq ? '#8b3a3a' : GOLD, letterSpacing: '0.07em', textTransform: 'uppercase' }}>
                {ready ? '✓ graduated' : disq ? '✕ disqualified' : 'earning'}
              </span>
            </div>
            <div style={{ height: 6, borderRadius: 99, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
              <div style={{ width: `${pct}%`, height: '100%', background: ready ? EM : disq ? '#8b3a3a' : GOLD, transition: 'width 0.5s ease' }} />
            </div>
            <div style={{ fontSize: 10.5, color: DIM, marginTop: 4 }}>
              {correct}/{target} clean{disq ? ` · ${s.false_pass} false-pass` : ''}{s.false_block_rate != null ? ` · ${Math.round(s.false_block_rate * 100)}% false-block` : ''}
            </div>
            {Array.isArray(s.blockers) && s.blockers.length > 0 && (
              <div style={{ marginTop: 5 }}>
                {s.blockers.map((b: string, i: number) => (
                  <div key={i} style={{ fontSize: 10.5, color: b.includes('FALSE-PASS') ? '#c98b8b' : MOSS, lineHeight: 1.4 }}>· {b}</div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function LeftDeck({ health, activity, proposals, ledger, panelOpacity, reloadProposals }: { health: Health; activity: Activity | null; proposals: Proposals; ledger: Ledger; panelOpacity: number; reloadProposals: () => void }) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'systems' | 'activity' | 'gate' | 'ledger'>('systems')
  const W = 330
  return (
    <>
      <div
        style={{
          position: 'fixed', top: 0, left: 0, height: '100vh', width: W, zIndex: 20,
          background: `rgba(7,17,13,${panelOpacity})`, backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)',
          borderRight: `1px solid ${BORDER}`,
          transform: open ? 'translateX(0)' : `translateX(-${W}px)`,
          transition: 'transform 0.34s cubic-bezier(.4,0,.2,1)',
          display: 'flex', flexDirection: 'column', fontFamily: 'ui-monospace, monospace', color: '#cfeae0',
        }}
      >
        <div style={{ padding: '18px 18px 12px' }}>
          <div style={{ fontSize: 11, letterSpacing: '0.22em', textTransform: 'uppercase', color: EMB }}>RouxYou · deck</div>
        </div>
        <div style={{ display: 'flex', borderBottom: `1px solid ${BORDER}` }}>
          {(['systems', 'activity', 'gate', 'ledger'] as const).map((tb) => (
            <button
              key={tb}
              onClick={() => setTab(tb)}
              style={{
                flex: 1, padding: '10px 0', fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase',
                background: tab === tb ? 'rgba(16,185,129,0.08)' : 'transparent',
                color: tab === tb ? EMB : MOSS, border: 'none',
                borderBottom: tab === tb ? `2px solid ${EM}` : '2px solid transparent',
                cursor: 'pointer', fontFamily: 'inherit',
              }}
            >
              {tb}
            </button>
          ))}
        </div>
        <div style={{ overflowY: 'auto', padding: 16, flex: 1, fontSize: 12 }}>
          {tab === 'systems' && <Systems health={health} activity={activity} />}
          {tab === 'activity' && <ActivityList activity={activity} />}
          {tab === 'gate' && <Gate proposals={proposals} reload={reloadProposals} />}
          {tab === 'ledger' && <LedgerPanel ledger={ledger} />}
        </div>
      </div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          position: 'fixed', top: '50%', left: open ? W : 0, transform: 'translateY(-50%)',
          transition: 'left 0.34s cubic-bezier(.4,0,.2,1)', zIndex: 21,
          background: PANEL_BG, border: `1px solid ${BORDER}`, borderLeft: 'none', borderRadius: '0 9px 9px 0',
          color: EM, padding: '16px 7px', cursor: 'pointer',
          writingMode: 'vertical-rl', fontSize: 11, letterSpacing: '0.18em', fontFamily: 'ui-monospace, monospace',
        }}
      >
        {proposals && proposals.count > 0 && !open && (
          <span
            style={{
              position: 'absolute', top: -7, right: -7, minWidth: 17, height: 17, borderRadius: 99,
              background: GOLD, color: '#06100c', fontSize: 10, fontWeight: 700,
              display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 3px',
              writingMode: 'horizontal-tb', animation: 'badgePulse 1.6s ease-in-out infinite',
            }}
          >
            {proposals.count}
          </span>
        )}
        {open ? '◂ HIDE' : 'DECK ▸'}
      </button>
    </>
  )
}

type Msg = { role: 'you' | 'roux'; text: string; thinking?: { stages: string[]; reasoning: string } }

function extractThink(raw: string): string {
  const m = raw.match(/<think(?:ing)?>([\s\S]*?)<\/think(?:ing)?>/i)
  return m ? m[1].trim() : ''
}
function stripThink(raw: string): string {
  return raw.replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '').trim()
}

function Bubble({ role, text, stage, thinking, defaultOpen }: { role: 'you' | 'roux'; text: string; stage?: string; thinking?: { stages: string[]; reasoning: string }; defaultOpen?: boolean }) {
  const [showThink, setShowThink] = useState(!!defaultOpen)
  const mine = role === 'you'
  const hasThink = !!thinking && (thinking.stages.length > 0 || !!thinking.reasoning)
  return (
    <div style={{ alignSelf: mine ? 'flex-end' : 'flex-start', maxWidth: '88%' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 3, flexDirection: mine ? 'row-reverse' : 'row' }}>
        <span style={{ fontSize: 9.5, letterSpacing: '0.12em', textTransform: 'uppercase', color: mine ? DIM : GOLD }}>
          {mine ? 'you' : 'roux'}
        </span>
        {hasThink && (
          <button
            onClick={() => setShowThink((s) => !s)}
            style={{ background: 'transparent', border: 'none', color: showThink ? EMB : MOSS, fontSize: 10, letterSpacing: '0.05em', cursor: 'pointer', padding: 0, fontFamily: 'ui-monospace, monospace' }}
          >
            {showThink ? '▾ thinking' : '▸ thinking'}
          </button>
        )}
      </div>
      {hasThink && showThink && (
        <div style={{ borderLeft: `2px solid ${BORDER}`, paddingLeft: 9, marginBottom: 6, fontSize: 11.5, color: DIM, lineHeight: 1.5 }}>
          {thinking!.stages.length > 0 && (
            <div style={{ marginBottom: thinking!.reasoning ? 6 : 0 }}>
              {thinking!.stages.map((s, i) => <div key={i} style={{ color: MOSS }}>· {s}</div>)}
            </div>
          )}
          {thinking!.reasoning && <div style={{ whiteSpace: 'pre-wrap', fontStyle: 'italic' }}>{thinking!.reasoning}</div>}
        </div>
      )}
      <div
        style={{
          background: mine ? 'rgba(255,255,255,0.04)' : 'rgba(16,185,129,0.07)',
          border: `1px solid ${mine ? BORDER : 'rgba(16,185,129,0.25)'}`,
          borderRadius: 9, padding: '8px 11px', color: '#dcefe6', fontSize: 13, lineHeight: 1.5,
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}
      >
        {text}
        {stage && <div style={{ color: EMB, fontSize: 11, marginTop: text ? 6 : 0, opacity: 0.85 }}>▸ {stage}</div>}
      </div>
    </div>
  )
}

function CompanionChat() {
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [stage, setStage] = useState('')
  const [draft, setDraft] = useState('')
  const [draftThink, setDraftThink] = useState('')
  const [convs, setConvs] = useState<any[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [showSaved, setShowSaved] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)

  // her conversation lives server-side; rehydrate the active one + the saved list on mount
  const loadActive = () =>
    fetch('/api/conversation').then((r) => r.json()).then((d) => {
      const msgs: Msg[] = (d.messages || [])
        .map((m: any) => ({
          role: (m.role === 'user' ? 'you' : 'roux') as 'you' | 'roux',
          text: m.content || m.text || '',
          thinking: m.metadata?.thinking ? { stages: [], reasoning: m.metadata.thinking } : undefined,
        }))
        .filter((m: Msg) => m.text)
      setMessages(msgs)
    }).catch(() => {})
  const loadConvs = () =>
    fetch('/api/conversations').then((r) => r.json()).then((d) => { setConvs(d.conversations || []); setActiveId(d.active_id || null) }).catch(() => {})
  const convAct = (action: string, extra: any = {}) =>
    fetch('/api/conversations', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action, ...extra }) }).then((r) => r.json())
  const newChat = async () => { await convAct('new'); setMessages([]); setShowSaved(false); loadConvs() }
  const switchTo = async (id: string) => { await convAct('activate', { id }); await loadActive(); setShowSaved(false); loadConvs() }
  const togglePin = async (id: string, pinned: boolean) => { await convAct('pin', { id, pinned: !pinned }); loadConvs() }
  const delConv = async (id: string) => { await convAct('delete', { id }); if (id === activeId) setMessages([]); loadConvs() }
  const pinActive = async () => { if (!activeId) return; const c = convs.find((x) => x.id === activeId); await togglePin(activeId, !!c?.pinned) }

  useEffect(() => { loadActive(); loadConvs() }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages, draft, draftThink, stage])

  const send = async () => {
    const msg = input.trim()
    if (!msg || streaming) return
    setMessages((m) => [...m, { role: 'you', text: msg }])
    setInput('')
    setStreaming(true)
    setStage('reaching Roux…')
    setDraft('')
    setDraftThink('')
    let raw = ''
    let response = ''
    let doneThink = ''
    let reasoningAcc = ''
    const stages: string[] = []
    try {
      const res = await fetch('/api/companion', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      })
      const reader = res.body!.getReader()
      const dec = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const frames = buf.split('\n\n')
        buf = frames.pop() || ''
        for (const f of frames) {
          const ev = (f.match(/event: (.*)/) || [])[1]?.trim()
          const dm = f.match(/data: ([\s\S]*)/)
          if (!dm) continue
          let data: any = {}
          try { data = JSON.parse(dm[1]) } catch {}
          if (ev === 'status') { const s = data.detail || data.stage || ''; if (s) { stages.push(s); setStage(s) } }
          else if (ev === 'reasoning') { reasoningAcc += data.text || ''; setDraftThink(reasoningAcc) } // live reasoning channel
          else if (ev === 'token') { raw += data.text || ''; setDraft(raw) }
          else if (ev === 'done') { response = data.response || response; doneThink = data.thinking || doneThink }
          else if (ev === 'error') response = '⚠ ' + (data.detail || 'error')
        }
      }
    } catch {
      response = response || '⚠ connection lost'
    }
    const reasoning = doneThink || reasoningAcc || extractThink(raw) // server thinking → live channel → inline fallback
    const answer = response || stripThink(raw) || raw || '…'
    const thinking = stages.length || reasoning ? { stages, reasoning } : undefined
    setMessages((m) => [...m, { role: 'roux', text: answer, thinking }])
    setStreaming(false)
    setStage('')
    setDraft('')
    setDraftThink('')
    loadConvs() // refresh titles/list (her first reply auto-titles the conversation)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ display: 'flex', gap: 6, padding: '8px 10px', borderBottom: `1px solid ${BORDER}`, alignItems: 'center' }}>
        <button onClick={newChat} style={CONV_BTN} title="new chat">+ new</button>
        <button onClick={pinActive} style={CONV_BTN} title="pin current chat">📌</button>
        <button onClick={() => setShowSaved((s) => !s)} style={{ ...CONV_BTN, marginLeft: 'auto', color: showSaved ? EMB : MOSS }} title="saved chats">saved ▾</button>
      </div>
      {showSaved && (
        <div style={{ maxHeight: 220, overflowY: 'auto', borderBottom: `1px solid ${BORDER}`, background: 'rgba(0,0,0,0.25)' }}>
          {convs.length === 0 && <div style={{ color: DIM, padding: 10, fontSize: 12 }}>no saved chats yet</div>}
          {convs.map((c) => (
            <div
              key={c.id}
              onClick={() => switchTo(c.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 7, padding: '7px 10px', cursor: 'pointer',
                borderBottom: '1px solid rgba(23,48,38,0.5)',
                background: c.id === activeId ? 'rgba(16,185,129,0.08)' : 'transparent',
              }}
            >
              <span onClick={(e) => { e.stopPropagation(); togglePin(c.id, !!c.pinned) }} title="pin" style={{ color: c.pinned ? GOLD : MOSS, fontSize: 11, cursor: 'pointer' }}>
                {c.pinned ? '📌' : '○'}
              </span>
              <span style={{ flex: 1, color: '#cfeae0', fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.title || 'untitled'}</span>
              <span style={{ color: DIM, fontSize: 10 }}>{c.msg_count}</span>
              <span onClick={(e) => { e.stopPropagation(); delConv(c.id) }} title="delete" style={{ color: '#8b3a3a', fontSize: 12, cursor: 'pointer', padding: '0 2px' }}>✕</span>
            </div>
          ))}
        </div>
      )}
      <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: 14, display: 'flex', flexDirection: 'column', gap: 11, fontFamily: 'ui-monospace, monospace' }}>
        {messages.length === 0 && !streaming && (
          <div style={{ color: DIM, fontSize: 12, marginTop: 6, lineHeight: 1.55 }}>
            Talk to Roux. She runs on v5.3, local — give her a second to think. You&apos;ll see her stages, then her words.
          </div>
        )}
        {messages.map((m, i) => <Bubble key={i} role={m.role} text={m.text} thinking={m.thinking} />)}
        {streaming && <Bubble role="roux" text={draft} stage={stage} thinking={draftThink ? { stages: [], reasoning: draftThink } : undefined} defaultOpen />}
      </div>
      <div style={{ borderTop: `1px solid ${BORDER}`, padding: 10, display: 'flex', gap: 8 }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') send() }}
          placeholder={streaming ? 'Roux is thinking…' : 'message Roux…'}
          disabled={streaming}
          style={{
            flex: 1, background: 'rgba(0,0,0,0.35)', border: `1px solid ${BORDER}`, borderRadius: 7,
            color: '#cfeae0', padding: '9px 11px', fontSize: 13, fontFamily: 'ui-monospace, monospace', outline: 'none',
          }}
        />
        <button
          onClick={send}
          disabled={streaming || !input.trim()}
          style={{
            background: streaming || !input.trim() ? 'transparent' : 'rgba(16,185,129,0.14)',
            border: `1px solid ${streaming || !input.trim() ? BORDER : EM}`, borderRadius: 7,
            color: streaming || !input.trim() ? MOSS : EM, padding: '0 14px', cursor: streaming || !input.trim() ? 'default' : 'pointer',
            fontFamily: 'ui-monospace, monospace', fontSize: 15,
          }}
        >
          →
        </button>
      </div>
    </div>
  )
}

export function RightDeck({ status, panelOpacity }: { status: string; panelOpacity: number }) {
  const [open, setOpen] = useState(false)
  const working = status && status !== 'idle'
  const W = 380
  return (
    <>
      <div
        style={{
          position: 'fixed', top: 0, right: 0, height: '100vh', width: W, zIndex: 20,
          background: `rgba(7,17,13,${panelOpacity})`, backdropFilter: 'blur(10px)', WebkitBackdropFilter: 'blur(10px)',
          borderLeft: `1px solid ${BORDER}`,
          transform: open ? 'translateX(0)' : `translateX(${W}px)`,
          transition: 'transform 0.34s cubic-bezier(.4,0,.2,1)',
          display: 'flex', flexDirection: 'column', fontFamily: 'ui-monospace, monospace',
        }}
      >
        <div style={{ padding: '16px 18px', borderBottom: `1px solid ${BORDER}`, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ width: 9, height: 9, borderRadius: 99, background: working ? EM : MOSS, boxShadow: working ? `0 0 9px ${EM}` : 'none' }} />
          <span style={{ fontSize: 14, color: '#e6f4ee', fontWeight: 600 }}>Roux</span>
          <span style={{ fontSize: 10, color: DIM, marginLeft: 'auto', letterSpacing: '0.1em' }}>v5.3 · local</span>
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          <CompanionChat />
        </div>
      </div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          position: 'fixed', top: '50%', right: open ? W : 0, transform: 'translateY(-50%)',
          transition: 'right 0.34s cubic-bezier(.4,0,.2,1)', zIndex: 21,
          background: PANEL_BG, border: `1px solid ${BORDER}`, borderRight: 'none', borderRadius: '9px 0 0 9px',
          color: EM, padding: '16px 7px', cursor: 'pointer',
          writingMode: 'vertical-rl', fontSize: 11, letterSpacing: '0.18em', fontFamily: 'ui-monospace, monospace',
        }}
      >
        {open ? 'HIDE ▸' : '◂ CHAT'}
      </button>
    </>
  )
}
