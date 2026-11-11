// Local DOM replay. Does not certify model narrative semantics.
const {JSDOM}=require('jsdom'),fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
const dom=new JSDOM(read('static/index.html'),{url:'http://127.0.0.1/workspace',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window;
w.AbortController=AbortController;w.eval(read('static/app.js').replace("hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);",'')+';window.qa={S,renderAIBrief,renderAIGrounding};');
const {S}=w.qa;S.state=JSON.parse(read('backend_sample_state.json'));const d=JSON.parse(read('backend_sample_detail.json'));S.detail=d;
d.ai={configured:false,reviews:[],brief:JSON.parse(read('tests/fixtures/accepted_ai_brief_authority_20261111.json'))};d.ai.brief.stale=false;d.ai.brief.review_revision=d.review_revision;
d.customers=[{customer_id:'C-105',shipment_count:0,link_status:'claims_without_sbl'}];
const section=w.document.createElement('section');section.innerHTML=w.qa.renderAIBrief(d);const boundary=section.querySelector('.ai-decision-boundaries');
for(const text of ['별도 승인 절차·승인권자: 제공된 원본에 명시 없음','추가 선행승인이나 승인 면제를 단정하지','F2 투자 승인: CEO/이사회','품질위원회의 회수 의결이나 기존 라인 재공급과 다른 결정','2년 장기+MOQ+자동갱신·위약'])assert(boundary.textContent.includes(text),text);
assert(!section.querySelector('.ai-contract-source').open);assert(!section.querySelector('.ai-untrusted').open);
assert(section.querySelector('.ai-untrusted').textContent.includes('재공급 승인 필요'),'preserve actual wrong sentence');
assert(section.querySelector('.ai-grounding').textContent.includes('해당 배치 SBL 없음: C-105'));
assert(section.querySelector('.ai-grounding').textContent.includes('고객 제외나 회수 범위 불일치를 확정하지'));
d.analysis.alternatives=[{id:'existing_line'}];d.analysis.supply_constraints.material={material_id:'M-A',supplier_id:'V-201',contract_type:'연간 단가계약',is_exclusive:false};
section.innerHTML=w.qa.renderAIGrounding(d);assert(!section.textContent.includes('F2 투자 승인:'));assert(section.querySelector('.ai-contract-source').textContent.includes('비독점'));
d.analysis.supply_constraints.material.contract_type='<img src=x onerror=alert(1)>';section.innerHTML=w.qa.renderAIGrounding(d);assert(!section.querySelector('img'));assert(section.textContent.includes('<img'));
dom.window.close();console.log('AI authority UI PASS: unknown extra approval/no exemption, department confirmations vs F2 CEO/board decision, original contract/source, exact C105 link gap, collapsed untrusted text, no false F2/nonexclusive inheritance, escaping');
