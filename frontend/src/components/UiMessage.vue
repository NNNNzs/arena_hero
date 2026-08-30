<script setup lang="ts">
import { useUiStore } from '../state/ui'

const ui = useUiStore()
</script>

<template>
  <div id="uiMessages" class="ui-messages" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="ui-message">
      <div v-for="message in ui.state.messages" :key="message.id" class="ui-message" :class="`is-${message.tone}`" role="status">
        <span>{{ message.text }}</span><button type="button" aria-label="关闭消息" @click="ui.removeMessage(message.id)">×</button>
      </div>
    </TransitionGroup>
  </div>
</template>
