// Run the shipped browser scripts with a small DOM and no network/model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function browser(file, search='') {
  const nodes = new Map(), messages=[];
  const element=()=>({value:'',hidden:false,textContent:'',innerHTML:'',dataset:{},
    classList:{add(){},toggle(){}},setAttribute(k,v){this[k]=v;},
    getAttribute(k){return this[k]??null;},removeAttribute(k){delete this[k];},
    append(){},replaceChildren(){},before(){},after(){},addEventListener(){},querySelectorAll(){return [];}});
  const document={querySelector(s){if(!nodes.has(s))nodes.set(s,element());return nodes.get(s);},
    querySelectorAll(){return [];},createElement:element,body:element()};
  const window={parent:{postMessage(data,origin){messages.push({data,origin});}},addEventListener(){}};
  const context=vm.createContext({document,window,location:{search,origin:'http://localhost'},
    URLSearchParams,URL,Map,Set,console,localStorage:{getItem(){return null;}},
    setTimeout(){},clearTimeout(){},setInterval(){},fetch(){return new Promise(()=>{});}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../code_ontology/resources',file,'app.js'),'utf8'),context);
  return {context,nodes,messages,run:code=>vm.runInContext(code,context)};
}
(async()=>{
  const child=browser('viewer','?embedded=1&mode=schema');
  child.run(`let renders=0;render=async()=>{renders++;};rememberPage=()=>{};selectGraphLayer('instances');`);
  assert.equal(child.messages[0].data.layer,'instances');
  assert.equal(child.run('renders'),0,'schema mode must wait for final data');

  const parent=browser('workbench');
  parent.run(`S.wid='workspace';S.ontology={run:{id:'selected-run'},detail:{}};
    $('#ontology-mode').value='schema';let requests=[];
    ontologyApi=async(name,args)=>{requests.push({name,args,mode:$('#ontology-mode').value});
      if(name==='runs')return {runs:[{id:'other',experiment:'other',view:'ast'},{id:'selected-run',experiment:'selected',view:'ast'}]};
      return {run:{id:args.id,metrics:{}},schema:{classes:[],relations:[],properties:[]}};};
    loadOntologyRows=async()=>{};`);
  // Reject unrelated windows, origins and invalid layers.
  for(const event of [
    `{origin:'http://evil',source:$('#ontology-graph').contentWindow,data:{type:'code-atlas-layer',layer:'instances'}}`,
    `{origin:location.origin,source:{},data:{type:'code-atlas-layer',layer:'instances'}}`,
    `{origin:location.origin,source:$('#ontology-graph').contentWindow,data:{type:'code-atlas-layer',layer:'bad'}}`
  ])await parent.run(`handleOntologyMessage(${event})`);
  assert.equal(parent.run('requests.length'),0);
  parent.run(`$('#ontology-graph').contentWindow={};`);
  await parent.run(`handleOntologyMessage({origin:location.origin,source:$('#ontology-graph').contentWindow,data:{type:'code-atlas-layer',layer:'instances'}})`);
  assert.equal(parent.run(`$('#ontology-mode').value`),'final');
  assert.equal(parent.run('S.ontology.run.id'),'selected-run');
  const src=new URL(parent.run(`$('#ontology-graph').src`),'http://localhost');
  assert.equal(src.searchParams.get('layer'),'instances');
  assert.equal(src.searchParams.get('mode'),'final');
  assert.equal(src.searchParams.get('run'),'selected-run');
  assert.equal(parent.run(`requests.every(r=>r.mode==='final')`),true);

  // The selected intermediate fragment drives both the native list and iframe.
  parent.run(`$('#ontology-mode').value='schema';
    S.ontology.baseDetail={run:{id:'selected-run',metrics:{}},schema:{classes:[],relations:[],properties:[]}};
    $('#ontology-schema').value='artifact:local/first.json';
    ontologyApi=async(name,args)=>({schema:{classes:[{id:'local',name:'Local'}],relations:[],properties:[]},
      schema_source:{location:'local/first.json'},path:args.path});`);
  await parent.run('selectOntologySchema()');
  assert.equal(parent.run('S.ontology.detail.schema.classes[0].id'),'local');
  const intermediateSrc=new URL(parent.run(`$('#ontology-graph').src`),'http://localhost');
  assert.equal(intermediateSrc.searchParams.get('schema'),'artifact:local/first.json');
  assert.equal(intermediateSrc.searchParams.get('mode'),'schema');
  parent.run(`$('#ontology-schema').value='';`);
  await parent.run('selectOntologySchema()');
  assert.equal(parent.run('S.ontology.detail.schema.classes.length'),0);

  // Final/standalone layer switches still render locally.
  for(const search of ['?embedded=1&mode=final','']){
    const view=browser('viewer',search);
    view.run(`let renders=0;render=async()=>{renders++;};rememberPage=()=>{};selectGraphLayer('instances');`);
    assert.equal(view.run('renders'),1);
    assert.equal(view.run('S.layer'),'instances');
  }
  // Exercise graph data conversion and empty-result guidance.
  const graph=browser('viewer','?embedded=1&mode=final&layer=instances');
  graph.run(`S.run={id:'selected-run'};S.detail={schema:{relations:[{id:'r',name:'关联'}]}};
    detailPanel=()=>'';let drawn;drawGraph=(nodes,edges)=>{drawn={nodes,edges};};
    api=async(name,args)=>({total:args.kind==='entities'?2:1,rows:args.kind==='entities'
      ?[{payload:{id:'a',name:'A'}},{payload:{id:'b',name:'B'}}]
      :[{payload:{subject:'a',object:'b',relation_id:'r'}}]});`);
  await graph.run('graphPage(S.epoch)');
  assert.equal(graph.run('drawn.nodes.length'),2);
  assert.equal(graph.run('drawn.edges[0].label'),'关联');
  graph.run('api=async()=>({total:0,rows:[]});');
  await graph.run('graphPage(S.epoch)');
  assert.match(graph.run(`$('#graph-note').textContent`),/归并实例产物/);
  // Stage stop buttons bind the active task, regardless of the selected history task.
  const stages=browser('workbench');
  stages.run(`renderDetail=()=>{};renderJob=()=>{};renderPrompt=()=>{};
    $('#settings-form').elements={namedItem:()=>({options:[]})};
    S.data={workspace:{view:'ast',profile:'maintenance',snapshot:''},policy:{},settings:{},roots:{},
      jobs:[{id:'live-merge',stage:'merge-schema',state:'running'}],
      stages:[{id:'discover-local',label:'Local',blocked:[]},{id:'merge-schema',label:'Merge',blocked:[]}]};
    S.job='unrelated-history';let stops=[];let cancelled=[];
    document.querySelectorAll=selector=>selector==='[data-stop]'?stops:[];
    api=async(name,args)=>{cancelled.push({name,args});};poll=async()=>{};
    stops=[{dataset:{stop:'live-merge'},disabled:false}];render();`);
  let html=stages.run(`$('#stages').innerHTML`);
  assert.match(html,/data-stop="" disabled/);
  assert.match(html,/data-stop="live-merge" >/);
  await stages.run(`stops[0].onclick({stopPropagation(){}})`);
  assert.equal(stages.run('cancelled[0].args.id'),'live-merge');
  assert.equal(stages.run('stops[0].disabled'),true);
  stages.run(`S.data.jobs[0].state='cancelling';render();`);
  assert.match(stages.run(`$('#stages').innerHTML`),/data-stop="live-merge" disabled>停止中/);
  stages.run(`S.data.jobs[0].state='succeeded';render();`);
  assert.doesNotMatch(stages.run(`$('#stages').innerHTML`),/data-stop="live-merge"/);
  // Drafts survive polling/stage switches and block execution until saved.
  const configPage=browser('workbench');
  configPage.run(`S.wid='workspace';S.stage='merge-schema';
    S.data={workspace:{env_file:'shared.env',config:'base.yaml'},jobs:[],stages:[],step_configurations:{
      'merge-schema':{revision:'r1',file:'merge.yaml',overrides:{policy:{},llm:{}},yaml:'policy: {}',
        connection:{model:'fixture',base_url:'http://localhost'},fields:[
          {section:'llm',key:'timeout',type:'number',label:'Timeout',help:'Wait',inherited:180,inherited_source:'shared.env'}]}}};
    renderStepConfiguration(true);`);
  assert.match(configPage.run(`$('#stage-config').innerHTML`),/shared.env/);
  configPage.run(`let draft=stepDrafts.get('workspace:merge-schema');draft.values.llm.timeout='777';draft.dirty=true;
    renderStepConfiguration(true);`);
  assert.match(configPage.run(`$('#stage-config').innerHTML`),/value="777"/);
  assert.throws(()=>configPage.run('assertSavedStepConfigurations()'),/尚未保存/);
  configPage.run(`render=()=>{};api=async(name,args)=>{if(name==='step-configuration'){window.saved=args;return S.data;}return S.data;};`);
  await configPage.run(`$('#save-step-config').onclick()`);
  assert.equal(configPage.run('window.saved.values.llm.timeout'),777);
  assert.equal(configPage.run('window.saved.revision'),'r1');
  assert.doesNotThrow(()=>configPage.run('assertSavedStepConfigurations()'));
  console.log('Graph navigation, configuration and rendering checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
