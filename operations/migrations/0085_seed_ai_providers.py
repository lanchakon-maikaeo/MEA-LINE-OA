"""ADR 0038: the three providers and their models as data, so the admin page lists them.

Existing rows keep their key; every model an existing profile uses is listed too, so the
chains in use can be chosen again. Gemini's models come first, in the order the user set.
"""

from django.db import migrations

GEMINI_NOTICE = "แผนฟรี: ห้ามใช้กับข้อมูลจริง"
PROVIDERS = [
    {
        "name": "gemini",
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_url": "https://aistudio.google.com/apikey",
        "notice": GEMINI_NOTICE,
        # Google asks for temperature 1.0 on Gemini 3; lower values can make the answer loop.
        "defaults": {"timeout_seconds": 15, "max_tokens": 1200, "temperature": "1.00"},
        "models": [
            ("gemini-3.8-flash", "Gemini 3.8 Flash"),
            ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
            ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
        ],
    },
    {
        "name": "opentyphoon",
        "label": "Typhoon",
        "base_url": "https://api.opentyphoon.ai/v1",
        "key_url": "https://playground.opentyphoon.ai/api-key",
        "defaults": {"timeout_seconds": 15, "max_tokens": 1200, "temperature": "0.40"},
        "models": [("typhoon-v2.5-30b-a3b-instruct", "Typhoon 2.5 30B")],
    },
    {
        "name": "thaillm",
        "label": "ThaiLLM",
        "base_url": "https://thaillm.or.th/api/v1",
        "enforces_schema": True,
        "defaults": {"timeout_seconds": 10, "max_tokens": 800, "temperature": "0.10"},
        "models": [],
    },
]


def seed(apps, schema_editor):
    AICredential = apps.get_model("operations", "AICredential")
    AIModel = apps.get_model("operations", "AIModel")
    AIProfile = apps.get_model("operations", "AIProfile")
    for spec in PROVIDERS:
        provider = AICredential.objects.filter(name=spec["name"]).first()
        if provider is None:
            provider = AICredential.objects.create(name=spec["name"], base_url=spec["base_url"])
        provider.label = spec["label"]
        provider.key_url = spec.get("key_url", "")
        provider.notice = spec.get("notice", "")
        provider.enforces_schema = spec.get("enforces_schema", False)
        provider.defaults = spec["defaults"]
        provider.save()
        for position, (model_id, label) in enumerate(spec["models"], start=1):
            AIModel.objects.get_or_create(
                provider=provider,
                model_id=model_id,
                defaults={"label": label, "position": position},
            )
    for credential_id, model_id in sorted(
        set(AIProfile.objects.values_list("credential_id", "model_id"))
    ):
        AIModel.objects.get_or_create(
            provider_id=credential_id, model_id=model_id, defaults={"position": 50}
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0084_ai_providers_models_releases")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
