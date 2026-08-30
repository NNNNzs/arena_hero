<template>
  <aside class="panel unit-panel">
    <div class="section-head"><div><span class="eyebrow">ORDER OF BATTLE</span><h2>战斗序列</h2></div><span id="unitFilterCount" class="tick">—</span></div>
    <div id="rosterTabs" class="tab-header" style="display:flex;gap:6px;margin:8px 0;">
      <button id="tabUnitsBtn" class="filter-btn is-active" style="flex:1;">单位列表</button>
      <button id="tabSquadsBtn" class="filter-btn" style="flex:1;">战术编组</button>
    </div>
    <div id="tabUnitsView">
      <input id="unitSearch" class="unit-search" type="search" placeholder="搜索脱敏别名…" aria-label="搜索单位">
      <div id="unitFilters" class="unit-filters"><button class="filter-btn is-active" data-kind="ALL">全部</button><button class="filter-btn" data-kind="CORE">核心</button><button class="filter-btn" data-kind="WORKER">工人</button><button class="filter-btn" data-kind="VANGUARD">先锋</button><button class="filter-btn" data-kind="RANGER">游侠</button></div>
      <div id="unitList" class="unit-list muted">尚无单位</div>
    </div>
    <div id="tabSquadsView" hidden>
      <div class="squad-toolbar" style="display:flex;justify-content:space-between;align-items:center;margin:6px 0 8px;">
        <span id="squadTotalStats" class="muted" style="font-size:11px;">共 0 个编组</span>
        <div style="display:flex;gap:4px;"><button id="btnExpandAllSquads" class="filter-btn" style="min-height:22px;padding:1px 6px;font-size:10px;">全部展开</button><button id="btnCollapseAllSquads" class="filter-btn" style="min-height:22px;padding:1px 6px;font-size:10px;">全部折叠</button></div>
      </div>
      <div id="squadList" class="squad-list muted">尚无编组数据</div>
    </div>
  </aside>
</template>
