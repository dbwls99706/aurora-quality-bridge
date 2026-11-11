const {JSDOM}=require('jsdom'), fs=require('fs'), cp=require('child_process'), assert=require('assert');
const root=require('path').resolve(__dirname,'..');
const server=cp.spawn(process.env.PYTHON || 'python',['backend.py','--port','0','--db','/tmp/aurora-dom-'+Date.now()+'.sqlite3'],{cwd:root});
let baseURL=null,startError='';server.stdout.on('data',chunk=>{const match=String(chunk).match(/http:\/\/127\.0\.0\.1:\d+/);if(match)baseURL=match[0];});server.stderr.on('data',chunk=>{startError+=String(chunk);});
const sleep=n=>new Promise(r=>setTimeout(r,n));
(async()=>{try{
let ready=false;for(let i=0;i<100;i++){if(server.exitCode!==null)throw Error('test server failed: '+startError);try{if(baseURL&&(await fetch(baseURL+'/api/state')).ok){ready=true;break}}catch{}await sleep(100)}assert(ready,'server ready');
const dom=new JSDOM(fs.readFileSync(root+'/static/index.html','utf8'),{url:baseURL+'/workspace',runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window;let errors=[];w.addEventListener('error',e=>errors.push(e.message));w.fetch=(u,o)=>fetch(new URL(u,w.location.href),o);w.AbortController=AbortController;w.confirm=()=>true;w.eval(fs.readFileSync(root+'/static/app.js','utf8')+'; window.__qa={S,renderView,replyModal,loadAllDetails};');
const wait=async fn=>{for(let i=0;i<100;i++){if(fn())return;await sleep(50)}throw Error('wait failed: '+fn.toString()+' | form: '+(w.document.querySelector('#form-error')?.textContent||'')+' | script errors: '+JSON.stringify(errors));};const q=s=>w.document.querySelector(s);const click=s=>{assert(q(s),'exists '+s);q(s).click()};const ev=(s,type)=>q(s).dispatchEvent(new w.Event(type,{bubbles:true,cancelable:true}));
await wait(()=>q('.detail-header')&&!q('#role-select').disabled);console.log('initial',q('.detail-header').textContent);
// Advanced supply assumptions use the same role-scoped API and persist through real HTTP.
q('#role-select').value='production';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
click('[data-action="conditions"]');q('.advanced-conditions').open=true;
assert(q('#qa_days').disabled);assert(q('#effective_date').disabled);
q('#production_daily_capacity').value='150';q('#production_lead_days').value='4';q('#reason').value='독립 QA: 생산팀 모의 능력과 준비기간 변경';ev('#action-form','submit');
await wait(()=>!q('.modal'));await wait(()=>w.__qa.S.detail.conditions.production_daily_capacity===150);assert.equal(w.__qa.S.detail.conditions.production_lead_days,4);
q('#role-select').value='quality';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
click('[data-action="conditions"]');q('.advanced-conditions').open=true;q('#qa_days').value='3';q('#effective_date').value='2026-07-02';q('#reason').value='독립 QA: 품질 확인 기간과 모의 적용일 변경';ev('#action-form','submit');
await wait(()=>!q('.modal'));await wait(()=>w.__qa.S.detail.conditions.qa_days===3);assert.equal(w.__qa.S.detail.conditions.effective_date,'2026-07-02');
click('[data-action="conditions"]');q('.advanced-conditions').open=true;q('#production_daily_capacity').value='200';q('#production_lead_days').value='3';q('#qa_days').value='2';q('#effective_date').value='2026-07-01';q('#reason').value='독립 QA: 다음 경로를 위한 기본 모의 가정 복원';ev('#action-form','submit');
await wait(()=>!q('.modal'));await wait(()=>w.__qa.S.detail.conditions.qa_days===2);
click('[data-action="evidence"]');assert(q('.drawer'));w.document.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert(!q('.drawer'));assert.equal(w.document.body.style.overflow,'');
click('[data-action="submit"]');click('[data-action="close-modal"]');assert(!q('.modal'));
click('[data-action="submit"]');ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('.detail-header').textContent.includes('의결 대기'));
q('#role-select').value='committee';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);click('[data-action="approve"]');q('#reason').value='독립 QA: 원본 근거와 계산 확인 완료';q('[name=reviewed]').checked=true;ev('#action-form','submit');ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('.detail-header').textContent.includes('회수 의결 완료'));
q('#role-select').value='quality';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);click('[data-action="conditions"]');click('[data-preset="delay"]');ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('.detail-header').textContent.includes('의결 유지 · 공급 재확인'));
click('[data-tab="recovery"]');assert(q('.detail-body').textContent.includes('공급'));
click('[data-view="tasks"]');await sleep(200);assert(q('#page-title').textContent.includes('공급'));click('[data-view="sources"]');assert(q('.source-card'));click('[data-source]');assert(q('.drawer'));click('[data-action="close-drawer"]');click('[data-view="audit"]');await sleep(200);assert(q('.audit-item'));
// Below-threshold correction must stay reviewable without granting recall authorization.
click('[data-view="workspace"]');click('[data-tab="workflow"]');
click('[data-action="conditions"]');q('#confirmed_qty_override').value='14000';q('#reason').value='독립 QA: 회수 기준 미달로 정정';ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('[data-action="submit-correction"]'));
click('[data-action="submit-correction"]');ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('[data-action="acknowledge-correction"]'));
q('#role-select').value='committee';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);click('[data-action="acknowledge-correction"]');q('#reason').value='독립 QA: 정정 근거 검토, 실행 보류 및 조사 계속';q('[name=reviewed]').checked=true;ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('.detail-header').textContent.includes('조사 계속'));assert(!w.__qa.S.detail.recall_authorized);
q('#role-select').value='quality';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
// Deterministically deliver pre-save board and navigation details after the successful mutation.
// The cached case lets the user submit while this navigation GET is in flight.
const fetchBeforeRace=w.fetch;const releaseDetails=[];
w.fetch=async(u,o)=>{
 const r=await fetchBeforeRace(u,o);
 if(String(u)==='/api/cases/CASE-002'&&!o?.method){
  const body=await r.text();
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Held detail was not released')),5000);releaseDetails.push(()=>{clearTimeout(timer);resolve();});});
  return new Response(body,{status:r.status});
 }
 return r;
};
const pendingBoardRefresh=w.__qa.loadAllDetails();
// A separate case exercises rejection, the missing-information path and refresh persistence.
click('[data-view="workspace"]');click('[data-tab="workflow"]');click('[data-case="CASE-002"]');await wait(()=>q('.detail-eyebrow').textContent.includes('CASE-002'));
await wait(()=>releaseDetails.length===2);
click('[data-action="submit"]');ev('#action-form','submit');await wait(()=>!q('.modal'));
const savedVersion=w.__qa.S.detail.version;
w.fetch=fetchBeforeRace;releaseDetails.forEach(release=>release());await pendingBoardRefresh;
// Wait for the exact navigation request to settle, not an arbitrary network delay.
await wait(()=>new URL(w.location.href).searchParams.get('case')==='CASE-002');
assert.equal(w.__qa.S.detail.version,savedVersion,'late navigation GET cannot overwrite a saved case');
assert.equal(w.__qa.S.details.get('CASE-002').version,savedVersion);
await wait(()=>q('.detail-header').textContent.includes('의결 대기'));
q('#role-select').value='committee';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
click('[data-action="reject"]');q('#reason').value='독립 QA: 추가 근거 확인 후 다시 검토';q('[name=reviewed]').checked=true;ev('#action-form','submit');await wait(()=>!q('.modal'));await wait(()=>q('.detail-header').textContent.includes('의결 반려 · 보완 필요'));
q('#role-select').value='quality';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
click('[data-action="conditions"]');q('#replacement_qty').value='1000';q('#reason').value='독립 QA: 모의 요청 수량 입력';ev('#action-form','submit');await wait(()=>!q('.modal'));await sleep(100);
click('[data-action="conditions"]');click('[data-preset="missing"]');ev('#action-form','submit');await wait(()=>!q('.modal'));await sleep(100);assert.equal(w.__qa.S.detail.conditions.replacement_qty,null);
const preservedVersion=w.__qa.S.detail.version;click('[data-action="reload"]');await wait(()=>!w.__qa.S.loading);assert.equal(w.__qa.S.detail.version,preservedVersion);assert.equal(w.__qa.S.detail.conditions.replacement_qty,null);
// Board/audit refresh must also refresh the selected workspace detail.
const beforeCache=w.__qa.S.detail.version, fetchBeforeCache=w.fetch;
w.fetch=async(u,o)=>{const r=await fetchBeforeCache(u,o);if(String(u)==='/api/cases/'+w.__qa.S.selected){const d=await r.json();d.version=beforeCache+100;return new Response(JSON.stringify(d),{status:200});}return r;};
await w.__qa.loadAllDetails();assert.equal(w.__qa.S.detail.version,beforeCache+100);assert.strictEqual(w.__qa.S.detail,w.__qa.S.details.get(w.__qa.S.selected));
w.fetch=fetchBeforeCache;await w.__qa.loadAllDetails();assert.equal(w.__qa.S.detail.version,beforeCache);
// Parsed select options regression: malformed closing option tags hid the blocked outcome.
w.eval("__qa.S.role='sales';__qa.S.details.set(__qa.S.detail.id,__qa.S.detail)");
const replyTask=w.__qa.S.detail.tasks.find(t=>t.type==='inventory_check');
await w.eval(`__qa.replyModal(${JSON.stringify(replyTask.id)},${JSON.stringify(w.__qa.S.detail.id)})`);
assert.deepEqual(Array.from(q('#outcome').options,x=>x.value),['confirmed','blocked']);click('[data-action="close-modal"]');
const oldTaskStatus=replyTask.status;replyTask.status='done';
await w.eval(`__qa.replyModal(${JSON.stringify(replyTask.id)},${JSON.stringify(w.__qa.S.detail.id)})`);
assert.deepEqual(Array.from(q('#outcome').options,x=>x.value),['blocked']);click('[data-action="close-modal"]');replyTask.status=oldTaskStatus;w.eval("__qa.S.role='quality';__qa.renderView()");
// Structured customer confirmation UI: starts unconfirmed for every affected customer.
const savedType=replyTask.type;replyTask.type='customer_promise';
await w.__qa.replyModal(replyTask.id,w.__qa.S.detail.id);
assert.equal(w.document.querySelectorAll('[name^="cs-"]').length,w.__qa.S.detail.customers.length);
assert(Array.from(w.document.querySelectorAll('[name^="cs-"]')).every(e=>e.value==='pending'));
click('[data-action="close-modal"]');replyTask.type=savedType;
// UI-only AI fixtures: no provider call or fabricated backend verification.
assert(q('[data-action="generate-ai"]'),'AI panel present on workflow');
click('[data-view="workspace"]');click('[data-tab="workflow"]');
assert(q('[data-action="generate-ai"]').disabled,'unconfigured AI disabled');
w.eval("__qa.S.detail.ai.configured=true; __qa.S.detail.ai.request_allowed=true; __qa.S.state.ai.configured=true; __qa.S.state.ai.request_allowed=true;__qa.renderView()");
click('[data-action="generate-ai"]');assert(q('[name="ai-consent"]'));click('[data-action="close-modal"]');
const realFetch=w.fetch;w.fetch=async(u,o)=>String(u).endsWith('/ai-brief')?new Response(JSON.stringify({error:{message:'모의 모델 호출 실패',code:'AI_CALL_FAILED'}}),{status:502}):realFetch(u,o);
click('[data-action="generate-ai"]');q('[name="ai-consent"]').checked=true;ev('#action-form','submit');await wait(()=>q('#form-error')?.textContent.includes('AI 요청을 완료하지 못했습니다'));assert(!q('#form-error').textContent.includes('모의 모델 호출 실패'),'raw provider-style error text is suppressed');assert(q('.modal'),'failed request stays reviewable');click('[data-action="close-modal"]');w.fetch=realFetch;
w.eval(`__qa.S.detail.ai.brief={model:'TEST FIXTURE ONLY',review_revision:__qa.S.detail.review_revision-1,generated_at:new Date().toISOString(),structured:{summary:'<img src=x onerror=alert(1)>',risks:['테스트 위험'],alternatives:['테스트 대안'],evidence_ids:['TEST-1']}};__qa.renderView()`);
assert(q('.ai-stale'));assert(!q('.ai-brief-card img'),'AI text escaped');assert(q('.ai-brief-card').textContent.includes('TEST FIXTURE ONLY'));
assert.deepEqual(errors,[]);console.log('DOM flow PASS: initial, evidence/Escape, submit/cancel/reopen, approval/repeated submit, condition change, four views');dom.window.close();
await require('./test_async_refresh_races')();
}finally{server.kill()}})().catch(e=>{console.error(e);process.exitCode=1});
