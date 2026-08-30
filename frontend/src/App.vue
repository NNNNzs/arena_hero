<script setup lang="ts">
import { onMounted } from 'vue'
import CommandHeader from './components/CommandHeader.vue'
import EventDrawer from './components/EventDrawer.vue'
import ReplayPanel from './components/ReplayPanel.vue'
import RosterPanel from './components/RosterPanel.vue'
import SituationPanel from './components/SituationPanel.vue'
import TacticalMapPanel from './components/TacticalMapPanel.vue'
import { bootLegacyDashboard } from './legacy/boot'

onMounted(() => {
  void bootLegacyDashboard()
})
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
