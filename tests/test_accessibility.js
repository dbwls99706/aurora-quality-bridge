const {JSDOM}=require('jsdom'),fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const js=fs.readFileSync(path.join(root,'static/app.js'),'utf8').replace("hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);",'');
const dom=new JSDOM(html,{url:'http://127.0.0.1',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,doc=w.document;
w.eval(js+';window.qa={S,renderView,replyModal,openModal,closeModal,renderSummary,mobileNav};');
const {S}=w.qa;
S.state=JSON.parse(fs.readFileSync(path.join(root,'backend_sample_state.json')));
S.detail=JSON.parse(fs.readFileSync(path.join(root,'backend_sample_detail.json')));
S.selected=S.detail.id;S.details.set(S.detail.id,S.detail);
w.qa.renderView();w.qa.renderSummary();
const q=s=>doc.querySelector(s),click=s=>q(s).click();
const key=(node,k)=>node.dispatchEvent(new w.KeyboardEvent('keydown',{key:k,bubbles:true,cancelable:true}));
const selectedTab=()=>q('[role="tab"][aria-selected="true"]');

// Tabs are a single tab stop with complete panel relationships and focus restored after render.
assert.equal(doc.querySelectorAll('[role="tab"][tabindex="0"]').length,1);
selectedTab().focus();key(selectedTab(),'ArrowRight');
assert.equal(S.tab,'evidence');assert.equal(doc.activeElement,selectedTab());
assert.equal(q('[role="tabpanel"]').getAttribute('aria-labelledby'),selectedTab().id);
assert.equal(selectedTab().getAttribute('aria-controls'),q('[role="tabpanel"]').id);
w.qa.renderView();assert.equal(doc.activeElement,selectedTab(),'tab focus survives DOM replacement');
key(selectedTab(),'End');assert.equal(S.tab,'audit');assert.equal(doc.activeElement,selectedTab());
key(selectedTab(),'ArrowRight');assert.equal(S.tab,'workflow');
key(selectedTab(),'ArrowLeft');assert.equal(S.tab,'audit');
key(selectedTab(),'Home');assert.equal(S.tab,'workflow');
click('[data-tab="recovery"]');assert.equal(doc.activeElement,selectedTab(),'clicked tab retains focus');

// Keyboard-style activation retains focus on the rerendered filter and exposes selection.
for(const filter of ['recall','investigation','all']){
 const button=q(`[data-filter="${filter}"]`);button.focus();button.dispatchEvent(new w.MouseEvent('click',{bubbles:true,detail:0}));
 assert.equal(doc.activeElement,q(`[data-filter="${filter}"]`));assert.equal(doc.activeElement.getAttribute('aria-pressed'),'true');
 assert.equal(doc.querySelectorAll('[data-filter][aria-pressed="true"]').length,1);
}

// A filtered-out selection never leaves actionable previous details beside an empty result.
const search=q('#case-search');search.focus();search.value='no-matching-case-884400';search.dispatchEvent(new w.Event('input',{bubbles:true}));
assert(q('#case-list').textContent.includes('검색 결과 없음'));
assert(!q('.detail-panel [data-action="conditions"]'));assert(!q('.detail-header'));
assert.equal(doc.activeElement,search,'typing does not replace the search input');
search.value='';search.dispatchEvent(new w.Event('input',{bubbles:true}));assert(q('.detail-header'));
assert.equal(S.selected,S.detail.id,'search does not mutate cached selection');

// Dismissal prompts only when reply fields changed; cancelling preserves form and focus trap.
const content='<label for="note">회신</label><textarea id="note" name="note"></textarea>';
w.qa.openModal('reply','회신','초안 보호',content);
let confirmCount=0;w.confirm=()=>{confirmCount++;return false;};
q('#note').value='저장 전 회신 초안';q('#note').focus();key(q('#note'),'Escape');
assert(q('.modal'));assert.equal(q('#note').value,'저장 전 회신 초안');assert.equal(confirmCount,1);
click('[data-action="close-modal"]');assert(q('.modal'));assert.equal(confirmCount,2);
w.confirm=()=>true;key(q('#note'),'Escape');assert(!q('.modal'));
w.qa.openModal('reply','회신','변경 없음',content);w.confirm=()=>{throw Error('unchanged reply must not prompt')};w.qa.closeModal();assert(!q('.modal'));
w.qa.openModal('reply','회신','저장 완료',content);q('#note').value='저장된 회신';w.qa.closeModal(true);assert(!q('.modal'),'save bypasses discard prompt');

// Mobile board can expose all column counts and direct links before the long open queue.
S.view='tasks';w.qa.renderView();
for(const status of ['open','blocked','complete']){
 const link=q(`.board-jump a[href="#board-${status}"]`);assert(link);assert(q(link.getAttribute('href')));
}
S.view='sources';w.qa.renderView();assert(q('.source-summary [data-action="guide"]').textContent.includes('운영 원칙'));

// Verify the design tokens used on both surfaces. Browser QA checks the cascade and geometry.
const css=fs.readFileSync(path.join(root,'static/workspace.css'),'utf8');
const landingCSS=fs.readFileSync(path.join(root,'static/landing.css'),'utf8');
const luminance=hex=>{if(hex.length===3)hex=hex.split('').map(x=>x+x).join('');return [0,2,4].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(c=>c<=.04045?c/12.92:((c+.055)/1.055)**2.4).reduce((n,c,i)=>n+c*[.2126,.7152,.0722][i],0);};
const contrast=(a,b)=>{const l=[luminance(a),luminance(b)].sort((a,b)=>b-a);return(l[0]+.05)/(l[1]+.05);};
const tokens=Object.fromEntries([...css.matchAll(/--([\w-]+):#([a-f0-9]{6})/g)].map(m=>[m[1],m[2]]));
const pairs=[['ink','bg'],['ink','panel'],['muted','bg'],['muted','panel'],['green','green-soft'],['amber','amber-soft'],['red','red-soft'],['blue','blue-soft'],['inverse','ink'],['inverse','green']];
for(const [a,b] of pairs)assert(contrast(tokens[a],tokens[b])>=4.5,`${a} on ${b} contrast`);
for(const [a,b] of [['d7ddcf','20312f'],['c6d1c7','20312f'],['f5f3eb','3a5047'],['59615c','eef0e6'],['69552f','f5eddb']])assert(contrast(a,b)>=4.5,`${a} on ${b} contrast`);
const landingTokens=Object.fromEntries([...landingCSS.matchAll(/--([\w-]+):#([a-f0-9]{6})/g)].map(m=>[m[1],m[2]]));
for(const [a,b] of [['ink','paper'],['muted','paper'],['muted','soft'],['accent','white'],['white','ink']])assert(contrast(landingTokens[a],landingTokens[b])>=4.5,`landing ${a} on ${b} contrast`);
const focusRule=css.match(/\.sidebar :focus-visible\{([^}]+)\}/);
assert(focusRule&&focusRule[1].includes('outline:3px solid var(--inverse)'), 'dark sidebar has its own visible focus style');
assert(contrast(tokens.inverse,tokens.ink)>=3,'sidebar focus ring meets the 3:1 non-text contrast threshold');
// Compute the applied cascade, including the legacy base sheet and responsive rules.
// jsdom resolves colors and keyboard-focus selectors; real-browser geometry remains a separate check.
w.qa.mobileNav();
const baseCSS=fs.readFileSync(path.join(root,'static/style.css'),'utf8');
const rgbHex=value=>{const n=value.match(/^rgb\((\d+),\s*(\d+),\s*(\d+)\)$/);assert(n,'resolved RGB color: '+value);return n.slice(1).map(v=>Number(v).toString(16).padStart(2,'0')).join('');};
for(const [width,surface,selector] of [[390,'.mobile-nav','.mobile-nav button'],[1280,'.sidebar','.sidebar [data-view]']]){
 const style=doc.createElement('style');style.textContent=baseCSS+'\n'+css;doc.head.append(style);
 const matches=query=>!/(print|prefers-reduced-motion)/.test(query)&&[...query.matchAll(/(max|min)-width:\s*(\d+)px/g)].every(([,kind,n])=>kind==='max'?width<=Number(n):width>=Number(n));
 const flatten=rules=>[...rules].map(rule=>rule.type===4?(matches(rule.conditionText||rule.media.mediaText)?flatten(rule.cssRules):''):rule.cssText).join('\n');
 style.textContent=flatten(style.sheet.cssRules);
 assert.notEqual(w.getComputedStyle(q(surface)).display,'none',surface+' is displayed at tested breakpoint');
 for(const button of doc.querySelectorAll(selector)){
  button.focus();assert(button.matches(':focus-visible'));
  const computed=w.getComputedStyle(button),bg=w.getComputedStyle(q(surface)).backgroundColor;
  assert.equal(computed.outlineWidth,'3px');assert.equal(computed.outlineStyle,'solid');
  assert(contrast(rgbHex(computed.outlineColor),rgbHex(bg))>=3,surface+' applied keyboard focus contrast');
  if(width===390)assert.equal(computed.outlineOffset,'0px','mobile ring stays inside the fixed navigation padding');
 }
 style.remove();
}
assert(css.includes('prefers-reduced-motion'));assert(landingCSS.includes('prefers-reduced-motion'));
console.log('Accessibility PASS: keyboard tabs/filters, focus restoration, empty search isolation, unsaved dismissal, board links, 20 text contrast pairs, applied sidebar/mobile focus cascade contrast and reduced-motion rules');
dom.window.close();
