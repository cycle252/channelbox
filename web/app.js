'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="guide-token"]').content;
const ROW = 104, PAGE = 80;
const state = {group:'', favorites:false, q:'', meta:null, current:null, count:0, pages:new Map(), loading:new Set(), generation:0, connected:false};
let queryTimer, toastTimer, scrollFrame, controlQueue = Promise.resolve();

function node(tag, cls, text) { const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e; }
async function api(path, data) {
  const options={headers:{'X-Guide-Token':token},signal:AbortSignal.timeout(12000)};
  if(data){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(data);}
  const response=await fetch(path,options); const result=await response.json();
  if(!response.ok)throw Error(result.error || 'Could not connect to ChannelBox');
  return result;
}
function toast(message, error=false) {const e=$('toast');e.textContent=message;e.classList.toggle('error',error);e.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.hidden=true,error?6500:3000);}
function closeMenu() {$('sidebar').classList.remove('open');$('menu-backdrop').hidden=true;}
function selectLibrary(group='', favorites=false) {state.group=group;state.favorites=favorites;closeMenu();updateNavigation();resetResults();}
function updateNavigation() {
  $('all-channels').classList.toggle('active',!state.group&&!state.favorites);
  $('favorites').classList.toggle('active',state.favorites);
  $('heading').textContent=state.favorites?'Favorites':state.group||'All channels';
  $('subtitle').textContent=state.favorites?'Your go-to channels, ready when you are.':state.group?'Explore this category. Select a channel to watch on your TV.':'A little less searching. A little more watching.';
  document.querySelectorAll('.category').forEach(b=>{const active=b.dataset.group===state.group;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});
}
function renderCategories() {
  if(!state.meta)return;
  const query=$('category-search').value.toLocaleLowerCase();
  const fragment=document.createDocumentFragment();
  state.meta.groups.filter(g=>g.name.toLocaleLowerCase().includes(query)).forEach(g=>{
    const b=node('button','category');b.dataset.group=g.name;b.title=g.name;
    b.append(node('span','category-label',g.name),node('span','category-count',g.count.toLocaleString()));
    b.addEventListener('click',()=>selectLibrary(g.name));fragment.append(b);
  });
  $('categories').replaceChildren(fragment);updateNavigation();
}
async function loadMeta() {
  const previous=state.meta?.revision, previousFavorites=state.meta?.favorites.join(','), previousEpg=state.meta?.epg?.updated_at;state.meta=await api('/api/meta');
  $('all-count').textContent=state.meta.total.toLocaleString();$('favorite-count').textContent=state.meta.favorites.length.toLocaleString();$('category-count').textContent=state.meta.groups.length;
  if(state.meta.updated_at){const date=new Date(state.meta.updated_at);if(!Number.isNaN(date.valueOf()))$('updated').textContent='Playlist refreshed '+new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(date);}
  const epg=state.meta.epg;
  if(epg?.updated_at){const time=new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(epg.updated_at));$('epg-updated').textContent='Listings updated '+time+(epg.refresh_failed?' · using saved listings':'');}
  else $('epg-updated').textContent=epg?.loading?'Loading program listings…':'Program listings unavailable';
  renderCategories();
  if(previous&&previous!==state.meta.revision)resetResults();
  else if(previousFavorites!==undefined&&previousFavorites!==state.meta.favorites.join(',')){
    const favorites=new Set(state.meta.favorites);state.pages.forEach(items=>items.forEach(item=>item.favorite=favorites.has(item.number)));
    if(state.favorites)resetResults();else renderRows();
  }
  if(previousEpg!==undefined&&previousEpg!==epg?.updated_at)refreshPrograms();
}
function parameters(page) {
  const params=new URLSearchParams({q:state.q,group:state.group,favorites:state.favorites?'1':'0',offset:String(page*PAGE),limit:String(PAGE)});
  return '/api/channels?'+params;
}
function resetResults(keepPosition=false) {
  state.generation++;state.pages.clear();state.loading.clear();
  if(!keepPosition){state.count=0;$('channel-scroll').scrollTop=0;$('channels').style.height='0px';}
  $('empty').hidden=true;$('channels').replaceChildren();$('result-count').textContent='Finding your channels…';
  loadPage(Math.floor($('channel-scroll').scrollTop/(ROW*PAGE)),state.generation);
}
async function loadPage(page, generation=state.generation, force=false) {
  if((state.pages.has(page)&&!force)||state.loading.has(page))return;
  state.loading.add(page);
  try {
    const result=await api(parameters(page));if(generation!==state.generation)return;
    state.pages.set(page,result.items);state.count=result.count;
    // Only keep a small window of rows in memory, even for large lineups.
    if(state.pages.size>8){const farthest=[...state.pages.keys()].sort((a,b)=>Math.abs(b-page)-Math.abs(a-page))[0];state.pages.delete(farthest);}
    $('result-count').textContent=result.count.toLocaleString()+' '+(result.count===1?'channel':'channels')+(state.q?' matching “'+state.q+'”':'');
    $('channels').style.height=(result.count*ROW)+'px';$('empty').hidden=result.count!==0;
    $('empty-text').textContent=state.favorites&&!state.q?'Tap the star beside any channel to build your favorites.':'Try another name, channel number, or category.';
    renderRows();
  } catch(error) {if(generation===state.generation){$('result-count').textContent='Could not load channels';toast(error.message,true);}}
  finally {if(generation===state.generation)state.loading.delete(page);}
}
const programmeClock=new Intl.DateTimeFormat(undefined,{hour:'numeric',minute:'2-digit'});
function programmeRange(programme){return programmeClock.format(new Date(programme.start*1000))+' – '+programmeClock.format(new Date(programme.stop*1000));}
function progress(programme){return Math.max(0,Math.min(100,(Date.now()/1000-programme.start)/(programme.stop-programme.start)*100));}
function showCurrentProgramme(item){
  const now=item.now;$('now-program-title').textContent=now?.title||'No program information';
  $('now-program-time').textContent=now?programmeRange(now):'';
  $('now-program-progress').style.width=now?progress(now)+'%':'0%';
  $('next-program').textContent=item.next?'NEXT · '+programmeClock.format(new Date(item.next.start*1000))+' · '+item.next.title:'';
}
async function refreshPrograms(){
  if(!state.meta||!state.count)return;
  const top=$('channel-scroll').scrollTop, height=$('channel-scroll').clientHeight;
  const first=Math.floor(Math.max(0,Math.floor(top/ROW)-3)/PAGE), last=Math.floor(Math.min(state.count-1,Math.ceil((top+height)/ROW)+5)/PAGE);
  await Promise.all(Array.from({length:last-first+1},(_,i)=>loadPage(first+i,state.generation,true)));
  if(state.current)loadCurrentDetails(state.current);
}
function logo(container, item, large=false) {
  const initials=item.name.replace(/[^\p{L}\p{N} ]/gu,'').split(/\s+/).filter(Boolean).slice(0,2).map(x=>x[0]).join('').toUpperCase()||'TV';
  container.textContent=initials;
  if(!item.logo)return;
  const img=node('img','logo-loading');img.alt='';img.loading='lazy';img.decoding='async';img.src=item.logo;
  img.onload=()=>{img.classList.remove('logo-loading');container.replaceChildren(img);};
  img.onerror=()=>img.remove();
  container.append(img);
}
function renderRows() {
  const scroll=$('channel-scroll');const start=Math.max(0,Math.floor(scroll.scrollTop/ROW)-3);const end=Math.min(state.count,start+Math.ceil(scroll.clientHeight/ROW)+8);
  const fragment=document.createDocumentFragment();
  for(let i=start;i<end;i++){
    const page=Math.floor(i/PAGE),item=state.pages.get(page)?.[i%PAGE];
    if(!item){const placeholder=node('div','channel-row skeleton');placeholder.style.top=(i*ROW)+'px';placeholder.setAttribute('aria-hidden','true');fragment.append(placeholder);if(!state.loading.has(page))loadPage(page);continue;}
    const row=node('div','channel-row');row.style.top=(i*ROW)+'px';row.dataset.number=item.number;row.setAttribute('role','listitem');
    const playing=state.current?.number===item.number;row.classList.toggle('playing',playing);
    const select=node('button','channel-select');select.setAttribute('aria-label','Watch channel '+item.number+', '+item.name);
    const image=node('span','channel-logo');logo(image,item);
    const copy=node('span','channel-copy');copy.append(node('span','channel-name',item.name));
    const programme=node('span','channel-program'+(item.now?'':' unavailable'));
    if(item.now){programme.append(node('span','program-label','NOW'),node('span','program-title',item.now.title));select.title=item.now.title+' · '+programmeRange(item.now)+(item.next?'\nNext: '+item.next.title:'');}
    else programme.append(node('span','program-title',state.meta?.epg?.loading?'Loading program listings…':'No program information'));
    copy.append(programme,node('span','channel-group',(item.now?programmeRange(item.now)+' · ':'')+item.group));
    if(item.now){const track=node('span','program-progress');const fill=node('span');fill.style.width=progress(item.now)+'%';track.append(fill);copy.append(track);}
    select.append(node('span','channel-number',item.number),image,copy);
    if(playing)select.append(node('span','playing-label','ON TV'));else select.append(node('span','play-symbol','▶'));
    select.addEventListener('click',()=>command({action:'tune',number:item.number,identity:item.identity},()=>toast('Playing '+item.name+' on your TV')));
    const star=node('button','favorite-button'+(item.favorite?' selected':''),item.favorite?'★':'☆');star.setAttribute('aria-label',(item.favorite?'Remove ':'Add ')+item.name+(item.favorite?' from':' to')+' favorites');star.setAttribute('aria-pressed',String(item.favorite));
    star.addEventListener('click',()=>command({action:'favorite',number:item.number,identity:item.identity,enabled:!item.favorite},async()=>{
      await loadMeta();resetResults(!state.favorites);toast(item.favorite?'Removed from favorites':'Added to favorites');
    }));
    row.append(select,star);fragment.append(row);
  }
  $('channels').replaceChildren(fragment);
}
function displayStatus(status) {
  const changed=state.current?.number!==status.number;state.current=status;state.connected=true;
  $('connection').textContent='Connected to your TV';$('connection-dot').className='dot online';
  $('now-number').textContent='CHANNEL '+status.number;$('now-name').textContent=status.name;
  const loaded=[...state.pages.values()].flat().find(c=>c.number===status.number);
  if(loaded){$('now-group').textContent=loaded.group;showCurrentProgramme(loaded);if(changed||!$('now-logo').querySelector('img'))logo($('now-logo'),loaded,true);}
  else if(changed){$('now-group').textContent='Playing on your TV';$('now-logo').textContent='▦';loadCurrentDetails(status);}
  if(changed)renderRows();
}
async function loadCurrentDetails(status) {
  try{const result=await api('/api/channels?'+new URLSearchParams({number:status.number}));if(state.current?.number!==status.number)return;const item=result.items.find(c=>c.number===status.number);if(item){$('now-group').textContent=item.group;showCurrentProgramme(item);logo($('now-logo'),item,true);}}
  catch(_){/* Channel remains usable without a logo. */}
}
async function status() {
  try{displayStatus(await api('/api/status'));}
  catch(_){state.connected=false;$('connection').textContent='TV player disconnected';$('connection-dot').className='dot offline';}
}
function command(data, done) {
  controlQueue=controlQueue.then(async()=>{try{displayStatus(await api('/api/control',data));if(done)await done();}catch(error){toast(error.message,true);await status();}});
  return controlQueue;
}
$('all-channels').onclick=()=>selectLibrary();$('favorites').onclick=()=>selectLibrary('',true);
$('clear-filters').onclick=()=>{$('search').value='';state.q='';selectLibrary();};
$('category-search').oninput=renderCategories;
$('search').oninput=()=>{clearTimeout(queryTimer);queryTimer=setTimeout(()=>{state.q=$('search').value.trim();resetResults();},220);};
$('channel-scroll').onscroll=()=>{if(!scrollFrame)scrollFrame=requestAnimationFrame(()=>{scrollFrame=null;renderRows();});};
window.addEventListener('resize',()=>renderRows());
$('open-menu').onclick=()=>{$('sidebar').classList.add('open');$('menu-backdrop').hidden=false;};$('close-menu').onclick=closeMenu;$('menu-backdrop').onclick=closeMenu;
$('reload-library').onclick=async()=>{try{await loadMeta();resetResults();await status();toast('Channel library refreshed');}catch(error){toast(error.message,true);}};
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>command({action:'key',key:b.dataset.key}));
document.addEventListener('keydown',e=>{if(e.key==='/'&&!e.target.closest('input')){e.preventDefault();$('search').focus();}if(e.key==='Escape'){closeMenu();if(document.activeElement===$('search')){$('search').value='';state.q='';resetResults();}}});
async function start(){try{await loadMeta();resetResults();await status();}catch(error){toast(error.message,true);$('subtitle').textContent='Could not load the channel library. Try Refresh library.';}}
start();setInterval(()=>{if(!document.hidden)status();},5000);setInterval(()=>{if(!document.hidden)loadMeta().catch(()=>{});},30000);
setInterval(()=>{if(!document.hidden)refreshPrograms();},30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden){loadMeta().catch(()=>{});refreshPrograms();}});
