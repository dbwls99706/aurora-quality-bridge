// Live HTTP + jsdom: quantity conservation, unknowns, no writes and interrupted requests.
const {JSDOM}=require('jsdom'),assert=require('assert'),fs=require('fs'),cp=require('child_process'),path=require('path');
const root=path.resolve(__dirname,'..'),sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const temp=fs.mkdtempSync('/tmp/aurora-supply-alternatives-');
const server=cp.spawn(process.env.PYTHON||'python',['backend.py','--port','0','--db',temp+'/test.sqlite3'],{cwd:root});
let base,error='';server.stdout.on('data',d=>{const m=String(d).match(/http:\/\/127\.0\.0\.1:\d+/);if(m)base=m[0]});server.stderr.on('data',d=>error+=d);
(async()=>{let dom;try{
 for(let i=0;i<150&&!base;i++){if(server.exitCode!==null)throw Error(error);await sleep(30)}assert(base);
 dom=new JSDOM(fs.readFileSync(root+'/static/index.html','utf8'),{url:base+'/workspace',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,q=s=>d.querySelector(s),errors=[];let writes=0,gets=0,hold=null,reject=false;
 w.confirm=()=>true;w.AbortController=AbortController;
 w.fetch=async(u,o)=>{if(o?.method==='POST'&&String(u)!=='/api/session')writes++;if(String(u).includes('/supply-alternatives?')){gets++;if(reject)throw Error('offline comparison');if(hold)await hold;}return fetch(new URL(u,w.location.href),o)};
 w.addEventListener('error',e=>errors.push(e.message));
 w.eval(fs.readFileSync(root+'/static/supply-alternatives.js','utf8'));
 w.eval(fs.readFileSync(root+'/static/app.js','utf8')+';window.qa={S};');const {S}=w.qa;
 const wait=async f=>{for(let i=0;i<200;i++){if(f())return;await sleep(20)}throw Error('Timed out: '+f+'; '+errors.join(';')+'; '+q('[data-alt-results]')?.textContent)};
 const event=(s,type='input')=>q(s).dispatchEvent(new w.Event(type,{bubbles:true,cancelable:true}));
 const enter=(s,value)=>{assert(q(s),s);q(s).value=value;event(s)};
 const submit=()=>event('form[data-supply-alternatives]','submit');
 await wait(()=>S.detail&&!q('#role-select').disabled);const before=structuredClone(S.detail);
 q('[data-tab="recovery"]').click();const form=q('form[data-supply-alternatives]');assert(form);
 assert.equal(q('#alt-total').value,'','never infer demand from 17900 confirmed defects');
 assert.equal(q('#alt-early').value,'');assert(q('.supply-alternatives').textContent.includes('확정 불량량을 자동 입력하지 않습니다'));
 const customerIds=[...form.querySelectorAll('[data-customer-id]')].map(n=>n.dataset.customerId);
 for(const row of form.querySelectorAll('[data-customer-id]')){assert.equal(row.querySelector('[data-customer-field="required_quantity"]').value,'');assert.equal(row.querySelector('[data-customer-field="due_date"]').value,'')}
 enter('#alt-total','17900');enter('#alt-early','1000');submit();await wait(()=>q('[data-alt-results]').textContent.includes('동일 총량 17,900 EA 비교'));
 let text=q('[data-alt-results]').textContent;assert(text.includes('2026.11.11'));assert(text.includes('2026.11.13'));assert(text.includes('114달력일'));assert(text.includes('미배정 17,900'));
 assert(text.includes('추가 검사·물류·전환 비용 근거'));assert(text.includes('두 안 모두 미승인'));assert.equal(writes,0);
 const setCustomer=(i,field,value)=>enter(`[data-customer-id="${customerIds[i]}"] [data-customer-field="${field}"]`,value);
 setCustomer(0,'required_quantity','1000');setCustomer(0,'due_date','2026-07-20');setCustomer(0,'early_quantity','1000');
 setCustomer(1,'required_quantity','16900');setCustomer(1,'due_date','2026-11-12');setCustomer(1,'remaining_quantity','16900');
 submit();await wait(()=>q('[data-alt-results]').textContent.includes('미배정 0 EA'));
 assert(q('[data-alt-results]').textContent.includes('-15,900 EA'));
 assert(q('[data-alt-results]').textContent.includes('3개 고객 필요량 미확인'));
 // An input change clears a result immediately, including when an old request later arrives.
 let release;hold=new Promise(r=>release=r);submit();await wait(()=>q('form[data-supply-alternatives] [type="submit"]').disabled);
 enter('#alt-early','1200');assert(q('[data-alt-results]').textContent.includes('다시 계산'));release();hold=null;await sleep(180);
 assert(!q('[data-alt-results]').textContent.includes('동일 총량'));assert(!q('form[data-supply-alternatives] [type="submit"]').disabled);
 // Older detached result must not paint over a fresh navigation or a new draft.
 hold=new Promise(r=>release=r);submit();await wait(()=>q('form[data-supply-alternatives] [type="submit"]').disabled);
 q('[data-tab="workflow"]').click();release();hold=null;await sleep(100);q('[data-tab="recovery"]').click();
 assert.equal(q('#alt-early').value,'1200');assert(!q('[data-alt-results]').textContent.includes('동일 총량'));
 // Network failure remains read-only and permits a retry.
 reject=true;submit();await wait(()=>q('[data-alt-results] .form-error'));assert(q('[data-alt-results]').textContent.includes('offline comparison'));assert.equal(writes,0);reject=false;
 // Return to compatible allocation bounds, then retry.
 enter('#alt-early','1000');submit();await wait(()=>q('[data-alt-results]').textContent.includes('동일 총량'));
 // A role/view re-render keeps draft inputs but invalidates old reservation evidence.
 q('#role-select').value='sales';event('#role-select','change');await wait(()=>!q('#role-select').disabled);
 assert.equal(q('#alt-total').value,'17900');assert(!q('[data-alt-results]').textContent.includes('동일 총량'));
 submit();await wait(()=>q('[data-alt-results]').textContent.includes('동일 총량'));
 // Invalid split and overallocated customer are rejected without replacing evidence.
 let beforeGets=gets;enter('#alt-early','17900');submit();await wait(()=>q('[data-alt-results] .form-error'));assert.equal(gets,beforeGets);
 enter('#alt-early','1000');setCustomer(0,'early_quantity','1001');submit();await wait(()=>q('[data-alt-results] .form-error'));
 assert(q('[data-alt-results]').textContent.includes('넘습니다'));assert.equal(writes,0);
 assert.deepEqual(await(await fetch(base+'/api/cases/CASE-001')).json(),before);
 assert.deepEqual(errors,[]);console.log('Supply alternatives UI PASS: stress schedule, same total, unallocated customers, unknown need/deadline, allocation validation, no writes, stale response, navigation interruption, retry, no approval or pricing inference');
}finally{dom?.window.close();server.kill();fs.rmSync(temp,{recursive:true,force:true})}})().catch(e=>{console.error(e);process.exitCode=1});
