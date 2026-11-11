// Independent local HTTP data; never connects to an AI provider or a running preview DB.
const {JSDOM}=require('jsdom'),fs=require('fs'),cp=require('child_process'),assert=require('assert');
const root=require('path').resolve(__dirname,'..');
const server=cp.spawn(process.env.PYTHON||'python',['backend.py','--port','0','--db','/tmp/aurora-navigation-'+Date.now()+'.sqlite3'],{cwd:root});
let baseURL=null,startError='';server.stdout.on('data',c=>{const m=String(c).match(/http:\/\/127\.0\.0\.1:\d+/);if(m)baseURL=m[0]});server.stderr.on('data',c=>startError+=String(c));
const sleep=n=>new Promise(r=>setTimeout(r,n));
(async()=>{let dom;try{
 for(let i=0;i<100&&!baseURL;i++){if(server.exitCode!==null)throw Error(startError);await sleep(50)}assert(baseURL);
 dom=new JSDOM(fs.readFileSync(root+'/static/index.html','utf8'),{url:baseURL+'/workspace',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,q=s=>d.querySelector(s),errors=[];
 w.AbortController=AbortController;w.confirm=()=>true;w.fetch=(u,o)=>fetch(new URL(u,w.location.href),o);w.addEventListener('error',e=>errors.push(e.message));
 w.eval(fs.readFileSync(root+'/static/app.js','utf8')+';window.qa={S,renderView};');const {S}=w.qa;
 const wait=async f=>{for(let i=0;i<120;i++){if(f())return;await sleep(30)}throw Error('Timed out: '+f+'; '+(q('#form-error')?.textContent||'')+'; '+errors.join(';'))};
 const click=s=>{assert(q(s),'exists '+s);q(s).click()},event=(s,type)=>q(s).dispatchEvent(new w.Event(type,{bubbles:true,cancelable:true}));
 const enter=(s,value)=>{q(s).value=value;event(s,'input')};
 await wait(()=>q('.detail-header')&&!q('#role-select').disabled);
 // Mapping confirmation must not promise an outcome the backend refuses without evidence.
 const mapping=S.detail.tasks.find(t=>t.type==='mapping_check');assert(mapping);
 click('[data-tab="recovery"]');q('#role-select').value='sales';event('#role-select','change');await wait(()=>!q('#role-select').disabled);
 click(`[data-reply="${mapping.id}"]`);assert.deepEqual([...q('#outcome').options].map(x=>x.value),['blocked']);assert(q('.modal').textContent.includes('모의 회신만으로 확인 완료 처리할 수 없으며'));
 enter('#note','SBL–SH 직접 연결 원본 근거가 없어 추가 확인 필요');event('#action-form','submit');await wait(()=>!q('.modal'));
 assert.equal(S.detail.tasks.find(t=>t.id===mapping.id).status,'blocked');assert(S.detail.analysis.missing_information.some(x=>x.includes('SBL')));
 q('#role-select').value='quality';event('#role-select','change');await wait(()=>!q('#role-select').disabled);click('[data-tab="workflow"]');
 // A saved correction is visible even when it stops matching the active recall filter.
 click('[data-filter="recall"]');assert.equal(S.filter,'recall');
 click('[data-action="conditions"]');enter('#confirmed_qty_override','14000');enter('#reason','회수 필터 중 기준 미달 모의 정정 검증');event('#action-form','submit');
 await wait(()=>!q('.modal'));assert.equal(S.filter,'all');assert.equal(S.selected,'CASE-001');assert(q('.detail-header'));assert(q('.selection-notice').textContent.includes('필터를 해제'));assert.equal(S.detail.analysis.recall_eligible,false);assert.equal(d.activeElement,q('.detail-header h2'));
 // Board links are specific to a case and task, preserve role, and return to the same task.
 click('[data-view="tasks"]');await wait(()=>!S.loadingAll&&d.querySelectorAll('[data-open-case]').length>0);
 const ownReply=[...d.querySelectorAll('[data-reply]')].find(button=>!button.disabled);assert(ownReply);
 const repliedTask=ownReply.dataset.reply,repliedCase=ownReply.dataset.taskCase;ownReply.click();
 q('#outcome').value='blocked';enter('#note','품질팀 확인 불가 회신 후 보드 키보드 포커스 검증');event('#action-form','submit');await wait(()=>!q('.modal'));
 assert.equal(d.activeElement.dataset.reply,repliedTask);assert.equal(d.activeElement.dataset.taskCase,repliedCase);assert.equal(d.activeElement.closest('.board-column').id,'board-blocked');
 const links=[...d.querySelectorAll('[data-open-case]')];assert.equal(links.length,[...S.details.values()].reduce((n,c)=>n+c.tasks.length,0));
 const target=links.find(x=>x.dataset.openCase==='CASE-003');assert(target);const taskId=target.dataset.taskId,oldRole=S.role;
 S.filter='recall';S.search='CASE-002';target.click();
 await wait(()=>S.view==='workspace'&&S.selected==='CASE-003'&&q('.detail-header')&&new URL(w.location.href).searchParams.get('case')==='CASE-003');
 assert.equal(S.tab,'recovery');assert.equal(S.role,oldRole);assert.equal(S.filter,'all');assert.equal(S.search,'');assert(q('.case-return'));assert(q('.detail-header').textContent.includes('CASE-003'));
 click('[data-action="back-to-board"]');await wait(()=>S.view==='tasks'&&!S.loadingAll&&d.activeElement?.dataset.taskId===taskId);assert.equal(d.activeElement.dataset.openCase,'CASE-003');
 // A new document opened from a board anchor restores the board and its target section.
 const resume=new JSDOM(fs.readFileSync(root+'/static/index.html','utf8'),{url:baseURL+'/workspace?case=CASE-001#board-blocked',runScripts:'outside-only',pretendToBeVisual:true});
 try{
  const rw=resume.window;rw.AbortController=AbortController;rw.fetch=(u,o)=>fetch(new URL(u,rw.location.href),o);
  rw.eval(fs.readFileSync(root+'/static/app.js','utf8')+';window.qa={S};');
  for(let i=0;i<120&&(rw.qa.S.loading||!rw.qa.S.state||rw.document.querySelector('#role-select').disabled);i++)await sleep(30);
  assert.equal(rw.qa.S.view,'tasks');assert.equal(rw.location.hash,'#board-blocked');assert(rw.document.querySelector('.board'));assert.equal(rw.document.activeElement.id,'board-blocked');
 }finally{resume.window.close();}
 // Back/Forward protects dirty modal data and keeps URL, view and overlays in one context.
 click('[data-view="audit"]');await wait(()=>S.view==='audit'&&!S.loadingAll);click('[data-view="workspace"]');
 const workspaceRoute=w.location.href;let prompts=0;
 click('[data-action="conditions"]');enter('#production_delay_days','5');enter('#reason','Back 취소 시 보존할 미저장 조건');await sleep(30);q('#reason').focus();
 w.confirm=()=>{prompts++;return false;};w.history.back();await wait(()=>prompts===1&&w.location.href===workspaceRoute);
 assert.equal(S.view,'workspace');assert(q('.modal'));assert.equal(q('#production_delay_days').value,'5');assert.equal(q('#reason').value,'Back 취소 시 보존할 미저장 조건');assert.equal(d.activeElement,q('#reason'));assert.equal(d.body.style.overflow,'hidden');
 w.confirm=()=>{prompts++;return true;};w.history.back();await wait(()=>S.view==='audit'&&!S.loadingAll&&!q('.modal'));
 assert.equal(w.location.hash,'#audit');assert.equal(d.body.style.overflow,'');assert.equal(d.activeElement,q('#page-title'));assert.equal(prompts,2);
 w.history.forward();await wait(()=>S.view==='workspace'&&w.location.href===workspaceRoute);assert(!q('.modal'));
 // Read-only drawers close without leaving the background scroll-locked.
 click('[data-action="evidence"]');assert(q('.drawer'));assert.equal(d.body.style.overflow,'hidden');
 w.history.back();await wait(()=>S.view==='audit'&&!S.loadingAll&&!q('.drawer'));assert.equal(d.body.style.overflow,'');
 w.history.forward();await wait(()=>S.view==='workspace'&&w.location.href===workspaceRoute);
 click('[data-action="conditions"]');w.confirm=()=>{throw Error('unchanged modal must not ask before navigation')};
 w.history.back();await wait(()=>S.view==='audit'&&!S.loadingAll&&!q('.modal'));assert.equal(d.body.style.overflow,'');
 w.history.forward();await wait(()=>S.view==='workspace'&&w.location.href===workspaceRoute);
 // Forward has the same draft protection, including within-case detail tab changes.
 click('[role="tab"][data-tab="workflow"]');const nextTabRoute=w.location.href;w.history.back();await wait(()=>w.location.href===workspaceRoute&&S.tab==='recovery');
 click('[data-action="conditions"]');enter('#production_delay_days','5');enter('#reason','Forward 취소 시 보존할 조건');await sleep(30);q('#reason').focus();
 prompts=0;w.confirm=()=>{prompts++;return false;};w.history.forward();await wait(()=>prompts===1&&w.location.href===workspaceRoute);
 assert.equal(S.tab,'recovery');assert(q('.modal'));assert.equal(q('#reason').value,'Forward 취소 시 보존할 조건');
 w.confirm=()=>true;w.history.forward();await wait(()=>S.tab==='workflow'&&w.location.href===nextTabRoute&&!q('.modal'));assert.equal(d.body.style.overflow,'');
 // An in-flight save must not leave its original context or reopen an old dialog later.
 const beforeBusyRoute=w.location.href,beforeBusyFetch=w.fetch;let releaseSave;
 w.fetch=(u,o)=>String(u).endsWith('/conditions')&&o?.method==='POST'?new Promise((resolve,reject)=>{releaseSave=()=>beforeBusyFetch(u,o).then(resolve,reject)}):beforeBusyFetch(u,o);
 click('[data-action="conditions"]');enter('#production_delay_days','6');enter('#reason','저장 중 Back 이동 방지 검증');event('#action-form','submit');await wait(()=>S.busy&&releaseSave);
 w.confirm=()=>{throw Error('busy modal must not offer discarding an in-flight save')};w.history.back();await wait(()=>q('#toast-region').textContent.includes('저장 결과를 확인 중')&&w.location.href===beforeBusyRoute);
 assert.equal(S.view,'workspace');assert(q('.modal'));assert(q('#form-submit').disabled);assert.equal(d.body.style.overflow,'hidden');
 releaseSave();await wait(()=>!q('.modal')&&!S.busy);w.fetch=beforeBusyFetch;w.confirm=()=>true;assert.equal(w.location.href,beforeBusyRoute);assert.equal(S.detail.conditions.production_delay_days,6);assert.equal(d.body.style.overflow,'');
 // Search narrows real source batches, with explicit zero results and no hidden stale selection.
 click('[data-view="workspace"]');click('[data-filter="recall"]');enter('#case-search','CASE-002');
 const existing=new Set(S.state.cases.map(c=>c.batch_id));const batch=S.state.batches.find(b=>!existing.has(b.batch_id)&&!b.recall_eligible);assert(batch);
 click('[data-action="new-case"]');assert.equal(q('#batch_id').options.length,S.state.batches.length-S.state.cases.length+1);
 enter('#batch-search','no-source-batch-123');assert(!q('#batch-empty').hidden);assert(q('#form-submit').disabled);assert.equal(q('#batch_id').value,'');assert(!q('#batch_id').checkValidity());
 enter('#batch-search',batch.product_name);assert(q('#batch_id').options.length>1);assert(!q('#form-submit').disabled);
 enter('#batch-search',batch.batch_id);assert.equal(q('#batch_id').options.length,2);q('#batch_id').value=batch.batch_id;
 enter('#title','필터 밖 신규 배치 등록 검증');
 enter('#batch-search','no-source-batch-123');assert.equal(q('#batch_id').value,'');assert(q('#batch-search-summary').textContent.includes('선택을 해제'));assert.equal(q('#title').value,'필터 밖 신규 배치 등록 검증');
 enter('#batch-search',batch.batch_id);assert.equal(q('#batch_id').value,'','searching must not silently select the only result');q('#batch_id').value=batch.batch_id;
 event('#action-form','submit');await wait(()=>!q('.modal'));
 assert.equal(S.selected,'CASE-004');assert.equal(S.filter,'all');assert.equal(S.search,'');assert.equal(S.detail.batch_id,batch.batch_id);assert(q('.detail-header').textContent.includes('필터 밖 신규 배치 등록 검증'));assert(q('.selection-notice').textContent.includes('등록한 사건 CASE-004'));assert.equal(new URL(w.location.href).searchParams.get('case'),'CASE-004');assert.equal(d.activeElement,q('.detail-header h2'));
 click('[data-action="reload"]');await wait(()=>!S.loading);assert.equal(S.selected,'CASE-004');assert(q('.detail-header').textContent.includes('CASE-004'));
 click('[data-action="new-case"]');enter('#batch-search',batch.batch_id);assert.equal(q('#batch_id').options.length,1,'registered batch remains excluded');click('[data-action="close-modal"]');
 // If the post is acknowledged but list refresh fails, the saved new detail stays visible.
 const newer=S.state.batches.find(b=>!S.state.cases.some(c=>c.batch_id===b.batch_id));click('[data-filter="recall"]');click('[data-action="new-case"]');enter('#batch-search',newer.batch_id);q('#batch_id').value=newer.batch_id;enter('#title','저장 후 조회 실패 신규 사건 검증');
 const actualFetch=w.fetch;w.fetch=(u,o)=>String(u)==='/api/state'?Promise.reject(Error('simulated state read failure')):actualFetch(u,o);
 event('#action-form','submit');await wait(()=>!q('.modal'));assert.equal(S.selected,'CASE-005');assert(q('.detail-header').textContent.includes('CASE-005'));assert(q('#toast-region').textContent.includes('입력은 저장됐지만'));assert(S.state.cases.some(c=>c.id==='CASE-005'));w.fetch=actualFetch;
 assert((await (await fetch(baseURL+'/api/state')).json()).cases.some(c=>c.id==='CASE-005'));
 assert.deepEqual(errors,[]);
 console.log('Navigation flow PASS: below-threshold correction and creation under active filters, searchable source batches/empty results/selection clearing, board case links/return focus/anchor reload, honest mapping replies, modal/drawer Back/Forward protection, save-in-flight routing and board reply focus, acknowledged-save/readback-failure detail visibility');
}finally{dom?.window.close();server.kill()}})().catch(e=>{console.error(e);process.exitCode=1});
