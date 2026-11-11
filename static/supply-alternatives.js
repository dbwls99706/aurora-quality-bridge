/* Read-only quantity-conserving alternatives. Does not call a mutation endpoint. */
(() => {
  'use strict';
  const states = new Map();
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = v => v === null || v === undefined ? '미확인' : Number(v).toLocaleString('ko-KR');
  const when = v => v ? esc(v.replaceAll('-', '.')) : '미확인';
  const keyOf = d => d.id + ':' + d.version;
  const amount = (id, label, value, max, required=false) => `<label for="${esc(id)}">${esc(label)}</label><input id="${esc(id)}" type="number" min="${required?1:0}" max="${max}" step="1" ${required?'required':''} value="${esc(value??'')}" data-field="${esc(id)}">`;
  function readDraft(form) {
    const value = field => form.querySelector(`[data-field="${field}"]`).value;
    return {total_quantity:value('alt-total'), early_quantity:value('alt-early'), allocations:[...form.querySelectorAll('[data-customer-id]')].map(row => {
      const get = name => row.querySelector(`[data-customer-field="${name}"]`).value;
      return {customer_id:row.dataset.customerId, required_quantity:get('required_quantity'), due_date:get('due_date'), early_quantity:get('early_quantity'), remaining_quantity:get('remaining_quantity')};
    })};
  }
  function render(d, request) {
    const key=keyOf(d), max=d.analysis.produced_qty;
    let state=states.get(key);
    if(!state) {
      state={sequence:0,result:null,draft:{total_quantity:d.conditions.replacement_qty??'',early_quantity:'',allocations:(d.customers||[]).map(c=>({customer_id:c.customer_id,required_quantity:'',due_date:'',early_quantity:0,remaining_quantity:0}))}};
      states.set(key,state);
    } else {
      // Reservations can change in another case without changing this version.
      // Re-rendered views retain drafts, never cached feasibility evidence.
      state.result=null;state.sequence++;
    }
    state.request=request;
    const draft=state.draft;
    const rows=(d.customers||[]).map((c,i)=>{
      const v=draft.allocations.find(a=>a.customer_id===c.customer_id)||{};
      const input=(field,label,type='number')=>`<label class="sr-only" for="alt-${i}-${field}">${esc(c.customer_id+' '+label)}</label><input id="alt-${i}-${field}" data-customer-field="${field}" type="${type}" ${type==='number'?`min="0" max="${max}" step="1"`:`min="${esc(d.conditions.effective_date)}" max="2030-12-31"`} value="${esc(v[field]??'')}" aria-label="${esc(c.customer_id+' '+label)}">`;
      return `<tr data-customer-id="${esc(c.customer_id)}"><th scope="row">${esc(c.customer_id)}<br>${esc(c.customer_name)}</th><td>${input('required_quantity','모의 필요량, 빈 값은 미확인')}</td><td>${input('due_date','모의 고객 기한, 빈 값은 미확인','date')}</td><td>${input('early_quantity','선공급 배분량')}</td><td>${input('remaining_quantity','잔량 배분량')}</td></tr>`;
    }).join('');
    return `<details class="supply-comparison supply-alternatives" ${state.result?'open':''}><summary>전체 물량을 유지한 공급 대안 비교 <span>전량 후공급 / 일부 선공급 + 잔량</span></summary><p>전체 모의 수량을 줄이지 않고 같은 물량의 공급 순서를 비교합니다. 확정 불량량과 출하 이력은 실제 재공급 필요량이 아닙니다. 고객별 입력도 모의 가정이며 승인·합의를 대신하지 않습니다.</p><form data-supply-alternatives data-key="${esc(key)}" data-case-id="${esc(d.id)}" data-version="${d.version}"><div class="form-grid"><div class="form-field">${amount('alt-total','전체 모의 수량 · EA',draft.total_quantity,max,true)}<small>빈 값은 미확인. 확정 불량량을 자동 입력하지 않습니다.</small></div><div class="form-field">${amount('alt-early','먼저 공급할 모의 수량 · EA',draft.early_quantity,max,true)}<small>잔량 = 전체 − 선공급. 잔량도 끝까지 계산합니다.</small></div></div><div class="table-scroll"><table class="data-table"><caption>고객별 모의 필요량·기한과 배분 · 빈 필요량/기한은 미확인, 0은 실제 불필요 확인 아님</caption><thead><tr><th scope="col">영향 고객</th><th scope="col">필요량 · EA</th><th scope="col">고객 기한</th><th scope="col">선공급 · EA</th><th scope="col">잔량 · EA</th></tr></thead><tbody>${rows}</tbody></table></div><p class="inline-note">배분하지 않은 물량은 미배정으로 남습니다. 동일 라인에서 각 공급분 생산·QA를 순차 계산하며 고객 도착일과 추가 비용은 미산정입니다.</p><button type="submit" class="button secondary">총량 유지 대안 계산</button><div data-alt-results aria-live="polite" class="preview-results">${state.result?renderResult(state.result):''}</div></form></details>`;
  }
  function renderResult(r) {
    const [a,b]=r.alternatives;
    const fields=[['전체 모의 수량',`${number(a.quantity)} EA`,`${number(b.quantity)} EA`],['공급분',a.lots.map(l=>`${number(l.quantity)} EA / ${when(l.ship_date)}`).join('<br>'),b.lots.map((l,i)=>`${i?'잔량':'선공급'} ${number(l.quantity)} EA / ${when(l.ship_date)}`).join('<br>')],['생산 영업일',number(a.production_days),number(b.production_days)],['저장된 공통 희망일 대비 최종 지연',`${number(a.final_delay_calendar_days)}달력일`,`${number(b.final_delay_calendar_days)}달력일`],['고객 기한 내 배정 물량',`${number(a.on_time_quantity)} EA`,`${number(b.on_time_quantity)} EA`],['고객 기한 초과 배정 물량',`${number(a.late_quantity)} EA`,`${number(b.late_quantity)} EA`],['고객 기한 미확인 배정 물량',`${number(a.unknown_deadline_quantity)} EA`,`${number(b.unknown_deadline_quantity)} EA`],['고객 미배정 물량',`${number(a.unallocated_quantity)} EA`,`${number(b.unallocated_quantity)} EA`]];
    const customerCell=c=>`${c.shipments.map(s=>`${number(s.quantity)} EA · ${when(s.ship_date)} · ${s.delay_calendar_days===null?'기한 미확인':s.delay_calendar_days?`${number(s.delay_calendar_days)}달력일 지연`:'입력 기한 내'}`).join('<br>')||'배정 없음'}<br>미충족 필요량: ${c.unmet_quantity===null?'필요량 미확인':`${number(c.unmet_quantity)} EA`}`;
    const issues=plan=>`<div class="form-notice"><strong>${esc(plan.name)}: 계산상 제약</strong><ul>${(plan.calculated_constraints.length?plan.calculated_constraints:['입력된 일정·능력에서 계산상 제약 없음. 실제 공급 가능 확인 아님.']).map(t=>`<li>${esc(t)}</li>`).join('')}</ul></div>`;
    return `<div class="section-title"><h3>동일 총량 ${number(r.total_quantity)} EA 비교</h3><span class="badge amber">두 안 모두 미승인 · 실행 없음</span></div><p>선공급 ${number(r.early_quantity)} EA + 잔량 ${number(r.remaining_quantity)} EA = 전체 ${number(r.total_quantity)} EA. 고객 배정 ${number(a.allocated_quantity)} EA + 미배정 ${number(a.unallocated_quantity)} EA = 전체 ${number(r.total_quantity)} EA.</p><div class="table-scroll"><table class="data-table"><thead><tr><th scope="col">비교 항목</th><th scope="col">${esc(a.name)}</th><th scope="col">${esc(b.name)}</th></tr></thead><tbody>${fields.map(row=>`<tr><th scope="row">${row[0]}</th><td>${row[1]}</td><td>${row[2]}</td></tr>`).join('')}</tbody></table></div><p class="inline-note">분할 시 첫 출하는 ${number(r.tradeoff.first_supply_earlier_calendar_days)}달력일 빨라지고 잔량 완료는 ${number(r.tradeoff.final_supply_later_calendar_days)}달력일 늦어집니다. 입력된 고객 기한 내 물량 변화: ${number(r.tradeoff.on_time_quantity_change)} EA. 한 지표만으로 유리한 안을 자동 선택하지 않습니다.</p><div class="table-scroll"><table class="data-table"><caption>같은 고객 배분을 두 안에서 비교 · 출하일 기준, 고객 도착일 미산정</caption><thead><tr><th scope="col">고객 · 모의 필요량 / 기한</th><th scope="col">전량 후공급</th><th scope="col">일부 선공급 + 잔량</th></tr></thead><tbody>${a.customers.map((c,i)=>`<tr><th scope="row">${esc(c.customer_id)} · ${esc(c.customer_name)}<br>${number(c.required_quantity)}${c.required_quantity===null?'':' EA'} / ${when(c.due_date)}</th><td>${customerCell(c)}</td><td>${customerCell(b.customers[i])}</td></tr>`).join('')}</tbody></table></div>${issues(a)}${issues(b)}<div class="form-notice"><strong>미확인 정보와 별도 승인</strong><ul>${[...a.missing_information,...a.approvals_required].map(t=>`<li>${esc(t)}</li>`).join('')}</ul><p>${esc(r.cost_comparison.reason)}</p><p>${esc(r.notice)}</p></div><details class="preview-basis"><summary>산식·원본 생산능력·가정 확인</summary><p>전량: 준비 + 기존 지연 + ceil(전체/일일 수량) + QA. 분할: 준비 + 기존 지연 + ceil(선공급/일일 수량) + QA + ceil(잔량/일일 수량) + QA.</p><p>일일 모의 ${number(r.conditions_used.production_daily_capacity)} EA · 보수적 일일 한도 ${number(r.capacity_basis.daily_capacity_limit)} EA · 잔여능력 ${number(r.capacity_basis.residual_capacity_qty)} EA · 적용일 ${when(r.conditions_used.effective_date)} · 준비 ${number(r.conditions_used.production_lead_days)}영업일 · 기존 지연 ${number(r.conditions_used.production_delay_days)}영업일 · 공급분별 QA ${number(r.conditions_used.qa_days)}영업일.</p>${r.capacity_basis.capacity_snapshot?`<p>원본: ${esc(r.capacity_basis.capacity_snapshot.source.path)} · ${number(r.capacity_basis.capacity_snapshot.source.row_number)}행 · 기준 ${when(r.capacity_basis.capacity_snapshot.snapshot_date)}.</p>`:''}<ul>${r.assumptions.map(t=>`<li>${esc(t)}</li>`).join('')}</ul></details>`;
  }
  document.addEventListener('input',event=>{
    const form=event.target.closest('form[data-supply-alternatives]');if(!form)return;
    const state=states.get(form.dataset.key);if(!state)return;
    state.draft=readDraft(form);state.result=null;state.sequence++;
    form.querySelector('[data-alt-results]').textContent='입력을 바꿨습니다. 총량 유지 대안을 다시 계산해 주세요.';
    form.querySelector('[type="submit"]').disabled=false;
  });
  document.addEventListener('submit',async event=>{
    const form=event.target;if(!form.matches('form[data-supply-alternatives]'))return;
    event.preventDefault();const state=states.get(form.dataset.key);if(!state)return;
    const node=form.querySelector('[data-alt-results]'),button=form.querySelector('[type="submit"]');
    if(!form.checkValidity()){form.reportValidity();return;}
    state.draft=readDraft(form);const d=state.draft,sequence=++state.sequence;
    const toInteger=(v,optional=false)=>{if(optional&&v==='')return null;if(!/^\d+$/.test(String(v)))throw Error('수량은 0 이상의 정수로 입력해 주세요.');const n=Number(v);if(!Number.isSafeInteger(n))throw Error('수량 범위를 확인해 주세요.');return n;};
    try {
      const total=toInteger(d.total_quantity),early=toInteger(d.early_quantity);
      if(early<1||early>=total)throw Error('선공급 수량은 1 EA 이상, 전체 모의 수량 미만이어야 합니다.');
      const entries=d.allocations.map(e=>({...e,required_quantity:toInteger(e.required_quantity,true),due_date:e.due_date||null,early_quantity:toInteger(e.early_quantity||'0'),remaining_quantity:toInteger(e.remaining_quantity||'0')}));
      const params=new URLSearchParams({total_quantity:total,early_quantity:early,allocations:JSON.stringify(entries),expected_version:form.dataset.version});
      button.disabled=true;node.textContent='전체 물량과 고객별 기한을 비교 중입니다.';
      const result=await state.request(`/api/cases/${encodeURIComponent(form.dataset.caseId)}/supply-alternatives?${params}`);
      if(!form.isConnected||state.sequence!==sequence)return;
      if(result.case_id!==form.dataset.caseId||result.version!==Number(form.dataset.version))throw Error('사건 조건이 바뀌었습니다. 최신 조건에서 다시 비교해 주세요.');
      state.result=result;node.innerHTML=renderResult(result);
    } catch(error) {
      if(form.isConnected&&state.sequence===sequence){state.result=null;node.innerHTML=`<p class="form-error" role="alert">${esc(error.message)}</p>`;}
    } finally {if(form.isConnected&&state.sequence===sequence)button.disabled=false;}
  });
  window.AuroraSupplyAlternatives={render,renderResult};
})();
