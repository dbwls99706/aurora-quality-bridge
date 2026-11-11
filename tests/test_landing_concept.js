// Read-only chapter interaction and fallback contracts. Geometry is verified separately in Chromium.
const {JSDOM}=require('jsdom'), fs=require('fs'), path=require('path'), assert=require('assert');
const root=path.resolve(__dirname,'..');
const read=p=>fs.readFileSync(path.join(root,p),'utf8');
(async()=>{
 const dom=new JSDOM(read('static/landing.html'),{url:'http://127.0.0.1/',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document;
 let calls=[];
 w.fetch=async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>JSON.parse(read('backend_sample_state.json'))};};
 w.AbortController=AbortController;
 assert(d.querySelector('#workflow-detail').textContent.includes('17,900'),'initial explanation is server-rendered without JS');
 assert(d.querySelector('noscript').textContent.includes('품질위원회')||d.querySelector('noscript').textContent.includes('위원회'));
 w.eval(read('static/landing.js')+';window.setupIndex=setupContentsDisclosure;');
 await new Promise(r=>setImmediate(r));
 const buttons=[...d.querySelectorAll('[data-chapter]')], detail=d.querySelector('#workflow-detail');
 assert.equal(buttons.length,3);
 const assertStage=stage=>{assert.equal(detail.dataset.stage,stage);assert.equal(d.querySelectorAll('[data-chapter][aria-pressed="true"]').length,1);assert.equal(d.querySelector('[aria-pressed="true"]').dataset.chapter,stage);};
 assertStage('evidence');
 for(const stage of ['decision','recovery','evidence','evidence','recovery']){
  buttons.find(b=>b.dataset.chapter===stage).click();assertStage(stage);
  assert(d.querySelector('#detail-next').textContent.length>15);
  assert(d.querySelector('#detail-boundary').textContent.includes('실행')||d.querySelector('#detail-boundary').textContent.includes('시뮬레이션'));
 }
 const press=(i,key)=>buttons[i].dispatchEvent(new w.KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));
 press(2,'ArrowRight');assertStage('evidence');assert.equal(d.activeElement,buttons[0]);
 press(0,'ArrowLeft');assertStage('recovery');assert.equal(d.activeElement,buttons[2]);
 press(2,'Home');assertStage('evidence');press(0,'End');assertStage('recovery');
 press(2,'ArrowUp');assertStage('decision');press(1,'ArrowDown');assertStage('recovery');
 assert.equal(calls.length,1,'chapter selection never makes a request');
 assert.equal(calls[0].url,'/api/state');assert(!calls[0].options.method,'only read-only case load');
 assert(!d.body.textContent.includes('로컬 서버'),'hosting-neutral server copy');
 assert(d.body.textContent.includes('실제 모델 응답을 받은 이력이 있습니다.'),'actual successful call history, not current availability promise');
 assert(d.body.textContent.includes('응답 성공이 내용의 정확성을 보장하지는 않습니다.'),'transport success is not semantic verification');
 assert(d.body.textContent.includes('AI 의견은 원본 대조와 사람 검토가 필요'),'human review and source verification remain required');
 assert(d.body.textContent.includes('규칙 계산·의결과 구분합니다.'),'AI never substitutes rules or decision authority');
 assert(!d.body.textContent.includes('AI 기본 미연결')&&!d.body.textContent.includes('실제 모델 응답은 아직 검증되지 않았습니다'),'remove stale static deployment claims');
 assert.equal(d.querySelectorAll('h1').length,1);
 const primary=[...d.querySelectorAll('.primary-nav a')];
 assert.deepEqual(primary.map(a=>a.getAttribute('href')),['/','/workspace','/workspace#tasks','/workspace#sources','/workspace#audit']);
 assert.equal(d.querySelectorAll('.primary-nav [aria-current="page"]').length,1);
 assert.equal(d.querySelector('.primary-nav [aria-current="page"]').textContent,'소개');
 for(const link of d.querySelectorAll('.page-contents a'))assert(d.querySelector(link.getAttribute('href')),'real local contents target');
 w.location.hash='#cases';w.dispatchEvent(new w.HashChangeEvent('hashchange'));assert.equal(d.querySelector('.page-contents [aria-current="location"]').hash,'#cases');
 w.location.hash='#overview';w.dispatchEvent(new w.HashChangeEvent('hashchange'));assert.equal(d.querySelector('.page-contents [aria-current="location"]').hash,'#overview');


 assert(!d.querySelector('.hero-bridge'),'overlapping decorative letter/check artwork removed');
 assert(!d.querySelector('.case-dossier a,.case-dossier button'),'source dossier is not an action');
 const css=read('static/landing.css');
 assert(!/https?:\/\//.test(css),'no external asset dependency');
 assert(css.includes('prefers-reduced-motion'));
 assert(css.includes('.hero a:focus-visible{outline-color:var(--accent)}'));
 const lum=hex=>[0,2,4].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
 const tokens=Object.fromEntries([...css.matchAll(/--([\w-]+):#([a-f0-9]{6})/g)].map(m=>[m[1],m[2]]));
 for(const [a,b] of [['ink','paper'],['muted','paper'],['accent','white'],['white','accent'],['ink','soft'],['muted','soft']]){const ls=[lum(tokens[a]),lum(tokens[b])].sort((a,b)=>b-a);assert((ls[0]+.05)/(ls[1]+.05)>=4.5,a+' on '+b);}
 let scheduled=[];w.setTimeout=(fn,delay)=>{scheduled.push({fn,delay});return scheduled.length};w.clearTimeout=()=>{};w.matchMedia=()=>({matches:false});
 const play=d.querySelector('#play-explanation'),demo=d.querySelector('#hero-demo');
 play.click();assert.equal(demo.dataset.motion,'playing');assert.equal(scheduled.at(-1).delay,9000);assert(play.textContent.includes('멈추기'));
 play.click();assert.equal(demo.dataset.motion,'idle');assert(play.textContent.includes('보기'));
 play.click();scheduled.at(-1).fn();assert.equal(demo.dataset.motion,'idle');assert(d.querySelector('#motion-status').textContent.includes('사람'));
 w.matchMedia=()=>({matches:true});const beforeReduced=scheduled.length;play.click();assert.equal(scheduled.length,beforeReduced,'reduced motion needs no timer');assert.equal(demo.dataset.motion,'idle');
 assert.equal(calls.length,1,'animation never makes additional requests');
 assert(d.querySelector('.human-check').nextElementSibling,'icon has its own flex column, separate from all text');

 const index=d.querySelector('.contents-disclosure');
 let mediaChange;
 w.matchMedia=query=>({matches:query.includes('max-width'),addEventListener:(event,listener)=>{mediaChange=listener;}});
 w.setupIndex();
 assert.equal(index.open,false,'mobile local index starts compact');
 index.open=true;d.querySelector('.page-contents a[href="#cases"]').click();
 assert.equal(index.open,false,'mobile index closes after selecting a section');
 assert.equal(d.activeElement,d.querySelector('#cases'),'section receives focus after index closes');
 assert(!/#[a-f0-9]{6}/i.test(d.querySelector('h1').getAttribute('style')||''));
 assert(!css.includes('#254faf')&&!css.includes('#3159d9'),'rejected blue palette removed');
 dom.window.close();console.log('Landing concept PASS: static fallback, all chapters, repeated selection, arrow/Home/End focus, zero mutations, semantic hierarchy, local assets, reduced motion and contrast, motion start/stop/completion/reduced-motion and zero animation requests.');
})().catch(e=>{console.error(e);process.exitCode=1;});
