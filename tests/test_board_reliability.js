// Real, isolated HTTP/SQLite data. Faults alter only response delivery, never business data.
const {JSDOM}=require('jsdom');
const fs=require('fs'),cp=require('child_process'),assert=require('assert'),path=require('path');
const root=path.resolve(__dirname,'..'),sleep=n=>new Promise(r=>setTimeout(r,n));
const app=process.env.AURORA_TEST_APP||path.join(root,'static/app.js');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const js=fs.readFileSync(app,'utf8');
const reports=[];
function snapshot(w,name){
 if(!process.env.AURORA_BOARD_SNAPSHOTS)return;
 const css=['style.css','workspace.css','common-nav.css'].map(f=>fs.readFileSync(path.join(root,'static',f),'utf8')).join('\n');
 const document=w.document.documentElement.cloneNode(true);
 document.querySelectorAll('script,link').forEach(n=>n.remove());
 const style=w.document.createElement('style');style.textContent=css;document.querySelector('head').append(style);
 fs.mkdirSync(process.env.AURORA_BOARD_SNAPSHOTS,{recursive:true});
 fs.writeFileSync(path.join(process.env.AURORA_BOARD_SNAPSHOTS,name+'.html'),'<!DOCTYPE html>\n'+document.outerHTML);
}
function gate(){let release;const promise=new Promise(resolve=>release=resolve);return {promise,release};}
async function fixture(name,run,prepare){
 const db='/tmp/aurora-board-'+require('crypto').randomUUID()+'.sqlite3';
 const server=cp.spawn(process.env.PYTHON||'python',['backend.py','--port','0','--db',db],{cwd:root});
 let base,error='',dom;
 server.stdout.on('data',b=>{base||=String(b).match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];});
 server.stderr.on('data',b=>error+=b);
 try{
  for(let i=0;i<160&&!base;i++)await sleep(20);assert(base,error);
  // Recreate the judge's CASE-001=8 / total=13 task scenario only in this fresh test DB.
  const post=async(url,body,token)=>{
   const r=await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':require('crypto').randomUUID(),...(token?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(body)});
   const data=await r.json();assert(r.ok,JSON.stringify(data));return data;
  };
  const quality=await post('/api/session',{actor_id:'quality'});
  const submitted=await post('/api/cases/CASE-001/submit',{expected_version:1},quality.token);
  const committee=await post('/api/session',{actor_id:'committee'});
  await post('/api/cases/CASE-001/decision',{expected_version:submitted.version,decision:'approve',reason:'격리된 보드 조회 회귀 시험용 모의 의결'},committee.token);
  dom=new JSDOM(html,{url:base+'/workspace',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,q=s=>w.document.querySelector(s),errors=[];
  w.AbortController=AbortController;w.confirm=()=>true;w.scrollTo=()=>{};
  w.fetch=(u,o)=>fetch(new URL(u,w.location.href),o);w.addEventListener('error',e=>errors.push(e.message));
  const wait=async f=>{for(let i=0;i<250;i++){if(f())return;await sleep(20)}throw Error('timeout: '+f);};
  prepare?.(w);
  w.eval(js+';window.qa={S,loadState,loadDetail,loadAllDetails,navigate,setRole,renderView,selectCase,openTaskCase};');
  const {S}=w.qa;
  await wait(()=>!S.loading&&!q('#role-select').disabled);
  const result=await run({w,q,S,base,wait});
  assert.deepEqual(errors,[]);reports.push({name,...result});console.log('Board reliability PASS:',name,JSON.stringify(result));
 }finally{
  dom?.window.close();server.kill();
 }
}
(async()=>{
 await fixture('partial-failure-late-success-and-selective-retry',async({w,q,S,base,wait})=>{
  const before=await (await fetch(base+'/api/state')).json();
  const actual=await Promise.all(before.cases.map(c=>fetch(base+'/api/cases/'+c.id).then(r=>r.json())));
  const total=actual.reduce((n,d)=>n+d.tasks.length,0),first=actual[0].tasks.length;
  assert.equal(actual.length,3);assert.equal(total,13);assert.equal(first,8);
  const real=w.fetch,held=gate(),calls={};let waiting=false;
  w.fetch=async(u,o)=>{
   const r=await real(u,o),key=String(u);calls[key]=(calls[key]||0)+1;
   if(key==='/api/cases/CASE-002')return new Response(JSON.stringify({error:{message:'사건별 시험 조회 실패'}}),{status:503});
   if(key==='/api/cases/CASE-003'){waiting=true;await held.promise;}
   return r;
  };
  const loading=w.qa.navigate('tasks');await wait(()=>waiting&&S.details.has('CASE-001'));
  await wait(()=>S.caseReads?.get('CASE-002')?.status==='error'||q('#toast-region').textContent.includes('사건별 시험 조회 실패'));
  assert(q('.read-coverage'),'partial board needs durable coverage, not only a toast: '+q('.filter-summary')?.textContent);
  assert(q('.filter-summary').textContent.includes('일부 업무 8개 표시'));
  assert(q('.filter-summary').textContent.includes('전체 건수 미확정'));
  assert(!q('.filter-summary').textContent.includes('전체 후속 업무 8개'));
  assert(q('[data-read-case="CASE-002"]').textContent.includes('조회 실패'));
  assert(q('[data-read-case="CASE-003"]').textContent.includes('조회 중'));
  assert(!q('.board').textContent.includes('대기 중인 업무 없음'));
  snapshot(w,'partial-eight-tasks');
  q('[data-board-filter="current"]').click();assert.equal(S.boardFilter,'current');
  assert(q('.filter-summary').textContent.includes('전체 건수 미확정'));
  q('[data-board-filter="all"]').click();
  // Another case's failure must not stop a successful delayed case appearing.
  held.release();await loading;
  assert(q('.read-coverage').textContent.includes('사건 조회 완료 2/3'));
  assert(w.document.querySelector('[data-open-case="CASE-003"]'));
  const beforeRetry={...calls};w.fetch=async(u,o)=>{calls[String(u)]=(calls[String(u)]||0)+1;return real(u,o);};
  const retry=q('[data-retry-case="CASE-002"]');retry.focus();retry.click();
  await wait(()=>!q('.read-coverage'));
  assert.equal(calls['/api/cases/CASE-001'],beforeRetry['/api/cases/CASE-001']);
  assert.equal(calls['/api/cases/CASE-003'],beforeRetry['/api/cases/CASE-003']);
  assert.equal(calls['/api/cases/CASE-002'],beforeRetry['/api/cases/CASE-002']+1);
  assert(q('.filter-summary').textContent.includes('3개 사건 · 전체 후속 업무 13개'));
  assert.equal(w.document.querySelectorAll('.task-card').length,13);
  snapshot(w,'complete-thirteen-tasks');
  assert(!q('[data-action="reload"]').disabled);
  const after=await (await fetch(base+'/api/state')).json();
  assert.deepEqual(after.cases.map(c=>[c.id,c.version]),before.cases.map(c=>[c.id,c.version]));
  return {cases:3,partialTasks:first,completeTasks:total,selectiveRetry:true,businessVersionsUnchanged:true};
 });
 await fixture('cached-failure-is-not-current-and-retry-is-deduplicated',async({w,q,S,wait})=>{
  await w.qa.navigate('tasks');assert(q('.filter-summary').textContent.includes('전체 후속 업무 13개'));
  const real=w.fetch;w.fetch=(u,o)=>String(u)==='/api/cases/CASE-002'?Promise.resolve(new Response(JSON.stringify({error:{message:'재조회 실패'}}),{status:503})):real(u,o);
  await w.qa.loadState();
  assert(S.details.has('CASE-002'),'cache is preserved, not destroyed');
  assert(q('[data-read-case="CASE-002"]').textContent.includes('조회 실패'));
  assert(!q('.filter-summary').textContent.includes('전체 후속 업무'));
  assert(!q('[data-open-case="CASE-002"]'),'failed stale cache is not published as fresh work');
  const held=gate();let requests=0;
  w.fetch=async(u,o)=>{if(String(u)==='/api/cases/CASE-002'){requests++;const r=await real(u,o);await held.promise;return r;}return real(u,o);};
  q('[data-retry-case="CASE-002"]').click();await wait(()=>requests===1);
  q('[data-retry-case="CASE-002"]').click();assert.equal(requests,1);
  assert(q('[data-retry-case="CASE-002"]').disabled);
  q('[data-board-filter="current"]').click();
  held.release();await wait(()=>!q('.read-coverage'));
  assert(q('.filter-summary').textContent.includes('/ 전체 13개'));
  assert.equal(w.document.activeElement.dataset.boardFilter,'current','retry must not steal newer filter focus');
  return {staleCacheExcluded:true,repeatedRetryRequests:requests};
 });
 await fixture('detail-timeout-and-persistent-selection-error',async({w,q,S,wait})=>{
  const real=w.fetch,timeout=w.setTimeout.bind(w);
  w.setTimeout=(fn,ms,...args)=>timeout(fn,ms===18000?500:ms,...args);
  w.fetch=async(u,o)=>{
   const r=await real(u,o);
   if(String(u)!=='/api/cases/CASE-002')return r;
   await r.text();return new Promise((_,reject)=>{
    const fail=()=>reject(new DOMException('timed out','AbortError'));
    if(o.signal.aborted)fail();else o.signal.addEventListener('abort',fail,{once:true});
   });
  };
  await w.qa.navigate('tasks');
  assert(q('[data-read-case="CASE-002"]').textContent.includes('조회 시간이 초과'));
  assert(!q('[data-read-case="CASE-002"]').textContent.includes('이력을 새로고침해 처리 여부'));
  assert.equal(w.document.querySelectorAll('.task-card').length,[...S.details.values()].filter(d=>d.id!=='CASE-002').reduce((n,d)=>n+d.tasks.length,0));
  // A board-to-case failure still owns the URL, can be retried, and survives render changes.
  await w.qa.openTaskCase('CASE-002').catch(()=>{});
  assert.equal(new URL(w.location.href).searchParams.get('case'),'CASE-002');
  assert.equal(w.location.hash,'#workspace');assert(q('[data-action="retry-detail"]'));
  await w.qa.setRole('sales');assert(q('[data-action="retry-detail"]'),'role change must not turn an error into an endless spinner');
  assert(q('[data-action="back-to-board"]'));
  w.fetch=real;await w.qa.loadDetail('CASE-002');assert(q('.detail-header').textContent.includes('CASE-002'));
  return {timeoutVisible:true,persistentRetry:true,selectedCase:S.selected};
 });
 await fixture('role-change-after-selected-detail-failure',async({w,q,S,wait})=>{
  const real=w.fetch;
  w.fetch=(u,o)=>String(u)==='/api/cases/CASE-002'?Promise.resolve(new Response(JSON.stringify({error:{message:'선택 사건 조회 실패'}}),{status:503})):real(u,o);
  await w.qa.loadDetail('CASE-002').catch(()=>{});assert.equal(S.detail,null);
  await w.qa.navigate('tasks');assert.equal(S.detail,null);
  q('[data-board-filter="current"]').click();assert(q('.board-notice').textContent.includes('품질팀 역할'));
  await w.qa.setRole('sales');
  assert.equal(S.role,'sales');assert.equal(S.detail,null);
  assert(q('.board-notice').textContent.includes('영업팀 역할'),'board must rerender after role change even without selected detail');
  assert(q('[data-board-filter="current"]').textContent.includes('영업팀'));
  const sales=[...S.details.values()].filter(d=>S.caseReads.get(d.id)?.status==='ready').flatMap(d=>d.tasks).filter(t=>t.department==='sales');
  assert(sales.length>0);assert.equal(w.document.querySelectorAll('.task-card').length,sales.length);
  assert(q('.read-coverage').textContent.includes('조회 실패'));
  const role='committee';await w.qa.setRole(role);
  assert(q('.board-notice').textContent.includes('품질위원회 역할'));
  assert(q('[data-action="new-case"]').disabled);
  return {selectedDetailUnavailable:true,roleViewsUpdated:['sales','committee'],partialCoveragePreserved:true};
 });
 await fixture('delayed-navigation-keeps-newer-filter-focus',async({w,q,S,wait})=>{
  const real=w.fetch,held=gate();let waiting=false;
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/cases/CASE-003'){waiting=true;await held.promise;}return r;};
  const navigating=w.qa.navigate('tasks');await wait(()=>waiting);
  q('[data-board-filter="current"]').click();
  held.release();await navigating;
  assert.equal(S.boardFilter,'current');assert.equal(w.document.activeElement.dataset.boardFilter,'current');
  return {focusStayedCurrent:true};
 });
 await fixture('latest-navigation-wins-while-case-opens',async({w,q,S,wait})=>{
  await w.qa.navigate('tasks');const real=w.fetch,held=gate();let waiting=false;
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/cases/CASE-002'){waiting=true;await held.promise;}return r;};
  const opening=w.qa.openTaskCase('CASE-002');await wait(()=>waiting);
  await w.qa.selectCase('CASE-003');const route=w.location.href;
  held.release();await opening;
  assert.equal(S.selected,'CASE-003');assert.equal(S.detail.id,'CASE-003');assert.equal(w.location.href,route);
  assert(q('.detail-header').textContent.includes('CASE-003'));
  return {selectedCase:S.selected,urlStayedCurrent:true};
 });
 await fixture('state-timeout-retry-and-audit-completeness',async({w,q,S,wait})=>{
  const real=w.fetch;await w.qa.navigate('tasks');
  w.fetch=(u,o)=>String(u)==='/api/state'?Promise.resolve(new Response(JSON.stringify({error:{message:'목록 재조회 실패'}}),{status:503})):real(u,o);
  await w.qa.loadState();assert(q('#view-container>.error-state'));
  await w.qa.navigate('tasks');assert(q('.read-coverage').textContent.includes('사건 목록 갱신 실패'));
  assert(!q('.filter-summary').textContent.includes('전체 후속 업무'));
  await w.qa.navigate('audit');assert(q('.read-coverage').textContent.includes('전체 건수 미확정'));
  w.fetch=real;await w.qa.loadState();assert(!q('.read-coverage'));
  return {stateFailureNotMasked:true,auditCoverage:true};
 });
 await fixture('initial-state-timeout-has-working-reload',async({w,q,S,wait})=>{
  assert(!S.state);assert(q('#view-container>.error-state').textContent.includes('조회 시간이 초과'));
  w.fetch=w.realStartupFetch;q('[data-action="reload"]').click();
  await wait(()=>!S.loading&&q('.detail-header'));assert.equal(S.selected,'CASE-001');
  return {startupRetry:true};
 },w=>{
  const real=w.fetch,timeout=w.setTimeout.bind(w);w.realStartupFetch=real;
  w.setTimeout=(fn,ms,...args)=>timeout(fn,ms===18000?500:ms,...args);
  w.fetch=(u,o)=>String(u)==='/api/state'?new Promise((_,reject)=>o.signal.addEventListener('abort',()=>reject(new DOMException('timed out','AbortError')),{once:true})):real(u,o);
 });
 if(process.env.AURORA_BOARD_REPORT)fs.writeFileSync(process.env.AURORA_BOARD_REPORT,JSON.stringify(reports,null,2)+'\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
