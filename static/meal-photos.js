"use strict";

window.MealPhotos=(()=>{
  let active=null;
  function dispose(){if(active?.url)URL.revokeObjectURL(active.url);active=null;}
  function file(){return active?.file||null;}
  function mount(root,enabled,onChange){
    dispose();if(!root)return;
    const s={root,file:null,url:null};active=s;
    root.innerHTML=`<div class="photo-actions"><button type="button" class="photo-pick">${icon('image-plus')}选择照片</button><button type="button" class="photo-camera">${icon('camera')}拍照</button><button type="button" class="icon-button photo-remove" aria-label="移除照片" title="移除照片" hidden>${icon('x')}</button></div>
      <input class="photo-file" type="file" accept="image/jpeg,image/png,image/webp" hidden><input class="photo-capture" type="file" accept="image/*" capture="environment" hidden>
      <img class="photo-preview" alt="待识别的食物照片" hidden><p class="photo-message small" role="status"></p>
      <p class="small muted">${enabled?'点击识别后，照片与补充说明会发往阿里云，并计入 AI 使用次数。':'照片识别未启用，仍可手动填写。'} 原图不留存；识别结果需核对食物和实际食用份量。</p>`;
    root.querySelector('.photo-pick').onclick=()=>root.querySelector('.photo-file').click();
    root.querySelector('.photo-camera').onclick=()=>root.querySelector('.photo-capture').click();
    function clear(){if(s.url)URL.revokeObjectURL(s.url);s.url=null;s.file=null;const preview=root.querySelector('img');preview.removeAttribute('src');preview.hidden=true;root.querySelector('.photo-remove').hidden=true;}
    root.querySelectorAll('input[type=file]').forEach(input=>input.onchange=()=>{
      const chosen=input.files[0];input.value='';if(!chosen)return;
      clear();const message=root.querySelector('.photo-message');
      if(!['image/jpeg','image/png','image/webp'].includes(chosen.type))message.textContent='请选择 JPEG、PNG 或 WebP；HEIC 请先转换格式';
      else if(!chosen.size||chosen.size>10*1024*1024)message.textContent='照片为空或超过10MB，请换一张较小的照片';
      else {s.file=chosen;s.url=URL.createObjectURL(chosen);const preview=root.querySelector('img');preview.src=s.url;preview.hidden=false;root.querySelector('.photo-remove').hidden=false;message.textContent='照片已选，尚未上传';}
      onChange();
    });
    root.querySelector('.photo-remove').onclick=()=>{clear();root.querySelector('.photo-message').textContent='照片已移除';onChange();};
  }
  return {mount,file,dispose};
})();
