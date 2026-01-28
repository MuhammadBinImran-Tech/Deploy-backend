from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AIProviderSubclassPrompt',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('prompt_template', models.TextField(help_text='Custom prompt template for this provider-subclass combination. Use template variables: {product_info}, {attributes}')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('provider', models.ForeignKey(db_column='provider_id', on_delete=django.db.models.deletion.CASCADE, related_name='subclass_prompts', to='products.aiprovider')),
                ('subclass', models.ForeignKey(db_column='subclass_id', on_delete=django.db.models.deletion.CASCADE, related_name='ai_prompts', to='products.subclass')),
            ],
            options={
                'verbose_name': 'AI Provider Subclass Prompt',
                'verbose_name_plural': 'AI Provider Subclass Prompts',
                'db_table': 'tbl_ai_provider_subclass_prompts',
                'managed': True,
            },
        ),
        migrations.AddConstraint(
            model_name='aiprovidersubclassprompt',
            constraint=models.UniqueConstraint(fields=('provider', 'subclass'), name='unique_provider_subclass'),
        ),
        migrations.AddIndex(
            model_name='aiprovidersubclassprompt',
            index=models.Index(fields=['provider', 'subclass', 'is_active'], name='idx_provider_subclass_active'),
        ),
    ]
