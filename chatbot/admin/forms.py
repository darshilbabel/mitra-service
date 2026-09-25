import os

from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import escape
from django.utils.safestring import mark_safe

from chatbot.models.company_models import CompanyStateMachine

# Load the JS once at module import time
_JS_PATH = os.path.join(
    os.path.dirname(__file__),
    '..', 'static', 'chatbot', 'admin', 'js', 'validation_fields.js',
)
try:
    with open(_JS_PATH) as f:
        _INLINE_JS = f.read()
except FileNotFoundError:
    _INLINE_JS = ""


class ChoicesWidget(forms.Widget):
    """
    Renders a repeatable list of label inputs for multiple choice options.
    Key is auto-derived from label (lowercased).
    """

    def render(self, name, value, attrs=None, renderer=None):
        rows = value or []
        widget_id = attrs.get("id", f"id_{name}") if attrs else f"id_{name}"
        container_id = f"container-{widget_id}"
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
        # Inject the shared JS inline (idempotent — JS guards against re-execution)
        if _INLINE_JS:
            html.append(f"<script>{_INLINE_JS}</script>")
        return mark_safe("\n".join(html))

    def _render_row(self, input_prefix, idx, label_val):
        return (
            f'<div class="dyn-row" style="display:flex;gap:8px;margin-bottom:4px;align-items:center;">'
            f'<input type="text" name="{escape(input_prefix)}_label_{idx}" value="{escape(label_val)}" '
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

        # Collect all submitted indices (handles gaps from row deletion)
        indices = sorted(
            int(k.rsplit("_", 1)[1])
            for k in data
            if k.startswith(f"{input_prefix}_label_") and k.rsplit("_", 1)[1].isdigit()
        )
        rows = []
        for idx in indices:
            label = (data.get(f"{input_prefix}_label_{idx}") or "").strip()
            if label:
                rows.append({"key": label.lower().replace(" ", "_"), "label": label})
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
            f'<input type="text" name="{escape(input_prefix)}_key_{idx}" value="{escape(key_val)}" '
            f'placeholder="key (e.g. required)" style="width:140px;">'
            f'<input type="text" name="{escape(input_prefix)}_label_{idx}" value="{escape(label_val)}" '
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

        # Collect all submitted indices (handles gaps from row deletion)
        indices = set()
        for k in data:
            if k.startswith(f"{input_prefix}_key_") or k.startswith(f"{input_prefix}_label_"):
                suffix = k.rsplit("_", 1)[1]
                if suffix.isdigit():
                    indices.add(int(suffix))
        rows = []
        for idx in sorted(indices):
            key = (data.get(f"{input_prefix}_key_{idx}") or "").strip()
            label = (data.get(f"{input_prefix}_label_{idx}") or "").strip()
            if key or label:
                rows.append({"key": key, "label": label})
        return rows


class CompanyStateMachineForm(forms.ModelForm):
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

        # Duplicate label validation — collect all duplicates before raising
        choices = cleaned.get("choices_field") or []
        seen = set()
        duplicates = []
        for row in choices:
            label_lower = row.get("label", "").strip().lower()
            if not label_lower:
                continue
            if label_lower in seen:
                duplicates.append(row.get("label"))
            seen.add(label_lower)
        if duplicates:
            labels = ", ".join(dict.fromkeys(duplicates))
            raise ValidationError({
                "choices_field": f"Duplicate choice labels: {labels}. Labels must be unique."
            })

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
                new_choices.append({"key": label.lower().replace(" ", "_"), "label": label})
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
