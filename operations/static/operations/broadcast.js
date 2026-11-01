(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const config = JSON.parse($('broadcast-config').textContent);
  let data, campaign = null, step = 0, page = 1, pages = 1, segmentEditing = null, busy = false, testRequest = null;
  const notice = text => { $('notice').textContent = text; $('notice').hidden = !text; };
  const csrf = () => document.cookie.split('; ').find(v => v.startsWith('csrftoken='))?.split('=').slice(1).join('=') || '';
  async function api(path, body) {
    const multipart = body instanceof FormData;
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials:'same-origin', headers: body === undefined ? {} : {...(multipart ? {} : {'Content-Type':'application/json'}),'X-CSRFToken':decodeURIComponent(csrf())}, ...(body === undefined ? {} : {body:multipart ? body : JSON.stringify(body)})});
    let value;
    try { value = await response.json(); } catch { throw new Error('ไม่สามารถโหลดข้อมูลได้ กรุณาลองใหม่อีกครั้ง'); }
    if (!response.ok) { const error = new Error(value.error || 'ไม่สามารถดำเนินการได้'); error.status = response.status; throw error; }
    return value;
  }
  async function run(fn) {
    if (busy) return;
    busy = true; notice('');
    document.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try { await fn(); } catch (error) { notice(error.message); $('notice').scrollIntoView({block:'nearest'}); }
    finally { busy = false; document.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
  }
  function node(tag, text, className) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (className) n.className = className; return n; }
  function options(id, rows) { $(id).replaceChildren(...rows.map(row => { const o = node('option', row.name); o.value = row.id; return o; })); }
  function section(name) { ['history','editor','details'].forEach(id => { $(id).hidden = id !== name; }); }
  const datetime = value => value ? new Date(value).toLocaleString('th-TH',{timeZone:'Asia/Bangkok',dateStyle:'medium',timeStyle:'short'}) : '—';
  function pairs(target, rows) { target.replaceChildren(...rows.flatMap(([label,value]) => [node('dt',label),node('dd',String(value))])); }
  function safeUrl(value) { try { const u = new URL(value); return u.protocol === 'https:' ? u.href : ''; } catch { return ''; } }
  function renderContent(target, content) {
    target.replaceChildren();
    if (content.url && safeUrl(content.url)) {
      const media = document.createElement(content.kind === 'video' ? 'video' : 'img');
      media.src = safeUrl(content.url);
      if (content.kind === 'video') { media.controls = true; media.preload = 'metadata'; if (safeUrl(content.preview)) media.poster = content.preview; }
      else media.alt = content.title || 'ภาพประกอบประกาศ';
      target.append(media);
    }
    if (content.title) target.append(node('h3', content.title));
    target.append(node('p', content.text || (content.kind === 'text' ? 'ข้อความประกาศของท่านจะแสดงที่นี่' : '')));
    const buttons = content.kind === 'click_image' ? [{label:'ดูรายละเอียด',url:content.link}] : content.buttons || [];
    buttons.forEach(button => { if (safeUrl(button.url)) { const a = node('a',button.label); a.href = button.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; target.append(a); } });
  }
  function content() {
    const c = {kind:$('kind').value,text:$('text').value};
    if (c.kind !== 'text') { c.url = $('media-url').value.trim(); if($('media-url').dataset.mediaId) c.media_id = $('media-url').dataset.mediaId; }
    if (c.kind === 'image' && $('media-url').dataset.previewUrl) c.preview = $('media-url').dataset.previewUrl;
    if (c.kind === 'video' && $('preview-url').dataset.mediaId) c.preview_id = $('preview-url').dataset.mediaId;
    if (c.kind === 'video') c.preview = $('preview-url').value.trim();
    if (['card','click_image'].includes(c.kind)) c.title = $('card-title').value;
    if (c.kind === 'click_image') c.link = $('link-url').value.trim();
    if (c.kind === 'card') c.buttons = [...$('button-rows').children].map(row => ({label:row.querySelector('[data-label]').value,url:row.querySelector('[data-url]').value.trim()}));
    return c;
  }
  function addCardButton(button = {label:'',url:''}) {
    if ($('button-rows').children.length >= 3) return;
    const row = node('div',undefined,'card-button-row');
    const label = node('label','ข้อความบนปุ่ม'), field = document.createElement('input');
    field.maxLength = 40; field.dataset.label = ''; field.value = button.label; label.append(field);
    const urlLabel = node('label','ลิงก์ปลายทาง (HTTPS)'), url = document.createElement('input');
    url.type = 'url'; url.dataset.url = ''; url.value = button.url; url.placeholder = 'https://'; urlLabel.append(url);
    const remove = node('button','ลบปุ่มนี้','text-button'); remove.type = 'button';
    remove.onclick = () => { if ($('button-rows').children.length > 1) { row.remove(); refreshFields(); } else notice('การ์ดต้องมีอย่างน้อยหนึ่งปุ่ม'); };
    row.append(label,urlLabel,remove); $('button-rows').append(row);
  }
  function refreshFields() {
    const kind = $('kind').value;
    $('media-upload').accept = kind === 'video' ? 'video/mp4' : 'image/jpeg,image/png';
    $('media-fields').hidden = kind === 'text'; $('video-fields').hidden = kind !== 'video';
    $('card-fields').hidden = !['card','click_image'].includes(kind); $('card-buttons').hidden = kind !== 'card'; $('click-link-field').hidden = kind !== 'click_image';
    $('segment-choice').hidden = document.querySelector('[name=audience]:checked').value !== 'segment';
    $('schedule-field').hidden = document.querySelector('[name=timing]:checked').value !== 'later';
    renderContent($('preview-message'),content());
  }
  function showStep(value) {
    step = value;
    document.querySelectorAll('[data-step]').forEach(el => { el.hidden = Number(el.dataset.step) !== step; });
    document.querySelectorAll('.steps li').forEach((el,i) => { el.classList.toggle('current',i === step); if(i === step) el.setAttribute('aria-current','step'); else el.removeAttribute('aria-current'); });
    $('previous-step').hidden = step === 0; $('next-step').hidden = step === 3; $('confirm').hidden = step !== 3;
    $('confirm-check').checked = false;
  }
  function start(row = null) {
    ['media','preview'].forEach(key => { $(key+'-url').dataset.mediaId = ''; $(key+'-url').dataset.previewUrl = ''; $(key+'-upload-status').textContent = ''; $(key+'-check').hidden = true; });
    campaign = row; testRequest = null; $('test-status').textContent = ''; $('composer').reset();
    const c = row?.content || {kind:'text',text:''};
    $('title').value = row?.title || ''; $('kind').value = c.kind; $('text').value = c.text || '';
    $('media-url').dataset.mediaId = c.media_id || ''; $('preview-url').dataset.mediaId = c.preview_id || ''; $('media-url').dataset.previewUrl = c.preview || '';
    $('media-url').value = c.url || ''; $('preview-url').value = c.preview || ''; $('card-title').value = c.title || '';
    $('link-url').value = c.link || ''; $('button-rows').replaceChildren(); (c.buttons || [{label:'ดูรายละเอียด',url:''}]).forEach(addCardButton);
    document.querySelector(`[name=audience][value=${row?.audience || 'all'}]`).checked = true;
    if (row?.segment_id) $('segment').value = row.segment_id;
    if (row?.scheduled_at) {
      document.querySelector('[name=timing][value=later]').checked = true;
      const local = new Date(new Date(row.scheduled_at).getTime() + 7*3600000).toISOString().slice(0,16);
      $('scheduled-at').value = local;
    }
    $('editor-heading').textContent = row ? 'แก้ไขประกาศ' : 'สร้างประกาศ';
    showStep(0); refreshFields(); section('editor');
  }
  async function save() {
    const selected = document.querySelector('[name=audience]:checked').value;
    if (selected === 'segment' && !$('segment').value) throw new Error('กรุณาเลือกหรือสร้างกลุ่มผู้รับ');
    const result = await api('/broadcasts/api/campaigns/', {id:campaign?.id,revision:campaign?.revision,title:$('title').value,content:content(),audience:selected,segment_id:selected === 'segment' ? Number($('segment').value) : null});
    campaign = result.campaign;
  }
  async function review() {
    await save();
    const preview = await api(`/broadcasts/api/campaigns/${campaign.id}/preview/`,{revision:campaign.revision});
    const later = document.querySelector('[name=timing]:checked').value === 'later';
    if (later && (!$('scheduled-at').value || new Date($('scheduled-at').value + ':00+07:00') <= new Date())) throw new Error('กรุณาระบุเวลาส่งในอนาคต');
    pairs($('review'),[['ชื่อประกาศ',campaign.title],['กลุ่มผู้รับ',campaign.audience === 'all' ? 'เพื่อนทั้งหมด' : $('segment').selectedOptions[0]?.textContent],['จำนวนโดยประมาณ',preview.recipient_count === null ? 'LINE จะตรวจจำนวนเพื่อนทั้งหมดก่อนส่ง' : `${preview.recipient_count.toLocaleString('th-TH')} คน (รวมผู้สร้าง)`],['เวลาส่ง',later ? datetime($('scheduled-at').value + ':00+07:00') : 'ทันทีหลังยืนยัน']]);
    $('confirm').textContent = later ? 'ยืนยันตั้งเวลาส่ง' : 'ยืนยันส่งประกาศ';
    showStep(3);
  }
  async function history() {
    const result = await api(`/broadcasts/api/campaigns/?page=${page}&status=${encodeURIComponent($('filter').value)}`);
    page = result.page; pages = result.pages; $('page-number').textContent = `${page} / ${pages}`;
    $('items').replaceChildren();
    if (!result.items.length) $('items').append(node('p','ยังไม่มีประกาศในรายการนี้ เริ่มต้นได้จากปุ่มสร้างประกาศ','panel'));
    result.items.forEach(row => {
      const item = node('article',undefined,'item'), info = node('div');
      info.append(node('span',(row.is_test ? 'ทดสอบ · ' : '') + row.status_label,'badge'),node('h3',row.title),node('p',`${row.creator} · ${datetime(row.created_at)}`,'muted'));
      const button = node('button','รายละเอียด','secondary'); button.type = 'button'; button.onclick = () => run(() => details(row.id));
      item.append(info,button); $('items').append(item);
    }); section('history');
  }
  async function details(id) {
    const row = (await api(`/broadcasts/api/campaigns/${id}/`)).campaign;
    $('detail-title').textContent = (row.is_test ? 'ทดสอบ · ' : '') + row.title;
    pairs($('detail-data'),[['สถานะ',row.status_label],['ผู้สร้าง',row.creator],['เวลาส่ง',datetime(row.scheduled_at)],['ผู้รับ',row.recipient_count ?? 'รอตรวจสอบ'],['รายละเอียด',row.detail || '—']]);
    renderContent($('detail-content'),row.content); $('detail-actions').replaceChildren();
    if (row.may_edit) {
      const edit = node('button','เริ่มแก้ไข'); edit.onclick = () => run(async () => { const result = await api(`/broadcasts/api/campaigns/${id}/edit/`,{revision:row.revision}); start(result.campaign); notice('พักการส่งแล้ว กรุณายืนยันใหม่หลังแก้ไขเสร็จ'); });
      const cancel = node('button','ยกเลิกประกาศ','secondary'); cancel.onclick = () => run(async () => { if (!window.confirm('ยืนยันยกเลิกประกาศนี้หรือไม่')) return; await api(`/broadcasts/api/campaigns/${id}/cancel/`,{revision:row.revision}); await details(id); });
      $('detail-actions').append(edit,cancel);
    }
    const copy = node('button','คัดลอกเป็นร่างใหม่','secondary'); copy.onclick = () => run(async () => { const result = await api(`/broadcasts/api/campaigns/${id}/copy/`,{}); start(result.campaign); }); $('detail-actions').append(copy);
    section('details');
  }
  async function bootstrap() {
    data = await api('/broadcasts/api/bootstrap/'); $('account').textContent = data.user;
    options('segment',data.segments); options('segment-divisions',data.divisions); options('segment-districts',data.districts); options('segment-roles',data.roles);
    $('quota').textContent = Math.max(0,data.quota.budget-data.quota.used).toLocaleString('th-TH') + ' ข้อความ';
    $('login').hidden = true; $('workspace').hidden = false;
  }
  async function lineLogin() {
    if (!config.liff_id) throw new Error('ยังไม่ได้ตั้งค่า LINE Mini App กรุณาเข้าสู่ระบบด้วยบัญชีเจ้าหน้าที่');
    if (!window.liff) throw new Error('ไม่สามารถโหลด LINE ได้ กรุณาลองใหม่');
    await window.liff.init({liffId:config.liff_id});
    if (!window.liff.isLoggedIn()) { window.liff.login({redirectUri:window.location.href}); return; }
    await api('/broadcasts/auth/line/',{id_token:window.liff.getIDToken()});
    await bootstrap(); if(config.campaign_id) await details(config.campaign_id); else await history();
  }
  $('line-login').onclick = () => run(lineLogin);
  $('new').onclick = () => start();
  $('back-history').onclick = $('detail-back').onclick = () => run(history);
  $('filter').onchange = () => run(async () => {page = 1; await history();});
  $('previous-page').onclick = () => run(async () => {page = Math.max(1,page-1); await history();});
  $('next-page').onclick = () => run(async () => {page = Math.min(pages,page+1); await history();});
  async function checkMedia(key) {
    const field = $(key+'-url'), id = field.dataset.mediaId;
    if(!id) return;
    $(key+'-check').hidden = true;
    for(let attempt=0; attempt<30; attempt++) {
      let row;
      try { row = (await api(`/broadcasts/api/media/${id}/`)).media; }
      catch(error) { $(key+'-check').hidden = false; throw error; }
      if(field.dataset.mediaId !== id) return;
      $(key+'-upload-status').textContent = row.detail || row.status_label;
      if(row.status === 'clean') {
        field.value = key === 'preview' ? row.preview : row.url;
        field.dataset.previewUrl = row.preview || '';
        refreshFields(); return;
      }
      if(row.status === 'rejected') { field.value = ''; throw new Error(row.detail || 'ไฟล์ไม่ผ่านการตรวจ กรุณาเลือกไฟล์ใหม่'); }
      await new Promise(resolve => setTimeout(resolve,2000));
    }
    $(key+'-check').hidden = false;
    $(key+'-upload-status').textContent = 'ไฟล์ยังอยู่ระหว่างตรวจสอบ ท่านสามารถตรวจสถานะอีกครั้งได้';
  }
  ['media','preview'].forEach(key => {
    $(key+'-check').onclick = () => run(() => checkMedia(key));
    $(key+'-upload').onchange = () => run(async () => {
      const file = $(key+'-upload').files[0]; if(!file) return;
      const limit = key === 'media' && $('kind').value === 'video' ? 200000000 : 10000000;
      if(file.size > limit) throw new Error('ไฟล์มีขนาดเกินที่กำหนด กรุณาเลือกไฟล์ใหม่');
      $(key+'-upload-status').textContent = 'กำลังอัปโหลดไฟล์ กรุณารอสักครู่';
      const form = new FormData(); form.append('file',file);
      const row = (await api('/broadcasts/api/media/',form)).media;
      $(key+'-url').dataset.mediaId = row.id;
      $(key+'-url').dataset.previewUrl = '';
      $(key+'-url').value = '';
      await checkMedia(key);
    });
  });
  $('add-card-button').onclick = () => {addCardButton(); refreshFields();};
  $('composer').onsubmit = event => event.preventDefault();
  $('composer').oninput = event => { if(['media-url','preview-url'].includes(event.target.id)) { event.target.dataset.mediaId = ''; event.target.dataset.previewUrl = ''; } refreshFields(); };
  $('composer').onchange = refreshFields;
  $('previous-step').onclick = () => showStep(Math.max(0,step-1));
  $('next-step').onclick = () => run(async () => { if(step === 2) await review(); else showStep(step+1); });
  $('save').onclick = () => run(async () => { await save(); notice('บันทึกร่างเรียบร้อยแล้ว'); });
  $('test-self').onclick = () => run(async () => {
    // Keep this key after a lost response; retrying must not enqueue a second test.
    if (!testRequest) { await save(); testRequest = {id:campaign.id,revision:campaign.revision,key:crypto.randomUUID()}; }
    let result;
    try { result = await api(`/broadcasts/api/campaigns/${testRequest.id}/test/`,{revision:testRequest.revision,request_key:testRequest.key}); }
    catch (error) {
      if (error.status >= 400 && error.status < 500) testRequest = null;
      else $('test-status').textContent = 'ยังไม่ทราบผลการส่งทดสอบ กดส่งทดสอบอีกครั้งเพื่อตรวจคำขอเดิมก่อนส่งข้อความที่แก้ไขใหม่';
      throw error;
    }
    $('test-status').replaceChildren(node('span','รับคำขอส่งทดสอบถึงบัญชี LINE ของท่านแล้ว '));
    const link = node('a','ตรวจสอบสถานะ'); link.href = `/broadcasts/${result.campaign.id}/`; link.target = '_blank'; link.rel = 'noopener'; $('test-status').append(link);
    testRequest = null;
  });
  $('confirm').onclick = () => run(async () => {
    if (!$('confirm-check').checked) throw new Error('กรุณายืนยันว่าท่านได้ตรวจสอบข้อความและกลุ่มผู้รับแล้ว');
    const later = document.querySelector('[name=timing]:checked').value === 'later';
    await api(`/broadcasts/api/campaigns/${campaign.id}/confirm/`,{revision:campaign.revision,scheduled_at:later ? $('scheduled-at').value + ':00+07:00' : null});
    await details(campaign.id); notice(later ? 'ตั้งเวลาส่งเรียบร้อยแล้ว' : 'รับคำขอส่งประกาศแล้ว ท่านสามารถตรวจสอบสถานะได้ในรายการนี้');
  });
  function openSegment(row) {
    segmentEditing = row || null; $('segment-form').reset(); $('segment-error').textContent = '';
    if(row) { $('segment-name').value = row.name; ['divisions','districts','roles'].forEach(key => [...$('segment-'+key).options].forEach(option => {option.selected = (row.criteria[key] || []).map(String).includes(option.value);})); }
    $('segment-dialog').showModal();
  }
  $('open-segment').onclick = () => openSegment();
  $('edit-segment').onclick = () => { const row = data.segments.find(s => s.id === Number($('segment').value)); if(!row?.may_edit) {notice('ท่านไม่มีสิทธิ์แก้ไขกลุ่มนี้ กรุณาสร้างกลุ่มใหม่');return;} openSegment(row); };
  $('close-segment').onclick = () => $('segment-dialog').close();
  $('segment-form').onsubmit = event => {
    event.preventDefault();
    run(async () => {
      const criteria = {}; ['divisions','districts','roles'].forEach(key => {const values = [...$('segment-'+key).selectedOptions].map(o => key === 'roles' ? o.value : Number(o.value)); if(values.length) criteria[key] = values;});
      try { const result = await api('/broadcasts/api/segments/',{id:segmentEditing?.id,revision:segmentEditing?.revision,name:$('segment-name').value,criteria});
        data.segments = data.segments.filter(s => s.id !== result.segment.id); data.segments.push(result.segment); options('segment',data.segments); $('segment').value = result.segment.id; $('segment-dialog').close();
      } catch(error) { $('segment-error').textContent = error.message; }
    });
  };
  (async () => {
    try { await bootstrap(); if(config.campaign_id) await details(config.campaign_id); else await history(); }
    catch { if(config.liff_id && window.liff) await run(lineLogin); else $('login-help').textContent = 'กรุณาเข้าสู่ระบบด้วยบัญชีแอดมินกองหรือแอดมินฝ่ายเพื่อดำเนินการ'; }
  })();
})();
