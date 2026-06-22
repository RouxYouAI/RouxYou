import { readFile } from 'fs/promises'
import path from 'path'

export type GNode = { id: string; label: string; group: string; functions: number; classes: number; degree: number }
export type Graph = { nodes: GNode[]; edges: [string, string][]; built_at: number | null }

// RouxYou's real self-map: 102 modules + dependency edges, from state/codebase_index.json
// (mirrors shared/codebase_index.get_dependency_graph()). Runs server-side.
export async function loadGraph(): Promise<Graph> {
  const file = path.resolve(process.cwd(), '..', 'state', 'codebase_index.json')
  const data = JSON.parse(await readFile(file, 'utf-8'))
  const modules: Record<string, any> = data.modules || {}
  const names = Object.keys(modules)

  const seen = new Set<string>()
  const edges: [string, string][] = []
  for (const name of names) {
    const imports: string[] = modules[name]?.imports || []
    const deps = new Set<string>()
    for (const imp of imports) {
      for (const other of names) {
        if (other === name) continue
        const dotted = other.replace(/\//g, '.')
        if (imp.includes(dotted) || imp.startsWith(dotted)) deps.add(other)
      }
    }
    for (const d of deps) {
      const k = `${name}->${d}`
      if (!seen.has(k)) {
        seen.add(k)
        edges.push([name, d])
      }
    }
  }

  const degree: Record<string, number> = {}
  for (const [a, b] of edges) {
    degree[a] = (degree[a] || 0) + 1
    degree[b] = (degree[b] || 0) + 1
  }

  const nodes: GNode[] = names.map((name) => ({
    id: name,
    label: name.split('/').pop() || name,
    group: name.split('/')[0] || 'root',
    functions: (modules[name]?.functions || []).length,
    classes: (modules[name]?.classes || []).length,
    degree: degree[name] || 0,
  }))

  return { nodes, edges, built_at: data.built_at ?? null }
}
