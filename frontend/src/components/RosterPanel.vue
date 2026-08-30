<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Squad, SquadMember } from '../api/client'
import { KIND_LABELS, SQUAD_TYPE_ICONS, SQUAD_TYPE_LABELS, actionLabel, statusLabel, taskLabel } from '../domain/labels'
import { useDashboardStore } from '../state/dashboard'

const dashboard = useDashboardStore()
const activeTab = ref<'units' | 'squads'>('units')
const activeKind = ref('ALL')
const search = ref('')
const collapsedSquadIds = ref(new Set<string>())
const filteredEntities = computed(() => dashboard.entities.value.filter(entity => {
  const matchesKind = activeKind.value === 'ALL' || entity.kind === activeKind.value
  const matchesSearch = !search.value.trim() || entity.alias.toLowerCase().includes(search.value.trim().toLowerCase())
  return matchesKind && matchesSearch
}))
const squads = computed(() => dashboard.view.value?.squads?.squads || [])
const totalMembers = computed(() => squads.value.reduce((sum, squad) => sum + (squad.members?.length || 0), 0))

function setTab(tab: 'units' | 'squads') { activeTab.value = tab }
function toggleSquad(id: string) {
  const next = new Set(collapsedSquadIds.value)
  next.has(id) ? next.delete(id) : next.add(id)
  collapsedSquadIds.value = next
}
function expandAll() { collapsedSquadIds.value = new Set() }
function collapseAll() { collapsedSquadIds.value = new Set(squads.value.map(squad => squad.id)) }
function memberSelected(alias: string) { dashboard.selectUnit(alias) }
function targetForSquad(squadId: string) {
  const beacon = dashboard.displayView.value?.current?.map?.beacon?.position
  if (squadId === 'squad_expedition_beacon' && Array.isArray(beacon)) return beacon as [number, number]
  return null
}
async function switchSquad(member: SquadMember, squadId: string) {
  let taskKind = 'HOLD_POSITION'
  if (squadId === 'squad_expedition_beacon') taskKind = 'MOVE_TO_CELL'
  else if (squadId === 'squad_base_defense') taskKind = 'RETREAT_TO_CORE'
  else if (squadId === 'squad_mining_escort') taskKind = 'HARVEST_VISIBLE'
  await dashboard.assignTask(member.alias, taskKind, 850, targetForSquad(squadId))
}
function focusSquad(squadId: string) { dashboard.focusSquad(squadId) }
</script>

<template>
  <aside class="panel unit-panel">
    <div class="section-head"><div><span class="eyebrow">ORDER OF BATTLE</span><h2>战斗序列</h2></div><span id="unitFilterCount" class="tick">{{ filteredEntities.length }}/{{ dashboard.entities.value.length }}</span></div>
    <div id="rosterTabs" class="tab-header" style="display:flex;gap:6px;margin:8px 0;">
      <button id="tabUnitsBtn" class="filter-btn" :class="{ 'is-active': activeTab === 'units' }" style="flex:1;" @click="setTab('units')">单位列表</button>
      <button id="tabSquadsBtn" class="filter-btn" :class="{ 'is-active': activeTab === 'squads' }" style="flex:1;" @click="setTab('squads')">战术编组</button>
    </div>
    <div id="tabUnitsView" v-show="activeTab === 'units'">
      <input id="unitSearch" v-model="search" class="unit-search" type="search" placeholder="搜索脱敏别名…" aria-label="搜索单位">
      <div id="unitFilters" class="unit-filters"><button v-for="kind in ['ALL', 'CORE', 'WORKER', 'VANGUARD', 'RANGER']" :key="kind" class="filter-btn" :class="{ 'is-active': activeKind === kind }" :data-kind="kind" @click="activeKind = kind">{{ kind === 'ALL' ? '全部' : KIND_LABELS[kind] }}</button></div>
      <div id="unitList" class="unit-list muted">
        <button v-for="entity in filteredEntities" :key="entity.alias" class="unit-row" :class="{ 'is-selected': entity.alias === dashboard.selectedAlias.value }" :data-alias="entity.alias" @click="memberSelected(entity.alias)">
          <span class="unit-glyph" :class="[`kind-${String(entity.kind || 'worker').toLowerCase()}`, { 'is-blocked': entity.blocker, 'is-idle': entity.status === 'IDLE' || entity.action === 'WAIT' }]" aria-hidden="true"></span>
          <span class="unit-row-main"><span class="unit-row-title">{{ KIND_LABELS[entity.kind || ''] || '单位' }} · {{ entity.alias }}</span><span class="unit-row-sub">{{ taskLabel(entity.task) || '空闲' }} · {{ actionLabel(entity.action) || '无动作' }} · {{ entity.position?.join(',') || '状态待同步' }}</span></span>
          <span class="state-pill">{{ statusLabel(entity.status || 'UNKNOWN') }}</span>
        </button>
        <div v-if="!filteredEntities.length" class="muted">没有符合筛选条件的单位</div>
      </div>
    </div>
    <div id="tabSquadsView" v-show="activeTab === 'squads'">
      <div class="squad-toolbar" style="display:flex;justify-content:space-between;align-items:center;margin:6px 0 8px;"><span id="squadTotalStats" class="muted" style="font-size:11px;">共 {{ squads.length }} 个编组 · {{ totalMembers }} 人</span><div style="display:flex;gap:4px;"><button id="btnExpandAllSquads" class="filter-btn" style="min-height:22px;padding:1px 6px;font-size:10px;" @click="expandAll">全部展开</button><button id="btnCollapseAllSquads" class="filter-btn" style="min-height:22px;padding:1px 6px;font-size:10px;" @click="collapseAll">全部折叠</button></div></div>
      <div id="squadList" class="squad-list muted">
        <div v-for="squad in squads" :key="squad.id" class="squad-card" :class="{ 'is-collapsed': collapsedSquadIds.has(squad.id) }" :data-squad-id="squad.id">
          <button class="squad-card-header" :data-squad-id="squad.id" @click="toggleSquad(squad.id)"><span style="display:flex;align-items:center;min-width:0;gap:4px;"><span class="squad-collapse-indicator">{{ collapsedSquadIds.has(squad.id) ? '▶' : '▼' }}</span><strong>{{ SQUAD_TYPE_ICONS[squad.type || ''] || '🚩' }} {{ squad.name }}</strong><span class="tag">{{ squad.members?.length || 0 }} 人</span></span><span class="state-pill">{{ squad.status }}</span></button>
          <div v-if="!collapsedSquadIds.has(squad.id)" class="squad-card-body"><div class="squad-meta">类型: {{ SQUAD_TYPE_LABELS[squad.type || ''] || squad.type }}<span v-if="squad.target"> · 目标 {{ squad.target.join(',') }}</span></div><button class="squad-flow focus-squad" :data-squad-id="squad.id" @click="focusSquad(squad.id)">{{ squad.causality?.flow_reason || '当前策略模式重新分配编组。' }}</button><div class="squad-members-box"><div v-for="member in squad.members || []" :key="member.alias" class="squad-member-row" :class="{ 'is-selected': member.alias === dashboard.selectedAlias.value }"><button class="squad-member-info select-squad-unit" :data-alias="member.alias" @click="memberSelected(member.alias)"><span class="unit-glyph" :class="`kind-${String(member.kind || 'worker').toLowerCase()}`"></span><b>{{ KIND_LABELS[member.kind || ''] || member.kind }}</b> <small class="muted">{{ member.alias.slice(0, 15) }}</small><div class="squad-member-sub">{{ member.position ? `[${member.position.join(',')}]` : '' }} {{ member.hp == null ? '' : `HP ${member.hp}` }}{{ member.cargo ? ` · 货 ${member.cargo}` : '' }}{{ member.task ? ` · ${taskLabel(member.task)}` : '' }}</div></button><select class="squad-switch-select" :value="squad.id" :data-alias="member.alias" @click.stop @change="switchSquad(member, ($event.target as HTMLSelectElement).value)"><option v-for="targetSquad in squads" :key="targetSquad.id" :value="targetSquad.id">{{ targetSquad.name }}</option></select></div><div v-if="!squad.members?.length" class="muted">暂无分配成员</div></div></div>
        </div>
        <div v-if="!squads.length" class="muted">尚无编组数据</div>
      </div>
    </div>
  </aside>
</template>
