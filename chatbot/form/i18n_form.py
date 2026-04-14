import json
from django.core.exceptions import ValidationError
from django.forms import ModelForm
from chatbot.models import TranslationFile
from chatbot.widgets.json_widget import PrettyJSONWidget


class TranslationFileAdminForm(ModelForm):
    class Meta:
        model = TranslationFile
        fields = "__all__"
        widgets = {
            "data": PrettyJSONWidget(
                attrs={
                    "rows": 10,
                    "style": "font-family: monospace; white-space: pre; width: 100%; tab-size: 2;"                }
            )
        }

    def clean_data(self):
        value = self.cleaned_data.get("data")

        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                raise ValidationError("Invalid JSON format")

        return value
