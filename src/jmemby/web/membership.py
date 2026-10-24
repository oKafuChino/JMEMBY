MARKUP = r'''
<section id="membership" class="panel" hidden aria-label="Bot 使用限制">
<div class="panel-head"><div><h2>Bot 使用限制</h2><p>用户必须同时加入指定频道和群组；普通、白名单用户均适用，管理员豁免</p></div><span id="membershipStatus" class="tag" role="status">等待加载</span></div>
<div class="panel-body"><form id="membershipForm">
<div class="batch-grid"><div>
<label for="membershipChannel" class="field-label">必加入频道 · Chat ID 或 @用户名</label>
<input id="membershipChannel" maxlength="64" placeholder="-1001234567890 或 @channel_name" required>
<label for="membershipChannelUrl" class="field-label">频道加入链接（私有频道必填）</label>
<input id="membershipChannelUrl" type="url" maxlength="256" placeholder="https://t.me/+邀请链接">
</div><div>
<label for="membershipGroup" class="field-label">必加入群组 · Chat ID 或 @用户名</label>
<input id="membershipGroup" maxlength="64" placeholder="-1009876543210 或 @group_name" required>
<label for="membershipGroupUrl" class="field-label">群组加入链接（私有群组必填）</label>
<input id="membershipGroupUrl" type="url" maxlength="256" placeholder="https://t.me/+邀请链接">
</div></div>
<div class="button-row"><button id="membershipSave" class="primary" disabled>验证并保存启用</button><button id="membershipDisable" type="button" class="quiet" disabled>关闭限制</button></div>
</form></div>
<div class="panel-foot">先将 Bot 加入这两个聊天并设为管理员，再保存。公开聊天可自动生成加入链接；私有聊天请提供对应的有效邀请链接。<br>每次使用时重新核验，退出任意一个聊天后无法继续使用；申请加入必须先获批准。网络或权限异常时暂不放行。<br>加入后重新发送原指令即可。注册资格、封禁和邀请码规则仍然生效；已入队下载和已有 Komga 账号不受此开关影响。</div>
</section>
'''

SCRIPT = r'''
let membershipRequest=0,membershipBusy=false,membershipEnabled=false;
function clearMembership(){membershipRequest++;membershipBusy=false;membershipEnabled=false;el('membershipSave').textContent='验证并保存启用';for(const id of ['membershipChannel','membershipChannelUrl','membershipGroup','membershipGroupUrl'])el(id).value='';el('membershipSave').disabled=true;el('membershipDisable').disabled=true;el('membershipStatus').textContent='等待加载';}
function showMembership(d){membershipEnabled=d.enabled;el('membershipStatus').textContent=d.enabled?'限制已启用':'限制已关闭';for(const [key,prefix] of [['channel','membershipChannel'],['group','membershipGroup']]){el(prefix).value=String(d[key]?.chat_id??'');el(prefix+'Url').value=d[key]?.join_url||'';}el('membershipSave').disabled=false;el('membershipDisable').disabled=!d.enabled;}
async function loadMembership(){if(membershipBusy)return;const request=++membershipRequest;el('membershipSave').disabled=true;el('membershipDisable').disabled=true;try{const d=await api('membership');if(request!==membershipRequest||adminId===null)return;showMembership(d);stamp();}catch(e){if(request!==membershipRequest)return;el('membershipStatus').textContent='加载失败，请刷新';throw e;}}
async function saveMembership(enabled){if(membershipBusy)return;const request=++membershipRequest;membershipBusy=true;el('membershipSave').textContent=enabled?'正在验证…':'正在关闭…';el('membershipSave').disabled=true;el('membershipDisable').disabled=true;try{const payload=enabled?{enabled:true,channel:{chat_id:el('membershipChannel').value.trim(),join_url:el('membershipChannelUrl').value.trim()},group:{chat_id:el('membershipGroup').value.trim(),join_url:el('membershipGroupUrl').value.trim()}}:{enabled:false};const d=await api('membership',payload);if(request!==membershipRequest||adminId===null)return;showMembership(d);if(view==='membership'){el('notice').dataset.tone='success';el('notice').textContent=enabled?'频道和群组已验证，使用限制立即生效':'Bot 使用限制已关闭，配置已保留';}stamp();}finally{if(request===membershipRequest){membershipBusy=false;el('membershipSave').textContent='验证并保存启用';el('membershipSave').disabled=false;el('membershipDisable').disabled=!membershipEnabled;}}}
el('membershipForm').onsubmit=e=>{e.preventDefault();safe(()=>saveMembership(true));};
el('membershipDisable').onclick=()=>safe(async()=>{if(confirm('关闭 Bot 加入限制？用户将无需加入指定频道和群组。'))await saveMembership(false);});
'''
