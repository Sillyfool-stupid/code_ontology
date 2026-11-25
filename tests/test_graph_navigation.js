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
    append(){},before(){},addEventListener(){},querySelectorAll(){return [];}});
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
  console.log('Graph navigation and rendering checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
