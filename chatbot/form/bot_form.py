import json
from django.core.exceptions import ValidationError
from django.forms import ModelForm
from chatbot.models import CompanyBot
from chatbot.widgets.json_widget import PrettyJSONWidget


class CompanyBotAdminForm(ModelForm):
    class Meta:
        model = CompanyBot
        fields = "__all__"
        widgets = {
            "tool_context": PrettyJSONWidget(
                attrs={
                    "rows": 10,
                    "style": "font-family: monospace; white-space: pre; width: 100%; tab-size: 2;"
                }
            ),
            "other_params": PrettyJSONWidget(
                attrs={
                    "rows": 10,
                    "style": "font-family: monospace; white-space: pre; width: 100%; tab-size: 2;"
                }
            )
        }


    def validate_json_field(self, value):
        if value in (None, "", "null"):
            return None

        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return json.dumps(parsed)
            except Exception:
                raise ValidationError("Invalid JSON format. Please provide valid JSON.")
        return value


    def clean_tool_context(self):
        return self.validate_json_field(self.cleaned_data.get("tool_context"))


    def clean_other_params(self):
        return self.validate_json_field(self.cleaned_data.get("other_params"))
