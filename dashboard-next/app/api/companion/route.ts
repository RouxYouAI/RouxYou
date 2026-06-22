import { NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'
// no maxDuration cap — self-hosted Node streams until she's done (her replies can run long, esp. cold)

// Proxy to Roux's streaming companion endpoint (orchestrator :8001), piping the SSE straight
// through so the browser can read status/token/done events same-origin (no CORS).
export async function POST(req: NextRequest) {
  const body = await req.text()
  try {
    const upstream = await fetch('http://127.0.0.1:8001/companion/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
    })
    if (!upstream.body) {
      return new Response('event: error\ndata: {"detail":"no stream from orchestrator"}\n\n', {
        headers: { 'content-type': 'text/event-stream' },
      })
    }
    return new Response(upstream.body, {
      headers: {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache, no-transform',
        connection: 'keep-alive',
      },
    })
  } catch (e: any) {
    return new Response(`event: error\ndata: {"detail":${JSON.stringify(String(e?.message || e))}}\n\n`, {
      headers: { 'content-type': 'text/event-stream' },
    })
  }
}
