from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.db import transaction

from products.models import HumanAnnotator


class Command(BaseCommand):
    help = 'Seed the database with sample users and groups for demos'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Remove existing users and groups before seeding',
        )

    def handle(self, *args, **options):
        force = options['force']

        self.stdout.write(self.style.SUCCESS('--- User Data Seeder ---'))
        
        if force:
            self._reset_user_data()

        with transaction.atomic():
            self._create_groups_and_users()

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('✓ User data seeding complete'))
        self.stdout.write(
            self.style.SUCCESS(
                f'  Users: {User.objects.count()} • '
                f'Groups: {Group.objects.count()} • '
                f'Annotators: {HumanAnnotator.objects.count()}'
            )
        )
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Login Accounts:'))
        self.stdout.write(self.style.SUCCESS('  Admin: admin / admin123'))
        self.stdout.write(self.style.SUCCESS('  Annotators:'))
        self.stdout.write(self.style.SUCCESS('    - annotator1 / annotator123'))
        self.stdout.write(self.style.SUCCESS('    - annotator2 / annotator123'))
        self.stdout.write(self.style.SUCCESS('    - annotator3 / annotator123'))

    def _reset_user_data(self):
        self.stdout.write(self.style.WARNING('Clearing existing users and groups...'))
        
        # Delete annotators first (foreign key to User)
        HumanAnnotator.objects.all().delete()
        
        # Delete users except system users
        User.objects.filter(is_superuser=False, is_staff=False).delete()
        
        # Optionally clear groups
        Group.objects.filter(name__in=['Admin', 'Annotator']).delete()
        
        self.stdout.write(self.style.SUCCESS('✓ Cleared existing user data'))

    def _create_groups_and_users(self):
        self.stdout.write(self.style.SUCCESS('Creating groups and users...'))
        
        # Create groups
        admin_group, created = Group.objects.get_or_create(name='Admin')
        if created:
            self.stdout.write(self.style.SUCCESS('✓ Created Admin group'))
        
        annotator_group, created = Group.objects.get_or_create(name='Annotator')
        if created:
            self.stdout.write(self.style.SUCCESS('✓ Created Annotator group'))

        # Create admin user
        admin_user, created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'admin@example.com',
                'is_staff': True,
                'is_superuser': True,
                'first_name': 'Admin',
                'last_name': 'User',
            },
        )
        if created:
            admin_user.set_password('admin123')
            admin_user.save()
            admin_user.groups.add(admin_group)
            self.stdout.write(self.style.SUCCESS('✓ Created admin user: admin'))
        else:
            self.stdout.write(self.style.WARNING('  Admin user already exists'))

        # Create annotator users
        annotators = [
            ('annotator1', 'John', 'Doe', 'john.doe@example.com'),
            ('annotator2', 'Jane', 'Smith', 'jane.smith@example.com'),
            ('annotator3', 'Bob', 'Johnson', 'bob.johnson@example.com'),
        ]
        
        for username, first, last, email in annotators:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'email': email,
                    'first_name': first,
                    'last_name': last,
                    'is_staff': False,
                    'is_superuser': False,
                },
            )
            if created:
                user.set_password('annotator123')
                user.save()
                user.groups.add(annotator_group)
                HumanAnnotator.objects.get_or_create(user=user)
                self.stdout.write(self.style.SUCCESS(f'✓ Created annotator: {username} ({first} {last})'))
            else:
                # Ensure annotator exists even if user exists
                HumanAnnotator.objects.get_or_create(user=user)
                self.stdout.write(self.style.WARNING(f'  Annotator {username} already exists'))