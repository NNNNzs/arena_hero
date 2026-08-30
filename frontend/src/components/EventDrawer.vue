<script setup lang="ts">
import { computed } from 'vue'
import type { Cell } from '../api/client'
import { useDashboardStore } from '../state/dashboard'

const dashboard = useDashboardStore()
const counts = computed(() => dashboard.eventState.value.category_counts || {})
const total = computed(() => dashboard.eventState.value.total ?? dashboard.eventState.value.events?.length ?? 0)
const events = computed(() => (dashboard.eventState.value.events || []).filter(item => dashboard.eventCategory.value === 'ALL' || String(item.category || '').toLowerCase() === dashboard.eventCategory.value.toLowerCase()))
const categories = [['ALL', '全部', 'eventCountAllDrawer'], ['combat', '战斗', 'eventCountCombat'], ['harvest', '采集', 'eventCountHarvest'], ['ops', '运营', 'eventCountOps'], ['anomaly', '异常', 'eventCountAnomaly']] as const
function count(category: string) { return category === 'ALL' ? total.value : counts.value[category] || 0 }
function focus(position: unknown) { if (Array.isArray(position) && position.length === 2) dashboard.focusCell(position as Cell) }
</script>

<template>
  <section id="eventDrawer" class="panel event-drawer" aria-label="事件日志" :hidden="!dashboard.eventDrawerOpen.value"><div class="section-head"><div><span class="eyebrow">EVENT LOG / COMBAT · HARVEST · OPERATIONS</span><h2>事件日志</h2></div><button id="eventDrawerClose" class="neutral event-drawer-close" title="收起" @click="dashboard.setEventDrawer(false)">✕</button></div><div id="eventFilters" class="event-filters"><button v-for="[category, label, id] in categories" :id="id" :key="category" class="filter-btn" :class="{ 'is-active': dashboard.eventCategory.value === category }" :data-event-category="category" @click="dashboard.setEventCategory(category)">{{ label }} <span class="event-count">{{ count(category) }}</span></button></div><div id="eventLogList" class="event-log-list muted"><button v-for="(item, index) in events" :key="`${item.tick}-${item.type}-${index}`" class="event-row" :class="`event-${item.category || 'ops'}`" :data-cell="Array.isArray(item.position) ? item.position.join(',') : undefined" @click="focus(item.position)"><span class="event-tick">#{{ item.tick }}</span><span class="event-icon">{{ item.category === 'combat' ? '⚔' : item.category === 'harvest' ? '⛏' : item.category === 'anomaly' ? '⚠' : '◈' }}</span><span class="event-description">{{ item.description }}{{ item.count > 1 ? ` × ${item.count}` : '' }}</span><span class="event-position">{{ Array.isArray(item.position) ? item.position.join(',') : '—' }}</span></button><div v-if="!events.length">没有符合筛选条件的事件</div></div></section>
</template>
