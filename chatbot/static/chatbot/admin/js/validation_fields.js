(function(){
  "use strict";
  if(window._smValidationReady) return;
  window._smValidationReady=true;

  // ── Utility: monotonic index counter per container ──
  function nextIdx(container){
    var n=parseInt(container.getAttribute('data-next-idx')||'0',10);
    // Ensure we're above any existing indices
    container.querySelectorAll('.dyn-row input[name]').forEach(function(inp){
      var m=inp.name.match(/_(\d+)$/);
      if(m) n=Math.max(n,parseInt(m[1],10)+1);
    });
    container.setAttribute('data-next-idx',String(n+1));
    return n;
  }

  function findFieldRows(container, fieldNames){
    var rows=[];
    container.querySelectorAll('.form-group').forEach(function(g){
      for(var i=0;i<fieldNames.length;i++){
        if(g.classList.contains('field-'+fieldNames[i])){rows.push(g);break;}
      }
    });
    return rows;
  }

  // ── Dynamic rows: add choice ──
  document.addEventListener('click',function(e){
    var btn=e.target.closest('.dyn-add-choice-btn');
    if(!btn) return;
    e.preventDefault();
    var c=document.getElementById(btn.getAttribute('data-container'));
    if(!c) return;
    var prefix=c.getAttribute('data-prefix');
    var idx=nextIdx(c);
    var d=document.createElement('div');
    d.className='dyn-row';
    d.style.cssText='display:flex;gap:8px;margin-bottom:4px;align-items:center;';
    d.innerHTML=
      '<input type="text" name="'+prefix+'_label_'+idx+'" placeholder="Choice label" '+
      'style="flex:1;min-width:200px;" class="choice-label-input">'+
      '<button type="button" class="dyn-remove-btn" '+
      'style="color:#ba2121;background:none;border:none;font-size:16px;cursor:pointer;font-weight:bold;" '+
      'title="Remove">\u2715</button>';
    c.appendChild(d);
  });

  // ── Dynamic rows: add error ──
  document.addEventListener('click',function(e){
    var btn=e.target.closest('.dyn-add-error-btn');
    if(!btn) return;
    e.preventDefault();
    var c=document.getElementById(btn.getAttribute('data-container'));
    if(!c) return;
    var prefix=c.getAttribute('data-prefix');
    var idx=nextIdx(c);
    var d=document.createElement('div');
    d.className='dyn-row';
    d.style.cssText='display:flex;gap:8px;margin-bottom:4px;align-items:center;';
    d.innerHTML=
      '<input type="text" name="'+prefix+'_key_'+idx+'" placeholder="key (e.g. required)" style="width:140px;">'+
      '<input type="text" name="'+prefix+'_label_'+idx+'" placeholder="Error text (English)" style="flex:1;min-width:200px;">'+
      '<button type="button" class="dyn-remove-btn" '+
      'style="color:#ba2121;background:none;border:none;font-size:16px;cursor:pointer;font-weight:bold;" '+
      'title="Remove">\u2715</button>';
    c.appendChild(d);
  });

  // ── Dynamic rows: remove ──
  document.addEventListener('click',function(e){
    var btn=e.target.closest('.dyn-remove-btn');
    if(!btn) return;
    e.preventDefault();
    btn.closest('.dyn-row').remove();
    checkDuplicateLabels(btn.closest('.inline-related'));
  });

  // ── Duplicate label detection ──
  function checkDuplicateLabels(panel){
    if(!panel) return false;
    var addBtn=panel.querySelector('.dyn-add-choice-btn');
    if(!addBtn) return false;
    var prev=addBtn.parentElement.querySelector('.dup-error-banner');
    if(prev) prev.remove();
    var inputs=panel.querySelectorAll('.choice-label-input');
    inputs.forEach(function(inp){inp.style.borderColor='';});

    var seen={};
    var dupes=[];
    inputs.forEach(function(inp){
      var val=inp.value.trim().toLowerCase();
      if(!val) return;
      if(seen[val]){
        dupes.push(seen[val].value.trim());
        inp.style.borderColor='#ba2121';
        seen[val].style.borderColor='#ba2121';
      } else {
        seen[val]=inp;
      }
    });

    if(dupes.length>0){
      var banner=document.createElement('div');
      banner.className='dup-error-banner';
      banner.style.cssText='color:#ba2121;font-size:12px;margin-top:4px;margin-bottom:4px;';
      banner.textContent='Duplicate labels found: '+[...new Set(dupes)].join(', ')+'. Labels must be unique.';
      addBtn.parentElement.insertBefore(banner,addBtn);
      return true;
    }
    return false;
  }

  document.addEventListener('input',function(e){
    if(!e.target.classList.contains('choice-label-input')) return;
    checkDuplicateLabels(e.target.closest('.inline-related'));
  });

  // ── Visibility toggles ──
  var VALIDATION_FIELDS=['validate_method','validation_type','render_as',
                         'errors_field','min_choices','max_choices','choices_field'];
  var MC_FIELDS=['min_choices','max_choices','choices_field'];

  function initToggles(){
    document.querySelectorAll('.inline-related').forEach(function(panel){
      var opSel=panel.querySelector('select[name$="-operation_type"]');
      var vtSel=panel.querySelector('select[name$="-validation_type"]');
      if(!opSel) return;

      var validationRows=findFieldRows(panel, VALIDATION_FIELDS);

      function update(){
        var isNonLlm=opSel.value==='non_llm';
        var isMC=vtSel && vtSel.value==='MULTIPLE_CHOICE';
        validationRows.forEach(function(r){
          var isMcRow=false;
          for(var i=0;i<MC_FIELDS.length;i++){
            if(r.classList.contains('field-'+MC_FIELDS[i])){isMcRow=true;break;}
          }
          r.style.display=(isMcRow?(isNonLlm&&isMC):isNonLlm)?'':'none';
        });
      }

      update();
      if(!opSel.dataset.tb){opSel.dataset.tb='1';opSel.addEventListener('change',update);}
      if(vtSel&&!vtSel.dataset.tb){vtSel.dataset.tb='1';vtSel.addEventListener('change',update);}

      // ── Real-time min/max validation ──
      var minI=panel.querySelector('input[name$="-min_choices"]');
      var maxI=panel.querySelector('input[name$="-max_choices"]');
      if(minI&&maxI&&!minI.dataset.vb){
        minI.dataset.vb='1';
        function valMM(){
          var mn=parseInt(minI.value,10),mx=parseInt(maxI.value,10);
          var eid=maxI.id+'_mmerr';
          var ex=document.getElementById(eid);
          if(ex)ex.remove();
          if(!isNaN(mn)&&!isNaN(mx)&&mx<mn){
            var m=document.createElement('div');
            m.id=eid;m.style.cssText='color:#ba2121;font-size:12px;margin-top:2px;';
            m.textContent='Max choices must be \u2265 min choices.';
            maxI.parentElement.appendChild(m);
            maxI.style.borderColor='#ba2121';
          } else { maxI.style.borderColor=''; }
        }
        minI.addEventListener('input',valMM);
        maxI.addEventListener('input',valMM);
      }
    });
  }

  // ── Prevent form submit if duplicate labels ──
  document.addEventListener('submit',function(e){
    var form=e.target;
    var panels=form.querySelectorAll('.inline-related');
    var blocked=false;
    panels.forEach(function(p){
      if(checkDuplicateLabels(p)) blocked=true;
    });
    if(blocked){
      e.preventDefault();
      alert('Please fix duplicate choice labels before saving.');
    }
  });

  function addActionHints(){
    document.querySelectorAll('.inline_actions').forEach(function(row){
      if(row.querySelector('.action-hint')) return;
      var hint=document.createElement('div');
      hint.className='action-hint help-block';
      hint.textContent='Save your changes before generating translations or audio.';
      row.parentElement.insertBefore(hint, row.nextSibling);
    });
  }

  // ── Collapsible SM cards ──
  function makeCollapsible(){
    document.querySelectorAll('.inline-related:not(.empty-form)').forEach(function(panel){
      var header=panel.querySelector('.card-header');
      var body=panel.querySelector('.card-body');
      if(!header || !body || header.dataset.collapsible) return;
      header.dataset.collapsible='1';
      header.style.cursor='pointer';
      body.style.display='none';
      var arrow=document.createElement('span');
      arrow.className='collapse-arrow';
      arrow.style.cssText='margin-left:8px;font-size:10px;';
      arrow.textContent='\u25B6';
      var title=header.querySelector('.card-title');
      if(title) title.appendChild(arrow);
      header.addEventListener('click',function(e){
        if(e.target.closest('input,a,button')) return;
        var hidden=body.style.display==='none';
        body.style.display=hidden?'':'none';
        arrow.textContent=hidden?'\u25BC':'\u25B6';
      });
    });
  }

  function init(){
    initToggles();
    addActionHints();
    makeCollapsible();
  }

  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',init);
  } else {
    init();
  }

  var grp=document.querySelector('.inline-group');
  if(grp){
    new MutationObserver(function(){init();}).observe(grp,{childList:true,subtree:false});
  }
})();
