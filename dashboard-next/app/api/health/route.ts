import { NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'

const SERVICES = [
  { name: 'gateway', port: 8000 },
  { name: 'orchestrator', port: 8001 },
  { name: 'coder', port: 8002 },
  { name: 'worker', port: 8003 },
  { name: 'memory', port: 8004 },
  { name: 'supervisor', port: 8010 },
  { name: 'rag_api', port: 8011 },
  { name: 'cron', port: 8012 },
  { name: 'voice', port: 8014 },
  { name: 'dashboard', port: 8501 },
]

async function up(port: number): Promise<boolean> {
  const ctrl = new AbortController()
  const t = setTimeout(() => ctrl.abort(), 900)
  try {
    // any HTTP response (even 404) means the port is listening = up
    await fetch(`http://127.0.0.1:${port}/`, { signal: ctrl.signal })
    return true
  } catch {
    return false // connection refused / timeout = down
  } finally {
    clearTimeout(t)
  }
}

export async function GET() {
  const results = await Promise.all(
    SERVICES.map(async (s) => ({ ...s, up: await up(s.port) }))
  )
  const upCount = results.filter((r) => r.up).length
  return NextResponse.json({ services: results, up: upCount, total: results.length })
}
