"use strict";
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = x => String(x ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pretty = value => JSON.stringify(value, null, 2);
const labels = {entities:"实体",relations:"实体关系",attributes:"属性断言",rules:"代码规则",candidates:"候选项",conflicts:"属性冲突"};
const schemaLabels = {classes:"概念",relations:"关系类型",properties:"属性类型"};
const pageTitles = {graph:["从代码中看见业务结构","探索概念与关系，沿证据回到源码。"],records:["每一条知识，都有出处","逐项查看实例、属性与规则，保留候选和冲突供复核。"],compare:["比较实验，而非比较数量","并列查看同一源码快照下不同视图与提示词的结果。"],stages:["沿着产物追踪每个阶段","从源码快照到最终本体，查看阶段边界与处理范围。"],review:["让质量判断有据可查","查看人工评审记录，导出标签用于离线汇总。"]};
const S = {runs:[],run:null,detail:null,page:"graph",layer:new URLSearchParams(location.search).get("layer")==="instances"?"instances":"schema",kind:"entities",offset:0,query:"",epoch:0,detailEpoch:0,review:{}};
try { S.review = JSON.parse(localStorage.getItem("code-atlas-reviews-v1") || "{}"); } catch (_) { S.review = {}; }
function toast(text){ const node=$("#toast");node.textContent=text;node.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.hidden=true,5000); }
async function api(name,args={}) { const r=await fetch(`/api/${name}?${new URLSearchParams(args)}`);const d=await r.json();if(!r.ok)throw Error(d.error || `HTTP ${r.status}`);return d; }
function failure(e){toast(e.message);console.error(e);}
function button(text,action,cls="button"){const b=document.createElement("button");b.textContent=text;b.className=cls;b.addEventListener("click",()=>Promise.resolve(action()).catch(failure));return b;}
function download(name,text,mime="application/json") {const a=document.createElement("a");const url=URL.createObjectURL(new Blob([text],{type:mime}));a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);}
function metric(label,value,sub){return `<div class="metric"><div class="label">${esc(label)}</div><strong>${esc(value ?? "—")}</strong><small>${esc(sub)}</small></div>`;}
async function refresh(){
  const list=await api("runs"); S.runs=list.runs;
  $("#run-select").innerHTML=S.runs.map(r=>`<option value="${r.id}">${esc(r.fixture ? "[模拟测试] " : "")}${esc(r.experiment)} · ${esc(r.view.toUpperCase())} / ${esc(r.profile)} · ${esc(r.snapshot.slice(0,10))}</option>`).join("");
  if(!S.runs.length){$("#run-select").innerHTML='<option>尚未找到实验产物</option>';$("#page-content").innerHTML='<div class="panel empty">此输出目录下还没有实验。<br>先运行 README 中的 smoke 命令，或使用 --root 指定已有 outputs 目录。</div>';return;}
  const selected=S.runs.find(r=>r.id===S.run?.id)||S.runs[0];$("#run-select").value=selected.id;
  if(list.warnings.length)toast(`${list.warnings.length} 个目录暂时无法读取；运行中产物可稍后刷新。`);
  await selectRun(selected.id);
}
async function selectRun(id){
  const d=await api("run",{id});if($("#run-select").value!==id)return;
  S.detail=d;S.run=d.run;S.snapshot=null;S.offset=0;S.detailEpoch++;
  const m=d.run.metrics;$("#model-label").textContent=`${d.run.model} · ${d.source.file_count} 个源码文件`;
  const status=$("#run-status");status.textContent=d.run.fixture?"模拟接口 · 不能评估质量":`${d.run.view.toUpperCase()} / ${d.run.profile} · ${d.run.status}`;
  status.className="tag"+(d.run.fixture||d.run.status!=="success"?" warn":"");
  const notices=[];
  if(d.run.fixture)notices.push("当前是模拟模型接口结果，仅用于验证流程。请切换到真实模型实验评估业务抽取质量。");
  if(d.run.status!=="success")notices.push("本次运行尚未完整成功；已有文件可能来自部分阶段或更早的尝试，请查看阶段状态。");
  if(d.schema_review?.state)notices.push(`AI 审查：${d.schema_review.state}；实例 Schema 来源：${d.schema_scope?.review_state||"尚未记录"}。`);
  notices.push(`发现阶段选择 ${d.discovery.selected} / ${d.discovery.total ?? "?"} 个证据包；实例阶段准备 ${d.prepared.packs ?? "?"} 个包。数量与证据可定位率不代表语义正确率。`);
  $("#notice").textContent=notices.join(" ");$("#notice").hidden=false;
  $("#metrics").innerHTML=metric("业务概念",d.schema.classes.length,"固定 schema 中的类")+metric("关系类型",d.schema.relations.length,"概念间的关联定义")+metric("抽取实体",m.entities,"含代码 / 配置 / 示例范围")+metric("代码规则",m.rules,"条件与动作，待人工核验")+metric("候选 / 冲突",`${m.candidates ?? "—"} / ${m.conflicts ?? "—"}`,"保留歧义供后续复核")+metric("证据可定位率",m.evidence_locatable_rate==null?"—":`${Math.round(m.evidence_locatable_rate*100)}%`,"只表示引用可回查");
  await render();
}
async function render(){
  const epoch=++S.epoch;const titles=pageTitles[S.page];$("#page-title").textContent=titles[0];$("#page-subtitle").textContent=titles[1];
  $$(".nav").forEach(n=>n.classList.toggle("active",n.dataset.page===S.page));
  if(!S.detail)return;
  if(S.page==="graph")await graphPage(epoch);
  if(S.page==="records")await recordsPage(epoch);
  if(S.page==="compare")comparePage();
  if(S.page==="stages")stagesPage();
  if(S.page==="review")reviewPage();
}
function detailPanel(){return '<aside class="panel"><div class="panel-head"><div><h2>条目与源码证据</h2><small>点击图中节点、关系或表格行</small></div></div><div id="detail-body" class="detail-placeholder"><div class="detail-icon">⌁</div>选择一个条目<br>查看定义、引用与源码位置</div></aside>';}
function shownSchema(){return S.snapshot||S.detail;}
function schemaRecord(kind,row){const context=shownSchema();return {record_id:context.schema_record_ids[kind][row.id],payload:row,kind:"schema."+kind,schema:context.schema};}
function reviewKey(record){return `${S.run.id}/${record.record_id}`;}
function showDetail(record){
  S.detailEpoch++;const target=$("#detail-body");if(!target)return;target.className="detail-body";
  const row=record.payload;const kind=record.kind;const schema=record.schema||S.detail.schema;const names=Object.fromEntries([...schema.classes,...schema.relations,...schema.properties].map(r=>[r.id,r.name]));
  const name=row.name||names[row.property_id]||names[row.relation_id]||row.reason||"条目详情";
  target.innerHTML=`<div class="detail-name">${esc(name)}</div><div class="detail-meta">${esc(kind)}${row.scope?" · "+esc(row.scope):""}${row.basis?" · "+esc(row.basis):""}</div>${row.definition?`<p class="subsection">${esc(row.definition)}</p>`:""}${row.aliases?`<div class="subsection">${row.aliases.map(a=>`<span class="pill">${esc(a)}</span>`).join("")}</div>`:""}<div id="related-schema"></div><h3>完整条目</h3><pre class="json">${esc(pretty(row))}</pre><h3>源码证据</h3><div id="evidence-buttons"></div><div id="evidence-source"></div><h3>人工判断</h3><div id="review-form"></div>`;
  if(kind==="schema.classes"){
    const related=$("#related-schema");const properties=schema.properties.filter(p=>p.domain.includes(row.id));
    related.innerHTML='<h3>属性定义</h3>';
    if(!properties.length)related.append(document.createTextNode("当前概念没有直接声明的属性。"));
    properties.forEach(p=>related.append(button(`${p.name} · ${p.datatype}${p.unit?" / "+p.unit:""}`,()=>showDetail(schemaRecord("properties",p)),"evidence-button")));
    if(row.parents.length){const h=document.createElement("h3");h.textContent="父类";related.append(h);row.parents.forEach(id=>{const c=schema.classes.find(c=>c.id===id);if(c)related.append(button(c.name,()=>showDetail(schemaRecord("classes",c)),"pill"));});}
  }
  const evidence=row.evidence_ids||row.payload?.evidence_ids||[];
  if(!evidence.length)$("#evidence-buttons").innerHTML='<p class="muted">该候选 / 冲突本身未提供证据引用，可从相关正式条目继续追溯。</p>';
  evidence.forEach(id=>$("#evidence-buttons").append(button(id,()=>showEvidence(id),"evidence-button")));
  reviewForm(record);
}
async function showEvidence(eid){
  const epoch=++S.detailEpoch;const id=S.run.id;const d=await api("evidence",{id,evidence_id:eid});if(epoch!==S.detailEpoch||id!==S.run.id)return;
  const target=$("#evidence-source");if(!target)return;
  target.innerHTML=`<div class="source-file">${esc(d.ref.file)} : ${d.ref.start_line}–${d.ref.end_line}<br>${d.source_hash_matches?"源码指纹与证据一致":"⚠ 源码指纹不一致，请重新校验"}${d.truncated?" · 当前仅展示部分内容":""}</div><div class="source">${d.lines.map(l=>`<div class="source-line${l.highlight?" highlight":""}"><span class="line-number">${l.number}</span><span>${esc(l.text)}</span></div>`).join("")}</div>`;
}
function reviewForm(record){
  const old=S.review[reviewKey(record)]||{};const target=$("#review-form");
  const fields=[['verdict','整体判断',[['','待评审'],['correct','正确'],['partial','部分正确'],['incorrect','错误'],['uncertain','无法判定']]],['evidence_support','源码支持',[['','待评审'],['supported','充分支持'],['partial','部分支持'],['unsupported','不支持'],['uncertain','无法判定']]],['domain_relevant','业务相关',[['','待评审'],['yes','相关'],['no','无关'],['uncertain','无法判定']]],['scope_correct','对象范围',[['','待评审'],['yes','正确'],['no','错误'],['na','不适用'],['uncertain','无法判定']]],['rule_condition_complete','规则条件完整',[['','待评审'],['yes','完整'],['no','遗漏条件'],['na','不适用'],['uncertain','无法判定']]]];
  target.innerHTML=`<div class="review-grid">${fields.map(([key,label,options])=>`<label>${label}<select data-review="${key}">${options.map(([v,t])=>`<option value="${v}"${old[key]===v?' selected':''}>${t}</option>`).join("")}</select></label>`).join("")}</div><textarea id="review-notes" rows="3" placeholder="说明错误、缺失条件、重复或业务价值">${esc(old.notes||"")}</textarea><div class="review-actions" id="review-buttons"></div><p class="help-text">仅保存在本机浏览器，不写入流水线产物。请在「人工评审」页导出。</p>`;
  $("#review-buttons").append(button("保存判断",()=>{
    const value={record_id:record.record_id,kind:record.kind,payload:record.payload,run_id:S.run.id,run_path:S.run.path,snapshot:S.run.snapshot,notes:$("#review-notes").value,reviewed_at:new Date().toISOString()};
    $$('[data-review]',target).forEach(n=>value[n.dataset.review]=n.value);S.review[reviewKey(record)]=value;
    try{localStorage.setItem("code-atlas-reviews-v1",JSON.stringify(S.review));toast("评审已保存在本机浏览器");}catch(_){toast("浏览器存储不足，记录暂存内存，请立即导出评审");}
  },"button primary"));
}
async function graphPage(epoch){
  document.title=`${S.layer==="schema"?"Schema":"Instances"} · ${S.run.experiment} · Code Atlas`;
  $("#page-content").innerHTML=`<div class="graph-grid"><section class="panel"><div class="panel-head"><div><h2>${S.layer==="schema"?"本体结构 · Schema":"实例关系 · Instances"}</h2><small>结构视图与实例视图分别展示，点击条目追溯证据</small></div><button class="button" id="export-svg">导出 SVG</button></div><div class="toolbar"><button class="button ${S.layer==="schema"?'selected':''}" id="schema-layer">本体结构</button><button class="button ${S.layer==="instances"?'selected':''}" id="instance-layer">抽取实例</button><input id="graph-search" aria-label="搜索图谱" placeholder="搜索名称或定义"><button class="button" id="graph-find">筛选</button><button class="button" id="graph-reset">复位</button></div><div class="graph-wrap" id="graph-host"></div><div class="legend"><span><i></i>${S.layer==="schema"?"业务概念":"实体"}</span><span><i class="blue"></i>${S.layer==="schema"?"层级 / 业务关系":"实例关系"}</span><span>滚轮缩放 · 拖动平移</span></div><div class="graph-note" id="graph-note"></div></section>${detailPanel()}</div>`;
  $("#schema-layer").onclick=()=>{S.layer="schema";render().catch(failure);};$("#instance-layer").onclick=()=>{S.layer="instances";render().catch(failure);};
  if(S.layer==="schema"){
    const bar=document.createElement("div");bar.className="toolbar";
    const label=document.createElement("label");label.textContent="Schema 版本";label.htmlFor="schema-version";
    const select=document.createElement("select");select.id="schema-version";select.setAttribute("aria-label","选择 Schema 历史版本");select.style.maxWidth="85%";
    const current=document.createElement("option");current.value="";current.textContent="当前结果（实例抽取使用的 Schema）";select.append(current);
    for(const row of S.detail.schema_history||[]){const option=document.createElement("option");option.value=row.path;option.textContent=`${row.stage==="merge"?"合并":"审查"} · ${row.created_at.slice(0,19)} · ${row.location} · ${row.path.split("/").pop()}${row.state!=="accepted"?" · 未校验候选":""}`;select.append(option);}
    select.value=S.snapshot?.path||"";select.onchange=async()=>{try{const id=S.run.id,value=select.value;const data=value?await api("schema-snapshot",{id,path:value}):null;if(id!==S.run.id||select.value!==value)return;S.snapshot=data;await render();}catch(e){failure(e);}};
    bar.append(label,select);$("#graph-host").before(bar);
  }
  let entities=[],relations=[],instanceTotal=0,relationTotal=0;
  if(S.layer==="instances"){
    const [a,b]=await Promise.all([api("records",{id:S.run.id,kind:"entities",limit:1000}),api("records",{id:S.run.id,kind:"relations",limit:1000})]);
    if(epoch!==S.epoch)return;entities=a.rows;relations=b.rows;instanceTotal=a.total;relationTotal=b.total;
  }
  function draw(){
    const query=$("#graph-search").value.toLocaleLowerCase();let nodes=[],edges=[],total=0;
    if(S.layer==="schema"){
      const schema=shownSchema().schema;total=schema.classes.length;
      nodes=schema.classes.filter(c=>!query||`${c.name} ${c.aliases.join(" ")} ${c.definition}`.toLocaleLowerCase().includes(query)).slice(0,80).map(c=>({id:c.id,label:c.name,sub:`${schema.properties.filter(p=>p.domain.includes(c.id)).length} 个属性`,record:schemaRecord("classes",c)}));
      for(const c of schema.classes)for(const p of c.parents)edges.push({from:c.id,to:p,label:"子类 → 父类",inheritance:true,record:schemaRecord("classes",c)});
      for(const r of schema.relations)for(const d of r.domain)for(const t of r.range)if(edges.length<600)edges.push({from:d,to:t,label:r.name,record:schemaRecord("relations",r)});
    }else{
      total=instanceTotal;nodes=entities.filter(r=>!query||pretty(r.payload).toLocaleLowerCase().includes(query)).slice(0,80).map(r=>({id:r.payload.id,label:r.payload.name,sub:r.payload.scope,record:{...r,kind:"entities"}}));
      const nameMap=Object.fromEntries(S.detail.schema.relations.map(r=>[r.id,r.name]));edges=relations.map(r=>({from:r.payload.subject,to:r.payload.object,label:nameMap[r.payload.relation_id]||r.payload.relation_id,record:{...r,kind:"relations"}}));
    }
    const ids=new Set(nodes.map(n=>n.id));edges=edges.filter(e=>ids.has(e.from)&&ids.has(e.to)).slice(0,160);
    const omitted=S.layer==="instances"&&relationTotal>relations.length?`；关系输入限前 ${relations.length}/${relationTotal} 条`:"";
    $("#graph-note").textContent=`当前展示 ${nodes.length}/${total} 个节点、${edges.length} 条边。图中最多显示 80 个节点、160 条边${omitted}，筛选仅作用于已加载节点；实例输入最多加载前 1000 个实体。完整结果请在「知识断言」中检索。属性定义在概念详情中查看。`;
    drawGraph(nodes,edges);
  }
  $("#graph-find").onclick=draw;$("#graph-search").onkeydown=e=>{if(e.key==="Enter")draw();};$("#graph-reset").onclick=()=>{$("#graph-search").value="";draw();};
  $("#export-svg").onclick=()=>{const svg=$("#graph-host svg");if(svg)download(`ontology-${S.layer}.svg`,new XMLSerializer().serializeToString(svg),"image/svg+xml");};draw();
}
function drawGraph(nodes,edges){
  const host=$("#graph-host");host.replaceChildren();if(!nodes.length){host.innerHTML='<div class="empty">当前筛选没有可展示的节点。</div>';return;}
  const NS="http://www.w3.org/2000/svg";const make=(tag,attrs={},text)=>{const el=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));if(text!==undefined)el.textContent=text;return el;};
  const cols=Math.max(2,Math.ceil(Math.sqrt(nodes.length*1.5))),width=Math.max(900,cols*210),height=Math.max(570,Math.ceil(nodes.length/cols)*145+110);
  const svg=make("svg",{viewBox:`0 0 ${width} ${height}`,role:"img","aria-label":"业务本体关系图",xmlns:NS});
  svg.append(make("title",{},`${S.run.experiment} · ${S.layer} · 当前筛选子图，${nodes.length} 节点 / ${edges.length} 边`));
  const defs=make("defs"),marker=make("marker",{id:"arrow",viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:6,markerHeight:6,orient:"auto-start-reverse"});marker.append(make("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#7b9f98"}));defs.append(marker);svg.append(defs);
  const positions=new Map();nodes.forEach((n,i)=>positions.set(n.id,{x:100+(i%cols)*(width-200)/Math.max(cols-1,1),y:80+Math.floor(i/cols)*145}));
  const lanes=new Map();for(const e of edges){const a=positions.get(e.from),b=positions.get(e.to);const key=[e.from,e.to].sort().join("/");const lane=lanes.get(key)||0;lanes.set(key,lane+1);let d,tx,ty;
    if(e.from===e.to){d=`M ${a.x-45} ${a.y-15} C ${a.x-100} ${a.y-100}, ${a.x+100} ${a.y-100}, ${a.x+45} ${a.y-15}`;tx=a.x;ty=a.y-67;}else{const bend=(lane%2===0?1:-1)*(18+Math.floor(lane/2)*19);const midX=(a.x+b.x)/2,midY=(a.y+b.y)/2;const length=Math.hypot(b.x-a.x,b.y-a.y)||1;const px=-(b.y-a.y)/length,py=(b.x-a.x)/length;const endX=b.x-(b.x-a.x)/length*67,endY=b.y-(b.y-a.y)/length*29;d=`M ${a.x} ${a.y} Q ${midX+px*bend} ${midY+py*bend} ${endX} ${endY}`;tx=midX+px*bend/2;ty=midY+py*bend/2-5;}
    const g=make("g",{class:"graph-edge",tabindex:0,role:"button","aria-label":e.label});g.append(make("path",{d,fill:"none",stroke:e.inheritance?"#acc0cf":"#91b3aa","stroke-width":1.2,"stroke-dasharray":e.inheritance?"5 4":"none","marker-end":"url(#arrow)"}));g.append(make("text",{x:tx,y:ty,"text-anchor":"middle","font-family":"Segoe UI, Microsoft YaHei, sans-serif","font-size":10,fill:"#64827a",stroke:"#fbfcfb","stroke-width":4,"paint-order":"stroke"},e.label.length>12?e.label.slice(0,12)+"…":e.label));g.append(make("title",{},e.label));g.onclick=()=>showDetail(e.record);g.onkeydown=ev=>{if(ev.key==="Enter")showDetail(e.record);};svg.append(g);
  }
  for(const n of nodes){const p=positions.get(n.id);const g=make("g",{transform:`translate(${p.x},${p.y})`,class:"graph-node",tabindex:0,role:"button","aria-label":n.label});g.append(make("rect",{x:-73,y:-27,width:146,height:54,rx:9,fill:S.layer==="schema"?"#e9f4ed":"#edf3f8",stroke:S.layer==="schema"?"#86b7a0":"#94b2c6","stroke-width":1}));g.append(make("text",{x:0,y:-3,"text-anchor":"middle","font-family":"Segoe UI, Microsoft YaHei, sans-serif","font-size":12,fill:"#254f41"},n.label.length>13?n.label.slice(0,13)+"…":n.label));g.append(make("text",{x:0,y:15,"text-anchor":"middle","font-family":"Segoe UI, Microsoft YaHei, sans-serif","font-size":9,fill:"#7c9588"},n.sub));g.append(make("title",{},n.label));g.onclick=()=>showDetail(n.record);g.onkeydown=e=>{if(e.key==="Enter")showDetail(n.record);};svg.append(g);}
  let view={x:0,y:0,w:width,h:height},drag=null;
  const apply=()=>svg.setAttribute("viewBox",`${view.x} ${view.y} ${view.w} ${view.h}`);
  svg.addEventListener("wheel",e=>{e.preventDefault();const scale=e.deltaY>0?1.12:.89;if(view.w*scale<200||view.w*scale>width*5)return;const box=svg.getBoundingClientRect();const x=(e.clientX-box.left)/box.width,y=(e.clientY-box.top)/box.height;view.x+=view.w*x*(1-scale);view.y+=view.h*y*(1-scale);view.w*=scale;view.h*=scale;apply();},{passive:false});
  svg.onpointerdown=e=>{if(e.target.closest(".graph-node,.graph-edge"))return;drag={x:e.clientX,y:e.clientY,v:{...view}};svg.setPointerCapture(e.pointerId);};svg.onpointermove=e=>{if(!drag)return;const b=svg.getBoundingClientRect();view.x=drag.v.x-(e.clientX-drag.x)*view.w/b.width;view.y=drag.v.y-(e.clientY-drag.y)*view.h/b.height;apply();};svg.onpointerup=()=>drag=null;svg.onpointercancel=()=>drag=null;host.append(svg);
}
function summaryRow(row,kind){
  const names=Object.fromEntries([...S.detail.schema.classes,...S.detail.schema.relations,...S.detail.schema.properties].map(c=>[c.id,c.name]));
  if(kind==="entities")return [row.name,names[row.type_id]||row.type_id,row.scope];
  if(kind==="relations")return [names[row.relation_id]||row.relation_id,`${row.subject} → ${row.object}`,row.basis];
  if(kind==="attributes")return [names[row.property_id]||row.property_id,`${pretty(row.value)} ${row.unit||""}`,row.subject];
  if(kind==="rules")return [row.name,row.condition,row.action];
  if(kind==="candidates")return [row.reason,pretty(row.payload),"需复核"];
  return [names[row.property_id]||row.property_id,pretty(row.values),row.status];
}
async function recordsPage(epoch){
  $("#page-content").innerHTML=`<div class="graph-grid"><section class="panel"><div class="panel-head"><div><h2>知识断言清单</h2><small>每页 30 条，搜索覆盖本集合的完整文件</small></div></div><div class="toolbar"><select id="kind-select" aria-label="断言类型">${Object.entries(labels).map(([k,v])=>`<option value="${k}"${S.kind===k?' selected':''}>${v}</option>`).join("")}</select><input id="record-search" aria-label="搜索断言" placeholder="搜索名称、条件、类型或证据 ID" value="${esc(S.query)}"><button class="button" id="record-find">搜索</button></div><div id="records-table" class="table-wrap"></div><div class="pagination"><span id="record-total"></span><div id="record-pages"></div></div></section>${detailPanel()}</div>`;
  $("#kind-select").onchange=e=>{S.kind=e.target.value;S.offset=0;render().catch(failure);};
  const search=()=>{S.query=$("#record-search").value;S.offset=0;render().catch(failure);};$("#record-find").onclick=search;$("#record-search").onkeydown=e=>{if(e.key==="Enter")search();};
  const result=await api("records",{id:S.run.id,kind:S.kind,offset:S.offset,limit:30,q:S.query});if(epoch!==S.epoch)return;
  $("#records-table").innerHTML=result.rows.length?`<table><thead><tr><th>名称 / 类型</th><th>内容 / 条件</th><th>范围 / 动作</th><th>证据</th></tr></thead><tbody>${result.rows.map((r,i)=>`<tr class="record-row" data-row="${i}" tabindex="0">${summaryRow(r.payload,S.kind).map(v=>`<td>${esc(v.length>160?v.slice(0,160)+"…":v)}</td>`).join("")}<td>${r.payload.evidence_ids?.length??"—"}</td></tr>`).join("")}</tbody></table>`:'<div class="empty">当前集合没有匹配条目。</div>';
  $$("[data-row]").forEach(n=>{const fn=()=>showDetail({...result.rows[Number(n.dataset.row)],kind:S.kind});n.onclick=fn;n.onkeydown=e=>{if(e.key==="Enter")fn();};});
  $("#record-total").textContent=`共 ${result.total} 条 · ${result.total?S.offset+1:0}–${Math.min(result.total,S.offset+30)}`;
  const prev=button("上一页",()=>{S.offset=Math.max(0,S.offset-30);return render();});prev.disabled=S.offset===0;const next=button("下一页",()=>{S.offset+=30;return render();});next.disabled=S.offset+30>=result.total;$("#record-pages").append(prev,next);
}
function comparePage(){
  $("#page-content").innerHTML=`<section class="panel"><div class="panel-head"><div><h2>实验组合与运行指标</h2><small>比较时固定源码范围、模型、预算与评审口径</small></div></div><div class="compare-filters"><label><input type="checkbox" id="same-snapshot" checked> 同一源码快照</label><label><input type="checkbox" id="include-fixture"> 包含模拟接口测试</label><span class="muted">点击实验名称切换</span></div><div id="compare-table" class="table-wrap"></div><div class="compare-note">概念数量、token 与耗时只反映规模和成本。质量需联合概念精确率 / 召回率、证据真实支持率、规则条件完整率和业务问题覆盖率。不同 schema 的结果不能仅按条数排名。</div></section>`;
  function draw(){const runs=S.runs.filter(r=>(!$("#same-snapshot").checked||r.snapshot===S.run.snapshot)&&($("#include-fixture").checked||!r.fixture));
    $("#compare-table").innerHTML=`<table><thead><tr><th>实验</th><th>视图 / Profile</th><th>模型 / 状态</th><th>概念</th><th>实体</th><th>规则</th><th>候选 / 冲突</th><th>耗时</th><th>请求 / 缓存</th><th>语义精确率</th></tr></thead><tbody>${runs.map(r=>{const m=r.metrics;return `<tr><td><button class="table-link" data-run="${r.id}">${esc(r.experiment)}${r.fixture?" [模拟]":""}</button></td><td>${esc(r.view)} / ${esc(r.profile)}</td><td>${esc(r.model)}<br>${esc(r.status)}</td><td>${m.schema_classes??"—"}</td><td>${m.entities??"—"}</td><td>${m.rules??"—"}</td><td>${m.candidates??"—"} / ${m.conflicts??"—"}</td><td>${m.elapsed_seconds??"—"} s</td><td>${m.llm?.requests??"—"} / ${m.llm?.cache_hits??"—"}</td><td>待人工评审</td></tr>`;}).join("")}</tbody></table>`;
    $$('[data-run]').forEach(b=>b.onclick=()=>{$("#run-select").value=b.dataset.run;selectRun(b.dataset.run).catch(failure);});}
  $("#same-snapshot").onchange=draw;$("#include-fixture").onchange=draw;draw();
}
function stagesPage(){
  const view=S.run.view;const base=`runs/${S.run.experiment}/${view}/${S.run.profile}`;
  const rows=[['源码快照','仓库文件 + include/exclude + 解析参数','manifest.json；source/','manifest','固定内容与编码，检查纳入范围与排除原因。'],['Joern 与图规范化','固定源码 + 编译信息','raw/cpg.bin；normalized/'+view+'/','normalized','一次解析后生成 AST / CFG / 综合 CPG，检查节点与边引用。'],['证据包准备','规范图 + 源码 + 分块预算','prepared/'+view+'/packs.jsonl','prepared','按函数 / 类型组织模型输入，记录上下文裁剪与跳过的符号。'],['局部 schema 发现','显式选择的证据包 + 领域提示词',base+'/schema/local/','selection','未选中的包不代表没有业务概念，采样覆盖必须明确。'],['Schema 合并草稿','局部候选 + 支持证据',base+'/schema/draft-schema.json；schema/history/','provenance','先保存合法草稿、来源映射与每轮历史；语义差异交给独立审查。'],['AI 审查并修正 Schema','合并草稿 + 来源映射 + 源码证据',base+'/schema/review-report.json；schema/schema.json','schema_review','分批审查和定向修正，保留意见与各轮候选；AI 审查不等于人工确认。'],['实例与规则抽取','固定 schema + 单批证据包',base+'/extraction/','extraction_selection','逐任务抽取实体、关系、属性与条件动作，记录省略的 schema 项。'],['校验与最终产物','Schema + 抽取批次 + 源码证据',base+'/final/','metrics','正式断言、候选、冲突分别落盘；数量不代表质量。'],['证据位置校验','源码快照 + 正式输出',base+'/validation.json','validation','需要独立执行 validate；验证引用和位置，不验证业务语义。'],['概念别名评估','人工概念参考答案 + schema',base+'/evaluation.json','evaluation','需要独立执行 evaluate；仅为概念召回导航。']];
  $("#page-content").innerHTML=`<div class="graph-grid"><section class="panel"><div class="panel-head"><div><h2>阶段输入与输出</h2><small>只读取已存在的文件，未执行阶段显示为空</small></div></div><div class="stage-list">${rows.map((r,i)=>`<article class="stage"><div class="stage-number">${String(i+1).padStart(2,"0")}</div><div><strong>${r[0]}</strong><p>输入：${esc(r[1])}</p><code>输出：${esc(r[2])}</code><p>${esc(r[4])}</p></div><button class="button" data-artifact="${r[3]}">查看产物</button></article>`).join("")}</div></section><aside class="panel"><div class="panel-head"><h2>产物摘要</h2></div><div class="panel-body" id="artifact-detail"><p>从左侧选择一个阶段。</p><h3>运行状态</h3><pre class="json">${esc(pretty(S.detail.stages))}</pre></div></aside></div>`;
  $$('[data-artifact]').forEach(b=>b.onclick=async()=>{try{const id=S.run.id;const d=await api("artifact",{id,name:b.dataset.artifact});if(id!==S.run.id)return;$("#artifact-detail").innerHTML=`<div class="source-file">${esc(d.path)}</div><pre class="json">${esc(pretty(d.data))}</pre>${d.data===null?'<p>尚未生成此产物，请按运行文档执行对应命令。</p>':''}`;}catch(e){failure(e);}});
}
function reviewPage(){
  const records=Object.values(S.review).filter(r=>r.run_id===S.run.id);const count=key=>records.filter(r=>r.verdict===key).length;
  $("#page-content").innerHTML=`<section class="panel"><div class="panel-head"><div><h2>当前实验 · 人工评审</h2><small>浏览器本地保存，导出文件后可使用 review summarize 汇总</small></div><button class="button primary" id="export-review">导出评审 JSON</button></div><div class="panel-body"><div class="review-note">已记录 ${records.length} 条：正确 ${count("correct")} · 部分正确 ${count("partial")} · 错误 ${count("incorrect")} · 无法判定 ${count("uncertain")}。<br>交互选择属于非随机样本，不能直接推断整个仓库的精确率。正式实验建议用 review sample 生成分层抽样 CSV；详见 docs/测试与评审.md。</div><div class="table-wrap"><table><thead><tr><th>集合</th><th>条目</th><th>整体判断</th><th>源码支持</th><th>备注</th></tr></thead><tbody>${records.map((r,i)=>`<tr class="record-row" data-review-row="${i}"><td>${esc(r.kind)}</td><td>${esc(r.payload.name||r.payload.reason||r.record_id)}</td><td>${esc(r.verdict||"待评审")}</td><td>${esc(r.evidence_support||"待评审")}</td><td>${esc(r.notes)}</td></tr>`).join("")}</tbody></table>${records.length?'':'<div class="empty">在图谱或知识断言页选择条目，填写并保存人工判断。</div>'}</div></div></section><div id="review-inspect" class="subsection"></div>`;
  $("#export-review").onclick=()=>download(`reviews-${S.run.experiment}-${S.run.view}-${S.run.profile}.json`,pretty({format_version:1,sampling_method:"interactive_nonrandom",run_id:S.run.id,run_path:S.run.path,snapshot:S.run.snapshot,exported_at:new Date().toISOString(),annotations:records}));
  $$('[data-review-row]').forEach(b=>b.onclick=()=>{$("#review-inspect").innerHTML=detailPanel();showDetail(records[Number(b.dataset.reviewRow)]);});
}
$("#refresh").onclick=()=>refresh().catch(failure);$("#run-select").onchange=e=>selectRun(e.target.value).catch(failure);
$$('.nav').forEach(n=>n.onclick=()=>{S.page=n.dataset.page;render().catch(failure);});refresh().catch(e=>{$("#page-content").innerHTML=`<div class="panel empty error">${esc(e.message)}<br>请检查终端服务状态，然后刷新产物。</div>`;failure(e);});
