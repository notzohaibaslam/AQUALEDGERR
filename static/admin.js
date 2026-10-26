'use strict';
const $=x=>document.querySelector(x);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token=sessionStorage.getItem('aqua_platform_token');
let plants=[], admin=null;
let activityPage=false;
async function call(path,options={}){
 const headers={'Content-Type':'application/json',...(token?{'Authorization':'Bearer '+token}:{})};
 let response;
 try{response=await fetch('/api/platform'+path,{...options,headers:{...headers,...options.headers}});}
 catch{throw Error('Cannot contact AquaLedger server.');}
 if(response.status===401&&path!=='/login'){signout();throw Error('Administrator session expired.');}
 if(!response.ok){let data=await response.json().catch(()=>({}));throw Error(typeof data.detail==='string'?data.detail:response.status===401?'Login was rejected outside the normal AquaLedger credential response. Refresh tunnel authentication and check the server logs.':'Request failed ('+response.status+')');}
 return response.json();
}
function notify(message,error=false){const t=$('#toast');t.textContent=message;t.classList.toggle('error',error);t.hidden=false;clearTimeout(notify.timer);notify.timer=setTimeout(()=>t.hidden=true,4500);}
function signout(){sessionStorage.removeItem('aqua_platform_token');token=null;admin=null;$('#console').hidden=true;$('#signin').hidden=false;}
$('#logout').onclick=signout;
$('#change-master').onclick=()=>{
 openDialog('Change master password',`<form id="master-form" class="fields dialog-content">
 ${formInput('current_password','Current master password','password',{required:true})}
 ${formInput('new_password','New master password (12+ characters recommended)','password',{required:true,minlength:12})}
 <div class="form-footer"><button type="button" class="secondary" id="cancel-master">Cancel</button><button class="primary">Update password</button></div></form>`);
 $('#cancel-master').onclick=()=>$('#dialog').close();
 $('#master-form').onsubmit=async e=>{e.preventDefault();try{const d=Object.fromEntries(new FormData(e.target));await call('/change-password',{method:'POST',body:JSON.stringify(d)});$('#dialog').close();signout();notify('Master password updated. Sign in again.');}catch(ex){notify(ex.message,true);}};
};
$('#login-form').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget;let b=$('#login-btn');b.disabled=true;try{const data=Object.fromEntries(new FormData(form));const result=await call('/login',{method:'POST',body:JSON.stringify(data)});token=result.token;sessionStorage.setItem('aqua_platform_token',token);form.reset();await openConsole();}catch(ex){notify(ex.message,true);}finally{b.disabled=false;}};
async function openConsole(){try{admin=await call('/me');$('#admin-name').textContent=admin.name;$('#signin').hidden=true;$('#console').hidden=false;await refresh();}catch(ex){signout();notify(ex.message,true);}}
const count=(x)=>Number(x||0).toLocaleString('en-PK');
async function refresh(){plants=await call('/plants');renderPlants();}
function renderPlants(){
 const active=plants.filter(p=>p.active).length;
 $('#stats').innerHTML=[['Total plants',plants.length],['Active clients',active],['Suspended',plants.length-active],['Registered customers',plants.reduce((a,p)=>a+p.customer_count,0)]].map(([a,b])=>`<div class="stat"><small>${esc(a)}</small><strong>${count(b)}</strong></div>`).join('');
 const query=$('#search').value.trim().toLowerCase();const visible=plants.filter(p=>[p.name,p.owner_name,p.owner_username,p.phone].join(' ').toLowerCase().includes(query));
 $('#plants-table').innerHTML=visible.length?`<table><thead><tr><th>Water plant</th><th>Plant owner</th><th>Customers / staff</th><th>Status</th><th>Administration</th></tr></thead><tbody>${visible.map(p=>`<tr><td><strong>${esc(p.name)}</strong><small>ID #${p.id} · ${esc(p.phone||'No phone')}</small></td><td>${esc(p.owner_name||'—')}<small>@${esc(p.owner_username||'—')} · ${esc(p.owner_email||'No email')}</small></td><td>${count(p.customer_count)} customers<small>${count(p.user_count)} user accounts · ${count(p.delivery_count)} deliveries</small></td><td><span class="pill ${p.active?'':'inactive'}">${p.active?'Active':'Suspended'}</span></td><td><div class="actions"><button type="button" class="secondary" data-reset="${p.id}">Reset owner password</button><button type="button" class="${p.active?'danger':'secondary'}" data-toggle="${p.id}">${p.active?'Suspend':'Reactivate'}</button></div></td></tr>`).join('')}</tbody></table>`:'<p class="sub">No water plants match your search.</p>';
 $('#plants-table').querySelectorAll('[data-reset]').forEach(b=>b.onclick=()=>resetOwner(Number(b.dataset.reset)));
 $('#plants-table').querySelectorAll('[data-toggle]').forEach(b=>b.onclick=()=>togglePlant(Number(b.dataset.toggle)));
}
$('#search').oninput=renderPlants;
function openDialog(title,content){$('#dialog-title').textContent=title;$('#dialog-body').innerHTML=content;$('#dialog').showModal();}
$('#dialog-close').onclick=()=>$('#dialog').close();
function formInput(name,label,type='text',opts={}){return `<label class="${opts.wide?'span2':''}">${esc(label)} ${opts.required?'<em aria-label="required">*</em>':(opts.optional?' <span class="hint">(optional)</span>':'')}<input name="${esc(name)}" type="${type}" ${opts.required?'required':''} ${opts.minlength?'minlength="'+opts.minlength+'"':''} maxlength="${opts.maxlength||150}" ${opts.placeholder?'placeholder="'+esc(opts.placeholder)+'"':''} ${opts.autocomplete?'autocomplete="'+esc(opts.autocomplete)+'"':''}></label>`;}
$('#new-plant').onclick=()=>{
 const markup=`<form id="create-plant" class="fields dialog-content"><div class="form-grid">
 ${formInput('plant','Plant / business name','text',{required:true,wide:true,maxlength:100,placeholder:'Saim Pure Water'})}
 ${formInput('name','Owner name','text',{required:true,placeholder:'Saim',maxlength:80})}
 ${formInput('username','Owner Login ID','text',{required:true,minlength:3,maxlength:40,placeholder:'saimwater'})}
 ${formInput('email','Email address','email',{optional:true,placeholder:'Optional'})}
 ${formInput('phone','Plant contact number','tel',{optional:true,placeholder:'+923001234567',maxlength:40})}
 ${formInput('password','Temporary owner password (8+ characters)','password',{required:true,wide:true,minlength:8,maxlength:200})}
 ${formInput('address','Plant address','text',{optional:true,wide:true,maxlength:240})}
 </div><p class="hint">Plant account creation is only available here. The owner can later add manager and driver accounts but cannot register additional plants. Give the password directly to the owner and ask them to change it after signing in.</p><div class="form-footer"><button class="secondary" type="button" id="cancel-create">Cancel</button><button class="primary" id="submit-create">Create water plant</button></div></form>`;
 openDialog('Create a new water plant',markup);$('#cancel-create').onclick=()=>$('#dialog').close();
 $('#create-plant').onsubmit=async e=>{e.preventDefault();const b=$('#submit-create');b.disabled=true;try{const data=Object.fromEntries(new FormData(e.target));if(!data.email)data.email=null;const item=await call('/plants',{method:'POST',body:JSON.stringify(data)});$('#dialog').close();await refresh();notify(`Created ${item.name}. Owner login: ${item.owner_username}`);}catch(ex){notify(ex.message,true);}finally{b.disabled=false;}};
};
function resetOwner(pid){const p=plants.find(x=>x.id===pid);if(!p)return;openDialog('Reset owner password',`<form id="reset-form" class="fields dialog-content"><p class="sub">Plant: <strong>${esc(p.name)}</strong><br>Owner: <strong>${esc(p.owner_name||'—')}</strong> (@${esc(p.owner_username||'—')})</p>${formInput('password','New owner password (8+ characters)','password',{required:true,minlength:8,maxlength:200})}<p class="hint">This action signs out the owner's existing sessions. It does not change staff passwords.</p><div class="form-footer"><button class="secondary" type="button" id="cancel-reset">Cancel</button><button class="primary">Save new password</button></div></form>`);$('#cancel-reset').onclick=()=>$('#dialog').close();$('#reset-form').onsubmit=async e=>{e.preventDefault();const password=new FormData(e.target).get('password');try{await call(`/plants/${pid}/owner-password`,{method:'PUT',body:JSON.stringify({password})});$('#dialog').close();notify('Owner password changed and old sessions revoked.');}catch(ex){notify(ex.message,true);}};}
async function togglePlant(pid){const p=plants.find(x=>x.id===pid);if(!p)return;const active=!p.active;if(!confirm(`${active?'Reactivate':'Suspend'} ${p.name}? ${active?'Users can sign in again.':'All of its users will lose access until reactivated.'}`))return;try{await call(`/plants/${pid}/status`,{method:'PUT',body:JSON.stringify({active})});await refresh();notify(`Plant ${active?'reactivated':'suspended'}.`);}catch(ex){notify(ex.message,true);}}
function setPage(activity){activityPage=activity;$('#nav-plants').classList.toggle('active',!activity);$('#nav-activity').classList.toggle('active',activity);$('#page-title').textContent=activity?'Administrator activity':'Client plants';$('#page-sub').textContent=activity?'Track account provisioning, suspensions and password resets.':'Create and manage independent water plant workspaces.';$('#breadcrumb').textContent=activity?'Activity':'Plants';$('#new-plant').hidden=activity;$('#stats').hidden=activity;$('#plants-section').hidden=activity;$('#activity-section').hidden=!activity;}
$('#nav-plants').onclick=()=>{setPage(false);refresh().catch(e=>notify(e.message,true));};
$('#nav-activity').onclick=async()=>{setPage(true);try{const rows=await call('/audit');$('#activity-list').innerHTML=`<table><thead><tr><th>Date</th><th>Action</th><th>Plant</th><th>By</th></tr></thead><tbody>${rows.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${esc(x.action.replaceAll('_',' '))}</td><td>${esc(x.plant_name||'—')}</td><td>${esc(x.by_admin||'—')}</td></tr>`).join('')}</tbody></table>`;}catch(ex){notify(ex.message,true);}};
if(token)openConsole();
