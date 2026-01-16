from django.core.management.base import BaseCommand
from products.models import AIProvider

class Command(BaseCommand):
    help = 'Setup AI providers for the annotation system'
    
    def handle(self, *args, **options):
        providers = [
            {
                'name': 'GPT-4o',
                'service_name': 'OpenAI',
                'model': 'gpt-4o',
                'config': {
                    'api_key_env': 'OPENAI_API_KEY',
                    'max_tokens': 1000,
                    'temperature': 0.1
                }
            },
            {
                'name': 'Claude Haiku',
                'service_name': 'Anthropic',
                'model': 'claude-3-haiku-20240307',
                'config': {
                    'api_key_env': 'ANTHROPIC_API_KEY',
                    'max_tokens': 1000,
                    'temperature': 0.1
                }
            }
        ]
        
        for provider_data in providers:
            provider, created = AIProvider.objects.get_or_create(
                name=provider_data['name'],
                defaults=provider_data
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f'Created AI provider: {provider.name}')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'AI provider already exists: {provider.name}')
                )