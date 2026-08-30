export type Cell = [number, number]
export type JsonObject = Record<string, any>

export interface ServiceSnapshot {
  running?: boolean
  connected?: boolean
  last_tick?: number | null
  [key: string]: any
}

export interface DashboardEntity {
  alias: string
  kind?: string
  task?: string
  action?: string
  status?: string
  goal?: string
  reason?: string
  blocker?: string | null
  wait_kind?: string
  wake_condition?: string
  position?: Cell | null
  target_cell?: Cell | null
  trace_tick?: number | null
  hp?: number | null
  shield?: number | null
  cargo?: number | null
  next_step?: string
  eta_ticks?: number | null
  waited_ticks?: number
  state_synced?: boolean
  assignment?: JsonObject | null
  candidate_intents?: JsonObject[]
  node_path?: JsonObject[]
  [key: string]: any
}

export interface MapEntity {
  alias?: string
  kind?: string
  position?: Cell | null
  target_cell?: Cell | null
  action?: string
  task?: string
  reason?: string
  enemy?: boolean
  [key: string]: any
}

export interface DashboardMap {
  friendly?: MapEntity[]
  enemies?: MapEntity[]
  resources?: Cell[]
  observed?: Cell[]
  beacon?: { position?: Cell | null; status?: string | null }
  [key: string]: any
}

export interface DashboardSnapshot {
  tick?: number | null
  mode?: string | null
  mode_label?: string | null
  resources?: number | null
  resource_capacity?: number | null
  population?: number | null
  map?: DashboardMap
  [key: string]: any
}

export interface DashboardCommandCenter {
  command_version?: number
  entities?: DashboardEntity[]
  goals?: JsonObject[]
  tasks?: JsonObject[]
  commands?: JsonObject[]
  timeline?: JsonObject[]
  causality?: JsonObject
  [key: string]: any
}

export interface SquadMember extends DashboardEntity {
  alias: string
}

export interface Squad {
  id: string
  name: string
  type?: string
  target?: Cell | null
  status?: string
  members?: SquadMember[]
  causality?: JsonObject
  [key: string]: any
}

export interface DashboardPayload {
  service?: ServiceSnapshot
  current?: DashboardSnapshot | null
  command_center?: DashboardCommandCenter
  event_log?: EventsPayload
  squads?: { squads?: Squad[]; assignments?: Record<string, string> }
  policy_config?: JsonObject
  migration_recommendation?: JsonObject
  chunk_saturation?: Record<string, JsonObject>
  map_memory_version?: number
  recent?: JsonObject[]
  [key: string]: any
}

export interface EventsPayload {
  events?: JsonObject[]
  category_counts?: Record<string, number>
  total?: number
  matched?: number
  [key: string]: any
}

export interface ReplayFrame {
  tick?: number | null
  snapshot?: DashboardSnapshot
  command_center?: DashboardCommandCenter | null
  markers?: JsonObject[]
  [key: string]: any
}

export interface ReplayPayload {
  frames?: ReplayFrame[]
  ticks?: number[]
  [key: string]: any
}

export interface MapMemoryPayload {
  version?: number
  explored_segments?: unknown[]
  mined?: unknown[]
  obstacles?: unknown[]
  known_resources?: unknown[]
  [key: string]: any
}

export interface CommandResponse extends JsonObject {
  command_version?: number
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { cache: 'no-store', ...init })
  let payload: any = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok) throw new Error(payload?.error || `HTTP ${response.status}`)
  return payload as T
}

export interface CommandAuth {
  csrf: string
  version: number
}

export const dashboardApi = {
  dashboard(): Promise<DashboardPayload> {
    return requestJson<DashboardPayload>('/api/dashboard')
  },

  events({ limit = 50, category = 'ALL', fromTick }: { limit?: number; category?: string; fromTick?: number | null } = {}): Promise<EventsPayload> {
    const params = new URLSearchParams({ limit: String(limit) })
    if (category && category !== 'ALL') params.set('category', category)
    if (fromTick != null && fromTick > 0) params.set('from_tick', String(fromTick))
    return requestJson<EventsPayload>(`/api/events?${params}`)
  },

  replay({ limit = 32, fromTick, toTick }: { limit?: number; fromTick?: number | null; toTick?: number | null } = {}): Promise<ReplayPayload> {
    const params = new URLSearchParams({ limit: String(limit) })
    if (fromTick != null) params.set('from_tick', String(fromTick))
    if (toTick != null) params.set('to_tick', String(toTick))
    return requestJson<ReplayPayload>(`/api/replay?${params}`)
  },

  replayTimeline(limit = 64): Promise<ReplayPayload> {
    return requestJson<ReplayPayload>(`/api/replay/timeline?limit=${encodeURIComponent(limit)}`)
  },

  mapMemory(): Promise<MapMemoryPayload> {
    return requestJson<MapMemoryPayload>('/api/map/memory')
  },

  command<T extends CommandResponse = CommandResponse>(path: string, method: string, data: JsonObject | undefined, auth?: CommandAuth): Promise<T> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (auth?.csrf) {
      headers['X-CSRF-Token'] = auth.csrf
      headers['If-Match'] = `"command-version-${auth.version}"`
      headers['Idempotency-Key'] = `ui-${crypto.randomUUID()}`
    }
    return requestJson<T>(path, { method, headers, body: data === undefined ? undefined : JSON.stringify(data) })
  },
}
