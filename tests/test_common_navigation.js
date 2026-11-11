// Shared site shell and actual workspace routes; isolated local data, no external calls.
const {JSDOM}=require('jsdom'), fs=require('fs'), cp=require('child_process'), path=require('path'), assert=require('assert');
const root=path.resolve(__dirname,'..');
const read=p=>fs.readFileSync(path.join(root,p),'utf8');
const sleep=n=>new Promise(r=>setTimeout(r,n));
const server=cp.spawn(process.env.PYTHON||'python',['backend.py','--port','0','--db','/tmp/aurora-common-navigation-'+Date.now()+'.sqlite3'],{cwd:root});
let base='',startError='';server.stdout.on('data',s=>{const m=String(s).match(/http:\/\/127\.0\.0\.1:\d+/);if(m)base=m[0]});server.stderr.on('data',s=>startError+=s);
(async()=>{let dom;try{
 for(let i=0;i<150&&!base;i++){assert(server.exitCode===null,startError);await sleep(40)}assert(base);
 for(const file of ['landing.html','index.html']){
  const doc=new JSDOM(read('static/'+file)).window.document;
  assert.equal(doc.querySelectorAll('[data-site-header]').length,1);
  assert.deepEqual([...doc.querySelectorAll('.primary-nav a')].map(a=>a.textContent),['소개','품질 검토','공급 복구','근거 자료','감사 이력']);
  assert.equal(doc.querySelector('link[href="/static/common-nav.css"]').rel,'stylesheet');
  assert(doc.querySelector('script[src="/static/common-nav.js"]'));
 }
 for(const asset of ['/static/common-nav.css','/static/common-nav.js'])assert.equal((await fetch(base+asset)).status,200);
 for(const blocked of ['/backend.py','/static/../backend.py','/package.json'])assert.equal((await fetch(base+blocked)).status,404);
 dom=new JSDOM(read('static/index.html'),{url:base+'/workspace?case=CASE-002#tasks',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,d=w.document,q=s=>d.querySelector(s),errors=[],calls=[];
 w.AbortController=AbortController;w.confirm=()=>true;w.scrollTo=()=>{};
 w.fetch=(u,o)=>{calls.push({url:String(u),method:o?.method||'GET'});return fetch(new URL(u,w.location.href),o)};
 w.addEventListener('error',e=>errors.push(e.message));
 w.eval(read('static/common-nav.js'));assert.equal(q('.primary-nav [aria-current="page"]').dataset.destination,'tasks','initial indicator works before API response');
 w.eval(read('static/app.js')+';window.qa={S};');
 const {S}=w.qa;
 const wait=async f=>{for(let i=0;i<150;i++){if(f())return;await sleep(25)}throw Error('Timed out: '+f)};
 const nav=view=>q(`.primary-nav [data-view="${view}"]`);
 await wait(()=>S.state&&!S.loading&&!S.loadingAll&&!q('#role-select').disabled);
 const role=S.role,initialSessionCount=calls.filter(c=>c.url==='/api/session').length;
 for(const view of ['sources','audit','workspace','tasks','tasks','workspace']){
  nav(view).click();await wait(()=>S.view===view&&!S.loadingAll);
  assert.equal(q('.primary-nav [aria-current="page"]').dataset.view,view);
  assert.equal(S.selected,'CASE-002');assert.equal(S.role,role);
  assert.equal(new URL(nav('tasks').href).searchParams.get('case'),'CASE-002');
 }
 await wait(()=>q('.detail-header'));const route=w.location.href;
 w.history.back();await wait(()=>S.view==='tasks');assert.equal(q('.primary-nav [aria-current="page"]').dataset.view,'tasks');
 w.history.forward();await wait(()=>S.view==='workspace'&&w.location.href===route);
 q('[data-action="conditions"]').click();q('#reason').value='헤더 이동 취소 시 보존할 미저장 사유';q('#reason').dispatchEvent(new w.Event('input',{bubbles:true}));
 w.confirm=()=>false;nav('sources').click();await sleep(50);assert.equal(S.view,'workspace');assert.equal(w.location.href,route);assert.equal(q('#reason').value,'헤더 이동 취소 시 보존할 미저장 사유');assert.equal(q('.primary-nav [aria-current="page"]').dataset.view,'workspace');
 w.confirm=()=>true;nav('sources').click();await wait(()=>S.view==='sources'&&!q('.modal'));assert.equal(d.body.style.overflow,'');
 const before=w.location.href;let modifiedIntercepted;d.addEventListener('click',event=>{if(event.ctrlKey){modifiedIntercepted=event.defaultPrevented;event.preventDefault();}},{once:true});nav('tasks').dispatchEvent(new w.MouseEvent('click',{bubbles:true,cancelable:true,ctrlKey:true}));await sleep(30);assert.equal(modifiedIntercepted,false,'app leaves modified links to browser');assert.equal(S.view,'sources');assert.equal(w.location.href,before,'modified click keeps current view');
 assert.equal(calls.filter(c=>c.url==='/api/session').length,initialSessionCount,'internal navigation never resets role/session');
 assert.deepEqual(calls.filter(c=>c.method!=='GET'&&c.url!=='/api/session'),[],'navigation never mutates case data');

 // Cross-document context stores only UI choices, never a token, draft or authoritative case state.
 w.AuroraShell.update('sources','CASE-002','recovery','sales');
 const saved=w.sessionStorage.getItem('aurora.workspace.navigation');
 assert.deepEqual(JSON.parse(saved),{view:'sources',caseId:'CASE-002',tab:'recovery',role:'sales'});
 const landing=new JSDOM(read('static/landing.html'),{url:base+'/',runScripts:'outside-only'});
 landing.window.sessionStorage.setItem('aurora.workspace.navigation',saved);landing.window.eval(read('static/common-nav.js'));
 assert.equal(landing.window.document.querySelector('.primary-nav [data-destination="workspace"]').getAttribute('href'),'/workspace?case=CASE-002&tab=recovery');
 assert.equal(landing.window.document.querySelector('.header-cta').getAttribute('href'),'/workspace?case=CASE-002#sources');assert.equal(landing.window.document.querySelector('.hero-actions .button').getAttribute('href'),'/workspace?case=CASE-002&tab=recovery');landing.window.close();
 const resumed=new JSDOM(read('static/index.html'),{url:base+'/workspace',runScripts:'outside-only'});
 resumed.window.sessionStorage.setItem('aurora.workspace.navigation',saved);resumed.window.eval(read('static/common-nav.js'));
 assert.equal(resumed.window.AuroraShell.restore().role,'sales');assert.equal(resumed.window.AuroraShell.restore().caseId,'CASE-002');resumed.window.close();
 assert.deepEqual(errors,[]);
 console.log('Common navigation PASS: same header/link labels, asset allowlist and denied source paths, immediate route indicator, case/role preservation, repeated navigation, Back/Forward, dirty cancellation/confirmation, modified links, no case mutations.');
}finally{dom?.window.close();server.kill()}})().catch(e=>{console.error(e);process.exitCode=1});
