import { NextResponse } from 'next/server'
import { loadActivity } from '@/lib/activity'

export const dynamic = 'force-dynamic'

// Live firing signal: her current thought (activity.json) + recent cron firings (watchtower log).
export async function GET() {
  try {
    return NextResponse.json(await loadActivity())
  } catch (e: any) {
    return NextResponse.json({ error: String(e?.message || e) }, { status: 500 })
  }
}
