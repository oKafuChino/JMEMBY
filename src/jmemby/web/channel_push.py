MARKUP = r'''
<section id="channelPush" hidden aria-label="频道推送">
<div class="panel"><div class="panel-head"><div><h2>入库频道推送</h2><p>将新完成入库的漫画信息发布到指定频道，与加入限制的频道独立配置</p></div><span id="channelPushStatus" class="tag" role="status">等待加载</span></div>
<div class="panel-body"><form id="channelPushForm">
<label for="channelPushId" class="field-label">目标频道 · Chat ID 或 @用户名</label>
<input id="channelPushId" maxlength="64" placeholder="-1001234567890 或 @channel_name" required>
<label for="channelPushBatch" class="field-label">网页批量下载入库后</label>
<select id="channelPushBatch"><option value="false">保持静默，不推送到频道（默认）</option><option value="true">也推送到频道</option></select>
<div class="button-row"><button id="channelPushSave" class="primary" disabled>验证并保存启用</button><button id="channelPushDisable" type="button" class="quiet" disabled>关闭推送</button></div>
</form></div><div class="panel-foot">先将 Bot 设为目标频道管理员并授予“发布消息”权限。保存时仅检查配置，不发送测试消息。<br>启用后只推送新完成入库的任务，不补发历史记录。内容包含标题、作者、漫画 ID、章节数和阅读地址，不公开下载者信息。<br>关闭或更换频道会跳过尚未发送的旧通知，不转发到新频道；已发出或正在发送的消息无法撤回。批量频道推送不会恢复用户私聊或群聊通知。</div></div>
<div class="panel"><div class="panel-head"><div><h2>最近推送记录</h2><p id="channelPushCounts">等待加载 · 点击右上角刷新查看最新状态</p></div><span class="tag">最近 10 条</span></div><div id="channelPushTable" class="scroll" tabindex="0" role="region" aria-label="频道推送记录"></div><div class="panel-foot">失败后自动重试，等待重试的首条通知会暂缓后续推送。若持续失败，请检查 Bot 发布权限和网络。</div></div>
</section>
'''

SCRIPT = r'''
let channelPushRequest=0,channelPushBusy=false,channelPushEnabled=false;
function clearChannelPush(){channelPushRequest++;channelPushBusy=false;channelPushEnabled=false;el('channelPushId').value='';el('channelPushBatch').value='false';el('channelPushSave').disabled=true;el('channelPushSave').textContent='验证并保存启用';el('channelPushDisable').disabled=true;el('channelPushStatus').textContent='等待加载';el('channelPushCounts').textContent='等待加载';empty('channelPushTable');}
function channelPushReason(n){return ({DISABLED:'推送已关闭',CHANNEL_CHANGED:'目标频道已更换',BATCH_SILENT:'批量任务保持静默',REMOVED:'漫画已移除',NO_LONGER_ELIGIBLE:'不再满足推送条件'})[n.last_error]||n.last_error||(n.status==='SKIPPED'?'已跳过':'等待处理');}
function showChannelPush(d){channelPushEnabled=d.enabled;el('channelPushStatus').textContent=d.enabled?'推送已启用':'推送已关闭';el('channelPushId').value=String(d.channel?.chat_id??'');el('channelPushBatch').value=String(d.include_batch);el('channelPushSave').disabled=false;el('channelPushDisable').disabled=!d.enabled;el('channelPushCounts').textContent='待发送 '+(d.counts.PENDING||0)+' · 已发送 '+(d.counts.SENT||0)+' · 已跳过 '+(d.counts.SKIPPED||0)+' · 手动刷新查看最新状态';table('channelPushTable',['漫画','目标频道','状态','失败次数','最近结果 / 重试时间'],d.recent.map(n=>[stack(n.title,'JM'+n.source_id),mono(n.chat_id),badge(({PENDING:'等待发送',SENT:'已发送',SKIPPED:'已跳过'})[n.status],({PENDING:'warn',SENT:'success',SKIPPED:'muted'})[n.status]),n.attempts,n.status==='SENT'?new Date(n.sent_at*1000).toLocaleString():stack(channelPushReason(n),n.status==='PENDING'&&n.next_at?new Date(n.next_at*1000).toLocaleString():'')]));}
async function loadChannelPush(){if(channelPushBusy)return;const request=++channelPushRequest;el('channelPushSave').disabled=true;el('channelPushDisable').disabled=true;try{const d=await api('channel-push');if(request!==channelPushRequest||adminId===null)return;showChannelPush(d);stamp();}catch(e){if(request!==channelPushRequest)return;el('channelPushStatus').textContent='加载失败，请刷新';throw e;}}
async function saveChannelPush(enabled){if(channelPushBusy)return;const request=++channelPushRequest;channelPushBusy=true;el('channelPushSave').disabled=true;el('channelPushDisable').disabled=true;el('channelPushSave').textContent=enabled?'正在验证…':'正在关闭…';try{const payload=enabled?{enabled:true,chat_id:el('channelPushId').value.trim(),include_batch:el('channelPushBatch').value==='true'}:{enabled:false};const d=await api('channel-push',payload);if(request!==channelPushRequest||adminId===null)return;showChannelPush(d);if(view==='channelPush'){el('notice').dataset.tone='success';el('notice').textContent=enabled?'频道推送已启用，仅推送新完成的入库任务':'频道推送已关闭，未发送的通知已跳过';}stamp();}finally{if(request===channelPushRequest){channelPushBusy=false;el('channelPushSave').disabled=false;el('channelPushSave').textContent='验证并保存启用';el('channelPushDisable').disabled=!channelPushEnabled;}}}
el('channelPushForm').onsubmit=e=>{e.preventDefault();safe(()=>saveChannelPush(true));};
el('channelPushDisable').onclick=()=>safe(async()=>{if(confirm('关闭频道推送并跳过尚未发送的通知？已经发出或正在发送的消息不会撤回。'))await saveChannelPush(false);});
'''
