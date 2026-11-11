// Isolated real HTTP/SQLite regression; only response delivery is controlled.
const root=require('path').resolve(__dirname,'..');
const {JSDOM}=require(root+'/node_modules/jsdom');
const fs=require('fs'),cp=require('child_process'),assert=require('assert');
const appPath=root+'/static/app.js';
const sleep=n=>new Promise(r=>setTimeout(r,n));
const report=[];
// Every held network response has a deadline, including failure cleanup paths.
function hold(register){return new Promise((resolve,reject)=>{
 const timer=setTimeout(()=>reject(Error('Held response was not released within 5 seconds')),5000);
 register(()=>{clearTimeout(timer);resolve();});
});}
async function fixture(name,run,prepare){
 const server=cp.spawn(process.env.PYTHON||'python',['backend.py','--port','0','--db',`/tmp/aurora-independent-${require('crypto').randomUUID()}-${name}.sqlite3`],{cwd:root});
 let base,err='',dom;
 server.stdout.on('data',b=>{const m=String(b).match(/http:\/\/127\.0\.0\.1:\d+/);if(m)base=m[0]});
 server.stderr.on('data',b=>err+=b);
 try {
  for(let i=0;i<150&&!base;i++)await sleep(20);assert(base,err);
  dom=new JSDOM(fs.readFileSync(root+'/static/index.html','utf8'),{url:base+'/workspace',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,q=s=>w.document.querySelector(s),errors=[];
  w.fetch=(u,o)=>fetch(new URL(u,w.location.href),o);w.AbortController=AbortController;w.confirm=()=>true;w.scrollTo=()=>{};
  w.addEventListener('error',e=>errors.push(e.message));
  prepare?.(w);
  w.eval(fs.readFileSync(appPath,'utf8')+';window.qa={S,loadState,loadAllDetails,loadDetail,navigate,setRole,selectCase,selectDetailTab,handleForm};');
  const {S}=w.qa;
  const wait=async f=>{for(let i=0;i<200;i++){if(f())return;await sleep(20)}throw Error('timeout '+f+' '+q('#form-error')?.textContent)};
  const click=s=>{assert(q(s),'missing '+s);q(s).click()};
  const submit=()=>q('#action-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  await wait(()=>!S.loading&&S.state&&!q('#role-select').disabled);
  const result=await run({w,q,S,wait,click,submit,base});
  assert.deepEqual(errors,[]);
  assert(!result.bug,name+' must retain current UI state: '+JSON.stringify(result));
  if(name==='stale-state-after-submit'){
   assert.equal(result.after.summaryVersion,result.saved.summaryVersion);
   assert.equal(result.after.pending,result.saved.pending);
  }
  if(name==='stale-state-after-new-case'){
   assert.equal(result.after.selected,result.saved.selected);
   assert.equal(result.after.caseCount,result.saved.caseCount);
   assert.equal(result.after.url,result.saved.url);
  }
  report.push({name,...result});console.log('Async refresh PASS:',name);
 }catch(e){throw new Error(name+': '+e.stack);}
 finally{dom?.window.close();server.kill();}
}
async function holdOneState({w,wait}){
 const real=w.fetch;let release,body,held=false;
 w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/state'&&!held){held=true;body=await r.text();await hold(res=>release=res);return new Response(body,{status:r.status})}return r};
 const p=w.qa.loadState();await wait(()=>release);return {release,p,real};
}
module.exports=async function(){
 await fixture('stale-state-after-submit',async c=>{
  const {w,q,S,wait,click,submit,base}=c;
  const held=await holdOneState(c);
  click('[data-action="submit"]');submit();await wait(()=>!q('.modal'));
  const saved={version:S.detail.version,status:S.detail.status,summaryVersion:S.state.cases[0].version,pending:S.state.summary.pending_approvals};
  held.release();await held.p;w.fetch=held.real;
  const actual=await (await fetch(base+'/api/cases/CASE-001')).json();
  return {saved,after:{detailVersion:S.detail.version,detailStatus:S.detail.status,summaryVersion:S.state.cases[0].version,summaryStatus:S.state.cases[0].status,pending:S.state.summary.pending_approvals,sidebar:q('[data-case="CASE-001"]').textContent,serverVersion:actual.version,serverStatus:actual.status},bug:S.state.cases[0].version<S.detail.version};
 });
 await fixture('stale-state-after-new-case',async c=>{
  const {w,q,S,wait,click,submit,base}=c;
  const batch=S.state.batches.find(b=>!S.state.cases.some(c=>c.batch_id===b.batch_id));
  const held=await holdOneState(c);
  click('[data-action="new-case"]');q('#batch_id').value=batch.batch_id;q('#title').value='독립 검증 신규 사건';submit();await wait(()=>!q('.modal'));
  const saved={selected:S.selected,caseCount:S.state.cases.length,url:w.location.href};
  held.release();await held.p;w.fetch=held.real;
  const actual=await (await fetch(base+'/api/state')).json();
  return {saved,after:{selected:S.selected,caseCount:S.state.cases.length,url:w.location.href,header:q('.detail-header').textContent,serverCaseCount:actual.cases.length,newExists:actual.cases.some(x=>x.id==='CASE-004')},bug:!S.state.cases.some(x=>x.id==='CASE-004')};
 });
 await fixture('uncached-case-board-workspace-bounce',async c=>{
  const {w,q,S,wait}=c;assert(!S.details.has('CASE-002'));
  const real=w.fetch,release=[];
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/cases/CASE-002'&&!o?.method){const body=await r.text();await hold(res=>release.push(res));return new Response(body,{status:r.status})}return r};
  const selecting=w.qa.selectCase('CASE-002');await wait(()=>release.length===1);
  const board=w.qa.navigate('tasks');await wait(()=>release.length===2);
  await w.qa.navigate('workspace');assert(q('.detail-loading'));
  release[0]();await selecting;
  const firstResult={cache:S.details.has('CASE-002'),detail:S.detail?.id,spinner:!!q('.detail-loading')};
  release[1]();await board;w.fetch=real;
  return {firstResult,after:{view:S.view,selected:S.selected,detail:S.detail?.id,cache:S.details.has('CASE-002'),header:!!q('.detail-header'),spinner:!!q('.detail-loading'),loadingAll:S.loadingAll},bug:!!q('.detail-loading')&&!!S.detail};
 });
 await fixture('stale-state-error-after-submit',async c=>{
  const {w,q,S,wait,click,submit}=c;const real=w.fetch;let release,held=false;
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/state'&&!held){held=true;await r.text();await hold(res=>release=res);return new Response(JSON.stringify({error:{message:'independent stale state error',code:'TEST_STALE_ERROR'}}),{status:503})}return r};
  const reload=w.qa.loadState();await wait(()=>release);
  click('[data-action="submit"]');submit();await wait(()=>!q('.modal'));
  release();await reload;w.fetch=real;
  return {after:{detailVersion:S.detail.version,detailStatus:S.detail.status,header:!!q('.detail-header'),errorScreen:!!q('#view-container>.error-state')},bug:!!q('#view-container>.error-state')};
 });
 await fixture('current-board-detail-error-is-retryable',async c=>{
  const {w,q,S,wait}=c;const real=w.fetch,releases=[];let requests=0;
  w.fetch=async(u,o)=>{
   const r=await real(u,o);
   if(String(u)==='/api/cases/CASE-002'&&!o?.method){
    const number=++requests,body=await r.text();await hold(res=>releases.push(res));
    return number===2?new Response(JSON.stringify({error:{message:'current detail failure'}}),{status:503}):new Response(body,{status:r.status});
   }
   return r;
  };
  const selecting=w.qa.selectCase('CASE-002');await wait(()=>releases.length===1);
  const board=w.qa.navigate('tasks');await wait(()=>releases.length===2);
  await w.qa.navigate('workspace');releases[0]();await selecting;releases[1]();await board;
  assert(q('[data-action="retry-detail"]'),'latest failed detail has a retry action');
  assert(!q('.detail-loading'),'latest failed detail must not stay loading');
  w.fetch=real;await w.qa.loadDetail('CASE-002');assert(q('.detail-header'));
  return {passed:true};
 });
 await fixture('stale-detail-error-after-submit',async c=>{
  const {w,q,S,wait,click,submit}=c;const real=w.fetch;let release;
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/cases/CASE-001'&&!o?.method){await r.text();await hold(res=>release=res);return new Response(JSON.stringify({error:{message:'stale detail failure'}}),{status:503});}return r;};
  const selecting=w.qa.loadDetail('CASE-001');await wait(()=>release);
  click('[data-action="submit"]');submit();await wait(()=>!q('.modal'));
  release();await selecting;w.fetch=real;
  assert.equal(S.detail.status,'awaiting_approval');assert(q('.detail-header'));assert(!q('.detail-panel .error-state'));
  return {passed:true};
 });
 await fixture('current-state-error-is-visible',async c=>{
  const {w,q}=c;const real=w.fetch;
  w.fetch=(u,o)=>String(u)==='/api/state'?Promise.resolve(new Response(JSON.stringify({error:{message:'current state failure'}}),{status:503})):real(u,o);
  await w.qa.loadState();assert(q('#view-container>.error-state'));assert(q('[data-action="reload"]'));
  w.fetch=real;await w.qa.loadState();assert(q('.detail-header'));
  return {passed:true};
 });
 await fixture('reload-detail-error-after-case-navigation',async c=>{
  const {w,q,S,wait}=c;const real=w.fetch;let release;
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/cases/CASE-001'&&!o?.method){await r.text();await hold(res=>release=res);return new Response(JSON.stringify({error:{message:'old selected case failed'}}),{status:503});}return r;};
  const reload=w.qa.loadState();await wait(()=>release);
  await w.qa.selectCase('CASE-002');w.qa.selectDetailTab('audit');await w.qa.setRole('production');
  const route=w.location.href;release();await reload;w.fetch=real;
  assert.equal(S.selected,'CASE-002');assert.equal(S.detail.id,'CASE-002');
  assert.equal(S.tab,'audit');assert.equal(S.role,'production');assert.equal(w.location.href,route);
  assert(q('.detail-header'));assert(!q('#view-container>.error-state'));
  // A genuinely current reload detail failure still belongs to its retry UI.
  w.fetch=(u,o)=>String(u)==='/api/cases/CASE-002'?Promise.resolve(new Response(JSON.stringify({error:{message:'current selected case failed'}}),{status:503})):real(u,o);
  await w.qa.loadState();assert(q('[data-action="retry-detail"]'));assert(!q('#view-container>.error-state'));
  w.fetch=real;await w.qa.loadDetail('CASE-002');assert(q('.detail-header'));
  return {passed:true};
 });
 for(const view of ['workspace','tasks']){
  await fixture('initial-'+view+'-detail-error-visible',async c=>{
   const {w,q,S}=c;
   assert.equal(S.selected,'CASE-001');assert(!q('.detail-loading'));
   if(view==='workspace')assert(q('[data-action="retry-detail"]'));
   else {assert(q('.board'));assert(q('#toast-region').textContent.includes('initial detail failure'));}
   w.fetch=w.startupRealFetch;await w.qa.loadState();
   assert(view==='workspace'?q('.detail-header'):q('.board'));assert(S.details.has('CASE-001'));
   return {passed:true};
  },w=>{
   if(view==='tasks')w.history.replaceState(null,'','/workspace#tasks');
   const real=w.fetch;w.startupRealFetch=real;
   w.fetch=(u,o)=>String(u)==='/api/cases/CASE-001'?Promise.resolve(new Response(JSON.stringify({error:{message:'initial detail failure'}}),{status:503})):real(u,o);
  });
 }
 await fixture('late-case-does-not-switch-context',async c=>{
  const {w,q,S,wait}=c;const real=w.fetch;let release;
  w.fetch=async(u,o)=>{const r=await real(u,o);if(String(u)==='/api/cases/CASE-002'&&!o?.method){const body=await r.text();await hold(res=>release=res);return new Response(body,{status:r.status})}return r};
  const pending=w.qa.selectCase('CASE-002');await wait(()=>release);
  await w.qa.selectCase('CASE-003');w.qa.selectDetailTab('audit');await w.qa.setRole('production');
  release();await pending;w.fetch=real;
  assert.equal(S.selected,'CASE-003');assert.equal(S.detail.id,'CASE-003');assert.equal(S.tab,'audit');assert.equal(S.role,'production');assert.equal(new URL(w.location.href).searchParams.get('case'),'CASE-003');
  return {passed:true,selected:S.selected,tab:S.tab,role:S.role,url:w.location.href};
 });
};
if(require.main===module)module.exports().catch(e=>{console.error(e);process.exitCode=1;});
