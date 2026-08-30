<script setup lang="ts">
import { computed } from 'vue'
import { useDashboardStore } from '../state/dashboard'

const dashboard = useDashboardStore()
const frame = computed(() => dashboard.selectedFrame.value)
const markerFrames = computed(() => dashboard.replayFrames.value.flatMap((item, index) => (item.markers || []).map(marker => ({ marker, index, tick: item.tick }))))
const replayState = computed(() => !frame.value ? '等待回放快照' : dashboard.replayLive.value ? '实时跟随最新 Tick' : '历史回放 · 自动态势仍在后台更新')
function selectSlider(event: Event) { dashboard.selectReplay(Number((event.target as HTMLInputElement).value)) }
</script>

<template>
  <section class="replay-panel" aria-label="作战时间轴"><div class="replay-heading"><h2>作战时间轴</h2><div><span id="replayState" class="muted">{{ replayState }}</span><strong id="replayTick" class="tick">{{ frame ? `#${frame.tick ?? '—'}` : '—' }}</strong></div></div><div id="replayTrack" class="replay-track"><input id="replaySlider" type="range" min="0" :max="Math.max(0, dashboard.replayFrames.value.length - 1)" :value="dashboard.replayIndex.value" step="1" aria-label="回放 Tick" @input="selectSlider"><div id="replayMarkers" class="replay-markers" aria-label="关键战局事件"><button v-for="item in markerFrames" :key="`${item.index}-${item.marker.kind}`" class="replay-marker" :class="String(item.marker.kind || '').toLowerCase()" :style="{ left: `${item.index / Math.max(1, dashboard.replayFrames.value.length - 1) * 100}%` }" :data-index="item.index" :title="`#${item.tick} · ${item.marker.label}`" @click="dashboard.selectReplay(item.index)"></button></div><div id="replayHover" class="replay-hover" role="status" aria-live="polite" hidden></div></div><div class="replay-controls"><button id="replayStart" class="neutral" title="回到当前窗口起点" @click="dashboard.selectReplay(0)">⏮</button><button id="replayPrev" class="neutral" title="上一帧；到达起点时加载更早历史" @click="dashboard.selectReplay(dashboard.replayIndex.value - 1)">⏪</button><button id="replayPlay" class="neutral" title="播放或暂停" @click="dashboard.toggleReplay">{{ dashboard.replayTimer.value ? '⏸ 暂停' : '▶ 播放' }}</button><button id="replayNext" class="neutral" title="下一帧" @click="dashboard.selectReplay(dashboard.replayIndex.value + 1)">⏩</button><button id="replayLoadEarlier" class="neutral" title="加载更早的历史" @click="dashboard.loadEarlierReplay">↞ 更早</button><button id="replayLive" class="neutral" title="跟随实时" @click="dashboard.setReplayLive">⏭ 实时</button><button id="eventDrawerToggle" class="neutral event-drawer-toggle" :aria-expanded="dashboard.eventDrawerOpen.value" aria-controls="eventDrawer" @click="dashboard.setEventDrawer(!dashboard.eventDrawerOpen.value)">📜 日志 <span id="eventCountAll" class="event-count">{{ dashboard.eventState.value.total || 0 }}</span></button></div></section>
</template>
