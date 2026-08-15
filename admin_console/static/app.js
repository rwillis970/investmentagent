const labels={"com.investmentagent.reconcile-loop":"Reconcile Loop","com.investmentagent.dashboard":"Dashboard Service"};
const esc=x=>String(x??'UNAVAILABLE').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const csrfHeaders={'X-InvestmentAgent-CSRF':document.querySelector('meta[name="investmentagent-csrf"]').content};
const tone=value=>['FAIL','ACTIVE','STOPPED'].includes(value)?'fail':['STALE','NOT_YET_OBSERVED'].includes(value)?'caution':['UNAVAILABLE','UNKNOWN'].includes(value)?'unknown':'good';
const card=(name,value,extra='')=>`<article><span>${esc(name)}</span><strong class="${tone(value)}">${esc(value)}</strong>${extra?`<small>${esc(extra)}</small>`:''}</article>`;
async function act(label,action){await fetch(`/api/services/${encodeURIComponent(label)}/${action}`,{method:'POST',headers:csrfHeaders}); await load();}
async function utility(name){const r=await fetch(`/api/utilities/${name}`,{method:'POST',headers:csrfHeaders}); document.querySelector('#utility-output').textContent=JSON.stringify(await r.json(),null,2); await load();}
async function logs(){const r=await fetch('/api/logs'); document.querySelector('#utility-output').textContent=JSON.stringify(await r.json(),null,2);}
async function load(){const s=await (await fetch('/api/status')).json(); let h='';
for(const [l,n] of Object.entries(labels)){const v=s.services[l]; h+=card(n,v.state,v.pid?`PID ${v.pid}`:v.detail);}
h+=card('Broker Environment',s.broker_environment)+card('Operational State',s.operational_state)+card('Reconciliation',s.runtime.reconciliation,s.runtime.reconciliation_at)+(s.runtime.stale?card('Runtime Status','STALE',`${s.runtime.status} · ${s.runtime.generated_at}`):card('Runtime Status',s.runtime.status,s.runtime.generated_at))+card('Failure Sentinel',s.failure_sentinel.state,s.failure_sentinel.last_at)+card('Local Settled Cash',s.local_settled_cash.status)+card('Broker Settled Cash',s.broker_settled_cash.status)+card('Positions',s.positions.status)+card('Latest Backup',s.backup.status,s.backup.timestamp||s.backup.path); document.querySelector('#system').innerHTML=h;
document.querySelector('#controls').innerHTML=Object.entries(labels).map(([l,n])=>`<div><b>${n}</b> ${['start','stop','restart'].map(a=>`<button onclick="act('${l}','${a}')">${a[0].toUpperCase()+a.slice(1)}</button>`).join(' ')}</div>`).join('');
document.querySelector('#protection').innerHTML=card('Git Branch',s.git.branch)+card('Git HEAD',s.git.head)+card('Working Tree',s.git.working_tree)+card('Runtime Data Git Tracking',s.runtime_data_git_tracking.status,(s.runtime_data_git_tracking.tracked||[]).join(', '));
const d=document.querySelector('#dashboard'); if(s.dashboard.status==='AVAILABLE'){d.textContent='Open Dashboard';d.href=s.dashboard.url;d.classList.remove('disabled');} document.querySelector('#updated').textContent=`Updated ${new Date().toLocaleString()}`;}
load(); setInterval(load,15000);
