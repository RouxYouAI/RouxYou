import { NextRequest, NextResponse } from 'next/server'
import { readFile, readdir } from 'fs/promises'
import path from 'path'

export const dynamic = 'force-dynamic'
const LOGS = path.resolve(process.cwd(), '..', 'logs')

// Click a node → its terminal. Maps a node id (e.g. "shared/shadow_verifier", "coder/coder")
// to a log file (logs/<name>.log). Direct-tail if a dedicated log exists; else grep mentions
// of the module name across the busy logs so every node shows *something* real.
export async function GET(req: NextRequest) {
  const node = (req.nextUrl.searchParams.get('node') || '').trim()
  if (!node) return NextResponse.json({ source: '', lines: [], mode: 'none' })
  const last = node.split('/').pop() || node
  const group = node.split('/')[0] || ''
  try {
    const files = await readdir(LOGS)
    const logset = new Set(files.filter((f) => f.endsWith('.log')))
    const candidates = [`${last}.log`, `${group}.log`].filter((c) => logset.has(c))
    if (candidates.length) {
      const raw = await readFile(path.join(LOGS, candidates[0]), 'utf-8')
      const lines = raw.split('\n').filter(Boolean).slice(-100)
      return NextResponse.json({ source: candidates[0], lines, mode: 'direct' })
    }
    // fallback: grep the module name across the high-traffic logs
    const grepLogs = ['watchtower.log', 'orchestrator.log', 'roux.log'].filter((f) => logset.has(f))
    const hits: string[] = []
    const needle = last.toLowerCase()
    for (const f of grepLogs) {
      const raw = await readFile(path.join(LOGS, f), 'utf-8')
      for (const ln of raw.split('\n')) if (ln && ln.toLowerCase().includes(needle)) hits.push(`[${f}] ${ln}`)
    }
    return NextResponse.json({ source: `mentions of "${last}"`, lines: hits.slice(-100), mode: 'grep' })
  } catch (e: any) {
    return NextResponse.json({ source: '', lines: [], error: String(e?.message || e) })
  }
}
