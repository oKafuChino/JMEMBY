"""Admin report review; Telegram only announces new reports."""

MARKUP = r'''
<section id="reports" class="panel" hidden aria-label="举报管理">
<div class="panel-head"><div><h2>举报管理</h2><p>在这里审核举报；Telegram 只发送提醒。移除漫画会同时关闭该漫画的其他待处理举报。</p></div><span class="tag" id="reportPendingCount">待处理 —</span></div>
<div class="panel-body"><form id="reportForm" class="search-form"><div class="search-field"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input id="reportQuery" maxlength="200" placeholder="搜索漫画、漫画 ID 或举报用户 ID" aria-label="搜索举报"></div><select id="reportStatus" aria-label="举报状态"><option value="PENDING">待处理</option><option value="RESOLVED">已移除漫画</option><option value="DISMISSED">已忽略</option><option value="all">全部</option></select><button class="primary">搜索</button><button type="button" id="reportClear" class="quiet">清空</button></form></div>
<div id="reportTable" class="scroll" tabindex="0" role="region" aria-label="举报请求列表"></div>
<div class="pagination"><span id="reportPage">第 1 页</span><button id="reportPrev" class="quiet">上一页</button><button id="reportNext" class="quiet">下一页</button></div>
</section>
'''

SCRIPT = r'''
let reportPage=1,reportQuery='',reportRequest=0,reportSession=0,reportBusy=false;
const reportLabels={PENDING:'待处理',RESOLVED:'已移除漫画',DISMISSED:'已忽略'};
function clearReports(){reportSession++;reportRequest++;reportPage=1;reportQuery='';reportBusy=false;el('reportQuery').value='';el('reportStatus').value='PENDING';el('reportPendingCount').textContent='待处理 —';empty('reportTable');}
function reportActions(r){if(r.status!=='PENDING')return '—';const group=document.createElement('div');group.className='node-actions';
const viewButton=document.createElement('button');viewButton.className='small';viewButton.textContent='查看漫画';viewButton.onclick=()=>safe(async()=>{libraryQuery=r.source_id;libraryPage=1;el('libraryQuery').value=r.source_id;setView('library');await loadLibrary();});group.append(viewButton);
for(const [operation,label] of [['remove','移除漫画'],['dismiss','忽略']]){const b=document.createElement('button');b.className='small '+(operation==='remove'?'danger':'quiet');b.textContent=label;b.disabled=reportBusy||(operation==='remove'&&!!r.busy);b.title=operation==='remove'&&r.busy?'请先取消未结束的下载任务，并等待节点释放':'';
b.onclick=()=>safe(async()=>{if(reportBusy)return;const question=operation==='remove'?'确认永久删除《'+r.title+'》（ID '+r.source_id+'）的已保存章节文件，并关闭该漫画的全部待处理举报？':'确认忽略举报 #'+r.id+'？漫画文件将保留。';if(!confirm(question))return;const session=reportSession;reportBusy=true;b.disabled=true;
try{const result=await api('reports',{action:operation,report_id:r.id,confirm:true});if(session!==reportSession||adminId===null)return;el('notice').dataset.tone=result.warning?'warning':'success';el('notice').textContent=result.warning||(operation==='remove'?'漫画已移除，相关举报已处理，已请求 Komga 扫描。':'举报已忽略。');}
finally{if(session===reportSession){reportBusy=false;if(adminId!==null)await loadReports();}}});group.append(b);}return group;}
async function loadReports(){const request=++reportRequest,session=reportSession;loading('reportTable');try{const d=await api('reports?page='+reportPage+'&status='+encodeURIComponent(el('reportStatus').value)+'&q='+encodeURIComponent(reportQuery));if(request!==reportRequest||session!==reportSession||adminId===null)return;
const last=Math.max(1,Math.ceil(d.total/d.limit));if(reportPage>last){reportPage=last;await loadReports();return;}
table('reportTable',['编号','漫画 / ID','举报用户','提交时间','状态','处理记录','操作'],d.reports.map(r=>[mono(r.id),stack(r.title,r.source_id),stack(r.display_name||String(r.user_id),(r.username?'@'+r.username+' · ':'')+'ID '+r.user_id),new Date(r.created_at*1000).toLocaleString(),badge(reportLabels[r.status],r.status==='PENDING'?'warn':'muted'),r.handled_at?stack('管理员 '+r.handled_by,new Date(r.handled_at*1000).toLocaleString()):'—',reportActions(r)]));
if(!d.reports.length)empty('reportTable',el('reportStatus').value==='PENDING'?'暂无匹配的待处理举报':'暂无匹配的举报','可调整搜索关键词或状态筛选。');el('reportPendingCount').textContent='待处理 '+d.pending;el('reportPage').textContent='第 '+d.page+' / '+last+' 页 · 共 '+d.total+' 条';el('reportPrev').disabled=reportPage===1;el('reportNext').disabled=reportPage>=last;stamp();
}catch(e){if(request!==reportRequest||session!==reportSession)return;empty('reportTable','举报加载失败','请刷新重试。');throw e;}}
el('reportForm').onsubmit=e=>{e.preventDefault();reportQuery=el('reportQuery').value.trim();reportPage=1;safe(loadReports);};
el('reportClear').onclick=()=>{reportQuery='';el('reportQuery').value='';reportPage=1;safe(loadReports);};
el('reportStatus').onchange=()=>{reportPage=1;safe(loadReports);};
for(const [id,delta] of [['reportPrev',-1],['reportNext',1]])el(id).onclick=()=>safe(async()=>{reportPage=Math.max(1,reportPage+delta);await loadReports();});
'''
