'use strict';
// Read-only case navigation. No sessions or workflow mutations are made on the landing page.
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const caseStatuses = {draft:'검토 준비',awaiting_approval:'의결 대기',approved:'회수 의결 완료',review_required:'재검토 필요',rejected:'재검토 필요',investigating:'조사 계속',recovery_ready:'복구 준비 완료'};
function liveCaseStatus(c){if(c.status==='review_required')return c.recall_authorized?'의결 유지 · 공급 재확인':'판정 정정 · 의결 재검토';if(c.status==='rejected')return '의결 반려 · 보완 필요';if(c.status==='awaiting_approval'&&c.recall_eligible===false)return '정정 검토 대기';return caseStatuses[c.status]||c.status;}
async function loadCaseExplorer() {
  const target = document.getElementById('case-explorer');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch('/api/state', {signal:controller.signal});
    if (!response.ok) throw new Error('State unavailable');
    const state = await response.json();
    const cases = state.cases || [];
    target.innerHTML = cases.length ? cases.map((c, i) => {
      const n = Number(c.defect_rate_pct);
      const rate = Number.isFinite(n) ? n.toFixed(n !== 10 && Math.abs(n - 10) < .005 ? 6 : 2) : '미확인';
      return `<a class="case-link" href="/workspace?case=${encodeURIComponent(c.id)}"><span class="case-number">${String(i + 1).padStart(2, '0')}</span><div class="case-link-title"><span>${escapeText(c.batch_id)}</span><h3>${escapeText(c.title)}</h3></div><div class="case-link-result"><strong>${escapeText(rate)}<small>%</small></strong><span>현재 확정 불량률</span></div><span class="case-link-status">${escapeText(liveCaseStatus(c))}</span><span class="case-link-arrow" aria-hidden="true">↗</span></a>`;
    }).join('') : '<p class="case-loading">등록된 사건이 없습니다. <a href="/workspace">워크스페이스에서 새 사건을 등록하세요.</a></p>';
  } catch {
    target.innerHTML = '<div class="case-loading"><p>현재 사건 목록을 불러오지 못했습니다. 서버 연결을 확인해 주세요.</p><button type="button" id="retry-cases">다시 불러오기 ↻</button><a href="/workspace">워크스페이스 열기 ↗</a></div>';
    document.getElementById('retry-cases').addEventListener('click', loadCaseExplorer);
  } finally { clearTimeout(timeout); }
}
loadCaseExplorer();

// Explanatory chapters only. Selecting one never calls an API or changes a case.
function setupWorkflowExplorer() {
  const chapters = [...document.querySelectorAll('[data-chapter]')];
  const detail = document.getElementById('workflow-detail');
  if (!detail || !chapters.length) return;
  const content = {
    evidence: {
      kicker: '01 / EVIDENCE', title: '같은 배치, 같은 기준.',
      description: '클레임 · 생산 배치 · 품질 출하를 배치 ID로 연결합니다. 확정 불량과 미확정 검사를 구분하고 계산에 사용한 원본을 확인합니다.',
      visual: '<div class="source-equation"><div><span>규격이탈 확정</span><strong>17,900 <small>EA</small></strong></div><span class="equation-symbol">÷</span><div><span>생산 배치</span><strong>150,745 <small>EA</small></strong></div></div><p class="visual-note">B-02-2602-01 · 불량률 11.87%<br>판정에는 반올림 전 비율을 적용합니다.</p>',
      next: '원본 행과 파일 해시(SHA-256)를 대조한 뒤, 품질위원회에 검토를 요청합니다.',
      boundary: '원인 조사는 별도입니다. 계산 결과만으로 회수를 실행하지 않습니다.'
    },
    decision: {
      kicker: '02 / HUMAN DECISION', title: '규칙은 계산하고, 사람은 의결합니다.',
      description: '품질팀의 상신과 위원회의 승인·반려를 모의 기록합니다. 판단에 사용한 근거와 사유를 함께 남기고, 조건이 달라지면 다시 확인할 내용을 구분합니다.',
      visual: '<div class="decision-rule"><strong>10%</strong><p>이상 회수 대상<span>위원회 의결 필요 · 자동 실행 없음</span></p></div><p class="visual-note">대표 배치 11.87%는 규칙상 회수 요건에 해당합니다.<br>이 설명은 현재 사건의 의결 상태가 아닙니다.</p>',
      next: '품질위원회가 근거를 검토한 뒤 승인 또는 반려 사유를 기록합니다.',
      boundary: '규칙 판정과 위원회 의결은 다릅니다. 역할 선택과 의결 처리는 시뮬레이션입니다.'
    },
    recovery: {
      kicker: '03 / RECOVERY READINESS', title: '공급 가능은, 확인이 모인 다음에.',
      description: '영업·생산·품질의 확인을 서로 다른 업무로 연결합니다. 수량이나 희망일이 바뀌면 기존 확인을 그대로 재사용하지 않고 새 조건에 맞춰 검토합니다.',
      visual: '<ul class="recovery-checks"><li><i aria-hidden="true"></i>고객 보유량</li><li><i aria-hidden="true"></i>생산 가능일</li><li><i aria-hidden="true"></i>품질 재승인</li></ul><p class="visual-note">확인해야 할 조건 예시입니다.<br>표시된 항목은 실제 업무의 완료 상태가 아닙니다.</p>',
      next: '부서별 회신과 생산 일정을 확인하고, 필요한 공급 조건을 비교합니다.',
      boundary: '회신이 없으면 확인할 업무로 남습니다. 실제 고객 연락이나 생산 발주는 실행하지 않습니다.'
    }
  };
  function selectChapter(button) {
    const key = button.dataset.chapter, item = content[key];
    if (!item) return;
    chapters.forEach(chapter => chapter.setAttribute('aria-pressed', String(chapter === button)));
    detail.dataset.stage = key;
    for (const field of ['kicker', 'title', 'description', 'next', 'boundary']) {
      document.getElementById(`detail-${field}`).textContent = item[field];
    }
    // All visual fragments are trusted, fixed local explanations, never API text.
    document.getElementById('detail-visual').innerHTML = item.visual;
    const reduce = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduce && typeof detail.animate === 'function') {
      detail.getAnimations?.().forEach(animation => animation.cancel());
      detail.animate([{opacity:.72,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:200,easing:'cubic-bezier(.2,.8,.2,1)'});
    }
  }
  chapters.forEach((button, index) => {
    button.addEventListener('click', () => selectChapter(button));
    button.addEventListener('keydown', event => {
      let target;
      if (['ArrowRight', 'ArrowDown'].includes(event.key)) target = (index + 1) % chapters.length;
      if (['ArrowLeft', 'ArrowUp'].includes(event.key)) target = (index - 1 + chapters.length) % chapters.length;
      if (event.key === 'Home') target = 0;
      if (event.key === 'End') target = chapters.length - 1;
      if (target === undefined) return;
      event.preventDefault();
      chapters[target].focus();
      selectChapter(chapters[target]);
    });
  });
}
setupWorkflowExplorer();

// A calm, user-triggered explanation. It never changes any case or approval.
function setupSignalExplanation() {
  const demo = document.getElementById('hero-demo');
  const button = document.getElementById('play-explanation');
  if (!demo || !button) return;
  const label = document.getElementById('play-label');
  const status = document.getElementById('motion-status');
  const motionPreference = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
  const summary = '근거 연결 → 기준 비교 → 의결·공급 조건 확인. 실제 승인은 사람이 기록합니다.';
  let finishTimer;
  let stageTimers = [];
  const stageLabel = demo.querySelector('.demo-header-actions>span');
  const initialStageLabel = stageLabel.textContent;
  function stage(name, labelText, message) {
    demo.dataset.step = name;
    stageLabel.textContent = labelText;
    status.textContent = message;
  }
  function stop(message = '흐름 설명입니다. 실제 사건 상태나 의결을 변경하지 않습니다.') {
    clearTimeout(finishTimer);
    stageTimers.forEach(clearTimeout); stageTimers = [];
    delete demo.dataset.step;
    stageLabel.textContent = initialStageLabel;
    demo.dataset.motion = 'idle';
    button.setAttribute('aria-pressed', 'false');
    label.textContent = '연결 과정 보기';
    button.querySelector('.play-symbol').textContent = '▶';
    status.textContent = message;
  }
  button.addEventListener('click', () => {
    if (demo.dataset.motion === 'playing') { stop('설명 재생을 멈췄습니다. 사건 데이터는 변경되지 않았습니다.'); return; }
    const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) { stop(summary); return; }
    demo.dataset.motion = 'playing';
    button.setAttribute('aria-pressed', 'true');
    label.textContent = '설명 멈추기';
    button.querySelector('.play-symbol').textContent = '■';
    stage('sources', '01 / 03 · 근거 연결', '01 / 03 · 클레임·생산·출하 기록을 같은 배치로 연결합니다. 전체 설명은 9초이며 언제든 멈출 수 있습니다.');
    stageTimers = [
      setTimeout(() => stage('comparison', '02 / 03 · 기준 비교', '02 / 03 · 확정 불량률 11.87%를 회수 기준 10%와 비교합니다. 숫자는 원본 기준이며 의결은 사람이 합니다.'), 3000),
      setTimeout(() => stage('handoff', '03 / 03 · 다음 확인', '03 / 03 · 고객 보유량, 생산 가능일, 품질 재승인을 차례로 확인합니다. 실제 승인이나 사건 상태를 변경하지 않습니다.'), 6000)
    ];
    finishTimer = setTimeout(() => stop(summary), 9000);
  });
  button.setAttribute('aria-pressed', 'false');
  motionPreference?.addEventListener?.('change', event => { if (event.matches) stop(summary); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  window.addEventListener('pagehide', () => stop());
  demo.addEventListener('keydown', event => {
    if (event.key === 'Escape' && demo.dataset.motion === 'playing') stop('설명 재생을 멈췄습니다. 사건 데이터는 변경되지 않았습니다.');
  });
}
setupSignalExplanation();


function setupPageContentsAndMotion() {
  const links = [...document.querySelectorAll('.page-contents a')];
  const sections = links.map(link => document.querySelector(link.getAttribute('href'))).filter(Boolean);
  function mark(id) { links.forEach(link => { if(link.hash === '#'+id) link.setAttribute('aria-current','location'); else link.removeAttribute('aria-current'); }); }
  const markHash = () => { const id=location.hash.slice(1); if(sections.some(section=>section.id===id)) mark(id); else if(!id) mark('overview'); };
  window.addEventListener('hashchange',markHash);markHash();
  if (typeof IntersectionObserver !== 'function') return;
  const contentsObserver = new IntersectionObserver(entries => {
    const visible = entries.filter(entry=>entry.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top);
    if(visible.length)mark(visible[0].target.id);
  },{rootMargin:'-10% 0px -70% 0px',threshold:0});
  sections.forEach(section=>contentsObserver.observe(section));
  const reduce=typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(reduce)return;
  const enterObserver=new IntersectionObserver(entries=>{
    entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('has-entered');enterObserver.unobserve(entry.target);}});
  },{threshold:.07});
  document.querySelectorAll('.data-band,.approach,.case-section,.boundaries,.closing').forEach(section=>{section.classList.add('motion-section');enterObserver.observe(section);});
}
setupPageContentsAndMotion();

// The page index is a disclosure on phones, distinct from product destinations.
function setupContentsDisclosure() {
  const index = document.querySelector('.contents-disclosure');
  if (!index || typeof matchMedia !== 'function') return;
  const compact = matchMedia('(max-width: 850px)');
  const sync = () => { index.open = !compact.matches; };
  sync(); compact.addEventListener?.('change', sync);
  index.querySelectorAll('a').forEach(link => link.addEventListener('click', () => {
    if (compact.matches) { index.open = false; document.querySelector(link.getAttribute('href'))?.focus({preventScroll: true}); }
  }));
}
setupContentsDisclosure();
