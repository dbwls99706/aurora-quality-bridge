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
// Independent local database only. No remote AI calls.
click('[data-action="submit"]');ev('#action-form','submit');await wait(()=>!q('.modal'));
q('#role-select').value='committee';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);click('[data-action="approve"]');q('#reason').value='로컬 회신 흐름 테스트 승인';q('[name=reviewed]').checked=true;ev('#action-form','submit');await wait(()=>!q('.modal'));
q('#role-select').value='sales';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
const task=w.__qa.S.detail.tasks.find(t=>t.type==='inventory_check');const original=w.__qa.S.detail.version;
await w.__qa.replyModal(task.id,w.__qa.S.detail.id);q('#reply-replacement_qty').value='1200';q('#note').value='고객 확인에 따른 모의 재공급 요청';ev('#action-form','submit');
assert.equal(w.__qa.S.modal.type,'conditions');assert.equal(q('#replacement_qty').value,'1200');assert.equal(w.__qa.S.detail.version,original,'proposal makes no write');assert(q('.modal-content').textContent.includes('아직 조건과 회신 모두 저장되지 않았습니다'));
click('[data-action="close-modal"]');assert.equal(w.__qa.S.detail.version,original,'cancel makes no write');
await w.__qa.replyModal(task.id,w.__qa.S.detail.id);q('#reply-replacement_qty').value='1200';q('#note').value='고객 확인에 따른 모의 재공급 요청';ev('#action-form','submit');ev('#action-form','submit');
await wait(()=>w.__qa.S.modal?.type==='reply'&&!w.__qa.S.busy);assert.equal(q('#note').value,'고객 확인에 따른 모의 재공급 요청');assert.equal(w.__qa.S.detail.conditions.replacement_qty,1200);assert.equal(w.__qa.S.detail.conditions.inventory_verified,false,'condition save does not confirm inventory');assert.equal(q('#reply-replacement_qty').value,'');ev('#action-form','submit');await wait(()=>!q('.modal'));assert.equal(w.__qa.S.detail.conditions.inventory_verified,true);
await w.__qa.replyModal(task.id,w.__qa.S.detail.id);q('#reply-replacement_qty').value='1300';q('#outcome').value='blocked';q('#note').value='추가 고객 확인이 필요합니다';ev('#action-form','submit');assert(q('#form-error').textContent.includes('추가 확인'));assert.equal(w.__qa.S.detail.conditions.replacement_qty,1200);
click('[data-action="close-modal"]');
q('#role-select').value='production';ev('#role-select','change');await wait(()=>!q('#role-select').disabled);
const prod=w.__qa.S.detail.tasks.find(t=>t.type==='production_schedule');await w.__qa.replyModal(prod.id,w.__qa.S.detail.id);q('#reply-production_delay_days').value='3';q('#note').value='생산 일정 추가 확인 모의 회신';ev('#action-form','submit');assert.equal(w.__qa.S.modal.type,'conditions');assert.equal(q('#production_delay_days').value,'3');assert(q('#customer_due_date').disabled,'production cannot change customer dates');
const originalFetch=w.fetch;let failRefresh=true;w.fetch=(u,o)=>{if(String(u)==='/api/state'&&failRefresh){failRefresh=false;return Promise.reject(Error('test refresh failure'));}return originalFetch(u,o);};ev('#action-form','submit');await wait(()=>w.__qa.S.modal?.type==='reply'&&!w.__qa.S.busy);w.fetch=originalFetch;assert.equal(q('#note').value,'생산 일정 추가 확인 모의 회신','draft restored after acknowledged save and failed refresh');assert.equal(w.__qa.S.detail.conditions.production_delay_days,3);click('[data-action="close-modal"]');
await w.__qa.replyModal(prod.id,w.__qa.S.detail.id);q('#note').value='버전 충돌 검증 모의 회신';w.__qa.S.modal.version--;ev('#action-form','submit');assert(q('#form-error').textContent.includes('버전'));
console.log('PASS reply proposal: cancel, no silent writes, explicit saves, restored draft, blocked rejection, production scope, stale version');
}finally{server.kill()}})().catch(e=>{console.error(e);process.exitCode=1});
