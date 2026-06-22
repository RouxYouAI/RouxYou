import { NextResponse } from 'next/server'

export const dynamic = 'force-dynamic'

// Her recent conversation (last ~20 turns) from the orchestrator, so the chat panel
// rehydrates across reloads — the convo lives server-side, we just load it.
export async function GET() {
  try {
    const r = await fetch('http://127.0.0.1:8001/conversation', { cache: 'no-store' })
    return NextResponse.json(await r.json())
  } catch (e: any) {
    return NextResponse.json({ messages: [], error: String(e?.message || e) })
  }
}
