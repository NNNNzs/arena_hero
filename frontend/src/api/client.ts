export interface DashboardPayload {
  service?: Record<string, unknown>
  current?: Record<string, unknown> | null
  recent?: Array<Record<string, unknown>>
  [key: string]: unknown
}

export interface EventsPayload {
  events?: Array<Record<string, unknown>>
  category_counts?: Record<string, number>
  total?: number
  [key: string]: unknown
}

export interface ReplayPayload {
  frames?: Array<Record<string, unknown>>
  ticks?: number[]
  [key: string]: unknown
}

export interface MapMemoryPayload {
  version?: number
  explored_segments?: unknown[]
  mined?: unknown[]
  obstacles?: unknown[]
  known_resources?: unknown[]
  [key: string]: unknown
}

async function requestJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: 'no-store' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json() as Promise<T>
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
}
