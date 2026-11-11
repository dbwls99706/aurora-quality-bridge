// Landing and operating-surface regressions. No external model or browser is used here.
const {JSDOM}=require('jsdom'), fs=require('fs'), path=require('path'), assert=require('assert');
const root=path.resolve(__dirname,'..');
const read=p=>fs.readFileSync(path.join(root,p),'utf8');
const sample=JSON.parse(read('backend_sample_state.json'));
const detail=JSON.parse(read('backend_sample_detail.json'));
const tick=()=>new Promise(r=>setTimeout(r,30));
const until=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(r=>setTimeout(r,15));}assert(predicate(),'asynchronous route completed');};
(async()=>{
  const dom=new JSDOM(read('static/index.html'),{url:'http://127.0.0.1/workspace?case=CASE-002&tab=evidence',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,q=s=>d.querySelector(s);
  const js=read('static/app.js').replace("hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);",'');
  w.eval(js+';window.qa={S,updateAI,showAI,conditionsModal,closeModal,renderView,renderNextAction,navigate,selectCase};');
  const {S}=w.qa;
  assert.equal(S.selected,'CASE-002','landing link selects requested case');assert.equal(S.tab,'evidence','deep link restores the detail tab');S.tab='workflow';
  S.state=structuredClone(sample);S.detail=structuredClone(detail);S.selected=S.detail.id;S.details.set(S.detail.id,S.detail);
  S.state.ai={configured:true,connected:false,message:'실제 호출 미검증'};
  w.qa.updateAI();assert(q('.ai-status').textContent.includes('설정됨 · 호출 미검증'));
  w.qa.showAI();assert(q('.drawer').textContent.includes('설정됨 · 호출 미검증'));q('[data-action="close-drawer"]').click();
  S.state.ai={configured:false,connected:false};w.qa.updateAI();assert(q('.ai-status').textContent.includes('연결 미설정'));
  S.state.ai={configured:true,connected:true};w.qa.updateAI();assert(q('.ai-status').textContent.includes('모델 응답 수신 · 본문 미검증'));
  S.state.ai={configured:false,connected:false};
  w.qa.renderView();assert(q('.next-action [data-action="submit"]'),'primary next step is near the case heading');
  assert.equal(q('.case-picker-toggle').getAttribute('aria-expanded'),'false');
  q('.case-picker-toggle').click();assert.equal(q('.case-picker-toggle').getAttribute('aria-expanded'),'true');
  assert(q('.queue-expanded'));assert.equal(d.activeElement,q('.case-picker-toggle'));
  q('.case-picker-toggle').click();assert(q('.queue-collapsed'));
  for(const role of ['quality','production','sales']){
    S.role=role;w.qa.renderView();assert.equal(q('[data-action="new-case"]').disabled,role!=='quality');w.qa.conditionsModal();
    for(const key of ['production_daily_capacity','production_lead_days','qa_days','effective_date']){
      assert(q('#'+key),'advanced input exists: '+key);
      const allowed=role==='quality'||role==='production'&&['production_daily_capacity','production_lead_days'].includes(key);
      assert.equal(q('#'+key).disabled,!allowed,role+' permissions for '+key);
    }
    w.qa.closeModal(true);
  }
  S.role='quality';w.qa.conditionsModal();
  q('#replacement_qty').value='2345';q('#replacement_qty').focus();let calls=0;
  w.confirm=()=>{calls++;return false;};
  d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert(q('.modal'));assert.equal(q('#replacement_qty').value,'2345');assert.equal(calls,1);
  q('[data-action="close-modal"]').click();assert(q('.modal'));assert.equal(calls,2);
  w.confirm=()=>true;w.qa.closeModal();assert(!q('.modal'));
  w.qa.conditionsModal();w.confirm=()=>{throw Error('unchanged form must close without confirmation')};w.qa.closeModal();assert(!q('.modal'));
  // Native browser history tracks main-view navigation without rewriting stored business state.
  await w.qa.navigate('sources');assert.equal(w.location.hash,'#sources');assert(q('.source-card'));
  await w.qa.navigate('workspace');assert.equal(w.location.hash,'#workspace');
  w.history.back();await until(()=>S.view==='sources'&&w.location.hash==='#sources');assert.equal(S.view,'sources');
  w.history.forward();await until(()=>S.view==='workspace'&&w.location.hash==='#workspace');assert.equal(S.view,'workspace');
  w.fetch=async url=>{const id=String(url).split('/').pop();const c=sample.cases.find(x=>x.id===id);return {ok:true,json:async()=>({...structuredClone(detail),...c})};};
  w.AbortController=AbortController;
  await w.qa.selectCase('CASE-003');assert.equal(new URL(w.location.href).searchParams.get('case'),'CASE-003');assert.equal(S.selected,'CASE-003');
  w.history.back();await until(()=>S.selected==='CASE-001'&&S.detail?.id==='CASE-001');assert.equal(S.selected,'CASE-001');assert.equal(S.detail.id,'CASE-001');
  w.history.forward();await until(()=>S.selected==='CASE-003'&&S.detail?.id==='CASE-003');assert.equal(S.selected,'CASE-003');assert.equal(S.detail.id,'CASE-003');
  q('[role="tab"][data-tab="evidence"]').click();assert.equal(new URL(w.location.href).searchParams.get('tab'),'evidence');
  q('[role="tab"][data-tab="recovery"]').click();assert.equal(new URL(w.location.href).searchParams.get('tab'),'recovery');
  w.history.back();await until(()=>S.tab==='evidence');assert.equal(S.tab,'evidence');assert(q('[role="tab"][data-tab="evidence"][aria-selected="true"]'));
  w.history.forward();await until(()=>S.tab==='recovery');assert.equal(S.tab,'recovery');
  const resumed=new JSDOM(read('static/index.html'),{url:w.location.href,runScripts:'outside-only'});resumed.window.eval(js+';window.qa={S};');assert.equal(resumed.window.qa.S.tab,'recovery');assert.equal(resumed.window.qa.S.selected,'CASE-003');resumed.window.close();
  S.view='tasks';w.qa.renderView();w.history.pushState({},'', '#board-blocked');w.dispatchEvent(new w.PopStateEvent('popstate'));await tick();assert.equal(S.view,'tasks','board jump anchors remain on the board');


  dom.window.close();

  const landing=new JSDOM(read('static/landing.html'),{url:'http://127.0.0.1/',runScripts:'outside-only'});
  const lw=landing.window;let requests=[];
  const dirty=structuredClone(sample);dirty.cases[0].title='<img src=x onerror=alert(1)> test';
  lw.fetch=async(url,options)=>{requests.push({url,options});return{ok:true,json:async()=>dirty};};
  lw.AbortController=AbortController;lw.eval(read('static/landing.js')+';window.reloadCases=loadCaseExplorer;');await tick();
  assert.equal(lw.document.querySelectorAll('.case-link').length,sample.cases.length);
  assert(!lw.document.querySelector('#case-explorer img'),'untrusted titles escaped');
  assert(lw.document.querySelector('.case-link').getAttribute('href').includes('case=CASE-001'));
  assert.deepEqual(requests.map(r=>r.url),['/api/state']);
  assert(requests.every(r=>!r.options.method),'landing makes no mutation request');
  for(const anchor of lw.document.querySelectorAll('a[href^="#"]'))assert(lw.document.querySelector(anchor.getAttribute('href')),'valid landing anchor');
  lw.fetch=async()=>{throw Error('offline')};lw.reloadCases();await tick();
  assert(lw.document.querySelector('#retry-cases'),'network failure provides a retry');
  lw.fetch=async()=>({ok:true,json:async()=>sample});lw.document.querySelector('#retry-cases').click();await tick();
  assert.equal(lw.document.querySelectorAll('.case-link').length,sample.cases.length,'retry restores cases');
  // Cascade-level font checks at explicit CSS breakpoints, not a browser geometry test.
  const cssText=read('static/landing.css');
  const important=['.hero-note','.metric-caption','.threshold>span','.measure-labels','.calculation>span','.flow-state','.dossier-flow div>span','figcaption','.case-link-status','.case-link-result>span','.case-link-title>span','.data-band small'];
  for(const width of [390,1200]){
    const style=lw.document.createElement('style');style.textContent=cssText;lw.document.head.append(style);
    const matches=query=>!/(print|prefers-reduced-motion)/.test(query)&&[...query.matchAll(/(max|min)-width:\s*(\d+)px/g)].every(([,kind,n])=>kind==='max'?width<=Number(n):width>=Number(n));
    const flatten=rules=>[...rules].map(rule=>rule.type===4?(matches(rule.conditionText||rule.media.mediaText)?flatten(rule.cssRules):''):rule.cssText).join('\n');
    style.textContent=flatten(style.sheet.cssRules);
    for(const selector of important)assert(parseFloat(lw.getComputedStyle(lw.document.querySelector(selector)).fontSize)>=12,`${selector} readable at CSS width ${width}`);
    assert.equal(lw.getComputedStyle(lw.document.querySelector('.threshold strong')).fontSize,'18px','threshold remains secondary to measured value');
    style.remove();
  }
  landing.window.close();
  console.log('Redesign PASS: landing case links, escaping, read-only requests, retry, AI status, dirty conditions, role-scoped advanced inputs, next action, case/tab URL resume and Back/Forward, landing font cascade at 390/1200');
})().catch(e=>{console.error(e);process.exitCode=1});
