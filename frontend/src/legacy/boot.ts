/**
 * Load the battle-map and dashboard controller after Vue has mounted the DOM
 * shell. The existing modules keep their public window bridge and API
 * behavior while they are being migrated into typed components.
 */
let booted = false

function ensurePixi(): Promise<void> {
  if ((window as Window & { PIXI?: unknown }).PIXI) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = '/static/pixi.min.js'
    script.async = false
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('PixiJS resource failed to load'))
    document.head.appendChild(script)
  })
}

export async function bootLegacyDashboard(): Promise<void> {
  if (booted) return
  booted = true
  try {
    await ensurePixi()
  } catch (error) {
    // The map renderer already has an explicit unavailable state; the rest of
    // the command center must still boot when Pixi cannot be loaded.
    console.warn('PixiJS unavailable; continuing with dashboard controls.', error)
  }
  await import('./tactical-map/layers.js')
  await import('./tactical-map/camera.js')
  await import('./tactical-map/radar.js')
  await import('./tactical-map/renderers.js')
  await import('./tactical-map/input.js')
  await import('./tactical-map/main.js')
  await import('./dashboard-controller')
}
