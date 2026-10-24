"""Managed manga library view using the panel's shared UI helpers."""

MARKUP = r'''
<section id="library" class="panel" hidden aria-label="漫画列表">
<div class="panel-head"><div><h2>漫画列表</h2><p>管理本工具记录的漫画；移除会删除已保存的章节文件，保留下载历史。</p></div><span class="tag">管理员管理</span></div>
<div class="panel-body"><form id="libraryForm" class="search-form"><div class="search-field"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg><input id="libraryQuery" maxlength="200" placeholder="搜索漫画标题或来源 ID" aria-label="搜索漫画"></div><button class="primary">搜索</button><button type="button" id="libraryClear" class="quiet">清空</button></form></div>
<div class="search-meta"><p>有排队、下载或重试任务时不可移除。移除后可重新下载；Komga 将通过扫描更新，文件删除不可撤销。</p><p id="libraryCount" role="status" aria-live="polite">等待加载漫画</p></div>
<div id="libraryTable" class="scroll" tabindex="0" role="region" aria-label="漫画列表"></div>
<div class="pagination"><span id="libraryPage">第 1 页</span><button id="libraryPrev" class="quiet">上一页</button><button id="libraryNext" class="quiet">下一页</button></div>
</section>
'''

SCRIPT = r'''
let libraryPage=1,libraryQuery='',libraryRequest=0,librarySession=0,libraryRemoving=false;
function clearLibrary(){librarySession++;libraryRequest++;libraryPage=1;libraryQuery='';libraryRemoving=false;el('libraryQuery').value='';empty('libraryTable');el('libraryCount').textContent='等待加载漫画';}
function librarySize(bytes){return bytes>=1073741824?(bytes/1073741824).toFixed(2)+' GiB':(bytes/1048576).toFixed(1)+' MiB';}
function removeMangaButton(m){const b=document.createElement('button');b.className='small danger';b.textContent='移除';b.disabled=!!m.busy||libraryRemoving;b.title=m.busy?'请先取消未结束任务，并等待节点释放':'删除该漫画的已保存章节';b.onclick=()=>safe(async()=>{
if(libraryRemoving||!confirm('确认移除《'+m.title+'》（'+m.source+':'+m.source_id+'）？\n将永久删除已保存的章节文件，保留下载历史，之后可重新下载。'))return;
const session=librarySession;libraryRemoving=true;b.disabled=true;b.textContent='移除中…';
try{const result=await api('library/remove',{manga_id:m.id,confirm:true});if(session!==librarySession||adminId===null)return;el('notice').dataset.tone=result.warning?'warning':'success';el('notice').textContent=result.warning||'漫画已移除，已请求 Komga 扫描；扫描完成后更新阅读库。';}
finally{if(session===librarySession){libraryRemoving=false;if(adminId!==null)await loadLibrary();}}});return b;}
async function loadLibrary(){const request=++libraryRequest,session=librarySession;loading('libraryTable');try{
const d=await api('library?page='+libraryPage+'&q='+encodeURIComponent(libraryQuery));if(request!==libraryRequest||session!==librarySession||adminId===null)return;
const last=Math.max(1,Math.ceil(d.total/d.limit));if(libraryPage>last){libraryPage=last;await loadLibrary();return;}
table('libraryTable',['漫画 / 来源 ID','已保存章节','记录大小','状态','操作'],d.manga.map(m=>[stack(m.title,m.source+':'+m.source_id),m.saved_chapters+' / '+m.chapters,librarySize(m.size),badge(m.busy?'任务未结束':m.saved_chapters?'已保存':'暂无已保存章节',m.busy?'info':m.saved_chapters?'success':'muted'),removeMangaButton(m)]));
if(!d.manga.length)empty('libraryTable',libraryQuery?'没有找到匹配的漫画':'漫画列表为空','本工具提交的漫画会显示在这里，移除后的漫画不再显示。');
el('libraryCount').textContent='共 '+d.total+' 本 · 每页 '+d.limit+' 本';el('libraryPage').textContent='第 '+d.page+' / '+last+' 页';el('libraryPrev').disabled=libraryPage===1;el('libraryNext').disabled=libraryPage>=last;stamp();
}catch(e){if(request!==libraryRequest||session!==librarySession)return;empty('libraryTable','漫画列表加载失败','请刷新重试。');throw e;}}
el('libraryForm').onsubmit=e=>{e.preventDefault();libraryQuery=el('libraryQuery').value.trim();libraryPage=1;safe(loadLibrary);};
el('libraryClear').onclick=()=>{libraryQuery='';el('libraryQuery').value='';libraryPage=1;safe(loadLibrary);};
for(const [id,delta] of [['libraryPrev',-1],['libraryNext',1]])el(id).onclick=()=>safe(async()=>{libraryPage=Math.max(1,libraryPage+delta);await loadLibrary();});
'''
