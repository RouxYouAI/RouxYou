import { NextResponse } from 'next/server'
import { loadGraph } from '@/lib/graph'

export const dynamic = 'force-dynamic' // always read fresh (the index reindexes every 30min)

// Live endpoint for her real self-map (kept for future live-refresh; the page itself
// now loads the graph server-side via loadGraph()).
export async function GET() {
  try {
    return NextResponse.json(await loadGraph())
  } catch (e: any) {
    return NextResponse.json({ error: String(e?.message || e) }, { status: 500 })
  }
}
