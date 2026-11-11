const fs=require('fs'),vm=require('vm'),path=require('path');
const root=path.resolve(__dirname,'..');
// Template-level smoke only: this lightweight DOM stub does not validate browser layout, focus, or HTTP flows.
const data={};const elements=new Map();const element=()=>({innerHTML:'',textContent:'',value:'',dataset:{},classList:{toggle(){}},setAttribute(){},focus(){},append(){},style:{}});
const document={querySelector:s=>{if(!elements.has(s))elements.set(s,element());return elements.get(s)},querySelectorAll:()=>[],addEventListener(){},body:element(),activeElement:element(),createElement:element};
const context={document,console,setTimeout:()=>0,clearTimeout(){},AbortController,crypto:require('crypto').webcrypto,fetch:()=>{},FormData:class{*[Symbol.iterator](){}},Map,Date};vm.createContext(context);
let js=fs.readFileSync(path.join(root,'static/app.js'),'utf8').replace('hydrateIcons();mobileNav();setRole(S.role,false);loadState(true);','');
vm.runInContext(js,context);
context.state=JSON.parse(fs.readFileSync(path.join(root,'backend_sample_state.json')));
context.detail=JSON.parse(fs.readFileSync(path.join(root,'backend_sample_detail.json')));
vm.runInContext(`S.state=state;S.detail=detail;S.selected=detail.id;S.details.set(detail.id,detail);`,context);
let count=0;for(const view of ['workspace','tasks','sources','audit'])for(const tab of ['workflow','evidence','recovery','audit']){vm.runInContext(`S.view='${view}';S.tab='${tab}';renderView();`,context);const html=elements.get('#view-container').innerHTML;if(!html||html.includes('[object Object]')||html.includes('undefined'))throw Error(view+'/'+tab+' invalid render');count++;}
for(const status of ['draft','awaiting_approval','approved','review_required','rejected','investigating','recovery_ready']){vm.runInContext(`S.detail.status='${status}';renderWorkflow(S.detail)`,context);count++;}
for(const role of ['quality','sales','production','committee']){vm.runInContext(`S.role='${role}';conditionsModal()`,context);count++;}
const escaped=vm.runInContext(`esc('<img src=x onerror=alert(1)>')`,context);if(escaped.includes('<'))throw Error('escape failed');
console.log('Frontend render smoke passed:',count,'view/status/role variants plus HTML escaping');

if(vm.runInContext('pct(9.999668)',context)!=='9.999668')throw Error('Below threshold rounded to 10');
if(vm.runInContext('pct(10.000332)',context)!=='10.000332')throw Error('Near threshold precision lost');
if(vm.runInContext('pct(10)',context)!=='10.00')throw Error('Exact threshold changed');
context.boundary=JSON.parse(JSON.stringify(context.detail));context.boundary.analysis.defect_rate_pct=9.999668;context.boundary.analysis.recall_eligible=false;context.boundary.analysis.confirmed_qty=15074;context.boundary.analysis.produced_qty=150745;context.boundary.status='investigating';context.boundary.recall_authorized=false;
const boundaryHTML=vm.runInContext('renderWorkflow(boundary)',context);if(!boundaryHTML.includes('9.999668')||!boundaryHTML.includes('기준 미달'))throw Error('Boundary explanation absent');
console.log('Threshold precision regression passed');
