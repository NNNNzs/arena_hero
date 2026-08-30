<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import CommandHeader from './components/CommandHeader.vue'
import EventDrawer from './components/EventDrawer.vue'
import ReplayPanel from './components/ReplayPanel.vue'
import RosterPanel from './components/RosterPanel.vue'
import SituationPanel from './components/SituationPanel.vue'
import TacticalMapPanel from './components/TacticalMapPanel.vue'
import { createDashboardStore, provideDashboardStore } from './state/dashboard'

const dashboard = createDashboardStore()
provideDashboardStore(dashboard)
onMounted(() => dashboard.start())
onUnmounted(() => dashboard.stop())
</script>

<template>
  <main class="command-center">
    <CommandHeader />
    <section class="operations-shell">
      <RosterPanel />
      <TacticalMapPanel />
      <SituationPanel />
    </section>
    <EventDrawer />
    <ReplayPanel />
  </main>
</template>
