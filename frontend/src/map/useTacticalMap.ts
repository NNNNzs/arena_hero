import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import type { DashboardStore } from '../state/dashboard'
import { showMessage } from '../state/ui'

type MapWindow = Window & Record<string, any>

export function useTacticalMap(viewport: Ref<HTMLElement | null>, dashboard: DashboardStore) {
  const fog = ref(true)
  const vision = ref('selected')
  const coordinates = ref(true)
  const labels = ref(true)
  const targetMode = ref(false)
  const ready = ref(false)
  let disposed = false

  async function loadEngine() {
    const browser = window as MapWindow
    if (!browser.PIXI) {
      await new Promise<void>(resolve => {
        const script = document.createElement('script')
        script.src = '/static/pixi.min.js'
        script.onload = () => resolve()
        script.onerror = () => resolve()
        document.head.appendChild(script)
      })
    }
    await import('./engine/layers.js')
    await import('./engine/camera.js')
    await import('./engine/radar.js')
    await import('./engine/renderers.js')
    await import('./engine/input.js')
    await import('./engine/main.js')
    if (disposed) return
    ready.value = Boolean(browser.TacticalMap)
    browser.updateDashboardMapCursor = (cell: [number, number] | null) => {
      const node = document.getElementById('mapCursor')
      if (node) node.textContent = cell ? `光标 ${cell.join(',')}` : '光标 —'
    }
    browser.updateDashboardTargetMode = (enabled: boolean) => {
      targetMode.value = enabled
      const node = document.getElementById('mapTargetMode')
      if (node) node.hidden = !enabled
    }
    browser.setDashboardMapTarget = (cell: [number, number]) => dashboard.setTargetFromMap(cell)
    browser.showDashboardMessage = (text: string, tone?: 'info' | 'success' | 'warning' | 'error') => showMessage(text, tone)
    browser.selectDashboardUnit = (alias: string) => dashboard.selectUnit(alias, { focusMap: false })
    render()
  }

  function render() {
    if (!ready.value || disposed || !viewport.value) return
    const browser = window as MapWindow
    browser.renderTacticalMap?.(dashboard.displayView.value)
  }

  function setLayer(name: 'fog' | 'vision' | 'coordinates' | 'labels', value: boolean | string) {
    if (name === 'fog') fog.value = Boolean(value)
    if (name === 'vision') vision.value = String(value)
    if (name === 'coordinates') coordinates.value = Boolean(value)
    if (name === 'labels') labels.value = Boolean(value)
    ;(window as MapWindow).setTacticalMapLayer?.(name, value)
  }

  function toggleTargetMode() {
    targetMode.value = !targetMode.value
    ;(window as MapWindow).setTacticalMapTargetMode?.(targetMode.value)
    const button = document.getElementById('mapPickTarget')
    if (button) button.setAttribute('aria-pressed', String(targetMode.value))
  }

  function applyRequest() {
    const request = dashboard.mapRequest.value
    if (!request) return
    const browser = window as MapWindow
    if (request.kind === 'cell' && request.cell) browser.focusTacticalCell?.(request.cell)
    if (request.kind === 'unit' && request.alias) browser.focusTacticalUnit?.(request.alias)
    if (request.kind === 'reset') browser.resetTacticalMap?.()
    if (request.kind === 'zoom') browser.zoomTacticalMap?.(request.factor || 1)
    if (request.kind === 'target-mode') browser.setTacticalMapTargetMode?.(true)
  }

  onMounted(() => { void loadEngine() })
  watch(() => dashboard.displayView.value, render, { deep: false })
  watch(() => dashboard.memory.value, render)
  watch(() => dashboard.mapRequest.value?.id, applyRequest)
  onBeforeUnmount(() => { disposed = true })

  return { fog, vision, coordinates, labels, targetMode, ready, setLayer, toggleTargetMode }
}
