// Merge a snapshot without replacing student drafts or duplicating imported notes.
export function mergeStudyImport(data,p,unitId){
 const next=structuredClone(data);let added=0;
 for(const item of p.materials){
  const existing=next.materials.find(m=>m.id===item.id);
  if(!existing){next.materials.push({...item,folder:unitId,kind:item.kind||'text',body:item.body||'',date:item.date||new Date().toISOString(),attachments:[],demo:false,importTags:p.importTag?[p.importTag]:[]});added++;continue}
  if(!p.updateExisting||existing.folder!==unitId||p.importTag&&existing.importTags?.includes(p.importTag))continue;
  for(const key of ['section','author','work','citation','week','weeks','weekBasis','notesSource','source'])if(item[key]!==undefined)existing[key]=item[key];
  if(item.body)existing.readingSummary=item.body;
  if(item.userNotes&&!(existing.userNotes||'').includes(item.userNotes))existing.userNotes=existing.userNotes?existing.userNotes+'\n\n────────\nИмпортированные заметки\n\n'+item.userNotes:item.userNotes;
  existing.importTags=[...(existing.importTags||[]),...(p.importTag?[p.importTag]:[])];
 }
 for(const key of ['events','deadlines']){
  next[key] ||= [];
  for(const item of p[key]){const i=next[key].findIndex(x=>x.id===item.id);if(i<0)next[key].push({...item,unit:unitId,...(key==='deadlines'?{done:!!item.done}:{})});else if(p.updateExisting&&next[key][i].unit===unitId)next[key][i]={...next[key][i],...item,unit:unitId,...(key==='deadlines'?{done:!!next[key][i].done}:{})}}
 }
 for(const item of p.essays){
  const existing=next.essays[item.id];
  if(!existing){next.essays[item.id]={...item,unit:unitId,plan:item.plan||'',research:item.research||'',draft:'',sources:item.sources||item.source||'',brief:item.brief||'',updated:new Date().toISOString(),importTags:p.importTag?[p.importTag]:[]};continue}
  if(!p.updateExisting||existing.unit!==unitId||p.importTag&&existing.importTags?.includes(p.importTag))continue;
  for(const key of ['title','type','brief','source'])if(typeof item[key]==='string')existing[key]=item[key];
  for(const key of ['plan','research','sources'])if(item[key]&&!(existing[key]||'').includes(item[key]))existing[key]=existing[key]?existing[key]+'\n\n────────\nИмпортированные записи\n\n'+item[key]:item[key];
  existing.updated=new Date().toISOString();existing.importTags=[...(existing.importTags||[]),...(p.importTag?[p.importTag]:[])];
 }
 const unit=next.folders.find(f=>f.id===unitId);if(p.unit.moodleUrl)unit.moodleUrl=p.unit.moodleUrl;if(typeof p.unit.notes==='string')unit.importNotes=p.unit.notes;if(p.checkedAt)unit.importedAt=p.checkedAt;
 return {next,added};
}
