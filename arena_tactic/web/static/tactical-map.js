(()=>{
  const svg=document.getElementById('map');if(!svg)return;
  const kindLabels={CORE:'核心',WORKER:'工人',VANGUARD:'先锋',RANGER:'游侠'};
  const kindColors={CORE:'#f4bd61',WORKER:'#58a6ff',VANGUARD:'#b98cff',RANGER:'#54dfcb'};
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const point=p=>Array.isArray(p)&&p.length===2&&p.every(Number.isFinite);
  const normalize=a=>String(a||'').replace(/^entity_/,'');
  const distance=(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1]);
  const bearing=(dx,dy)=>{const names=['东','东北','北','西北','西','西南','南','东南'];return names[Math.round(Math.atan2(dy,dx)/(Math.PI/4)+8)%8]};
  function draw(data){
    const m=data?.current?.map||{},cc=data?.command_center||{},byAlias=new Map((cc.entities||[]).map(e=>[normalize(e.alias),e]));
    const friendly=(m.friendly||[]).filter(o=>point(o.position)).map(o=>({...o,...(byAlias.get(normalize(o.alias))||{})}));
    const enemies=(m.enemies||[]).filter(o=>point(o.position)),resources=(m.resources||[]).filter(point),obstacles=(m.obstacles||[]).filter(point),beacon=point(m.beacon?.position)?m.beacon.position:null;
    if(!friendly.length){svg.innerHTML='<text x="16" y="28">暂无己方可见实体，无法建立局部视口</text>';return}
    const core=friendly.find(o=>o.kind==='CORE'), anchor=core?.position||friendly.reduce((sum,o)=>[sum[0]+o.position[0],sum[1]+o.position[1]],[0,0]).map(v=>v/friendly.length);
    // Only current local facts may influence scale. Remote landmarks remain radar indicators.
    const local=[...friendly]; const far=[];
    const include=(position,item)=>distance(anchor,position)<=28?local.push(item):far.push(item);
    enemies.forEach(o=>include(o.position,o)); resources.forEach(position=>include(position,{position,resource:true})); obstacles.forEach(position=>include(position,{position,obstacle:true}));
    const localBeacon=beacon&&distance(anchor,beacon)<=28?beacon:null;
    if(beacon&&!localBeacon)far.push({position:beacon,label:'信标',color:'#54dfcb'});
    const xs=local.map(o=>o.position[0]),ys=local.map(o=>o.position[1]); let minX=Math.min(...xs)-3,maxX=Math.max(...xs)+3,minY=Math.min(...ys)-3,maxY=Math.max(...ys)+3;
    const span=Math.max(maxX-minX,maxY-minY,12); minX=anchor[0]-span/2;maxX=anchor[0]+span/2;minY=anchor[1]-span/2;maxY=anchor[1]+span/2;
    const scale=Math.min(28,Math.max(9,Math.min(352/(maxX-minX),205/(maxY-minY)))),x=p=>200+(p[0]-anchor[0])*scale,y=p=>132-(p[1]-anchor[1])*scale;
    const marker=(p,color,size=7,extra='')=>`<circle cx="${x(p)}" cy="${y(p)}" r="${Math.max(size/2,scale*.32)}" fill="${color}" stroke="#081018" stroke-width="1.5" ${extra}/>`;
    let body=`<text x="10" y="13">局部视口 · 中心 ${Math.round(anchor[0])},${Math.round(anchor[1])} · ${esc(data.current?.mode_label||'当前态势')}</text>`;
    for(let gx=Math.ceil(minX/5)*5;gx<=maxX;gx+=5)body+=`<line x1="${x([gx,minY])}" y1="${y([gx,minY])}" x2="${x([gx,maxY])}" y2="${y([gx,maxY])}" stroke="#294054" stroke-width=".5" opacity=".65"/>`;
    for(let gy=Math.ceil(minY/5)*5;gy<=maxY;gy+=5)body+=`<line x1="${x([minX,gy])}" y1="${y([minX,gy])}" x2="${x([maxX,gy])}" y2="${y([maxX,gy])}" stroke="#294054" stroke-width=".5" opacity=".65"/>`;
    body+=`<rect x="20" y="25" width="360" height="210" fill="none" stroke="#36536a" stroke-dasharray="3 3" opacity=".65"/>`;
    local.filter(o=>o.obstacle).forEach(o=>body+=`<rect x="${x(o.position)-4}" y="${y(o.position)-4}" width="8" height="8" fill="#5b6673"><title>障碍物 ${esc(o.position.join(','))}</title></rect>`);
    local.filter(o=>o.resource).forEach(o=>body+=`<rect x="${x(o.position)-4}" y="${y(o.position)-4}" width="8" height="8" fill="#f4bd61" transform="rotate(45 ${x(o.position)} ${y(o.position)})"><title>资源 ${esc(o.position.join(','))}</title></rect>`);
    if(localBeacon)body+=`<circle cx="${x(localBeacon)}" cy="${y(localBeacon)}" r="${Math.max(5,scale*.55)}" fill="none" stroke="#54dfcb" stroke-width="2"/><text x="${x(localBeacon)+6}" y="${y(localBeacon)-6}" fill="#54dfcb">信标</text>`;
    friendly.forEach(o=>{const color=kindColors[o.kind]||'#58a6ff',shoot=o.kind==='RANGER'&&o.action==='SHOOT'&&point(o.target_cell);if(point(o.target_cell)&&distance(anchor,o.target_cell)<=30)body+=`<line x1="${x(o.position)}" y1="${y(o.position)}" x2="${x(o.target_cell)}" y2="${y(o.target_cell)}" stroke="${color}" stroke-width="${shoot?'2':'1'}" stroke-dasharray="${shoot?'5 3':'3 3'}" opacity=".9"/>`;body+=marker(o.position,color, o.status==='BLOCKED'?10:7,o.status==='BLOCKED'?'stroke="#ff6b7a" stroke-width="3"':'')+`<title>${esc(kindLabels[o.kind]||'单位')} ${esc(o.alias)} · HP ${esc(o.hp??'—')} · ${esc(o.action||'待命')}</title>`});
    local.filter(o=>o.enemy).forEach(o=>body+=marker(o.position,'#ff6b7a',8)+`<text x="${x(o.position)+6}" y="${y(o.position)-5}" fill="#ff6b7a">敌</text>`);
    const radar=far.filter(o=>point(o.position)).slice(0,8); radar.forEach((o,index)=>{const dx=o.position[0]-anchor[0],dy=o.position[1]-anchor[1],len=Math.max(1,Math.hypot(dx,dy)),px=200+dx/len*170,py=132-dy/len*96,label=o.label||(o.enemy?'敌方':'远端地标'),d=Math.round(len);body+=`<g><path d="M ${px} ${py} l ${-dx/len*8-dy/len*4} ${dy/len*8-dx/len*4} l ${-dx/len*8+dy/len*4} ${dy/len*8+dx/len*4} Z" fill="${o.color||(o.enemy?'#ff6b7a':'#f4bd61')}"/><text x="${Math.max(6,Math.min(316,px-28))}" y="${Math.max(28,Math.min(228,py+(index%2?14:-6)))}" fill="${o.color||(o.enemy?'#ff6b7a':'#f4bd61')}">${esc(label)} ${bearing(dx,dy)} ${d}格</text></g>`});
    svg.innerHTML=body;
  }
  window.renderTacticalMap=draw;
  if(window.DashboardReplay?.selected)draw(window.DashboardReplay.selected);
})();
