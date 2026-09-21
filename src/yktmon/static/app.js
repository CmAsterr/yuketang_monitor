'use strict';
const $ = id => document.getElementById(id);
const names = {wecom:'企业微信',dingtalk:'钉钉',feishu:'飞书',qqbot:'QQ 官方机器人'};
const types = {1:'单选',2:'多选',3:'投票',4:'填空',5:'主观'};
const states = {pending:'等待处理',processing:'正在解答',done:'已解答',failed:'需要处理',skipped:'人工查看'};
let settings={},status={},offset=0,rows=[],courses=[],refreshTimer=null,refreshing=false,refreshAgain=false,dirty=false,deleteId=null,problemFingerprint="",courseFingerprint="",records=[],tabs=[],activeRecord=null,renameTarget=null,closingTab=null,modelTimer=null,modelRequest=0,recordViews={},modelChoices=[],keyRequest=0,tabFingerprint="",recordFingerprint="";
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=String(text);if(cls)e.className=cls;return e;}
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
async function api(path,method='GET',data){const r=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-Yktmon':'local'},...(data!==undefined?{body:JSON.stringify(data)}:{})});let result;try{result=await r.json();}catch{throw new Error('服务返回无效响应');}if(!r.ok)throw new Error(typeof result.detail==='string'?result.detail:'请求失败：'+r.status);return result;}
async function action(button,fn){button.disabled=true;try{await fn();}catch(e){notice(e.message);}finally{button.disabled=false;}}
const disclosureState=new Map();
function rememberDisclosure(details,key,defaultOpen=false){
 details.dataset.disclosureKey=key;details.classList.add('fold-panel');
 details.open=disclosureState.has(key)?disclosureState.get(key):defaultOpen;
 details.addEventListener('toggle',()=>{if(details.isConnected)disclosureState.set(key,details.open);});
 return details;
}
function captureDisclosures(root){for(const d of root.querySelectorAll('details[data-disclosure-key]'))disclosureState.set(d.dataset.disclosureKey,d.open);}
function recordStatus(r){
 if(r.listening)return {label:'监听中',tone:'live',detail:r.state==='live'?'已连接课堂':'等待开课或恢复连接'};
 if(r.state==='archived')return {label:'已结束',tone:'archived',detail:'只读，可继续本节'};
 if(r.started_at!=null || r.problem_count>0)return {label:'已暂停',tone:'paused',detail:'只读，可继续本节'};
 return {label:'尚未开始',tone:'draft',detail:'只读，尚未开启监听'};
}
function recordBadge(r){const state=recordStatus(r);return node('span',state.label,'badge record-status '+state.tone);}
function recordStartLabel(r,short=false){return recordStatus(r).tone==='draft'?'开始监听':short?'继续监听':'继续本节监听';}
async function resumeRecord(id){await api(`records/${id}/resume`,'POST');await refresh();openRecord(id);}
function currentRecord(){return records.find(r=>r.id===activeRecord);}
function rememberTabs(){try{localStorage.setItem('class-tabs:'+status.domain,JSON.stringify(tabs));}catch{}}
function navigate(view){
 if(activeRecord)recordViews[activeRecord]={offset,search:$('problemSearch').value};
 if(view.startsWith('record-')){const id=Number(view.slice(7));if(records.some(r=>r.id===id)){activeRecord=id;if(!tabs.includes(id))tabs.push(id);offset=recordViews[id]?.offset||0;$('problemSearch').value=recordViews[id]?.search||'';rows=[];problemFingerprint='';rememberTabs();view='problems';}else view='courses';}
 if(!['problems','courses','settings'].includes(view)||view==='problems'&&!currentRecord())view='courses';
 notice('');for(const name of ['problems','courses','settings'])$('view-'+name).hidden=name!==view;
 for(const b of document.querySelectorAll('[data-view]')){if(b.dataset.view===view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}
 const hash=view==='problems'?'record-'+activeRecord:view;if(location.hash!=='#'+hash)history.replaceState(null,'','#'+hash);
 renderTabs();if(view==='problems'){renderRecord();renderProblems([]);scheduleRefresh();}
}
function openRecord(id){navigate('record-'+id);}
function renderTabs(){
 tabs=tabs.filter(id=>records.some(r=>r.id===id));
 const fingerprint=JSON.stringify([tabs.map(id=>{const r=records.find(x=>x.id===id);return [id,r.title,r.course_name,recordStatus(r)];}),activeRecord,$('view-problems').hidden]);
 if(fingerprint===tabFingerprint)return;tabFingerprint=fingerprint;
 $('classTabs').replaceChildren();
 for(const id of tabs){const r=records.find(x=>x.id===id);const tab=node('div',undefined,'class-tab'+(!$('view-problems').hidden&&id===activeRecord?' active':''));
  const b=node('button',undefined,'tab-name');b.setAttribute('aria-label',r.title);b.title=r.course_name+' · '+r.title+' · '+recordStatus(r).label;b.append(node('span',r.title,'tab-title'),recordBadge(r));b.onclick=()=>openRecord(id);tab.append(b);
  const close=node('button','×','tab-close');close.setAttribute('aria-label','关闭标签：'+r.title);close.title='关闭页面，不删除记录';close.onclick=()=>{if(r.listening){closingTab=id;$('closeTabDialog').showModal();}else removeTab(id);};tab.append(close);$('classTabs').append(tab);
 }
}
function removeTab(id){tabs=tabs.filter(x=>x!==id);rememberTabs();if(activeRecord===id){activeRecord=null;navigate('courses');}else renderTabs();}
function editName(kind,id,name){renameTarget={kind,id};$('renameHeading').textContent=kind==='course'?'修改课程名称':'修改本节课标题';$('renameInput').value=name;$('renameDialog').showModal();$('renameInput').focus();}
function recordButton(label,actionName,cls='secondary'){const id=activeRecord;const b=node('button',label,cls);b.onclick=()=>action(b,async()=>{await api(`records/${id}/${actionName}`,'POST');await refresh();});return b;}
function renderRecord(){const r=currentRecord();if(!r)return;
 const worker=(status.lessons||[]).find(w=>w.record_id===r.id);
 $('lastError').textContent=(status.record_errors||{})[r.id]||status.last_error||'';
 $('lessons').textContent=worker?worker.state:recordStatus(r).detail;
 const fingerprint=JSON.stringify([r.id,r.title,r.course_name,r.listening,r.state,r.started_at,r.course_deleted,r.problem_count]);
 if(fingerprint===recordFingerprint)return;recordFingerprint=fingerprint;
 $('recordCourse').textContent=r.course_name;$('problemsTitle').textContent=r.title;$('recordStateBadge').replaceChildren(recordBadge(r));
 $('recordDescription').textContent=r.listening?'新题会保存到本节。切换课堂标签不停止监听；关闭网页后的退出规则见页脚。':recordStatus(r).label+' · '+recordStatus(r).detail+'。查看记录不会重新作答或发消息。';
 $('recordActions').replaceChildren();
 if(r.listening){$('recordActions').append(recordButton('暂停监听','pause'),recordButton('结束本节','finish'));}
 else{const start=recordButton(recordStartLabel(r),'resume','primary');start.disabled=!!r.course_deleted;if(r.course_deleted)start.title='请先恢复课程';$('recordActions').append(start);}
 const rename=node('button','修改标题','text-button');rename.onclick=()=>editName('record',r.id,r.title);
 const fresh=node('button','新开一节','secondary');fresh.onclick=()=>newRecord(r.course_id);fresh.disabled=!!r.course_deleted;
 const folder=node('button','图片文件夹','text-button');folder.onclick=()=>action(folder,()=>api('images/open-folder','POST'));
 $('recordActions').append(fresh,rename,folder);
}
function newRecord(courseId){$('recordCourseSelect').replaceChildren();for(const c of courses){const option=node('option',c.display_name||c.name);option.value=c.id;$('recordCourseSelect').append(option);}if(!courses.length){navigate('courses');notice('请先添加课程，或登录后等待自动发现课程。');return;}if(courseId)$('recordCourseSelect').value=courseId;$('newRecordTitle').value='';$('newRecordListen').checked=false;$('newRecordDialog').showModal();}
for(const b of document.querySelectorAll('[data-view]'))b.onclick=()=>navigate(b.dataset.view);
for(const b of document.querySelectorAll('[data-go]'))b.onclick=()=>navigate(b.dataset.go);
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
function field(label,key,value='',secret=false){const wrap=node('label',label);const input=node('input');input.type=secret?'password':'text';input.dataset.key=key;input.value=value;input.autocomplete=secret?'new-password':'off';wrap.append(input);return wrap;}
function fillConfig(c={}){
 clearTimeout(modelTimer);modelRequest++;$('fetchModels').disabled=false;
 settings=c;const servers=c.servers||{'长江雨课堂':'changjiang.yuketang.cn'};$('domain').replaceChildren();
 for(const [name,value] of Object.entries(servers)){const option=node('option',name);option.value=value;$('domain').append(option);}
 $('domain').value=c.domain||'changjiang.yuketang.cn';$('scan').value=c.scan_interval??30;
 const ai=c.ai||{};$('baseUrl').value=ai.base_url||'https://api.deepseek.com';$('model').value=ai.model||'deepseek-flash';$('apiKey').value='';$('apiKey').placeholder=ai.api_key_configured?'正在读取已保存的 Key…':'请输入 API Key';$('apiKey').type='password';$('toggleKey').textContent='👁';$('toggleKey').setAttribute('aria-label','显示 API Key');$('toggleKey').setAttribute('aria-pressed','false');$('keyState').textContent=ai.api_key_configured?'已保存，可点击眼睛查看':'尚未配置';$('tokens').value=ai.max_tokens??4096;$('timeout').value=ai.timeout??60;$('aiEnabled').checked=ai.enabled!==false;
 for(const [kind,name] of Object.entries(names).filter(([kind])=>kind!=='qqbot')){
  const item=(Array.isArray(c.channels)?c.channels:[]).find(x=>x.kind===kind)||{};
  const box=$('channel-'+kind);box.replaceChildren();box.dataset.kind=kind;
  const enable=node('label',undefined,'check');const check=node('input');check.type='checkbox';check.dataset.key='enabled';check.checked=!!item.enabled;enable.append(check,node('span','启用'+name));box.append(enable);
  const badge=$('channel-state-'+kind);badge.textContent=item.enabled?'已启用':'未启用';check.addEventListener('change',()=>{badge.textContent=check.checked?'待保存 · 启用':'待保存 · 关闭';});
  const fields=[['Webhook 地址','webhook_url',true],...(kind==='wecom'?[]:[['签名 Secret（可选）','secret',true]])];
  for(const [label,key,secret] of fields){const wrap=field(label,key,secret?'':item[key]||'',secret);if(item[key+'_configured'])wrap.append(node('small','已保存，留空保留'));box.append(wrap);}
  box.append(node('p','使用下方“保存并应用”保存。未启用时不发送；此渠道按当前全局配置推送。','hint'));
 }
 $('saveState').classList.remove('unsaved');$('saveHint').classList.remove('unsaved');renderModelChoices();if(ai.api_key_configured)restoreKey();
 dirty=false;$('testAI').disabled=status.diagnostic?.state==='running';$('saveState').textContent='已保存';$('saveHint').textContent='设置保存在本机';
}
function renderStatus(s={}){
 status=s;$('discoveryState').textContent=s.discovery_state||'';
 $('running').textContent=(s.lessons||[]).length+' 节课已连接';
 $('loginState').textContent=s.login||'未登录';$('qr').hidden=!s.qr;if(s.qr)$('qr').src=s.qr;else $('qr').removeAttribute('src');$('lastError').textContent=(s.record_errors||{})[activeRecord]||s.last_error||'';
 $('login').textContent=s.has_session?'重新扫码':'获取二维码';$('logout').hidden=!s.has_session;
 renderRecord();
 const d=s.diagnostic||{};$('diagnostic').replaceChildren();$('testAI').disabled=d.state==='running'||dirty;
 if(d.state==='running')$('diagnostic').append(node('p','正在测试文字与随机图片识别，请稍候…'));
 if(d.state==='done'){
  $('diagnostic').append(node('h3',d.ok?'AI 自检通过':'AI 自检未通过'));
  for(const check of d.checks||[])$('diagnostic').append(node('div',`${check.ok?'通过':'未通过'} · ${check.name}：${check.detail||''}`,'diagnostic-row'));
  if(d.error)$('diagnostic').append(node('p',d.error,'error'));
  if(d.available_models?.length)$('diagnostic').append(node('p','Key 可见模型：'+d.available_models.join('、'),'hint'));
 }
}
function renderCourses(input){if(input)courses=input;
 const query=$('courseSearch').value.trim().toLowerCase();const fingerprint=JSON.stringify([courses,records,query]);if(fingerprint===courseFingerprint)return;courseFingerprint=fingerprint;
 captureDisclosures($('courses'));
 const list=courses.filter(c=>(c.display_name||c.name||'').toLowerCase().includes(query)||(c.name||'').toLowerCase().includes(query));$('courseCount').textContent=courses.length;$('courseSummary').textContent=`${courses.length} 门课程 · ${records.filter(r=>r.listening).length} 节正在监听`;$('courses').replaceChildren();
 if(!list.length){const empty=node('div',undefined,'empty');empty.append(node('h2',query?'没有匹配的课程':'从一门课程开始'),node('p','添加课程，或在设置中扫码登录，自动发现正在上课的课程。'));$('courses').append(empty);return;}
 for(const c of list){const name=c.display_name||c.name,items=records.filter(r=>r.course_id===c.id),live=items.filter(r=>r.listening).length;
  const block=node('details',undefined,'course-block fold-panel');block.dataset.courseId=c.id;
  const summary=node('summary',undefined,'course-card');summary.setAttribute('aria-label','课程：'+name);
  const info=node('span',undefined,'course-info');info.append(node('span',name,'course-name'),node('span',`${items.length} 节课堂 · ${live} 节监听中`,'course-meta'),node('span','雨课堂：'+c.name+(c.classroom?.startsWith('name:')?' · 待匹配':' · ID '+c.classroom),'course-id'));summary.append(info);
  const actions=node('span',undefined,'actions');
  function headerButton(label,aria,handler,cls){const b=node('button',label,cls);b.type='button';b.setAttribute('aria-label',aria);b.onclick=e=>{e.preventDefault();e.stopPropagation();handler(b);};return b;}
  actions.append(headerButton('新开一节','新开一节：'+name,()=>newRecord(c.id),'primary'),headerButton('通知群','通知群：'+name,()=>window.openQQTargets(c.id,name),'secondary'),headerButton('改名','修改课程名称：'+name,()=>editName('course',c.id,name),'secondary'),headerButton('删除','删除课程：'+name,()=>{deleteId=c.id;$('deleteDescription').textContent='将“'+name+'”移出课程管理，并暂停该课程的监听。';$('deleteDialog').showModal();},'delete-button'));
  summary.append(actions);block.append(summary);
  const history=node('div',undefined,'records-list');
  if(!items.length)history.append(node('p','还没有课堂记录。点击“新开一节”创建。','record-empty'));
  for(const r of items){const line=node('div',undefined,'record-row');line.dataset.recordId=r.id;
   const details=node('div',undefined,'record-info'),titleRow=node('div',undefined,'record-title-row'),open=node('button',r.title,'text-button record-link');open.onclick=()=>openRecord(r.id);titleRow.append(open,recordBadge(r));details.append(titleRow,node('div',new Date(r.created*1000).toLocaleDateString()+' · '+r.problem_count+' 道题 · '+recordStatus(r).detail,'hint'));
   const actions=node('div',undefined,'actions');const view=node('button',r.listening?'进入课堂':'查看','secondary');view.onclick=()=>openRecord(r.id);actions.append(view);
   const renameRecord=node('button','改名','secondary record-rename');renameRecord.setAttribute('aria-label','修改课堂标题：'+r.title);renameRecord.onclick=()=>editName('record',r.id,r.title);actions.append(renameRecord);
   if(!r.listening){const start=node('button',recordStartLabel(r,true),'text-button');const other=items.find(x=>x.listening&&x.id!==r.id);start.disabled=!!other;start.title=other?'请先暂停此课程正在监听的“'+other.title+'”':'';start.onclick=()=>action(start,()=>resumeRecord(r.id));actions.append(start);}
   line.append(details,actions);history.append(line);
  }
  block.append(history);rememberDisclosure(block,'course:'+c.id);$('courses').append(block);
 }
}
function rich(html,text,cls){const e=node('div',undefined,'markdown '+cls);if(typeof html==='string')e.innerHTML=html;else e.textContent=text||'';return e;}
function openImage(src,download){$('largeImage').src=src;$('imageDownload').href=download;$('imageDialog').showModal();}
function renderProblems(input){if(input)rows=input;const query=$('problemSearch').value.trim().toLowerCase();const fingerprint=JSON.stringify([rows,query,offset,currentRecord()?.listening]);if(fingerprint===problemFingerprint)return;problemFingerprint=fingerprint;const list=rows.filter(r=>JSON.stringify([r.payload?.body,r.payload?.course,r.answer?.answer,r.answer?.reasoning]).toLowerCase().includes(query));
 captureDisclosures($('problems'));$('problems').replaceChildren();$('count').textContent=query?`找到 ${list.length} 道题`:rows.length?`本页 ${rows.length} 道题`:'等待题目';$('prev').disabled=offset===0;$('next').disabled=rows.length<50;$('page').textContent=`第 ${offset/50+1} 页`;
 if(!list.length){const empty=node('div',undefined,'empty');empty.append(node('h2',query?'没有匹配的习题':offset?'这一页暂无记录':'本节还没有习题'),node('p',query?'换一个关键词试试。':currentRecord()?.listening?'正在等待老师推送新题。':'点击上方监听按钮，新题会保存到这份课堂记录。'));$('problems').append(empty);return;}
 for(const row of list){const p=row.payload||{},a=row.answer||{},d=row.display||{},card=node('article',undefined,'problem');const head=node('div',undefined,'problem-head');const heading=node('div');heading.append(node('div',p.course||'课堂','problem-course'),node('div',(types[p.type]||'习题')+' · '+(row.created?new Date(row.created*1000).toLocaleString():''),'problem-meta'));head.append(heading,node('span',states[row.status]||'未知状态','badge '+row.status));card.append(head);const content=node('div',undefined,'problem-content');
  const local=!!row.image;const src=local?'/api/problems/'+row.id+'/image':typeof p.cover==='string'&&p.cover.startsWith('https://')?p.cover:'';
  if(src){const wrap=node('div',undefined,'image-wrap');const b=node('button',undefined,'image-open');b.setAttribute('aria-label','查看原题大图');const img=node('img');img.src=src;img.alt='习题题面';img.loading='lazy';img.referrerPolicy='no-referrer';img.onerror=()=>{const warn=node('p','原图暂时无法加载；已过期链接可尝试重新作答或等待补抓。','error');if(!wrap.querySelector('.error'))wrap.append(warn);};b.append(img);const download=local?src+'?download=true':src;b.onclick=()=>openImage(src,download);wrap.append(b);const links=node('div',undefined,'image-actions');const zoom=node('button','查看大图');zoom.onclick=()=>openImage(src,download);links.append(zoom);const save=node('a',local?'下载原图':'打开原图');save.href=download;if(local)save.download='';else{save.target='_blank';save.rel='noopener noreferrer';}links.append(save,node('span',local?'已保存到本机':'原始图片链接'));wrap.append(links);content.append(wrap);}else content.append(node('div',p.body||'题面图片尚未就绪','problem-body'));
  // The original image already contains the choices. No duplicate option list.
  if(a.answer?.length||a.unanswerable||a.reasoning||a.confidence!==undefined){const block=node('section',undefined,'answer-block');const title=node('div',undefined,'answer-title');title.append(node('span','答案'));block.append(title,rich(d.answer_html,Array.isArray(a.answer)?a.answer.join('；'):a.answer||'无法作答','answer-markdown'));content.append(block);
   if(a.reasoning){const reason=node('section',undefined,'reason-block');reason.append(node('div','解析','reason-label'),rich(d.reasoning_html,a.reasoning,'reason-markdown'));content.append(reason);}
   const confidence=node('section',undefined,'confidence-block');confidence.append(node('span','可信度','reason-label'),node('span',d.confidence_label||d.confidence||'未提供（模型未返回自评）','confidence '+(typeof a.confidence==='number'&&a.confidence<.7?'low':'')));content.append(confidence);
  }
  if(row.error)content.append(node('p',row.error,'error'));
  if(a.repaired||a.structured===false){const details=node('details',undefined,'raw-output');details.append(node('summary',a.repaired?'已修复输出格式 · 查看原始内容':'未能识别答案格式 · 查看原始内容'),node('pre',a.raw||a.answer?.join('\n')||''));rememberDisclosure(details,'problem:'+row.id+':'+details.className);content.append(details);}
  const foot=node('div',undefined,'problem-footer');foot.append(node('span',(a.model||'')+(a.elapsed!=null?' · '+a.elapsed+' 秒':''),'hint'));if(currentRecord()?.listening&&!['processing','pending'].includes(row.status)){const retry=node('button','重新作答','secondary');retry.onclick=()=>action(retry,async()=>{await api(`problems/${row.id}/retry`,'POST');scheduleRefresh();});foot.append(retry);}else foot.append(node('span',states[row.status],'hint'));const preview=node('button','群消息预览','text-button');preview.onclick=()=>action(preview,async()=>{const data=await api(`problems/${row.id}/notification-preview`);$('previewReminder').className='message-preview markdown';$('previewResult').className='message-preview markdown';$('previewReminder').replaceChildren(rich(data.reminder_html,data.reminder,''));$('previewResult').replaceChildren(rich(data.result_html,data.result,''));$('previewImageCaption').textContent=data.image_caption||'';for(const id of ['previewReminder','previewResult']){const title=$(id).querySelector('strong');if(title)title.classList.add('preview-question-caption');}$('previewImageCaption').hidden=!data.image;$('previewImage').hidden=!data.image;if(data.image)$('previewImage').src=data.image;$('notificationDialog').showModal();});foot.append(preview);content.append(foot);
  if(p.limit>=0&&p.unlocked)content.append(node('p','答题截止：'+new Date((p.unlocked+p.limit)*1000).toLocaleString(),'hint'));
  if(row.deliveries?.length){const details=node('details',undefined,'delivery');details.append(node('summary','群通知记录'));for(const x of row.deliveries)details.append(node('p',`${names[x.channel]||x.channel} · ${x.phase==='reminder'?'新题提醒':'解答'}：${{sent:'已送达',failed:'失败',sending:'发送中'}[x.status]||x.status}${x.detail?' · '+x.detail:''}`));if(currentRecord()?.listening&&row.deliveries.some(x=>x.status==='failed')){const retry=node('button','重试失败通知','secondary');retry.onclick=()=>action(retry,async()=>{await api(`problems/${row.id}/notify`,'POST');scheduleRefresh();});details.append(retry);}rememberDisclosure(details,'problem:'+row.id+':'+details.className);content.append(details);}
  if(row.qq_deliveries?.length){const details=node('details',undefined,'delivery');details.append(node('summary','QQ 通知记录'));for(const x of row.qq_deliveries)details.append(node('p',`${x.group_name} · ${{reminder:'提醒',image:'图片',result:'解答'}[x.phase]||x.phase}：${{pending:'排队中',sending:'发送中',sent:'接口确认送达',failed:'失败',unknown:'结果未知',cancelled:'已取消'}[x.state]||x.state}${x.error?' · '+x.error:''}`));rememberDisclosure(details,'problem:'+row.id+':qq-delivery');content.append(details);}card.append(content);$('problems').append(card);
 }
}
async function refresh(){if(refreshing){refreshAgain=true;return;}refreshing=true;
 try{
  const results=await Promise.allSettled([api('courses'),api('records')]);
  if(results[0].status==='fulfilled')courses=results[0].value;else notice(results[0].reason.message);
  if(results[1].status==='fulfilled')records=results[1].value;else notice(results[1].reason.message);
  renderCourses();renderTabs();renderRecord();
  if(activeRecord){const requested=activeRecord;const data=await api('problems?record_id='+requested+'&offset='+offset);if(requested===activeRecord)renderProblems(data);}
 }catch(e){notice(e.message);}finally{refreshing=false;if(refreshAgain){refreshAgain=false;scheduleRefresh();}}
}
function scheduleRefresh(){clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,150);}
function markDirty(){dirty=true;$('saveState').classList.add('unsaved');$('saveHint').classList.add('unsaved');$('testAI').disabled=true;$('saveState').textContent='有未保存的更改';$('saveHint').textContent='切换页面保留输入，保存后才生效';}
$('configForm').oninput=markDirty;
$('configForm').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{
 const channels=[...document.querySelectorAll('.channel')].map(box=>{const result={kind:box.dataset.kind};for(const input of box.querySelectorAll('input'))result[input.dataset.key]=input.type==='checkbox'?input.checked:input.value;return result;});
 const patch={domain:$('domain').value,scan_interval:Number($('scan').value),ai:{base_url:$('baseUrl').value,model:$('model').value,api_key:$('apiKey').value,max_tokens:Number($('tokens').value),timeout:Number($('timeout').value),enabled:$('aiEnabled').checked},channels:[...channels,...(settings.channels||[]).filter(c=>c.kind==='qqbot')]};
 const oldDomain=settings.domain;fillConfig(await api('config','PUT',patch));if(oldDomain!==settings.domain){activeRecord=null;tabs=[];records=[];navigate('courses');}notice('配置已保存并应用。');scheduleRefresh();});};

$('login').onclick=()=>action($('login'),()=>api('login','POST'));
$('logout').onclick=()=>action($('logout'),()=>api('logout','POST'));
$('testAI').onclick=()=>action($('testAI'),async()=>{if(dirty){notice('请先保存设置，再测试新的 AI 配置。');return;}await api('ai/test','POST');});
$('prev').onclick=()=>{offset=Math.max(0,offset-50);scheduleRefresh();};$('next').onclick=()=>{offset+=50;scheduleRefresh();};
$('problemSearch').oninput=()=>renderProblems();$('courseSearch').oninput=()=>renderCourses();

$('closeNotification').onclick=()=>$('notificationDialog').close();
$('closeImage').onclick=()=>{$('imageDialog').close();$('largeImage').removeAttribute('src');};
$('addCourseToggle').onclick=()=>{const open=$('courseForm').hidden;$('courseForm').hidden=!open;$('addCourseToggle').setAttribute('aria-expanded',String(open));if(open)$('courseName').focus();};
$('cancelCourse').onclick=()=>{$('courseForm').hidden=true;$('addCourseToggle').setAttribute('aria-expanded','false');};
$('courseForm').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{await api('courses','POST',{name:$('courseName').value,classroom:$('classroomId').value});$('courseForm').reset();$('cancelCourse').click();notice('课程已添加，可以新开一节课。');await refresh();});};
$('cancelDelete').onclick=()=>$('deleteDialog').close();$('confirmDelete').onclick=()=>action($('confirmDelete'),async()=>{await api('courses/'+deleteId,'DELETE');$('deleteDialog').close();notice('课程规则已删除，历史习题和图片已保留。');await refresh();});
$('newTab').onclick=()=>newRecord();$('cancelNewRecord').onclick=()=>$('newRecordDialog').close();
$('newRecordForm').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{const row=await api('records','POST',{course_id:Number($('recordCourseSelect').value),title:$('newRecordTitle').value,listening:$('newRecordListen').checked});$('newRecordDialog').close();await refresh();openRecord(row.id);});};
$('cancelRename').onclick=()=>$('renameDialog').close();$('renameForm').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{await api((renameTarget.kind==='course'?'courses/':'records/')+renameTarget.id,renameTarget.kind==='course'?'PUT':'PATCH',renameTarget.kind==='course'?{display_name:$('renameInput').value}:{title:$('renameInput').value});$('renameDialog').close();await refresh();});};
$('closeCancel').onclick=()=>$('closeTabDialog').close();$('closeKeep').onclick=()=>{$('closeTabDialog').close();removeTab(closingTab);};$('closePause').onclick=()=>action($('closePause'),async()=>{await api(`records/${closingTab}/pause`,'POST');$('closeTabDialog').close();removeTab(closingTab);await refresh();});
async function restoreKey(){const request=++keyRequest;try{const result=await api('ai/key','POST');if(request!==keyRequest||$('apiKey').value)return;$('apiKey').value=result.api_key||'';$('apiKey').placeholder='请输入 API Key';if($('apiKey').value)loadModels();}catch(e){$('keyState').textContent=e.message;}}
$('toggleKey').onclick=()=>{const show=$('apiKey').type==='password';$('apiKey').type=show?'text':'password';$('toggleKey').classList.toggle('revealed',show);$('toggleKey').setAttribute('aria-label',show?'隐藏 API Key':'显示 API Key');$('toggleKey').setAttribute('aria-pressed',String(show));};
function renderModelChoices(){const current=$('model').value;const select=$('modelSelect');select.replaceChildren();for(const name of modelChoices){const o=node('option',name);o.value=name;select.append(o);}if(current&&!modelChoices.includes(current)){const o=node('option',current+'（当前值）');o.value=current;select.append(o);}const manual=node('option','手动填写其他模型…');manual.value='__manual__';select.append(manual);select.value=current||'__manual__';$('manualModel').hidden=!!current;}
$('modelSelect').onchange=()=>{const value=$('modelSelect').value;$('manualModel').hidden=value!=='__manual__';if(value!=='__manual__')$('model').value=value;else $('model').focus();markDirty();};
async function loadModels(){const request=++modelRequest;const base=$('baseUrl').value,key=$('apiKey').value;$('modelsState').textContent='正在获取模型列表…';$('fetchModels').disabled=true;
 try{const data=await api('ai/models','POST',{base_url:base,api_key:key});if(request!==modelRequest||base!==$('baseUrl').value||key!==$('apiKey').value)return;modelChoices=data.models||[];renderModelChoices();$('modelsState').textContent=`已获取 ${data.models.length} 个模型，可从下拉框直接选择。`+data.message;}
 catch(e){if(request===modelRequest)$('modelsState').textContent=e.message+'；仍可手动输入模型名称。';}
 finally{if(request===modelRequest)$('fetchModels').disabled=false;}
}
$('fetchModels').onclick=loadModels;
for(const id of ['baseUrl','apiKey'])$(id).addEventListener('input',()=>{if(id==='apiKey')keyRequest++;clearTimeout(modelTimer);modelRequest++;$('fetchModels').disabled=false;modelChoices=[];renderModelChoices();$('modelsState').textContent='连接信息已更改，等待获取模型列表。';if($('baseUrl').validity.valid&&($('apiKey').value||settings.ai?.api_key_configured&&$('baseUrl').value===settings.ai.base_url))modelTimer=setTimeout(loadModels,800);});
const initialHash=location.hash.slice(1);
fillConfig({});navigate('courses');
(async()=>{
 const results=await Promise.allSettled([api('config').then(fillConfig),api('status').then(renderStatus),refresh()]);for(const r of results)if(r.status==='rejected')notice(r.reason.message);
 try{tabs=JSON.parse(localStorage.getItem('class-tabs:'+status.domain)||'[]');if(!Array.isArray(tabs))tabs=[];}catch{tabs=[];}
 navigate(initialHash.startsWith('record-')?initialHash:initialHash==='settings'?'settings':'courses');renderTabs();
 const stream=new EventSource('/api/events');stream.onopen=()=>{$('connection').textContent='看板实时连接正常';};stream.onerror=()=>{$('connection').textContent='连接中断，正在重连…';};stream.onmessage=e=>{try{const event=JSON.parse(e.data);if(event.type==='status')renderStatus(event.data||{});if(['problem','resync','status'].includes(event.type))scheduleRefresh();}catch{notice('收到无法解析的更新，请刷新看板。');}};
})();
