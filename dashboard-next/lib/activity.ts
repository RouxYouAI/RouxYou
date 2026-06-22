import { readFile } from 'fs/promises'
import path from 'path'

const ROOT = path.resolve(process.cwd(), '..')

// keyword (from a cron job name / log line) -> graph node id to pulse
const MAP: [RegExp, string][] = [
  [/proposal_executor|dispatch/i, 'shared/proposal_executor'],
  [/proposer|topic_gap/i, 'shared/proposer'],
  [/draft|author/i, 'shared/authoring'],
  [/reflection/i, 'shared/reflection_coach'],
  [/coach/i, 'shared/reflection_coach'],
  [/research/i, 'shared/researcher'],
  [/codebase|reindex/i, 'shared/codebase_index'],
  [/shadow|verif/i, 'shared/shadow_verifier'],
  [/adjudicat|ledger|trust/i, 'shared/trust_ledger'],
  [/memory|decay/i, 'shared/memory'],
  [/coder/i, 'coder/coder'],
  [/worker/i, 'worker/worker'],
  [/companion/i, 'orchestrator/orchestrator'],
  [/watchtower|cron|job|health|restart|adjud/i, 'orchestrator/watchtower'],
]

function jobToNode(job: string): string {
  for (const [re, id] of MAP) if (re.test(job)) return id
  return 'orchestrator/watchtower'
}

export type Firing = { job: string; node: string; t: string; key: string }
export type Activity = {
  status: string
  thought: string
  agent: string | null
  task_title: string | null
  recent: Firing[]
}

export async function loadActivity(): Promise<Activity> {
  let act: any = {}
  try {
    act = JSON.parse(await readFile(path.join(ROOT, 'state', 'activity.json'), 'utf-8'))
  } catch {}

  const recent: Firing[] = []
  try {
    const log = await readFile(path.join(ROOT, 'logs', 'watchtower.log'), 'utf-8')
    const lines = log.split('\n').slice(-160)
    for (const line of lines) {
      const m = line.match(/Running job:\s*([a-z_]+)/i)
      if (!m) continue
      const tm = line.match(/\[(\d{2}:\d{2}:\d{2})\]/)
      const t = tm ? tm[1] : ''
      const job = m[1]
      recent.push({ job, node: jobToNode(job), t, key: `${t}|${job}` })
    }
  } catch {}

  // also surface the current active agent (if working) as a firing
  if (act.status && act.status !== 'idle' && act.agent) {
    recent.push({ job: act.agent, node: jobToNode(act.agent), t: 'now', key: `active|${act.agent}|${act.updated_at}` })
  }

  const seen = new Set<string>()
  const deduped = recent.filter((f) => (seen.has(f.key) ? false : (seen.add(f.key), true)))

  return {
    status: act.status || 'idle',
    thought: act.thought || 'System idle.',
    agent: act.agent ?? null,
    task_title: act.task_title ?? null,
    recent: deduped.slice(-14),
  }

}
