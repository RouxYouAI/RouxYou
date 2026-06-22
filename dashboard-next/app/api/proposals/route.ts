import { NextRequest, NextResponse } from 'next/server'
import { readFile } from 'fs/promises'
import path from 'path'

export const dynamic = 'force-dynamic'
const WT = 'http://127.0.0.1:8012' // watchtower (proposals endpoints)

// GET: active gate-queue proposals with the detail fields needed for the expand view.
export async function GET() {
  try {
    const file = path.resolve(process.cwd(), '..', 'state', 'proposals_active.json')
    const raw = JSON.parse(await readFile(file, 'utf-8'))
    const arr: any[] = Array.isArray(raw) ? raw : raw.proposals || raw.active || []
    const proposals = arr.map((p) => ({
      id: p.id,
      title: p.title,
      category: p.category,
      source: p.source,
      state: p.state || p.status || 'pending',
      verdict: p?.disclosure?.observer_verification?.status || p?.observer_verification?.status || null,
      description: p.description || '',
      action: p.proposed_action || '',
      evidence: p.evidence || '',
      reasoning: p?.disclosure?.reasoning || p.coach_reasoning || '',
      comment: p?.disclosure?.observer_verification?.comment || '',
    }))
    return NextResponse.json({ proposals, count: proposals.length })
  } catch (e: any) {
    return NextResponse.json({ proposals: [], count: 0, error: String(e?.message || e) })
  }
}

// POST: approve or dismiss a proposal (HIL control surface). Dismiss carries a reason →
// the watchtower feeds it to coach_on_dismissal so Roux learns why she was turned down.
export async function POST(req: NextRequest) {
  try {
    const { id, action, reason } = await req.json()
    if (!id || (action !== 'approve' && action !== 'dismiss')) {
      return NextResponse.json({ error: 'bad request' }, { status: 400 })
    }
    const url = action === 'approve' ? `${WT}/proposals/${id}/approve` : `${WT}/proposals/${id}/dismiss`
    const body = action === 'approve' ? { approved_by: 'human' } : { reason: reason || 'dismissed by DJ from the dashboard', by: 'human' }
    const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
    return NextResponse.json(await r.json().catch(() => ({ success: r.ok })))
  } catch (e: any) {
    return NextResponse.json({ error: String(e?.message || e) }, { status: 500 })
  }
}
