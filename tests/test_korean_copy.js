// Copy contracts: clear actions, domain terminology, units, and honest simulation scope.
const {JSDOM}=require('jsdom'),fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..'),read=p=>fs.readFileSync(path.join(root,p),'utf8');
const dom=new JSDOM(read('static/index.html'),{url:'http://127.0.0.1/workspace',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,q=s=>w.document.querySelector(s);
w.eval(read('static/app.js').replace("hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);",'')+';window.qa={S,renderView,renderCorrectionReview,conditionsModal,closeModal,renderSupplyComparisonResult,showPolicy,showEvidence,closeDrawer};');
const {S}=w.qa;S.state=JSON.parse(read('backend_sample_state.json'));S.detail=JSON.parse(read('backend_sample_detail.json'));S.selected=S.detail.id;S.details.set(S.selected,S.detail);
assert(new JSDOM(read('static/landing.html')).window.document.querySelector('h1').textContent.includes('품질 판단에서,다음 공급까지.'));
for(const filename of ['static/landing.html','static/index.html','static/app.js']){
 const source=read(filename);
 for(const obsolete of ['품질의 판단','사람의 의결 대기','고객의 다음 공급 약속','후보 가정','별도 트랙','모의입력','AI 결과로 가장'])assert(!source.includes(obsolete),filename+': '+obsolete);
}
w.qa.renderView();assert.equal(q('#breadcrumb-current').textContent,'품질 검토');assert(q('.next-action small').textContent.startsWith('다음 할 일'));
assert(q('.workflow').textContent.includes('규칙 기반 계산'));assert(q('.workflow').textContent.includes('품질위원회'));
const corrected={...structuredClone(S.detail),correction_review_required:true};
assert(w.qa.renderCorrectionReview(corrected).includes('모의 정정 후 확정 불량률이 10% 미만'));
assert(!w.qa.renderCorrectionReview(corrected).includes('검사 수량은 10%'));
w.qa.conditionsModal();assert(q('label[for="customer_due_date"]').textContent.includes('고객 희망일'));assert(q('#customer_due_date').parentElement.textContent.includes('계획 적용일과 같거나 이후'));assert(q('[data-preset="delay"]').textContent.includes('7영업일'));w.qa.closeModal(true);
S.tab='recovery';w.qa.renderView();assert(q('#preview-submit').textContent.includes('조건 비교하기'));
assert(q('#preview-date-help').textContent.includes('과 같거나 이후'));
const result={case_id:S.detail.id,baseline:{quantity:1000},candidate:{quantity:500,blockers:['담당자 확인 필요']},conditions_used:S.detail.conditions};
const comparison=w.qa.renderSupplyComparisonResult(result);assert(comparison.includes('현재 요청보다 500 EA 적게 입력'));assert(comparison.includes('남은 수량의 공급 계획은 별도로 확인'));assert(comparison.includes('이 조건으로 변경 창 열기'));assert(comparison.includes('아직 저장하지 않았습니다'));
w.qa.showPolicy();assert(q('.drawer').textContent.includes('10% 이상'));assert(q('.drawer').textContent.includes('실제 회수, 생산 발주, 외부 메시지 발송은 실행하지 않습니다'));w.qa.closeDrawer();
w.qa.showEvidence();assert(q('.drawer').textContent.includes('실제 AI 모델은 연결되지 않았으며'));w.qa.closeDrawer();
const backend=read('backend.py');assert(!backend.includes('최조'));assert(!backend.includes('가용 슬롯'));assert(backend.includes('생산 예약의 합계'));assert(backend.includes('AI가 작성한 설명과 정책 해석은 아직 검증되지 않았습니다'));
dom.window.close();console.log('Korean copy PASS: readable domain terminology, action-specific buttons, defect-rate units, inclusive due date, business-day labels, unsaved comparison and explicit AI/simulation boundaries');
