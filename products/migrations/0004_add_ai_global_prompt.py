from django.db import migrations, models


DEFAULT_PROMPT = (
    "{{PRODUCT_INFO}}\n\n"
    "{{IMAGE_INFO}}\n\n"
    "{{ATTRIBUTES}}\n\n"
    "Critical Instructions:\n"
    "1. Analyze ALL available information: text description AND product image (if provided)\n"
    "2. For RESTRICTED attributes: MUST choose ONLY from the allowed values list\n"
    "   - If none of the allowed values fit, you may provide your own value\n"
    "   - If you cannot determine any value, use \"Unknown\"\n"
    "3. For FREE-FORM attributes: Provide your best inference - be descriptive and specific\n"
    "4. ONLY use \"Unknown\" if:\n"
    "   - For restricted attributes: None of the allowed values apply AND you cannot infer a reasonable value\n"
    "   - For free-form attributes: You genuinely cannot make ANY reasonable inference from text OR image\n"
    "5. Avoid \"Unknown\" whenever possible - make educated inferences based on ALL available data\n"
    "6. When image is available: Prioritize visual evidence for appearance-related attributes\n"
    "7. **MANDATORY**: You MUST return EXACTLY all attributes listed above in your response\n"
    "8. **MANDATORY**: Every attribute from the list above MUST be present in your JSON response\n"
    "9. Return ONLY a valid JSON object with attribute names as keys and values as strings\n\n"
    "ATTRIBUTE CHECKLIST - YOU MUST INCLUDE ALL ATTRIBUTES:\n"
    "{{ATTRIBUTES}}\n\n"
    "Example response format:\n"
    "{\n"
    "  \"Color\": \"Navy Blue\",\n"
    "  \"Material\": \"Cotton Blend\",\n"
    "  \"Size\": \"Large\",\n"
    "  \"Pattern\": \"Solid\",\n"
    "  \"Fit\": \"Regular Fit\"\n"
    "}\n\n"
    "CRITICAL REMINDER: Your response MUST contain ALL attributes listed above.\n"
    "Missing even one attribute is an error. If you cannot determine a value, use \"Unknown\".\n\n"
    "Respond with JSON only, no additional text:\n"
)


def seed_global_prompt(apps, schema_editor):
    AIGlobalPrompt = apps.get_model('products', 'AIGlobalPrompt')
    if not AIGlobalPrompt.objects.filter(id=1).exists():
        AIGlobalPrompt.objects.create(id=1, prompt_template=DEFAULT_PROMPT)


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0003_remove_aiprovidersubclassprompt_unique_provider_subclass_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='AIGlobalPrompt',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ('prompt_template', models.TextField()),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'tbl_ai_global_prompt',
                'managed': True,
                'verbose_name': 'AI Global Prompt',
                'verbose_name_plural': 'AI Global Prompts',
            },
        ),
        migrations.RunPython(seed_global_prompt, migrations.RunPython.noop),
    ]
