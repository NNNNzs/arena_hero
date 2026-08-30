<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useUiStore } from '../state/ui'

const ui = useUiStore()
const dialog = ref<HTMLElement | null>(null)

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && ui.state.modal) ui.closeModal()
}
function closeFromBackdrop(event: MouseEvent) {
  if (event.target === event.currentTarget) ui.closeModal()
}
watch(() => ui.state.modal, async modal => {
  if (!modal) return
  await nextTick()
  dialog.value?.focus()
}, { deep: true })
window.addEventListener('keydown', onKeydown)
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Transition name="ui-modal">
    <div v-if="ui.state.modal" id="uiModal" class="ui-modal-backdrop" role="presentation" @click="closeFromBackdrop">
      <section ref="dialog" class="ui-modal" role="dialog" aria-modal="true" :aria-label="ui.state.modal.title" tabindex="-1">
        <header class="ui-modal-head">
          <div><span v-if="ui.state.modal.eyebrow" class="eyebrow">{{ ui.state.modal.eyebrow }}</span><h2>{{ ui.state.modal.title }}</h2></div>
          <button class="dialog-close" type="button" aria-label="关闭弹窗" @click="ui.closeModal">×</button>
        </header>
        <div class="ui-modal-body"><p v-for="line in ui.state.modal.lines" :key="line">{{ line }}</p></div>
        <footer class="ui-modal-actions"><button class="neutral" type="button" @click="ui.closeModal">{{ ui.state.modal.closeLabel || '知道了' }}</button></footer>
      </section>
    </div>
  </Transition>
</template>
