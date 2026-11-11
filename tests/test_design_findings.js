// Independent design findings: honest affordances, read-only role filters and keyboard evidence regions.
const {JSDOM}=require('jsdom'),fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
const sample=JSON.parse(read('backend_sample_state.json')),detail=JSON.parse(read('backend_sample_detail.json'));
(async()=>{
 const landing=new JSDOM(read('static/landing.html'));
 const figure=landing.window.document.querySelector('.case-dossier');
 assert(figure);assert(!figure.textContent.includes('↗'),'static example must not suggest a dead link');
 assert(!figure.querySelector('a,button,[tabindex]'),'source example stays a static figure');
 assert(figure.textContent.includes('원본 기준 예시'));landing.window.close();
 const dom=new JSDOM(read('static/index.html'),{url:'http://127.0.0.1/workspace',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,q=s=>d.querySelector(s),qa=s=>[...d.querySelectorAll(s)];
 w.AbortController=AbortController;w.confirm=()=>true;
 w.eval(read('static/app.js').replace("hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);",'')+';window.qa={S,renderView,setRole,showSource,showRecord,showAllClaims,openTaskCase,returnToBoard,openModal,closeModal};');
 const {S}=w.qa;S.state=structuredClone(sample);S.detail=structuredClone(detail);S.selected=S.detail.id;
 const data=new Map(sample.cases.map((c,i)=>[c.id,{...structuredClone(detail),...c,tasks:[
  {...structuredClone(detail.tasks[0]),id:`DESIGN-${i}-sales`,case_id:c.id,department:'sales',status:'open'},
  {...structuredClone(detail.tasks[0]),id:`DESIGN-${i}-quality`,case_id:c.id,department:'quality',status:'blocked'},
  {...structuredClone(detail.tasks[0]),id:`DESIGN-${i}-production`,case_id:c.id,department:'production',status:'done'}
 ]}]));
 S.details=new w.Map(data);S.detail=S.details.get(S.selected);let requests=[];
 w.fetch=async(url,opts={})=>{requests.push({url,method:opts.method||'GET'});return{ok:true,json:async()=>url==='/api/session'?{token:'design-session-'+JSON.parse(opts.body).actor_id}:structuredClone(data.get(url.split('/').pop()))};};
 const storedBefore=JSON.stringify([...S.details]),summaryBefore=JSON.stringify(S.state.summary),routeBefore=w.location.href;
 S.view='tasks';w.qa.renderView();assert.equal(S.boardFilter,'all');assert.equal(qa('.task-card').length,9);
 for(const type of ['current','all','current']){
  q(`[data-board-filter="${type}"]`).click();
  assert.equal(S.boardFilter,type);assert.equal(d.activeElement,q(`[data-board-filter="${type}"]`));
  assert.equal(qa('[data-board-filter][aria-pressed="true"]').length,1);
  assert.equal(q(`[data-board-filter="${type}"]`).getAttribute('aria-pressed'),'true');
 }
 assert.equal(qa('.task-card').length,3);assert(qa('.task-card-meta').every(n=>n.textContent.includes('품질팀')));
 assert(q('.filter-summary').textContent.includes('3개 표시 / 전체 9개'));
 assert.equal(q('.board-jump a[href="#board-open"] strong').textContent,'0');
 assert.equal(q('.board-jump a[href="#board-blocked"] strong').textContent,'3');
 assert.equal(qa('.board-column .empty-state').length,2);assert(qa('.board-column .empty-state').every(n=>n.textContent.includes('현재 역할')));
 assert.equal(S.role,'quality');assert.equal(S.token,null);assert.equal(requests.length,0,'view filters make no network request');
 assert.equal(JSON.stringify([...S.details]),storedBefore);assert.equal(JSON.stringify(S.state.summary),summaryBefore);assert.equal(w.location.href,routeBefore);
 await w.qa.setRole('production');assert.equal(S.boardFilter,'current');assert.equal(qa('.task-card').length,3);assert(qa('.task-card-meta').every(n=>n.textContent.includes('생산팀')));
 assert.equal(q('.board-jump a[href="#board-complete"] strong').textContent,'3');assert(qa('[data-reply]').every(n=>!n.disabled));
 await w.qa.setRole('committee');assert.equal(qa('.task-card').length,0);assert(q('.filter-summary').textContent.includes('0개 표시 / 전체 9개'));assert(q('.board-filter-note').textContent.includes('다른 부서'));
 q('[data-board-filter="all"]').click();assert.equal(qa('.task-card').length,9);assert(qa('[data-reply]').every(n=>n.disabled),'filtering never grants another role permission');
 await w.qa.setRole('sales');q('[data-board-filter="current"]').click();
 const target=q('[data-open-case]'),caseId=target.dataset.openCase,taskId=target.dataset.taskId;
 await w.qa.openTaskCase(caseId,taskId);assert.equal(S.view,'workspace');await w.qa.returnToBoard();
 assert.equal(S.boardFilter,'current');assert.equal(d.activeElement.dataset.taskId,taskId,'return preserves visible filtered task');
 await w.qa.openTaskCase(caseId,taskId);await w.qa.setRole('quality');await w.qa.returnToBoard();
 assert.equal(S.boardFilter,'all','return reveals the previous task after an explicit role change');assert.equal(d.activeElement.dataset.taskId,taskId);
 assert(requests.every(r=>r.method==='GET'||r.url==='/api/session'),'no workflow mutation is made');
 assert.equal(JSON.stringify([...S.details]),storedBefore);assert.equal(JSON.stringify(S.state.summary),summaryBefore);
 S.view='sources';w.qa.renderView();const sourceButton=q('[data-source="claims"]');
 assert(sourceButton.textContent.includes('출처 정보'));assert(sourceButton.getAttribute('aria-label').includes('출처 정보'));sourceButton.focus();sourceButton.click();
 assert(q('#drawer-title').textContent.includes('출처 정보'));assert(q('.drawer').textContent.includes('무결성 확인용 해시'));assert(q('.drawer').textContent.includes(S.selected));
 assert(q('.drawer').textContent.includes(S.state.sources.find(s=>s.id==='claims').sha256));
 // Geometry is mocked only for the DOM focus-trap check. Real keyboard scrolling needs browser QA.
 const testTrap=()=>{
  const regions=qa('.drawer .raw-record,.drawer .table-scroll');assert(regions.length);
  for(const region of regions){assert.equal(region.tabIndex,0);assert.equal(region.getAttribute('role'),'region');assert(region.getAttribute('aria-label').includes('스크롤 영역'));}
  const close=q('.drawer .close-button'),last=regions.at(-1);
  [close,...regions].forEach(n=>n.getClientRects=()=>[{width:100,height:40}]);
  const press=(node,shiftKey=false)=>{const e=new w.KeyboardEvent('keydown',{key:'Tab',shiftKey,bubbles:true,cancelable:true});node.dispatchEvent(e);return e;};
  close.focus();assert(press(close,true).defaultPrevented);assert.equal(d.activeElement,last,'Shift+Tab reaches last scrollable evidence region');
  last.focus();assert(press(last).defaultPrevented);assert.equal(d.activeElement,close,'Tab wraps after final evidence region');
  if(regions.length>1){regions[0].focus();assert(!press(regions[0]).defaultPrevented,'intermediate regions remain normal tab stops');}
 };
 testTrap();d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert(!q('.drawer'));assert.equal(d.activeElement,sourceButton);assert.equal(d.body.style.overflow,'');
 w.qa.showRecord(S.detail.claims[0].claim_id);testTrap();q('[data-action="close-drawer"]').click();
 w.qa.showAllClaims();testTrap();q('[data-action="close-drawer"]').click();
 const policy=S.state.sources.find(s=>s.row_count==null);assert(policy);w.qa.showSource(policy.id);assert(!q('.drawer').textContent.includes('CSV는'),'document source must not claim a CSV encoding');q('[data-action="close-drawer"]').click();
 const source=S.state.sources[0],original=source.path;source.path='<img src=x onerror=alert(1)>';
 w.qa.showSource(source.id);assert(!q('.drawer img'));assert(q('.drawer').textContent.includes(source.path));q('[data-action="close-drawer"]').click();source.path=original;
 w.qa.openModal('test','키보드','펼침 영역','<details><summary>고급 내용</summary><input id="hidden-field"></details>');
 const summary=q('.modal summary'),close=q('.modal .close-button');
 qa('.modal button,.modal summary').forEach(n=>n.getClientRects=()=>[{width:100,height:40}]);
 summary.focus();const next=new w.KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true});summary.dispatchEvent(next);assert(!next.defaultPrevented,'native summary is included in normal dialog tab order');
 w.qa.closeModal(true);dom.window.close();
 console.log('Design findings PASS: static landing affordance, truthful escaped source info, all/current role filters with counts, permissions and return context, focusable evidence regions, modal/drawer Tab cycling and Escape restoration');
})().catch(e=>{console.error(e);process.exitCode=1;});
