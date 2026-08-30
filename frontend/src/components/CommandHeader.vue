<script setup lang="ts">
import { computed } from 'vue'
import { modeCausalityLines, modeCausalityText } from '../domain/labels'
import { useDashboardStore } from '../state/dashboard'
import { useUiStore } from '../state/ui'

const dashboard = useDashboardStore()
const ui = useUiStore()
const current = computed(() => dashboard.displayView.value?.current)
const service = computed(() => dashboard.displayView.value?.service || {})
const commandCenter = computed(() => dashboard.displayView.value?.command_center || {})
const statusText = computed(() => !service.value.running ? '服务已停止' : service.value.connected ? '已连接 · 对战中' : '服务在线 · 等待连接')
const causalityLines = computed(() => modeCausalityLines(commandCenter.value.causality))
const causalityText = computed(() => modeCausalityText(commandCenter.value.causality))
function showCausality() { ui.openModal({ eyebrow: '策略判定链', title: '当前模式判定链', lines: causalityLines.value }) }
function openAuthDialog() { dashboard.openAuthDialog() }
</script>

<template>
  <header class="command-bar">
    <div class="brand"><span class="eyebrow">ARENA HERO</span><h1>作战指挥中心</h1></div>
    <div class="top-metrics" aria-label="实时作战状态">
      <div class="metric"><span>TICK</span><strong id="tick">{{ current?.tick ?? service.last_tick ?? '—' }}</strong></div>
      <div class="metric"><span>资源 / 容量</span><strong id="resources">{{ current?.resources == null ? '—' : `${current.resources}/${current.resource_capacity ?? '—'}` }}</strong></div>
      <div class="metric"><span>策略模式</span><strong id="mode">{{ current?.mode_label || '等待数据' }}</strong><button id="modeCausality" class="mode-causality" type="button" :title="causalityText" @click="showCausality">{{ commandCenter.causality?.mode ? '查看判定链' : '等待判定链' }}</button></div>
      <div class="metric"><span>单位在线</span><strong id="unitCount">{{ commandCenter.entities?.length || 0 }}</strong></div>
    </div>
    <div class="status-tools"><button class="auth-trigger" type="button" :class="{ ready: dashboard.csrf.value }" aria-controls="commandPasswordDialog" @click="openAuthDialog"><span class="auth-trigger-icon">{{ dashboard.csrf.value ? '●' : '○' }}</span><span>{{ dashboard.csrf.value ? '口令已认证' : '配置口令' }}</span></button><div id="status" class="status" :class="{ ok: service.connected }">{{ statusText }}</div></div>
  </header>
</template>
