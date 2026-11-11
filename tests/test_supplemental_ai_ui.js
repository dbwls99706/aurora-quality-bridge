// Offline DOM fixtures only: no model requests or account usage claims.
const {JSDOM}=require('jsdom'),fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
(async()=>{
 const dom=new JSDOM(read('static/index.html'),{url:'http://localhost/workspace',runScripts:'outside-only'}),w=dom.window;
 w.eval(read('static/app.js').replace('hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);','')+';window.qa={S,renderAIBrief,aiStatusLabel,aiBriefModal,handleForm,closeModal,updateAI,aiDiagnosticText,api};');
 const {S}=w.qa;S.state=JSON.parse(read('backend_sample_state.json'));S.detail=JSON.parse(read('backend_sample_detail.json'));S.selected=S.detail.id;S.details.set(S.detail.id,S.detail);
 const ready={configured:true,connected:true,request_allowed:true,status:'supplemental_ready',usage_limits:{mode:'supplemental_one_attempt',history_reconciled:false,supplemental_remaining_attempts:1}};
 const render=()=>{const el=w.document.createElement('div');el.innerHTML=w.qa.renderAIBrief(S.detail);return el;};
 const unsafe='PRIVATE_PROVIDER_TEXT<img src=x onerror=alert(1)>';
 const stages=['admission','count_transport','count_decode','count_usage','messages_transport','messages_decode','messages_envelope','messages_usage','messages_tool','schema','evidence','facts'];
 const reasons=['admission_rejected','transport_failed','http_error','response_too_large','invalid_json','invalid_envelope','unexpected_stop','model_mismatch','invalid_usage','input_limit','invalid_tool','invalid_schema','invalid_evidence','invalid_facts','canonical_fact_mismatch'];
 for(const stage of stages)assert(w.qa.aiDiagnosticText({stage}).startsWith('이번 요청의 실패 단계: '));
 for(const validation_reason of reasons)assert(w.qa.aiDiagnosticText({stage:'facts',validation_reason}).includes(' · '));
 for(const stage of [unsafe,'unknown','__proto__','constructor',1,{},null])assert.equal(w.qa.aiDiagnosticText({stage,validation_reason:'invalid_json'}),'');
 for(const validation_reason of [unsafe,'unknown','__proto__','constructor',{},null]){
  const text=w.qa.aiDiagnosticText({stage:'facts',validation_reason,field:unsafe,stop_reason:unsafe,input_tokens:unsafe,output_tokens:unsafe,raw_response:unsafe});
  assert.equal(text,'이번 요청의 실패 단계: 규칙 기반 사실 대조.');
 }
 assert.equal(w.qa.aiDiagnosticText(null),'');assert.equal(w.qa.aiDiagnosticText([]),'');
 w.fetch=async()=>({ok:false,status:502,json:async()=>({error:{message:unsafe,diagnostic:{stage:'facts',validation_reason:'canonical_fact_mismatch',field:unsafe,stop_reason:unsafe,input_tokens:999999999,raw_response:unsafe}}})});
 await assert.rejects(w.qa.api('/api/cases/offline/ai-brief'),error=>{
  assert.equal(error.aiDiagnosticText,'이번 요청의 실패 단계: 규칙 기반 사실 대조 · 응답의 사실이 규칙 기반 값과 일치하지 않습니다.');
  assert(!('diagnostic' in error),'raw diagnostic object is never attached to the error');return true;
 });
 S.state.ai={...ready};S.detail.ai={...ready};
 assert(!render().querySelector('[data-action="generate-ai"]').disabled);
 assert(render().textContent.includes('과거 호출 이력은 완전히 확인되지 않았습니다'));
 assert(render().textContent.includes('추가 1회 시도만 허용'));
 assert(render().textContent.includes('계정 전체 사용량을 뜻하지 않습니다'));
 S.detail.ai={...ready,usage_limits:{...ready.usage_limits,supplemental_sequence:2,predecessor_attempts_reserved:1}};
 assert(render().textContent.includes('이전에 승인된 1회 시도는 이미 사용되었으며 그대로 보존'));
 assert(render().textContent.includes('별도 승인된 후속 추가 1회'));
 assert(render().textContent.includes('기존 한도를 초기화하지 않으며'));
 assert(render().textContent.includes('이전 실패의 정확한 원인은 이 정보만으로 확인할 수 없습니다'));
 S.detail.ai={...ready};
 for(const status of ['supplemental_exhausted','credit_expired','budget_blocked','unknown']){
  S.state.ai={...ready,status,request_allowed:false};
  const el=render();assert(el.querySelector('[data-action="generate-ai"]').disabled);
  assert(!w.qa.aiStatusLabel(S.state.ai).includes('요청 가능'));
  assert(!el.textContent.includes('아래 버튼에서 전송 범위를 확인한 후 요청하세요'));
  w.qa.updateAI();assert(w.document.querySelector('.ai-status .status-dot').classList.contains('neutral'));
  w.qa.aiBriefModal();assert(!w.document.querySelector('.modal'));
 }
 S.state.ai={...ready};S.detail.ai={...ready,request_allowed:false,status:'supplemental_exhausted'};
 assert(render().querySelector('[data-action="generate-ai"]').disabled,'detail-level blocking is respected');
 S.detail.ai={...ready};w.qa.aiBriefModal();assert(w.document.querySelector('.modal').textContent.includes('추가 1회'));
 S.state.ai={...ready,request_allowed:false,status:'supplemental_exhausted'};
 let calls=0;w.fetch=async()=>{calls++;throw new Error('must not fetch');};
 await w.qa.handleForm(w.document.querySelector('#action-form'));assert.equal(calls,0,'an already-open modal cannot submit after a budget block');
 w.qa.closeModal(true);
 // Failed or timed-out model responses refresh only GET endpoints, and never
 // advertise a reusable one-shot reservation when either refresh is uncertain.
 for(const scenario of ['exhausted','state-unavailable','detail-unavailable','ready','timeout']){
  S.detail=JSON.parse(read('backend_sample_detail.json'));S.state.ai={...ready};S.detail.ai={...ready};S.details.set(S.detail.id,S.detail);
  const caseId=S.detail.id;
  w.qa.aiBriefModal();
  const requests=[];
  w.fetch=async(url,opts={})=>{
   requests.push({url,method:opts.method||'GET'});
   if(opts.method==='POST'){
    if(scenario==='timeout'){const error=new Error('timed out');error.name='AbortError';throw error;}
    return {ok:false,status:502,json:async()=>({error:{message:unsafe,diagnostic:scenario==='exhausted'?{stage:'facts',validation_reason:'canonical_fact_mismatch',field:unsafe,stop_reason:unsafe,raw_response:unsafe}:{stage:unsafe,validation_reason:unsafe}}})};
   }
   const state=url==='/api/state';
   if((state&&scenario==='state-unavailable')||(!state&&scenario==='detail-unavailable'))throw new Error('offline refresh failure');
   const payload=JSON.parse(JSON.stringify(state?S.state:S.detail));
   payload.ai=scenario==='ready'?{...ready}:{...ready,request_allowed:false,status:'supplemental_exhausted'};
   return {ok:true,json:async()=>payload};
  };
  await w.qa.handleForm(w.document.querySelector('#action-form'));
  assert.equal(requests.filter(r=>r.method==='POST').length,1);
  assert(requests.some(r=>r.method==='GET'&&r.url==='/api/state'));
  assert(requests.some(r=>r.method==='GET'&&r.url===`/api/cases/${caseId}`));
  assert.equal(w.document.querySelector('#form-submit').disabled,scenario!=='ready');
  const errorText=w.document.querySelector('#form-error').textContent;
  assert(!errorText.includes('PRIVATE_PROVIDER_TEXT'),'provider error messages and diagnostic values are never displayed');
  assert(!w.document.querySelector('#form-error img'));
  assert(errorText.includes(scenario==='exhausted'?'이번 요청의 실패 단계: 규칙 기반 사실 대조':'정확한 실패 원인은 확인할 수 없습니다'));
  if(scenario!=='ready'){
   assert(w.document.querySelector('#form-error').textContent.includes('이미 사용되었을 수 있습니다'));
   assert(!w.document.querySelector('.ai-status').textContent.includes('요청 가능'));
   const count=requests.length;await w.qa.handleForm(w.document.querySelector('#action-form'));assert.equal(requests.length,count,'blocked modal cannot retry programmatically');
  }
  w.qa.closeModal(true);
  if(scenario!=='ready'){w.qa.aiBriefModal();assert(!w.document.querySelector('.modal'),'closing a failed request must not bypass retry block');}
 }
 // Repair pool values are narrow numeric contracts, never provider strings.
 const pool=(remaining=5)=>({configured:true,connected:true,request_allowed:remaining>0,status:remaining?'repair_ready':'repair_exhausted',usage_limits:{mode:'repair_pool',repair_max_attempts:5,repair_attempts_reserved:5-remaining,repair_remaining_attempts:remaining,repair_expired:false}});
 S.state.ai=pool();S.detail.ai=pool();
 assert(!render().querySelector('[data-action="generate-ai"]').disabled);
 for(const text of ['남은 시도: 5회','자동 재시도는 없습니다','실패해도 시도 한도를 사용','기존 2회는 그대로 보존','초기화하지 않습니다','$1.0512','7회 / $1.47168','불확실성 보류액은 별도','계정 전체 사용량을 뜻하지 않습니다'])assert(render().textContent.includes(text),text);
 assert(w.qa.aiStatusLabel(pool(3)).includes('3회 남음'));
 for(const value of [unsafe,'5',null,{},[],true,-1,6,1.5,Infinity,NaN]){
  S.detail.ai=pool();S.detail.ai.usage_limits.repair_remaining_attempts=value;
  const el=render();assert(el.querySelector('[data-action="generate-ai"]').disabled);
  assert(el.textContent.includes('남은 시도: 확인 필요'));assert(!el.textContent.includes('PRIVATE_PROVIDER_TEXT'));
 }
 for(const change of [{repair_max_attempts:unsafe},{repair_attempts_reserved:unsafe},{repair_attempts_reserved:1},{repair_expired:true},{repair_expired:undefined}]){
  S.detail.ai=pool();Object.assign(S.detail.ai.usage_limits,change);assert(render().querySelector('[data-action="generate-ai"]').disabled);
 }
 for(const status of ['repair_exhausted','credit_expired','budget_blocked','__proto__','constructor']){
  S.state.ai={...pool(0),status};S.detail.ai=pool();
  assert(render().querySelector('[data-action="generate-ai"]').disabled);assert.equal(typeof w.qa.aiStatusLabel(S.state.ai),'string');
  w.qa.aiBriefModal();assert(!w.document.querySelector('.modal'));
 }
 const schemaReasons=['schema_top_level_type','schema_top_level_keys','schema_summary_type','schema_summary_blank','schema_summary_length','schema_list_type','schema_list_count','schema_list_item_type','schema_list_item_blank','schema_list_item_length'];
 for(const validation_reason of schemaReasons){
  const text=w.qa.aiDiagnosticText({stage:'schema',validation_reason,schema_field:'risks',observed_type:unsafe,missing_keys:[unsafe],observed_count:unsafe,item_index:unsafe});
  assert(text.includes('확인 항목: 유의 사항'));assert(!text.includes('PRIVATE_PROVIDER_TEXT'));assert(!text.includes(validation_reason));
 }
 for(const schema_field of [unsafe,'__proto__','constructor',{},null])assert(!w.qa.aiDiagnosticText({stage:'schema',validation_reason:'schema_list_type',schema_field}).includes('확인 항목:'));
 // A failed attempt only enables a later manual retry after BOTH reads verify allowance.
 for(const scenario of ['remaining','exhausted','state-unavailable','detail-unavailable','malformed']){
  S.detail=JSON.parse(read('backend_sample_detail.json'));S.selected=S.detail.id;S.state.ai=pool();S.detail.ai=pool();S.details.set(S.detail.id,S.detail);w.qa.aiBriefModal();
  const requests=[];w.fetch=async(url,opts={})=>{
   requests.push({url,method:opts.method||'GET'});
   if(opts.method==='POST')return {ok:false,status:502,json:async()=>({error:{message:unsafe,diagnostic:{stage:'schema',validation_reason:'schema_list_item_blank',schema_field:'alternatives'}}})};
   const state=url==='/api/state';
   if((state&&scenario==='state-unavailable')||(!state&&scenario==='detail-unavailable'))throw new Error('fixture read unavailable');
   const payload=JSON.parse(JSON.stringify(state?S.state:S.detail));payload.ai=pool(scenario==='exhausted'?0:4);
   if(scenario==='malformed')payload.ai.usage_limits.repair_remaining_attempts=unsafe;
   return {ok:true,json:async()=>payload};
  };
  await w.qa.handleForm(w.document.querySelector('#action-form'));
  assert.equal(requests.filter(r=>r.method==='POST').length,1,'never automatically retries');
  assert.equal(w.document.querySelector('#form-submit').disabled,scenario!=='remaining');
  assert(w.document.querySelector('#form-error').textContent.includes('확인 항목: 대안'));
  if(scenario==='remaining'){
   assert(w.document.querySelector('.modal [data-ai-allowance]').textContent.includes('남은 시도: 4회'),'modal allowance refreshes after a consumed attempt');
   await w.qa.handleForm(w.document.querySelector('#action-form'));
   assert.equal(requests.filter(r=>r.method==='POST').length,2,'verified allowance permits a later explicit manual submit');
  }else{
   const count=requests.length;await w.qa.handleForm(w.document.querySelector('#action-form'));assert.equal(requests.length,count);
  }
  w.qa.closeModal(true);
 }
 S.detail=JSON.parse(read('backend_sample_detail.json'));S.state.ai={configured:true};S.detail.ai={configured:true};
 assert(!render().querySelector('[data-action="generate-ai"]').disabled,'legacy configured status remains usable');
 assert.equal(w.qa.aiStatusLabel({configured:true}),'AI 설정됨 · 호출 미검증');
 assert.equal(w.qa.aiStatusLabel({configured:true,connected:true}),'모델 응답 수신 · 본문 미검증');
 S.detail.ai={configured:false};assert(render().querySelector('[data-action="generate-ai"]').disabled);
 dom.window.close();console.log('Supplemental and repair pool AI UI PASS: bounded disclosure, blocked/exhausted/expired states, stale modal guard, legacy compatibility');
})().catch(e=>{console.error(e);process.exitCode=1;});
