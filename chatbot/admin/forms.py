from django import forms
from django.core.exceptions import ValidationError
from django.utils.safestring import mark_safe

from chatbot.models.company_models import CompanyStateMachine


class ChoicesWidget(forms.Widget):
    """
    Renders a repeatable list of label inputs for multiple choice options.
    Key is auto-derived from label (lowercased).
    """

    def render(self, name, value, attrs=None, renderer=None):
        rows = value or []
        widget_id = attrs.get("id", f"id_{name}") if attrs else f"id_{name}"
        container_id = f"container-{widget_id}"
        # Derive inline-form-scoped input prefix
        input_prefix = self._input_prefix(widget_id)

        html = [f'<div id="{container_id}" class="dyn-rows-container" data-prefix="{input_prefix}">']
        for idx, row in enumerate(rows):
            html.append(self._render_row(input_prefix, idx, row.get("label", "")))
        html.append("</div>")
        html.append(
            f'<button type="button" class="dyn-add-choice-btn" data-container="{container_id}" '
            f'style="margin-top:4px;cursor:pointer;background:#417690;color:#fff;border:none;'
            f'padding:4px 12px;border-radius:3px;">+ Add Choice</button>'
        )
        return mark_safe("\n".join(html))

    def _render_row(self, input_prefix, idx, label_val):
        return (
            f'<div class="dyn-row" style="display:flex;gap:8px;margin-bottom:4px;align-items:center;">'
            f'<input type="text" name="{input_prefix}_label_{idx}" value="{label_val}" '
            f'placeholder="Choice label" style="flex:1;min-width:200px;" class="choice-label-input">'
            f'<button type="button" class="dyn-remove-btn" '
            f'style="color:#ba2121;background:none;border:none;font-size:16px;cursor:pointer;'
            f'font-weight:bold;" title="Remove">\u2715</button>'
            f'</div>'
        )

    def _input_prefix(self, widget_id):
        prefix_parts = widget_id.replace("id_", "", 1).rsplit("-", 1)
        return f"{prefix_parts[0]}-choice" if len(prefix_parts) > 1 else "choice"

    def value_from_datadict(self, data, files, name):
        widget_id = self.attrs.get("id", f"id_{name}") if self.attrs else f"id_{name}"
        input_prefix = self._input_prefix(widget_id)

        rows = []
        idx = 0
        while True:
            label = data.get(f"{input_prefix}_label_{idx}")
            if label is None:
                break
            label = label.strip()
            if label:
                rows.append({"key": label.lower(), "label": label})
            idx += 1
        return rows


class ErrorMessagesWidget(forms.Widget):
    """
    Renders key + English text rows for error messages.
    """

    def render(self, name, value, attrs=None, renderer=None):
        rows = value or []
        widget_id = attrs.get("id", f"id_{name}") if attrs else f"id_{name}"
        container_id = f"container-{widget_id}"
        input_prefix = self._input_prefix(widget_id)

        html = [f'<div id="{container_id}" class="dyn-rows-container" data-prefix="{input_prefix}">']
        for idx, row in enumerate(rows):
            html.append(self._render_row(input_prefix, idx, row.get("key", ""), row.get("label", "")))
        html.append("</div>")
        html.append(
            f'<button type="button" class="dyn-add-error-btn" data-container="{container_id}" '
            f'style="margin-top:4px;cursor:pointer;background:#417690;color:#fff;border:none;'
            f'padding:4px 12px;border-radius:3px;">+ Add Error</button>'
        )
        return mark_safe("\n".join(html))

    def _render_row(self, input_prefix, idx, key_val, label_val):
        return (
            f'<div class="dyn-row" style="display:flex;gap:8px;margin-bottom:4px;align-items:center;">'
            f'<input type="text" name="{input_prefix}_key_{idx}" value="{key_val}" '
            f'placeholder="key (e.g. required)" style="width:140px;">'
            f'<input type="text" name="{input_prefix}_label_{idx}" value="{label_val}" '
            f'placeholder="Error text (English)" style="flex:1;min-width:200px;">'
            f'<button type="button" class="dyn-remove-btn" '
            f'style="color:#ba2121;background:none;border:none;font-size:16px;cursor:pointer;'
            f'font-weight:bold;" title="Remove">\u2715</button>'
            f'</div>'
        )

    def _input_prefix(self, widget_id):
        prefix_parts = widget_id.replace("id_", "", 1).rsplit("-", 1)
        return f"{prefix_parts[0]}-error" if len(prefix_parts) > 1 else "error"

    def value_from_datadict(self, data, files, name):
        widget_id = self.attrs.get("id", f"id_{name}") if self.attrs else f"id_{name}"
        input_prefix = self._input_prefix(widget_id)

        rows = []
        idx = 0
        while True:
            key = data.get(f"{input_prefix}_key_{idx}")
            label = data.get(f"{input_prefix}_label_{idx}")
            if key is None and label is None:
                break
            if key or label:
                rows.append({"key": (key or "").strip(), "label": (label or "").strip()})
            idx += 1
        return rows


INLINE_SCRIPT = """
<script>
(function(){
  if(window._smValidationReady) return;
  window._smValidationReady=true;

  // ── Utility ──
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
    var idx=c.querySelectorAll('.dyn-row').length;
    var d=document.createElement('div');
    d.className='dyn-row';
    d.style.cssText='display:flex;gap:8px;margin-bottom:4px;align-items:center;';
    d.innerHTML=
      '<input type="text" name="'+prefix+'_label_'+idx+'" placeholder="Choice label" '+
      'style="flex:1;min-width:200px;" class="choice-label-input">'+
      '<button type="button" class="dyn-remove-btn" '+
      'style="color:#ba2121;background:none;border:none;font-size:16px;cursor:pointer;font-weight:bold;" '+
      'title="Remove">\\u2715</button>';
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
    var idx=c.querySelectorAll('.dyn-row').length;
    var d=document.createElement('div');
    d.className='dyn-row';
    d.style.cssText='display:flex;gap:8px;margin-bottom:4px;align-items:center;';
    d.innerHTML=
      '<input type="text" name="'+prefix+'_key_'+idx+'" placeholder="key (e.g. required)" style="width:140px;">'+
      '<input type="text" name="'+prefix+'_label_'+idx+'" placeholder="Error text (English)" style="flex:1;min-width:200px;">'+
      '<button type="button" class="dyn-remove-btn" '+
      'style="color:#ba2121;background:none;border:none;font-size:16px;cursor:pointer;font-weight:bold;" '+
      'title="Remove">\\u2715</button>';
    c.appendChild(d);
  });

  // ── Dynamic rows: remove ──
  document.addEventListener('click',function(e){
    var btn=e.target.closest('.dyn-remove-btn');
    if(!btn) return;
    e.preventDefault();
    btn.closest('.dyn-row').remove();
    // Re-validate duplicates after removal
    checkDuplicateLabels(btn.closest('.inline-related'));
  });

  // ── Duplicate label detection ──
  function checkDuplicateLabels(panel){
    if(!panel) return false;
    // Find the choices add button — the banner goes right before it
    var addBtn=panel.querySelector('.dyn-add-choice-btn');
    if(!addBtn) return false;
    // Remove any previous error banner
    var prev=addBtn.parentElement.querySelector('.dup-error-banner');
    if(prev) prev.remove();
    // Reset all input borders
    var inputs=panel.querySelectorAll('.choice-label-input');
    inputs.forEach(function(inp){inp.style.borderColor='';});

    var seen={};
    var dupes=[];
    inputs.forEach(function(inp){
      var val=inp.value.trim().toLowerCase();
      if(!val) return;
      if(seen[val]){
        // Use the original (first) label text for the error message
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

  // Listen for input on choice labels for duplicate detection
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
      var mcRows=findFieldRows(panel, MC_FIELDS);

      // Always hide script widget row
      findFieldRows(panel,['_validation_scripts']).forEach(function(r){r.style.display='none';});

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
            m.textContent='Max choices must be \\u2265 min choices.';
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
      // Start collapsed
      body.style.display='none';
      // Add toggle indicator
      var arrow=document.createElement('span');
      arrow.className='collapse-arrow';
      arrow.style.cssText='margin-left:8px;font-size:10px;';
      arrow.textContent='\\u25B6';
      var title=header.querySelector('.card-title');
      if(title) title.appendChild(arrow);
      header.addEventListener('click',function(e){
        if(e.target.closest('input,a,button')) return;
        var hidden=body.style.display==='none';
        body.style.display=hidden?'':'none';
        arrow.textContent=hidden?'\\u25BC':'\\u25B6';
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
</script>
"""


class InlineScriptWidget(forms.HiddenInput):
    """Invisible widget that injects the shared JS once."""

    def render(self, name, value, attrs=None, renderer=None):
        return mark_safe(INLINE_SCRIPT)


class CompanyStateMachineForm(forms.ModelForm):
    _validation_scripts = forms.CharField(
        required=False,
        widget=InlineScriptWidget(),
        label="",
    )
    choices_field = forms.Field(
        required=False,
        label="Choices",
        help_text="Labels for multiple choice options. Keys are auto-generated from labels.",
        widget=ChoicesWidget(),
    )
    errors_field = forms.Field(
        required=False,
        label="Error messages",
        help_text="Key and English text for each validation error.",
        widget=ErrorMessagesWidget(),
    )

    class Meta:
        model = CompanyStateMachine
        exclude = ("type",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            config = self.instance.validation_config or {}
            self.initial["choices_field"] = [
                {"key": c.get("key", ""), "label": c.get("label", "")}
                for c in config.get("choices", [])
            ]
            self.initial["errors_field"] = [
                {
                    "key": e.get("key", ""),
                    "label": (e.get("labels", {}).get("en") or {}).get("text", ""),
                }
                for e in (self.instance.error_message or [])
            ]

    def clean(self):
        cleaned = super().clean()

        # min/max validation
        min_c = cleaned.get("min_choices")
        max_c = cleaned.get("max_choices")
        if min_c is not None and max_c is not None and max_c < min_c:
            raise ValidationError({
                "max_choices": "Max choices must be greater than or equal to min choices."
            })

        # Duplicate label validation
        choices = cleaned.get("choices_field") or []
        seen = set()
        for row in choices:
            label_lower = row.get("label", "").strip().lower()
            if not label_lower:
                continue
            if label_lower in seen:
                raise ValidationError({
                    "choices_field": f"Duplicate choice label: \"{row.get('label')}\". Labels must be unique."
                })
            seen.add(label_lower)

        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Build validation_config — only EN source data, no translations
        choices_data = self.cleaned_data.get("choices_field") or []
        if choices_data:
            new_choices = []
            for row in choices_data:
                label = row.get("label", "").strip()
                if not label:
                    continue
                new_choices.append({"key": label.lower(), "label": label})
            instance.validation_config = {"choices": new_choices} if new_choices else None
        else:
            instance.validation_config = None

        # Build error_message — only EN source text
        errors_data = self.cleaned_data.get("errors_field") or []
        if errors_data:
            new_errors = []
            for row in errors_data:
                key = row.get("key", "").strip()
                text = row.get("label", "").strip()
                if not key:
                    continue
                new_errors.append({
                    "key": key,
                    "labels": {"en": {"text": text}},
                })
            instance.error_message = new_errors if new_errors else None
        else:
            instance.error_message = None

        if commit:
            instance.save()
        return instance
