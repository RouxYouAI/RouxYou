import { NextRequest, NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'
const ORCH = 'http://127.0.0.1:8001'

export async function GET() {
  try {
    const r = await fetch(`${ORCH}/conversations`, { cache: 'no-store' })
    return NextResponse.json(await r.json())
  } catch (e: any) {
    return NextResponse.json({ conversations: [], active_id: null, error: String(e?.message || e) })
  }
}

export async function POST(req: NextRequest) {
  try {
    const body = await req.json()
    const map: Record<string, { path: string; payload?: any }> = {
      new: { path: '/conversations/new' },
      activate: { path: '/conversations/activate', payload: { id: body.id } },
      pin: { path: '/conversations/pin', payload: { id: body.id, pinned: body.pinned } },
      delete: { path: '/conversations/delete', payload: { id: body.id } },
    }
    const m = map[body.action]
    if (!m) return NextResponse.json({ error: 'unknown action' }, { status: 400 })
    const r = await fetch(`${ORCH}${m.path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(m.payload ?? {}),
    })
    return NextResponse.json(await r.json())
  } catch (e: any) {
    return NextResponse.json({ error: String(e?.message || e) }, { status: 500 })
  }
}
