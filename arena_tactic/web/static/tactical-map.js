(()=>{const svg=document.getElementById('map');if(!svg)return;
const kindLabels={CORE:'核心',WORKER:'工人',VANGUARD:'先锋',RANGER:'游侠'};
const kindColors={CORE:'#f4bd61',WORKER:'#58a6ff',VANGUARD:'#b98cff',RANGER:'#54dfcb'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const point=p=>Array.isArray(p)&&p.length===2;
const normalize=a=>{const s=String(a||'');return s.startsWith('entity_')?s.slice(7):s};
function draw(d){const m=d?.current?.map||{},cc=d?.command_center||{},byAlias=new Map((cc.entities||[]).map(e=>[normalize(e.alias),e]));
  const friendly=(m.friendly||[]).map(o=>({...o,...(byAlias.get(normalize(o.alias))||{})}));
  const enemies=m.enemies||[],resources=m.resources||[],obstacles=m.obstacles||[],beacon=point(m.beacon?.position)?m.beacon.position:null;
  const all=[...friendly,...enemies,...resources.map(position=>({position})),...obstacles.map(position=>({position})),...(beacon?[{position:beacon}]:[])],cells=all.map(x=>x.position).filter(point);
  if(!cells.length){svg.innerHTML='<text x="16" y="28">暂无当前可见地图事实</text>';return}
  const xs=cells.map(p=>p[0]),ys=cells.map(p=>p[1]);let minX=Math.min(...xs)-2,maxX=Math.max(...xs)+2,minY=Math.min(...ys)-2,maxY=Math.max(...ys)+2;
  const scale=Math.min(34,Math.max(10,Math.min(360/(maxX-minX+1),210/(maxY-minY+1)))),x=p=>20+(p[0]-minX)*scale,y=p=>235-(p[1]-minY)*scale;
  const marker=(p,color,size=7)=>`<circle cx="${x(p)}" cy="${y(p)}" r="${Math.max(size/2,scale*.42)}" fill="${color}" stroke="#081018" stroke-width="1.5"/>`;
  let body=`<text x="10" y="13">范围 x:${minX}..${maxX} · y:${minY}..${maxY} · ${esc(d.current?.mode_label||'当前态势')}</text>`;
  for(let gx=Math.ceil(minX/5)*5;gx<=maxX;gx+=5)body+=`<line x1="${x([gx,minY])}" y1="${y([gx,minY])}" x2="${x([gx,maxY])}" y2="${y([gx,maxY])}" stroke="#294054" stroke-width=".5" opacity=".55"/>`;
  for(let gy=Math.ceil(minY/5)*5;gy<=maxY;gy+=5)body+=`<line x1="${x([minX,gy])}" y1="${y([minX,gy])}" x2="${x([maxX,gy])}" y2="${y([maxX,gy])}" stroke="#294054" stroke-width=".5" opacity=".55"/>`;
  body+=obstacles.map(p=>`<rect x="${x(p)-Math.max(3,scale*.3)}" y="${y(p)-Math.max(3,scale*.3)}" width="${Math.max(6,scale*.6)}" height="${Math.max(6,scale*.6)}" fill="#5b6673" opacity=".85"><title>障碍物 ${esc(p.join(','))}</title></rect>`).join('');
  body+=resources.map(p=>`<rect x="${x(p)-Math.max(3,scale*.35)}" y="${y(p)-Math.max(3,scale*.35)}" width="${Math.max(6,scale*.7)}" height="${Math.max(6,scale*.7)}" fill="#f4bd61" transform="rotate(45 ${x(p)} ${y(p)})"><title>资源 ${esc(p.join(','))}</title></rect>`).join('');
  if(beacon)body+=`<circle cx="${x(beacon)}" cy="${y(beacon)}" r="${Math.max(5,scale*.7)}" fill="none" stroke="#54dfcb" stroke-width="2"/><text x="${x(beacon)+6}" y="${y(beacon)-6}" fill="#54dfcb">信标</text>`;
  friendly.forEach(o=>{if(!point(o.position))return;const color=kindColors[o.kind]||'#58a6ff',label=kindLabels[o.kind]||'单位';if(point(o.target_cell)&&String(o.action||'')!=='WAIT')body+=`<line x1="${x(o.position)}" y1="${y(o.position)}" x2="${x(o.target_cell)}" y2="${y(o.target_cell)}" stroke="${color}" stroke-width="1" stroke-dasharray="3 3" opacity=".8"/>`;body+=marker(o.position,color);body+=`<title>${esc(label)} ${esc(o.alias)} · 位置 ${esc(o.position.join(','))}${point(o.target_cell)?` · 目标 ${esc(o.target_cell.join(','))}`:''}</title>`});
  enemies.forEach(o=>{if(!point(o.position))return;body+=marker(o.position,'#ff6b7a',8)+`<text x="${x(o.position)+6}" y="${y(o.position)-5}" fill="#ff6b7a">敌</text><title>敌方单位 · 位置 ${esc(o.position.join(','))}</title>`});
  const counts=friendly.reduce((a,o)=>(a[o.kind]=(a[o.kind]||0)+1,a),{});let lx=12;body+=`<rect x="8" y="242" width="${Math.min(380,90+Object.keys(counts).length*72)}" height="15" rx="4" fill="#0b141d" opacity=".92"/>`;Object.entries(counts).forEach(([k,n])=>{body+=`<circle cx="${lx+4}" cy="249" r="3" fill="${kindColors[k]||'#58a6ff'}"/><text x="${lx+10}" y="252">${esc(kindLabels[k]||k)} ${n}</text>`;lx+=72});body+=`<text x="${Math.min(360,lx+4)}" y="252" fill="#ff6b7a">敌方 ${enemies.length}</text>`;svg.innerHTML=body}
async function refresh(){try{const response=await fetch('/api/dashboard',{cache:'no-store'});if(response.ok)draw(await response.json())}catch{}}refresh();setInterval(refresh,3000)})();
