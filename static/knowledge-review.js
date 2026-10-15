"use strict";

window.KnowledgeReviewView=(()=>{
  let active=null;
  const endpoint='/knowledge-workbench';
  const status={pending:'待检查',checked:'检查通过，待人工预审',approved:'人工预审通过，未发布',rejected:'已驳回',withdrawn:'已撤回',invalidated:'已失效，需重新提交'};
  const reasons={candidate_changed:'候选已变化',answer_changed:'原回答已变化',source_changed:'来源已变化',corpus_changed:'语料或检查规则已变化',gate_changed:'检查结果已变化',reviewer_changed:'审核权限已变化',owner_withdrew:'本人撤回',source_problem:'出处需要核实',needs_context:'摘录缺少上下文',not_relevant:'不适合作为知识',manual_pre_review:'已完成人工预审'};
  const checks={public_provenance:'公共出处',literal_excerpt:'连续摘录',content_hash:'内容版本',source_lookup:'来源可读取',lexical_source_smoke:'词法检索可读取'};
  const live=s=>active===s&&state.user===s.user&&s.root.isConnected;
  const q=(s,selector)=>s.root.querySelector(selector);
  const esc=value=>escapeHtml(String(value??''));
  const releaseStatuses={registered:'已登记，待发布准备检查',blocked:'发布条件未满足',withdrawn:'登记已撤回',invalidated:'登记已失效'};
  const releaseReasons={registered:'版本已绑定',quality_acceptance_missing:'缺少独立质量验收',quality_passed:'质量检查通过',approver_changed:'批准人权限变化',quality_or_approval_changed:'质量凭据或批准变化',reviewer_withdrew:'审核人撤回',submission_changed:'原提交或预审变化',submission_deleted:'关联资料已删除',registrant_changed:'登记人权限变化',package_changed:'构建文件变化',sources_changed:'公共来源变化',protocol_or_date_changed:'检查协议或日期变化'};
  const pendingChecks={independent_semantic_quality:'独立语义质量',hybrid_ranking_quality:'混合排名质量',activation_and_rollback:'在线切换与回滚接线'};
  function dispose(){active?.controller?.abort();active=null;}
  function message(s,text){q(s,'.review-status').textContent=text;}
  function busy(s,value){s.busy=value;s.root.querySelectorAll('button,input,select').forEach(node=>node.disabled=value);}
  function source(item){return `<details><summary>核对完整公共摘要及范围</summary><p>${esc(item.source.publisher)} · ${esc(item.source.source_version)}</p><p class="workbench-text">${esc(item.source.scope)}</p><p class="workbench-text">${esc(item.source.excerpt)}</p><p>${esc(item.source.url)}</p><p class="small">${esc(item.source.license)} · ${esc(item.source.license_url)}</p></details>`;}
  function card(item,reviewer){
    const open=['pending','checked','approved'].includes(item.status);
    const report=item.gate?.report;
    return `<article class="workbench-item" data-submission="${esc(item.id)}"><strong>${esc(item.source.title)}</strong><p class="workbench-text">${esc(item.content)}</p><p class="small muted">${esc(status[item.status])} · 候选版本 ${item.candidate_version} · 审核版本 ${item.version}${item.source_unavailable?' · 来源暂不可读':''}</p><p class="small">${esc(reasons[item.reason])}</p>${source(item)}${report?`<p class="small">${Object.entries(report.checks).map(([key,value])=>`${esc(checks[key]||key)}：${value?'通过':'未通过'}`).join('；')}</p><p class="small muted">仅为离线工程检查，真实语义质量未评测，发布回归未运行。</p>`:''}${reviewer&&['pending','checked'].includes(item.status)?`<div class="workbench-actions"><button type="button" data-review-check="${esc(item.id)}">运行离线检查</button></div>${item.status==='checked'&&item.gate?.passed?`<label class="candidate-confirm"><input type="checkbox" data-review-confirm="${esc(item.id)}">已人工核对完整出处、上下文与适用范围</label><button type="button" data-review-approve="${esc(item.id)}">通过人工预审</button>`:''}<label>驳回原因<select data-review-reason="${esc(item.id)}"><option value="needs_context">摘录缺少上下文</option><option value="source_problem">出处需要核实</option><option value="not_relevant">不适合作为知识</option></select></label><button type="button" data-review-reject="${esc(item.id)}">驳回这版提交</button>`:''}${!reviewer&&open?`<button type="button" data-review-withdraw="${esc(item.id)}">撤回提交</button>`:''}<details><summary>审核变更记录</summary>${item.events.map(event=>`<p class="small">v${event.version} · ${esc(status[event.action]||reasons[event.action]||({submitted:'已提交',checked:'检查通过',check_failed:'检查未通过'}[event.action])||event.action)}</p>`).join('')}</details></article>`;
  }
  function releaseCard(item){
    const open=['registered','blocked'].includes(item.status),approved=item.approval?.state==='approved';
    const title=item.published?'当前在线版本':approved?'已批准，待切换':item.can_approve?'质量检查通过，待人工批准':releaseStatuses[item.status]||item.status;
    const pending=item.report?.blockers||[];
    return `<article class="workbench-item"><strong>${esc(title)}</strong><p class="small muted">${esc(item.id)} · 登记版本 ${item.version}</p><p>${esc(releaseReasons[item.reason]||item.reason)}</p>${item.source_unavailable?'<p>公共来源暂不可核对，请稍后刷新。</p>':''}${item.report?.policy?`<p>${pending.length?'仍缺：'+pending.map(key=>esc(pendingChecks[key]||key)).join('、'):'固定协议的检索质量检查已通过，仍需人工核对完整出处。'}</p><p class="small muted">检查复用公开问题编码，没有调用云模型；检索质量不等于回答准确率。</p>`:''}${open&&!item.approval?`<button type="button" data-release-check="${esc(item.id)}">检查发布准备情况</button>`:''}${item.can_approve?`<label class="candidate-confirm"><input type="checkbox" data-publication-approve-confirm="${esc(item.id)}">已核对完整出处、适用范围及这份质量报告</label><button type="button" data-publication-approve="${esc(item.id)}">批准这个版本</button>`:''}${approved&&!item.published?`<label class="candidate-confirm"><input type="checkbox" data-publication-switch-confirm="${esc(item.id)}">确认将问答切换到这个版本</label><button type="button" data-publication-switch="${esc(item.id)}">切换到这个版本</button>`:''}${open?`<label class="candidate-confirm"><input type="checkbox" data-release-withdraw-confirm="${esc(item.id)}">确认撤回这个登记，撤回后不能重新启用</label><button type="button" data-release-withdraw="${esc(item.id)}">撤回此登记</button>`:''}<details><summary>版本变更记录</summary>${item.events.map(event=>`<p class="small">v${event.version} · ${esc(releaseReasons[event.action]||event.action)}</p>`).join('')}</details></article>`;
  }
  function render(s,data){
    s.items=data.items;s.queue=data.queue;s.builds=data.builds;s.releases=data.releases||[];s.live=data.live;
    q(s,'.review-permission').textContent=data.permissions.reviewer?'当前账号具有公共摘录审核权限。':'当前账号可提交本人摘录，没有公共审核权限。';
    q(s,'.review-candidates').innerHTML=s.candidates.filter(item=>item.status==='draft'&&!item.source_stale&&!item.origin_stale&&!s.items.some(entry=>entry.candidate_id===item.id&&['pending','checked','approved'].includes(entry.status))).map(item=>`<article class="workbench-item"><strong>${esc(item.source.title)} · v${item.version}</strong><p class="workbench-text">${esc(item.content)}</p><label class="candidate-confirm"><input type="checkbox" data-review-share="${esc(item.id)}">同意将此公共摘录及出处提交给审核人，不包含私人反馈或对话</label><button type="button" data-review-submit="${esc(item.id)}">提交公共摘录供预审</button></article>`).join('');
    q(s,'.review-own').innerHTML=s.items.length?s.items.map(item=>card(item,false)).join(''):'<p class="small muted">尚未提交公共摘录。</p>';
    q(s,'.review-queue-section').hidden=!data.permissions.reviewer;
    q(s,'.review-queue').innerHTML=s.queue.length?s.queue.map(item=>card(item,true)).join(''):'<p class="small muted">暂无待查看提交。</p>';
    q(s,'.build-selection').innerHTML=s.queue.filter(item=>item.status==='approved'&&!item.source_unavailable).map(item=>`<label class="candidate-confirm"><input type="checkbox" data-build-select="${esc(item.id)}">${esc(item.source.title)} · 审核版本 ${item.version}</label>`).join('')||'<p class="small muted">先选择有效提交并完成人工预审。</p>';
    q(s,'.candidate-build-confirmed').checked=false;
    q(s,'.knowledge-build-list').innerHTML=data.builds.length?data.builds.map(item=>`<article class="workbench-item"><strong>${item.status==='invalid_package'?'构建文件校验失败':'已构建，未发布'}</strong><p class="small muted">${esc(item.id)}</p>${item.status==='invalid_package'?'<p>请在本机检查文件；未接入在线知识库。</p>':`<p>公共知识块 ${item.chunk_count} · 新摘录 ${item.new_chunks} · 重复摘要 ${item.deduplicated}</p><p>开发题词法回归：${item.regression.comparable} 条可比较，${item.regression.not_comparable} 条证据不可比较，${item.regression.regressions} 条退化。</p><p class="small muted">${item.currently_stale?'内容、出处或预审已变化，不能继续发布。':'文件完整，仍待独立语义、混合排名及发布/回滚验收。'}${item.source_unavailable?' 来源暂不可读。':''}</p>`}</article>`).join(''):'<p class="small muted">暂无候选知识版本。</p>';
    q(s,'.knowledge-live-status').textContent=(s.live.release_id?'当前版本 '+s.live.release_id:'当前使用原始审核知识库')+' · 切换序号 '+s.live.revision;
    q(s,'.knowledge-rollback-target').innerHTML='<option value="">原始审核知识库</option>'+s.releases.filter(item=>item.approval?.state==='approved'&&s.live.events.some(event=>event.release_id===item.id)).map(item=>`<option value="${esc(item.id)}">${esc(item.id)}</option>`).join('');
    q(s,'.knowledge-rollback-confirm').checked=false;
    q(s,'.knowledge-live-events').innerHTML=s.live.events.slice(0,10).map(event=>`<p>第 ${event.revision} 次 · ${esc(({activate:'切换',rollback:'回滚',registration_changed:'登记失效，恢复原库',publication_deleted:'版本删除，恢复原库'})[event.action]||event.action)}</p>`).join('');
    q(s,'.knowledge-build-failures').innerHTML=(data.failures||[]).slice(-5).map(item=>`<p class="small muted">构建失败记录 ${esc(item.id)}：${esc(({vector_cache_unavailable:'缺少匹配的已有向量',development_regression:'开发题词法退化',permissions_or_sources_changed:'权限或来源变化',artifact_failure:'构建文件异常'})[item.code]||item.code)}，未发布。</p>`).join('');
    q(s,'.release-register-list').innerHTML=s.builds.filter(item=>item.status!=='invalid_package'&&!item.currently_stale&&!s.releases.some(entry=>entry.id===item.id)).map(item=>`<article class="workbench-item"><p class="small muted">${esc(item.id)}</p><label class="candidate-confirm"><input type="checkbox" data-release-confirm="${esc(item.id)}">已核对这个构建版本；登记后仍未发布</label><button type="button" data-release-register="${esc(item.id)}">登记此版本</button></article>`).join('');
    q(s,'.knowledge-release-list').innerHTML=s.releases.length?s.releases.map(releaseCard).join(''):'<p class="small muted">暂无已登记版本。</p>';
  }
  function switchVersion(s,id,action){
    const item=id?s.releases.find(entry=>entry.id===id):null;
    if(id&&item?.approval?.state!=='approved'){message(s,'目标版本没有有效批准，请刷新。');return;}
    const key=action+':'+id+':'+s.live.revision;
    if(!s.pending.has(key))s.pending.set(key,{client_id:crypto.randomUUID(),expected_revision:s.live.revision,action,release_id:id,publication_version:item?.approval?.version??null,confirmed:true});
    operation(async(session,signal)=>{await api(endpoint+'/live/switch',{method:'POST',signal,body:JSON.stringify(s.pending.get(key))});s.pending.delete(key);return load(signal);},action==='rollback'?'已回滚在线版本，旧版本回答会重新核对依据。':'在线知识版本已切换。');
  }
  async function load(signal){
    const [permissions,own]=await Promise.all([api(endpoint+'/permissions',{signal}),api(endpoint+'/submissions',{signal})]);
    const [queue,builds,releases,liveVersion]=permissions.reviewer?await Promise.all([api(endpoint+'/review-queue',{signal}),api(endpoint+'/builds',{signal}),api(endpoint+'/releases',{signal}),api(endpoint+'/live',{signal})]):[{items:[]},{items:[]},{items:[]},{revision:0,release_id:null,events:[]}];
    return {permissions,items:own.items,queue:queue.items,builds:builds.items,failures:builds.failures||[],releases:releases.items,live:liveVersion};
  }
  async function operation(request,success){
    const s=active;if(!s||s.busy)return;
    s.controller?.abort();s.controller=new AbortController();busy(s,true);
    try{const data=await request(s,s.controller.signal);if(!live(s))return;render(s,data);message(s,success);}
    catch(error){if(live(s)&&error.name!=='AbortError')message(s,error.message+' 请刷新预审面板后重新核对。');}
    finally{if(live(s)){busy(s,false);if(s.refreshPending){s.refreshPending=false;refresh();}}}
  }
  function refresh(){const s=active;if(!s)return;if(s.busy){s.refreshPending=true;return;}operation((session,signal)=>load(signal),'预审、发布批准和在线版本状态已刷新。');}
  function setCandidates(items){if(active){active.candidates=items;refresh();}}
  function mount(root){
    dispose();if(!root)return;
    root.innerHTML=`<details class="knowledge-review"><summary>公共摘录提交与预审</summary><p class="small muted">仅在明确提交后，审核人才能看到公共摘录与出处。原问题、回答及私人反馈不会提交。人工预审通过仍需后续质量门禁和版本发布。</p><p class="review-permission small muted"></p><button type="button" class="review-refresh">刷新预审状态</button><p class="review-status small muted" role="status"></p><div class="review-candidates workbench-list"></div><h3>本人提交</h3><div class="review-own workbench-list"></div><section class="review-queue-section" hidden><h3>公共摘录审核队列</h3><div class="review-queue workbench-list"></div></section></details>`;
    const s=active={root,user:state.user,candidates:[],items:[],queue:[],pending:new Map(),busy:false};
    const buildSection=document.createElement('section');buildSection.className='knowledge-build-section';
    buildSection.innerHTML='<h3>候选知识版本（未发布）</h3><p class="small muted">只构建所选公共摘录。完全相同摘要去重；仅复用编码内容一致的已有向量，缺缓存时暂停，不自动调用云服务。词法回归不是独立回答质量验收。</p><div class="build-selection"></div><label class="candidate-confirm"><input type="checkbox" class="candidate-build-confirmed">已核对构建列表，理解这次构建不会发布或切换知识库</label><button type="button" class="candidate-build-button">构建候选知识版本</button><div class="knowledge-build-list workbench-list"></div>';
    q(s,'.review-queue-section').append(buildSection);
    const failures=document.createElement('div');failures.className='knowledge-build-failures';buildSection.append(failures);
    const releaseSection=document.createElement('section');releaseSection.className='knowledge-release-section';
    releaseSection.innerHTML='<h3>版本登记与发布准备</h3><p class="small muted">登记绑定具体构建及预审版本。发布质量条件未满足时阻止批准；批准后需明确切换，撤回和失效不能通过恢复旧内容来撤销。</p><div class="release-register-list workbench-list"></div><div class="knowledge-release-list workbench-list"></div>';
    q(s,'.review-queue-section').append(releaseSection);
    const liveSection=document.createElement('section');liveSection.className='knowledge-live-section';
    liveSection.innerHTML='<h3>在线知识版本</h3><p class="knowledge-live-status small muted"></p><label>回滚目标<select class="knowledge-rollback-target"></select></label><label class="candidate-confirm"><input type="checkbox" class="knowledge-rollback-confirm">确认回滚在线知识版本</label><button type="button" class="knowledge-rollback-button">回滚到所选版本</button><div class="knowledge-live-events small muted"></div>';
    q(s,'.review-queue-section').append(liveSection);
    q(s,'.knowledge-rollback-button').addEventListener('click',()=>{
      if(s.busy)return;
      if(!q(s,'.knowledge-rollback-confirm').checked){message(s,'请先确认回滚目标。');return;}
      switchVersion(s,q(s,'.knowledge-rollback-target').value||null,'rollback');
    });
    root.addEventListener('change',event=>{if(event.target.matches('[data-build-select],.candidate-build-confirmed'))s.pendingBuild=null;});
    q(s,'.candidate-build-button').addEventListener('click',()=>{
      if(s.busy)return;
      const selected=Array.from(s.root.querySelectorAll('[data-build-select]:checked')).map(node=>s.queue.find(item=>item.id===node.dataset.buildSelect));
      if(!selected.length||selected.some(item=>!item||item.status!=='approved'||item.source_unavailable)){message(s,'请先选择有效、已人工预审的公共摘录。');return;}
      if(!q(s,'.candidate-build-confirmed').checked){message(s,'请先核对构建列表并勾选构建确认。');return;}
      s.pendingBuild=s.pendingBuild||{client_id:crypto.randomUUID(),submissions:selected.map(item=>({id:item.id,version:item.version})),confirmed:true};
      operation(async(session,signal)=>{await api(endpoint+'/builds',{method:'POST',signal,body:JSON.stringify(s.pendingBuild)});s.pendingBuild=null;return load(signal);},'候选知识版本已构建，未发布，在线知识库没有切换。');
    });
    q(s,'.review-refresh').addEventListener('click',refresh);
    root.addEventListener('click',event=>{
      const button=event.target.closest('button');if(!button||s.busy)return;
      if(button.dataset.publicationApprove){
        const item=s.releases.find(entry=>entry.id===button.dataset.publicationApprove);if(!item)return;
        if(!q(s,`[data-publication-approve-confirm="${item.id}"]`)?.checked){message(s,'请先核对出处和质量报告。');return;}
        operation(async(session,signal)=>{await api(endpoint+'/releases/'+item.id+'/approve',{method:'POST',signal,body:JSON.stringify({version:item.version,binding_hash:item.report.binding_hash,reviewed:true})});return load(signal);},'版本已批准；明确切换后才用于在线问答。');return;
      }
      if(button.dataset.publicationSwitch){
        const id=button.dataset.publicationSwitch;
        if(!q(s,`[data-publication-switch-confirm="${id}"]`)?.checked){message(s,'请先确认切换目标。');return;}
        switchVersion(s,id,'activate');return;
      }
      if(button.dataset.releaseRegister){
        const item=s.builds.find(entry=>entry.id===button.dataset.releaseRegister);if(!item)return;
        if(!q(s,`[data-release-confirm="${item.id}"]`)?.checked){message(s,'请先核对具体构建版本并勾选登记确认。');return;}
        operation(async(session,signal)=>{await api(endpoint+'/releases',{method:'POST',signal,body:JSON.stringify({id:item.id,manifest_hash:item.manifest_hash,confirmed:true})});return load(signal);},'版本已登记，仍未发布。');return;
      }
      if(button.dataset.releaseCheck||button.dataset.releaseWithdraw){
        const action=button.dataset.releaseCheck?'check':'withdraw',id=button.dataset.releaseCheck||button.dataset.releaseWithdraw;
        const item=s.releases.find(entry=>entry.id===id);if(!item)return;
        if(action==='withdraw'&&!q(s,`[data-release-withdraw-confirm="${id}"]`)?.checked){message(s,'请先确认撤回这个登记。');return;}
        operation(async(session,signal)=>{await api(endpoint+'/releases/'+id+'/'+action,{method:'POST',signal,body:JSON.stringify({version:item.version})});return load(signal);},action==='check'?'检查已记录；请查看质量报告与缺失项，尚未自动发布。':'登记已撤回，未切换在线知识库。');return;
      }
      if(button.dataset.reviewSubmit){
        const candidate=s.candidates.find(item=>item.id===button.dataset.reviewSubmit);if(!candidate)return;
        if(!q(s,`[data-review-share="${candidate.id}"]`)?.checked){message(s,'请先明确同意提交这段公共摘录与出处。');return;}
        const key=candidate.id+':'+candidate.version;
        if(!s.pending.has(key))s.pending.set(key,{version:candidate.version,client_id:crypto.randomUUID(),share_public_excerpt:true});
        operation(async(session,signal)=>{await api(endpoint+'/candidates/'+candidate.id+'/submit',{method:'POST',signal,body:JSON.stringify(s.pending.get(key))});s.pending.delete(key);return load(signal);},'公共摘录已提交，未发布；私人反馈和对话没有进入审核队列。');return;
      }
      const action=['check','approve','reject','withdraw'].find(value=>button.dataset['review'+value[0].toUpperCase()+value.slice(1)]);
      if(!action)return;
      const id=button.dataset['review'+action[0].toUpperCase()+action.slice(1)],item=(action==='withdraw'?s.items:s.queue).find(entry=>entry.id===id);if(!item)return;
      const body={version:item.version};
      if(action==='approve'){
        if(!q(s,`[data-review-confirm="${id}"]`)?.checked){message(s,'请先人工核对完整出处、上下文与适用范围。');return;}
        Object.assign(body,{gate_id:item.gate.id,binding_hash:item.gate.binding_hash,reviewed:true});
      }
      if(action==='reject')body.reason=q(s,`[data-review-reason="${id}"]`).value;
      const path=action==='withdraw'?'/submissions/':'/review-queue/';
      operation(async(session,signal)=>{await api(endpoint+path+id+'/'+action,{method:'POST',signal,body:JSON.stringify(body)});return load(signal);},action==='approve'?'人工预审通过；仍未发布，不参与检索。':'审核状态已更新，未发布。');
    });
  }
  return {mount,dispose,setCandidates,refresh};
})();
