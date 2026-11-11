// Read-only supply previews and explicit, role-scoped handoff to the existing conditions API.
const {JSDOM}=require('jsdom'),fs=require('fs'),cp=require('child_process'),assert=require('assert');
const root=require('path').resolve(__dirname,'..'),sleep=n=>new Promise(r=>setTimeout(r,n));
const server=cp.spawn(process.env.PYTHON||'python',['backend.py','--port','0','--db','/tmp/aurora-preview-'+Date.now()+'.sqlite3'],{cwd:root});
let baseURL,startError='';server.stdout.on('data',c=>{const m=String(c).match(/http:\/\/127\.0\.0\.1:\d+/);if(m)baseURL=m[0]});server.stderr.on('data',c=>startError+=String(c));
(async()=>{let dom;try{
 for(let i=0;i<100&&!baseURL;i++){if(server.exitCode!==null)throw Error(startError);await sleep(50)}assert(baseURL);
 dom=new JSDOM(fs.readFileSync(root+'/static/index.html','utf8'),{url:baseURL+'/workspace',runScripts:'outside-only',pretendToBeVisual:true});const w=dom.window,d=w.document,q=s=>d.querySelector(s),errors=[];
 let posts=0;w.AbortController=AbortController;w.confirm=()=>true;w.fetch=(u,o)=>{if(o?.method==='POST'&&String(u)!=='/api/session')posts++;return fetch(new URL(u,w.location.href),o)};w.addEventListener('error',e=>errors.push(e.message));
 w.eval(fs.readFileSync(root+'/static/app.js','utf8')+';window.qa={S};');const {S}=w.qa;
 const wait=async f=>{for(let i=0;i<150;i++){if(f())return;await sleep(20)}throw Error('Timed out: '+f+'; '+q('#form-error')?.textContent+'; '+errors.join(';'))};
 const click=s=>{assert(q(s),s);q(s).click()},event=(s,type)=>q(s).dispatchEvent(new w.Event(type,{bubbles:true,cancelable:true})),enter=(s,v)=>{q(s).value=v;event(s,'input')};
 await wait(()=>S.detail&&!q('#role-select').disabled);const original=structuredClone(S.detail);
 // The native input contract and application validation agree with the server.
 click('[data-action="conditions"]');assert.equal(q('#replacement_qty').min,'1');assert.equal(Number(q('#replacement_qty').max),original.analysis.produced_qty);assert(q('#replacement-qty-help').textContent.includes('빈 값은 미확인'));
 for(const [value,valid] of [['',true],['0',false],['1',true],[String(original.analysis.produced_qty+1),false],['1.5',false]]){q('#replacement_qty').value=value;assert.equal(q('#replacement_qty').checkValidity(),valid);}
 q('#replacement_qty').value='0';q('#reason').value='0개 입력의 오류 의미 검증';event('#action-form','submit');await wait(()=>q('#form-error').textContent.includes('1 EA 이상'));assert.equal(posts,0);assert.equal(S.detail.version,original.version);click('[data-action="close-modal"]');
 click('[data-tab="recovery"]');q('.supply-comparison').open=true;enter('#preview-quantity','1000');enter('#preview-due','2026-07-20');event('#supply-preview-form','submit');await wait(()=>S.supplyPreview&&q('[data-action="use-supply-preview"]'));
 assert.equal(S.supplyPreview.candidate.earliest_ship_date,'2026-07-15');assert.equal(S.supplyPreview.candidate.production_days,5);assert.equal(S.supplyPreview.candidate.feasible,false);assert(q('.preview-basis').textContent.includes('261영업일'));assert(q('.preview-basis').textContent.includes('strat_capacity_cv.csv'));assert.equal(posts,0);assert.deepEqual(await(await fetch(baseURL+'/api/cases/CASE-001')).json(),original);
 // Choosing a candidate does not save it; the ordinary dirty-form protections still apply.
 click('[data-action="use-supply-preview"]');assert.equal(q('#replacement_qty').value,'1000');assert.equal(q('#reason').value,'');assert.equal(posts,0);assert(q('.modal').textContent.includes('아직 저장하지 않았습니다'));
 w.confirm=()=>false;d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert(q('.modal'));w.confirm=()=>true;click('[data-action="close-modal"]');assert.equal(S.detail.conditions.replacement_qty,null);
 for(const role of ['committee','production','sales']){q('#role-select').value=role;event('#role-select','change');await wait(()=>!q('#role-select').disabled);assert.equal(q('[data-action="use-supply-preview"]').disabled,role!=='sales');}
 q('#role-select').value='quality';event('#role-select','change');await wait(()=>!q('#role-select').disabled);
 click('[data-action="use-supply-preview"]');enter('#reason','비교 결과를 검토한 모의 재공급 수량 변경');event('#action-form','submit');await wait(()=>!q('.modal'));assert.equal(S.detail.conditions.replacement_qty,1000);assert.equal(posts,1);assert.equal(S.detail.version,original.version+1);assert(!q('[data-action="use-supply-preview"]'),'a saved revision invalidates the old preview');
 q('.supply-comparison').open=true;enter('#preview-quantity','500');enter('#preview-due','2026-07-10');event('#supply-preview-form','submit');await wait(()=>S.supplyPreview?.version===S.detail.version&&q('[data-action="use-supply-preview"]'));
 assert.equal(S.supplyPreview.candidate.earliest_ship_date,'2026-07-13');assert.equal(S.supplyPreview.candidate.delay_days,3);assert(q('#supply-preview-results').textContent.includes('현재 요청보다 500 EA 적게 입력'));assert(q('#supply-preview-results').textContent.includes('남은 수량의 공급 계획은 별도로 확인'));
 const version=S.detail.version;enter('#preview-quantity','600');assert.equal(S.supplyPreview,null);assert(!q('[data-action="use-supply-preview"]'));assert.equal(S.detail.version,version);assert.equal(posts,1);
 const actualFetch=w.fetch;w.fetch=(u,o)=>String(u).includes('/supply-preview?')?Promise.reject(Error('preview offline')):actualFetch(u,o);event('#supply-preview-form','submit');await wait(()=>q('#supply-preview-results .form-error'));assert.equal(S.detail.version,version);assert(!q('#preview-submit').disabled);w.fetch=actualFetch;
 event('#supply-preview-form','submit');await wait(()=>S.supplyPreview?.candidate.quantity===600);assert.equal(posts,1);
 // A later effective date changes preview limits; the same candidate must be saveable.
 click('[data-action="conditions"]');enter('#effective_date','2026-08-01');enter('#customer_due_date','2026-08-20');enter('#reason','적용일 이후 날짜 비교의 일관성 검증');event('#action-form','submit');await wait(()=>!q('.modal'));assert.equal(S.detail.conditions.effective_date,'2026-08-01');assert.equal(posts,2);
 q('.supply-comparison').open=true;assert.equal(q('#preview-due').min,'2026-08-01');assert(q('#preview-date-help').textContent.includes('2026.08.01'));
 let previewGets=0;w.fetch=(u,o)=>{if(String(u).includes('/supply-preview?'))previewGets++;return actualFetch(u,o)};
 enter('#preview-quantity','600');enter('#preview-due','2026-07-20');assert.equal(q('#preview-due').checkValidity(),false);event('#supply-preview-form','submit');await sleep(30);assert.equal(previewGets,0);assert.equal(S.supplyPreview,null);assert(!q('[data-action="use-supply-preview"]'));
 const invalid=await fetch(baseURL+'/api/cases/CASE-001/supply-preview?quantity=600&due_date=2026-07-20');assert.equal(invalid.status,400);assert((await invalid.json()).error.message.includes('2026-08-01'));
 enter('#preview-due','2026-08-01');assert(q('#preview-due').checkValidity());event('#supply-preview-form','submit');await wait(()=>S.supplyPreview?.candidate.due_date==='2026-08-01');const previewDate=S.supplyPreview.candidate.earliest_ship_date;assert.equal(previewGets,1);
 click('[data-action="use-supply-preview"]');enter('#reason','적용일과 같은 날짜의 후보를 명시적으로 저장');event('#action-form','submit');await wait(()=>!q('.modal'));assert.equal(S.detail.conditions.customer_due_date,'2026-08-01');assert.equal(S.detail.analysis.recovery_plan.earliest_ship_date,previewDate);assert.equal(posts,3);
 click('[data-action="conditions"]');enter('#customer_due_date','2026-07-20');enter('#reason','조건 창에서도 적용일 이전 날짜 차단 확인');event('#action-form','submit');await wait(()=>q('#form-error').textContent.includes('2026-08-01'));assert.equal(posts,3);click('[data-action="close-modal"]');
 assert.deepEqual(errors,[]);console.log('Supply comparison PASS: empty/positive quantity contract, actionable zero error, read-only HTTP preview, source/assumption disclosure, no reused confirmations, role-scoped unsaved handoff, explicit save, revision/input invalidation, retry and shared effective-date constraint');
}finally{dom?.window.close();server.kill()}})().catch(e=>{console.error(e);process.exitCode=1});
