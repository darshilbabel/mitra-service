from chatbot.models import CompanyBot

def get_company_bot(route: str, profile=None) -> CompanyBot:
    if profile:
        return CompanyBot.objects.get(company=profile.company, route=route)
    return CompanyBot.objects.get(route=route)