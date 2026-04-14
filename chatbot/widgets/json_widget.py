import json
from django import forms

class PrettyJSONWidget(forms.Textarea):
    def format_value(self, value):
        if not value:
            return "{}"

        try:
            if isinstance(value, str):
                value = json.loads(value)

            return json.dumps(value, indent=2, ensure_ascii=False)
        except Exception:
            return value
