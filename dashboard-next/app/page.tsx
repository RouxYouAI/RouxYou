import { loadGraph } from '@/lib/graph'
import Scene from './Scene'

// Server component: load her real self-map server-side and hand it to the client scene
// as a prop — data is in the initial render, no loading flash, no hydration/fetch race.
export default async function Home() {
  const graph = await loadGraph()
  return <Scene graph={graph} />
}
