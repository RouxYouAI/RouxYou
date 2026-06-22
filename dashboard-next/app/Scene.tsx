'use client'

import { Canvas, useFrame } from '@react-three/fiber'
import { OrbitControls, Html } from '@react-three/drei'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import { useControls, folder, Leva } from 'leva'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import type { Graph, GNode } from '@/lib/graph'
import type { Activity, Firing } from '@/lib/activity'
import { LeftDeck, RightDeck, NodeLogPanel } from './Panels'

type Vec3 = [number, number, number]

function placePositions(layout: string, n: number): Vec3[] {
  const out: Vec3[] = []
  if (layout === 'layered') {
    const layers = 6
    const per = Math.ceil(n / layers)
    const xspan = 10
    for (let i = 0; i < n; i++) {
      const l = Math.floor(i / per)
      const k = i % per
      out.push([-xspan / 2 + (l / (layers - 1)) * xspan, (k - (per - 1) / 2) * 0.55, ((k % 3) - 1) * 0.4])
    }
    return out
  }
  if (layout === 'cloud') {
    let i = 0
    while (i < n) {
      const x = Math.random() * 2 - 1, y = Math.random() * 2 - 1, z = Math.random() * 2 - 1
      if (x * x + y * y + z * z > 1) continue
      out.push([x * 4.6, y * 4.6, z * 4.6])
      i++
    }
    return out
  }
  const r = 4.3 // sphere (fibonacci)
  for (let i = 0; i < n; i++) {
    const phi = Math.acos(1 - (2 * (i + 0.5)) / n)
    const theta = Math.PI * (1 + Math.sqrt(5)) * i
    out.push([r * Math.sin(phi) * Math.cos(theta), r * Math.cos(phi), r * Math.sin(phi) * Math.sin(theta)])
  }
  return out
}

function NodeGeo({ shape, size }: { shape: string; size: number }) {
  if (shape === 'box') return <boxGeometry args={[size * 1.7, size * 1.7, size * 1.7]} />
  if (shape === 'icosahedron') return <icosahedronGeometry args={[size, 0]} />
  if (shape === 'octahedron') return <octahedronGeometry args={[size, 0]} />
  return <sphereGeometry args={[size, 18, 18]} />
}

// reused scratch colors (avoid per-frame allocation)
const _base = new THREE.Color()
const _flare = new THREE.Color()
const _tmp = new THREE.Color()

function SelfMap({ graph, firings, ...p }: any) {
  const group = useRef<THREE.Group>(null!)
  const [hovered, setHovered] = useState<number | null>(null)
  const [lastHover, setLastHover] = useState<number | null>(null)
  const outTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const nodeRefs = useRef<(THREE.Mesh | null)[]>([])
  const pulseRef = useRef<number[]>([])
  const seenRef = useRef<Set<string>>(new Set())
  if (pulseRef.current.length !== graph.nodes.length) pulseRef.current = new Array(graph.nodes.length).fill(0)

  const positions = useMemo(() => placePositions(p.layout, graph.nodes.length), [p.layout, graph.nodes.length])
  const idIndex = useMemo(() => {
    const m: Record<string, number> = {}
    graph.nodes.forEach((n: GNode, i: number) => (m[n.id] = i))
    return m
  }, [graph])

  // edge index pairs + node->edges (for path glow propagation)
  const edgeData = useMemo(() => {
    const pairs: [number, number][] = []
    for (const [a, b] of graph.edges) {
      const ia = idIndex[a], ib = idIndex[b]
      if (ia === undefined || ib === undefined) continue
      pairs.push([ia, ib])
    }
    const nodeToEdges: number[][] = graph.nodes.map(() => [] as number[])
    pairs.forEach(([a, b], ei) => { nodeToEdges[a].push(ei); nodeToEdges[b].push(ei) })
    return { pairs, nodeToEdges }
  }, [graph, idIndex])

  const edgeEnergyRef = useRef<number[]>([])
  if (edgeEnergyRef.current.length !== edgeData.pairs.length) edgeEnergyRef.current = new Array(edgeData.pairs.length).fill(0)

  const edgeGeo = useMemo(() => {
    const g = new THREE.BufferGeometry()
    const pos = new Float32Array(edgeData.pairs.length * 6)
    edgeData.pairs.forEach(([a, b], ei) => {
      const A = positions[a], B = positions[b]
      pos.set([A[0], A[1], A[2], B[0], B[1], B[2]], ei * 6)
    })
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    g.setAttribute('color', new THREE.Float32BufferAttribute(new Float32Array(edgeData.pairs.length * 6), 3))
    return g
  }, [edgeData, positions])

  // new firings -> light up node + its paths
  useEffect(() => {
    for (const f of (firings as Firing[]) || []) {
      if (seenRef.current.has(f.key)) continue
      seenRef.current.add(f.key)
      const idx = idIndex[f.node]
      if (idx !== undefined) {
        pulseRef.current[idx] = 1
        for (const ei of edgeData.nodeToEdges[idx]) edgeEnergyRef.current[ei] = 1
      }
    }
    if (seenRef.current.size > 240) seenRef.current = new Set(Array.from(seenRef.current).slice(-120))
  }, [firings, idIndex, edgeData])

  const enter = (i: number) => (e: any) => {
    e.stopPropagation()
    if (outTimer.current) clearTimeout(outTimer.current)
    setHovered(i)
    setLastHover(i)
  }
  const leave = () => {
    if (outTimer.current) clearTimeout(outTimer.current)
    outTimer.current = setTimeout(() => setHovered(null), 320)
  }

  useFrame((state, dt) => {
    if (group.current) group.current.rotation.y += dt * p.rotationSpeed
    const t = state.clock.elapsedTime

    // nodes: decay pulse, breathe, flare
    const pulses = pulseRef.current
    const refs = nodeRefs.current
    for (let i = 0; i < refs.length; i++) {
      const mesh = refs[i]
      if (!mesh) continue
      if (pulses[i] > 0) pulses[i] = Math.max(0, pulses[i] - dt * p.flareDecay)
      const breathe = p.breathe * Math.sin(t * 1.1 + i * 0.5)
      const mat = mesh.material as THREE.MeshStandardMaterial
      mat.emissiveIntensity = 2 + breathe + pulses[i] * p.nodeFlare
      mesh.scale.setScalar(1 + pulses[i] * p.nodeFlareScale)
    }

    // edges: decay energy, lerp base -> flare color (the "signal traveling the paths")
    const colAttr = edgeGeo.getAttribute('color') as THREE.BufferAttribute
    const arr = colAttr.array as Float32Array
    _base.set(p.edgeColor)
    _flare.set(p.edgeFlareColor)
    const en = edgeEnergyRef.current
    for (let e = 0; e < en.length; e++) {
      if (en[e] > 0) en[e] = Math.max(0, en[e] - dt * p.flareDecay)
      const x = p.pulsePaths ? Math.min(1, en[e] * p.edgeFlare) : 0
      _tmp.copy(_base).lerp(_flare, x)
      const o = e * 6
      arr[o] = _tmp.r; arr[o + 1] = _tmp.g; arr[o + 2] = _tmp.b
      arr[o + 3] = _tmp.r; arr[o + 4] = _tmp.g; arr[o + 5] = _tmp.b
    }
    colAttr.needsUpdate = true
  })

  return (
    <group ref={group}>
      {p.showEdges && (
        <lineSegments geometry={edgeGeo}>
          <lineBasicMaterial vertexColors transparent opacity={p.edgeOpacity} toneMapped={false} />
        </lineSegments>
      )}
      {p.showNodes &&
        graph.nodes.map((n: GNode, i: number) => {
          const isHub = n.degree >= p.hubThreshold
          const color = isHub ? p.hubColor : p.nodeColor
          const size = (isHub ? p.hubSize : p.nodeSize) * (1 + Math.min(n.degree, 12) * 0.04)
          const hit = Math.max(size * 3.4, 0.3)
          return (
            <group key={n.id} position={positions[i]}>
              <mesh onPointerOver={enter(i)} onPointerOut={leave} onClick={(e) => { e.stopPropagation(); p.onSelectNode && p.onSelectNode(n.id) }}>
                <sphereGeometry args={[hit, 8, 8]} />
                <meshBasicMaterial transparent opacity={0} depthWrite={false} />
              </mesh>
              <mesh ref={(el) => { nodeRefs.current[i] = el }}>
                <NodeGeo shape={p.nodeShape} size={size} />
                <meshStandardMaterial color={color} emissive={color} emissiveIntensity={2} toneMapped={false} />
              </mesh>
            </group>
          )
        })}
      {lastHover !== null && (
        <Html position={positions[lastHover]} center distanceFactor={9} zIndexRange={[100, 0]} style={{ pointerEvents: 'none' }}>
          <div
            style={{
              transform: 'translateY(-150%)',
              whiteSpace: 'nowrap',
              padding: '4px 9px',
              borderRadius: 6,
              background: 'rgba(6,16,12,0.92)',
              border: `1px solid ${graph.nodes[lastHover].degree >= p.hubThreshold ? p.hubColor : p.edgeColor}`,
              color: '#e6f4ee',
              fontSize: 12,
              fontFamily: 'ui-monospace, monospace',
              pointerEvents: 'none',
              opacity: hovered !== null ? 1 : 0,
              transition: 'opacity 0.28s ease',
            }}
          >
            <span style={{ color: graph.nodes[lastHover].degree >= p.hubThreshold ? p.hubColor : '#7af2c0' }}>
              {graph.nodes[lastHover].id}
            </span>
            <span style={{ opacity: 0.6 }}>
              {'  '}· {graph.nodes[lastHover].functions} fn · {graph.nodes[lastHover].degree} links
            </span>
          </div>
        </Html>
      )}
    </group>
  )
}

export default function Scene({ graph }: { graph: Graph }) {
  const [act, setAct] = useState<Activity | null>(null)
  const [health, setHealth] = useState<any>(null)
  const [proposals, setProposals] = useState<any>(null)
  const [ledger, setLedger] = useState<any>(null)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    const poll = () =>
      fetch('/api/activity')
        .then((r) => r.json())
        .then((d) => { if (alive) setAct(d) })
        .catch(() => {})
    poll()
    const id = setInterval(poll, 2000)
    return () => { alive = false; clearInterval(id) }
  }, [])
  useEffect(() => {
    let alive = true
    const poll = () => {
      fetch('/api/health').then((r) => r.json()).then((d) => { if (alive) setHealth(d) }).catch(() => {})
      fetch('/api/proposals').then((r) => r.json()).then((d) => { if (alive) setProposals(d) }).catch(() => {})
      fetch('/api/ledger').then((r) => r.json()).then((d) => { if (alive) setLedger(d) }).catch(() => {})
    }
    poll()
    const id = setInterval(poll, 5000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  const c = useControls({
    Layout: folder({
      layout: { value: 'sphere', options: ['sphere', 'layered', 'cloud'] },
      rotationSpeed: { value: 0.15, min: 0, max: 2, step: 0.05 },
    }),
    Nodes: folder({
      nodeShape: { value: 'icosahedron', options: ['sphere', 'box', 'icosahedron', 'octahedron'] },
      nodeSize: { value: 0.08, min: 0.02, max: 0.3, step: 0.01 },
      hubSize: { value: 0.18, min: 0.05, max: 0.6, step: 0.01 },
      hubThreshold: { value: 7, min: 2, max: 34, step: 1 },
      showNodes: true,
    }),
    Edges: folder({
      showEdges: true,
      edgeOpacity: { value: 0.3, min: 0, max: 1, step: 0.02 },
    }),
    'Swamp Palette': folder({
      nodeColor: '#10b981',
      hubColor: '#c89b3c',
      edgeColor: '#3d6b48',
      background: '#06100c',
      panelOpacity: { value: 0.9, min: 0.25, max: 1, step: 0.05 },
    }),
    Effects: folder({
      bloom: { value: 2.6, min: 0, max: 3, step: 0.05 },
      bloomThreshold: { value: 0.09, min: 0, max: 1, step: 0.01 },
    }),
    Animation: folder({
      breathe: { value: 0.18, min: 0, max: 0.6, step: 0.01 },
      flareDecay: { value: 0.7, min: 0.2, max: 3, step: 0.1 },
      nodeFlare: { value: 5, min: 0, max: 12, step: 0.5 },
      nodeFlareScale: { value: 0.85, min: 0, max: 2, step: 0.05 },
      pulsePaths: true,
      edgeFlare: { value: 1.5, min: 0, max: 4, step: 0.1 },
      edgeFlareColor: '#7af2c0',
    }),
  })

  const working = !!act && act.status !== 'idle'

  return (
    <main className="fixed inset-0 overflow-hidden" style={{ background: c.background }}>
      <Leva collapsed={true} titleBar={{ title: 'RouxYou — look' }} />
      <Canvas camera={{ position: [0, 0, 12], fov: 50 }} dpr={[1, 2]}>
        <color attach="background" args={[c.background]} />
        <ambientLight intensity={0.4} />
        <pointLight position={[12, 12, 12]} intensity={1.1} />
        {graph.nodes.length > 0 && <SelfMap graph={graph} firings={act?.recent ?? []} onSelectNode={setSelectedNode} {...c} />}
        <OrbitControls enablePan={false} enableZoom makeDefault />
        <EffectComposer>
          <Bloom luminanceThreshold={c.bloomThreshold} intensity={c.bloom} mipmapBlur radius={0.7} />
        </EffectComposer>
      </Canvas>

      <div className="pointer-events-none absolute left-10 top-8 select-none">
        <div className="text-[34px] font-bold tracking-tight text-emerald-50">RouxYou</div>
        <div className="mt-1 text-[11px] uppercase tracking-[0.22em] text-emerald-200/50">
          sovereign · self-mapping · alive
        </div>
        <div className="mt-3 flex items-center gap-2 text-[12px]" style={{ fontFamily: 'ui-monospace, monospace' }}>
          <span
            style={{
              width: 9, height: 9, borderRadius: 99,
              background: working ? '#10b981' : '#3d6b48',
              boxShadow: working ? '0 0 10px #10b981' : 'none',
              transition: 'all 0.4s ease',
            }}
          />
          <span style={{ color: working ? '#7af2c0' : '#3d6b48' }}>{act?.status ?? '…'}</span>
          <span style={{ color: 'rgba(230,244,238,0.45)', maxWidth: 480, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {act?.thought ?? ''}
          </span>
        </div>
      </div>
      <div className="pointer-events-none absolute bottom-7 left-10 select-none text-[11px] tracking-wide text-emerald-200/40">
        her anatomy · {graph.nodes.length} modules · {graph.edges.length} dependencies — hover a node · nodes + paths pulse as subsystems fire
      </div>

      <LeftDeck health={health} activity={act} proposals={proposals} ledger={ledger} panelOpacity={c.panelOpacity} reloadProposals={() => fetch('/api/proposals').then((r) => r.json()).then(setProposals).catch(() => {})} />
      <RightDeck status={act?.status || 'idle'} panelOpacity={c.panelOpacity} />
      {selectedNode && <NodeLogPanel node={selectedNode} onClose={() => setSelectedNode(null)} />}
    </main>
  )
}
