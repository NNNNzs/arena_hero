import { reactive } from 'vue'

export type UiTone = 'info' | 'success' | 'warning' | 'error'

export interface UiModalState {
  eyebrow?: string
  title: string
  lines: string[]
  closeLabel?: string
}

export interface UiMessageState {
  id: number
  text: string
  tone: UiTone
}

const state = reactive<{ modal: UiModalState | null; messages: UiMessageState[] }>({ modal: null, messages: [] })
let messageId = 0
const timers = new Map<number, number>()

function openModal(modal: UiModalState) { state.modal = modal }
function closeModal() { state.modal = null }
function removeMessage(id: number) {
  state.messages = state.messages.filter(message => message.id !== id)
  const timer = timers.get(id)
  if (timer) window.clearTimeout(timer)
  timers.delete(id)
}
export function showMessage(text: string, tone: UiTone = 'info', duration = 2600) {
  const id = ++messageId
  state.messages.push({ id, text, tone })
  if (duration > 0) timers.set(id, window.setTimeout(() => removeMessage(id), duration))
}

export function useUiStore() {
  return { state, openModal, closeModal, showMessage, removeMessage }
}
