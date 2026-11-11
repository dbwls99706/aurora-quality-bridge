// Reviewer follow-up: display semantics, permissions and save/refresh isolation.
const {JSDOM}=require('jsdom'),fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
const sample=JSON.parse(read('backend_sample_state.json')),detail=JSON.parse(read('backend_sample_detail.json'));
const pause=()=>new Promise(r=>setTimeout(r,30));
(async()=>{
 const dom=new JSDOM(read('static/index.html'),{url:'http://127.0.0.1/workspace',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,q=s=>d.querySelector(s);
 w.AbortController=AbortController;w.confirm=()=>true;
 w.eval(read('static/app.js').replace("hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);",'')+';window.qa={S,renderView,renderSummary,conditionsModal,closeModal,handleForm};');
 const {S}=w.qa;
 S.state=structuredClone(sample);S.detail=structuredClone(detail);S.selected=S.detail.id;S.details.set(S.detail.id,S.detail);
 S.state.cases[0].open_departments=['production','sales'];
 for(const [authorized,label] of [[true,'의결 유지 · 공급 재확인'],[false,'판정 정정 · 의결 재검토']]){
  Object.assign(S.detail,{status:'review_required',recall_authorized:authorized});
  Object.assign(S.state.cases[0],{status:'review_required',recall_authorized:authorized});
  w.qa.renderView();
  assert(q('.detail-header').textContent.includes(label));assert(q('.case-item').textContent.includes(label));
 }
 assert(q('.case-ownership').textContent.includes('생산팀 · 영업팀'));
 assert.equal(q('.open-information').querySelectorAll('li').length,detail.analysis.missing_information.length);
 for(const item of detail.analysis.missing_information)assert(q('.open-information').textContent.includes(item));
 S.detail=structuredClone(detail);S.details.set(S.detail.id,S.detail);S.state.cases[0]=structuredClone(sample.cases[0]);
 w.qa.renderSummary();w.qa.renderView();
 assert.equal(d.querySelectorAll('.summary-primary').length,2);assert.equal(d.querySelectorAll('.summary-secondary').length,2);
 assert.equal(q('.summary-more').getAttribute('aria-expanded'),'false');q('.summary-more').click();
 assert.equal(q('.summary-more').getAttribute('aria-expanded'),'true');assert(q('.summary-expanded'));
 q('.summary-more').click();assert(!q('.summary-expanded'));
 // Passive view metadata must not capture delegated clicks for presets, filters or sources.
 assert.equal(d.body.dataset.activeView,'workspace');assert(!d.body.hasAttribute('data-view'));
 q('[data-filter="investigation"]').click();assert.equal(S.filter,'investigation');q('[data-filter="all"]').click();assert.equal(S.filter,'all');
 w.qa.conditionsModal();q('[data-preset="delay"]').click();assert.equal(q('#production_delay_days').value,'7');assert(q('#reason').value.length>5);w.qa.closeModal(true);
 S.role='sales';w.qa.renderView();
 assert(q('.next-action [data-action="submit"]').disabled);
 assert(q('.next-action .role-help').textContent.includes('품질팀'));
 q('.next-action [data-action="choose-role"]').click();assert.equal(d.activeElement,q('#role-select'));assert.equal(S.role,'sales','focus does not silently switch authorization');
 assert.equal(q('#creation-role-note').hidden,false);
 S.role='committee';w.qa.renderView();
 assert([...d.querySelectorAll('[data-action="conditions"]')].every(b=>b.disabled));
 S.role='quality';S.tab='recovery';w.qa.renderView();
 for(const button of d.querySelectorAll('[data-reply]')){
  assert(button.getAttribute('aria-label').includes(button.dataset.taskCase));
  assert(button.getAttribute('aria-label').includes(button.closest('.task-card').querySelector('h4').textContent));
  if(button.disabled)assert(button.closest('.task-card').querySelector('.role-help'));
 }
 for(const button of d.querySelectorAll('.detail-panel [data-action]'))assert(button.getAttribute('aria-label').startsWith(S.detail.id+' '));
 for(const view of ['sources','tasks','audit']){
  S.view=view;w.qa.renderView();assert(q('#summary-cards').hidden);assert(q('.notice-strip').hidden);assert(q('[data-action="new-case"]').hidden);
 }
 S.view='sources';w.qa.renderView();
 const source=q('[data-source]');assert(source.getAttribute('aria-label').includes(source.closest('.source-card').querySelector('h3').textContent));source.click();assert(q('.drawer'));q('[data-action="close-drawer"]').click();
 S.view='workspace';S.tab='workflow';w.qa.renderView();assert(!q('#summary-cards').hidden);assert(!q('.notice-strip').hidden);
 // Save remains locked until its readback finishes, preventing a later modal from being overwritten.
 let resolveRefresh,posts=0;
 w.fetch=async(url,opts)=>{
  if(opts.method==='POST'){posts++;return{ok:true,json:async()=>({...structuredClone(detail),version:2,conditions:{...detail.conditions,production_delay_days:7}})};}
  return new Promise(resolve=>{resolveRefresh=()=>resolve({ok:true,json:async()=>structuredClone(sample)})});
 };
 w.qa.conditionsModal();q('[data-preset="delay"]').click();const save=w.qa.handleForm(q('#action-form'));await pause();
 assert(S.busy);assert(q('#form-submit').disabled);assert(q('[data-action="close-modal"]').disabled);w.qa.closeModal();assert(q('.modal'));
 resolveRefresh();await save;assert(!S.busy);assert(!q('.modal'));assert.equal(posts,1);
 // Acknowledged save + failed readback must not offer the same mutation for resubmission.
 w.fetch=async(url,opts)=>{if(opts.method==='POST'){posts++;return{ok:true,json:async()=>({...structuredClone(detail),version:3})}}throw Error('readback offline');};
 w.qa.conditionsModal();q('#production_delay_days').value='8';q('#reason').value='모의 조건 변경 재조회 실패 시험';await w.qa.handleForm(q('#action-form'));
 assert(!q('.modal'));assert.equal(posts,2);assert(q('#toast-region').textContent.includes('입력은 저장됐지만'));
 dom.window.close();
 console.log('Review polish PASS: distinct review states, actual owners and gaps, contextual summaries, mobile metric toggle, role guidance/focus, descriptive names, event routing and save refresh protection');
})().catch(e=>{console.error(e);process.exitCode=1});
