import { NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'

// Trust-ledger graduation readiness per tier + autonomy status (watchtower :8012).
export async function GET() {
  try {
    const r = await fetch('http://127.0.0.1:8012/ledger', { cache: 'no-store' })
    return NextResponse.json(await r.json())
  } catch (e: any) {
    return NextResponse.json({ tiers: {}, paused: null, min_clean: 25, error: String(e?.message || e) })
  }
}
