from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from chatbot.filter.custom_date_from_filter import CustomAdvanceDateFilter
from chatbot.models.program_model import Program


@admin.register(Program)
class ProgramAdmin(SimpleHistoryAdmin):
    """
    Admin interface for the programme master list.
    Keeps a history of every change, since programmes drive how reports are grouped on the
    dashboard. program_uuid is an opaque generated id, not human-searchable, so it is left
    out of search/list.
    """

    list_display = ('name', 'created_at')
    list_filter = (CustomAdvanceDateFilter,)
    search_fields = ('name',)
    ordering = ('name',)
