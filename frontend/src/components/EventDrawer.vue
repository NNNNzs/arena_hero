<template>
  <section id="eventDrawer" class="panel event-drawer" aria-label="事件日志" hidden>
    <div class="section-head"><div><span class="eyebrow">EVENT LOG / COMBAT · HARVEST · OPERATIONS</span><h2>事件日志</h2></div><button id="eventDrawerClose" class="neutral event-drawer-close" title="收起">✕</button></div>
    <div id="eventFilters" class="event-filters"><button class="filter-btn is-active" data-event-category="ALL">全部 <span id="eventCountAllDrawer" class="event-count">0</span></button><button class="filter-btn" data-event-category="combat">战斗 <span id="eventCountCombat" class="event-count">0</span></button><button class="filter-btn" data-event-category="harvest">采集 <span id="eventCountHarvest" class="event-count">0</span></button><button class="filter-btn" data-event-category="ops">运营 <span id="eventCountOps" class="event-count">0</span></button><button class="filter-btn" data-event-category="anomaly">异常 <span id="eventCountAnomaly" class="event-count">0</span></button></div>
    <div id="eventLogList" class="event-log-list muted">尚无关键事件</div>
  </section>
</template>
