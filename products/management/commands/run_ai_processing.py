"""
Management command to run automated AI processing
Place this file in: products/management/commands/run_ai_processing.py
"""

from django.core.management.base import BaseCommand
from django.utils import timezone
from products.models import *
from products.ai_runner import AIBatchProcessor
import random
import time
from datetime import datetime
from typing import List


class Command(BaseCommand):
    help = 'Run automated AI processing for all pending products'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=10,
            help='Number of products per batch (default: 10)'
        )
        parser.add_argument(
            '--providers',
            type=str,
            help='Comma-separated list of AI provider IDs (default: all active providers)'
        )
        parser.add_argument(
            '--continuous',
            action='store_true',
            help='Run continuously until all products are processed'
        )
        parser.add_argument(
            '--delay',
            type=float,
            default=1.0,
            help='Delay between batches in seconds (default: 1.0)'
        )
        parser.add_argument(
            '--simulate-delay',
            type=float,
            default=0.1,
            help='Simulated processing delay per product in seconds (default: 0.1)'
        )
        parser.add_argument(
            '--seed-products',
            type=int,
            default=0,
            help='Ensure at least this many pending_ai products exist by generating synthetic ones before processing.',
        )
    
    def handle(self, *args, **options):
        batch_size = options['batch_size']
        continuous = options['continuous']
        batch_delay = options['delay']
        simulate_delay = options['simulate_delay']
        seed_target = options['seed_products']
        
        # Parse provider IDs
        provider_ids = []
        if options['providers']:
            provider_ids = [int(id.strip()) for id in options['providers'].split(',')]
        
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('AI Processing System'))
        self.stdout.write(self.style.SUCCESS('=' * 60))
        
        self.stdout.write(f"Batch size: {batch_size}")
        self.stdout.write(f"Continuous mode: {continuous}")
        self.stdout.write(f"Batch delay: {batch_delay}s")
        self.stdout.write(f"Simulate delay: {simulate_delay}s")
        
        # Check if AI processing is paused
        control = AIProcessingControl.get_control()
        if control.is_paused:
            self.stdout.write(self.style.WARNING('⚠ AI processing is currently paused'))
            self.stdout.write(self.style.WARNING('Use admin interface or API to resume processing'))
            return
        
        if seed_target:
            self._ensure_pending_products(seed_target)
        
        if continuous:
            self.stdout.write(self.style.SUCCESS('Starting continuous AI processing...'))
            self.stdout.write(self.style.SUCCESS('Press Ctrl+C to stop'))
            self.stdout.write('')
            self._process_all_batches(batch_size, provider_ids, batch_delay, simulate_delay)
        else:
            self.stdout.write(self.style.SUCCESS('Processing single batch...'))
            self.stdout.write('')
            self._process_single_batch(batch_size, provider_ids, simulate_delay)
    
    def _process_single_batch(self, batch_size, provider_ids, simulate_delay):
        """Process a single batch of products"""
        # Get AI providers
        if provider_ids:
            ai_providers = AIProvider.objects.filter(
                id__in=provider_ids,
                is_active=True
            )
        else:
            ai_providers = AIProvider.objects.filter(is_active=True)
        
        if not ai_providers.exists():
            self.stdout.write(self.style.ERROR('❌ No active AI providers found'))
            return
        
        self.stdout.write(f"Using AI providers: {', '.join([p.name for p in ai_providers])}")
        
        # Get pending products not already in AI batches
        pending_products = list(BaseProduct.objects.filter(
            processing_status__in=['pending', 'pending_ai']
        ).exclude(
            id__in=BatchItem.objects.filter(batch_type='ai').values_list('product_id', flat=True)
        ).order_by('created_at')[:batch_size])
        
        if not pending_products:
            self.stdout.write(self.style.WARNING('⚠ No pending products found'))
            return
        
        self.stdout.write(f"Found {len(pending_products)} pending products")
        
        try:
            # Create batch
            batch = AnnotationBatch.objects.create(
                name=f"AI Batch - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                description='Processed via management command',
                batch_type='ai',
                batch_size=len(pending_products)
            )
            
            self.stdout.write(f"Created batch: {batch.name} (ID: {batch.id})")
            
            # Create batch items
            batch_items = []
            for product in pending_products:
                batch_item = BatchItem.objects.create(
                    batch=batch,
                    product=product,
                    batch_type='ai'
                )
                batch_items.append(batch_item)
            
            # Create assignments for each AI provider
            assignments = []
            for provider in ai_providers:
                assignment = BatchAssignment.objects.create(
                    batch=batch,
                    assignment_type='ai',
                    assignment_id=provider.id,
                    status='in_progress'
                )
                assignments.append(assignment)
                
                # Create assignment items for all products
                for batch_item in batch_items:
                    BatchAssignmentItem.objects.create(
                        assignment=assignment,
                        batch_item=batch_item,
                        status='ai_in_progress'
                    )
            
            self.stdout.write(f"Created {len(assignments)} assignments")
            
            # Update product status
            product_ids = [product.id for product in pending_products]
            BaseProduct.objects.filter(id__in=product_ids).update(
                processing_status='ai_in_progress',
                updated_at=timezone.now()
            )
            
            self.stdout.write(f"Updated {len(pending_products)} products to 'ai_in_progress'")
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('Starting AI processing...'))
            self.stdout.write('')
            
            # Process the batch
            processor = AIBatchProcessor([p.id for p in ai_providers])
            processor.process_batch(batch.id)
            
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('✓ Batch processing completed'))
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Error creating batch: {e}'))
    
    def _process_all_batches(self, batch_size, provider_ids, batch_delay, simulate_delay):
        """Process all pending products in batches"""
        batch_count = 0
        total_processed = 0
        
        # Get AI providers
        if provider_ids:
            ai_providers = AIProvider.objects.filter(
                id__in=provider_ids,
                is_active=True
            )
        else:
            ai_providers = AIProvider.objects.filter(is_active=True)
        
        if not ai_providers.exists():
            self.stdout.write(self.style.ERROR('❌ No active AI providers found'))
            return
        
        self.stdout.write(f"Using AI providers: {', '.join([p.name for p in ai_providers])}")
        
        while True:
            # Check if processing is paused
            control = AIProcessingControl.get_control()
            if control.is_paused:
                self.stdout.write(self.style.WARNING('⚠ AI processing paused, waiting...'))
                time.sleep(5)
                continue
            
            # Get pending products
            pending_products = list(BaseProduct.objects.filter(
                processing_status__in=['pending', 'pending_ai']
            ).exclude(
                id__in=BatchItem.objects.filter(batch_type='ai').values_list('product_id', flat=True)
            ).order_by('created_at')[:batch_size])
            
            if not pending_products:
                self.stdout.write(self.style.SUCCESS(f'✓ All products processed!'))
                self.stdout.write(self.style.SUCCESS(f'  Total batches: {batch_count}'))
                self.stdout.write(self.style.SUCCESS(f'  Total products: {total_processed}'))
                break
            
            batch_count += 1
            total_processed += len(pending_products)
            
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS(f'Batch {batch_count} - Processing {len(pending_products)} products'))
            self.stdout.write(f"  Start time: {datetime.now().strftime('%H:%M:%S')}")
            
            try:
                # Create batch
                batch = AnnotationBatch.objects.create(
                    name=f"Auto AI Batch {batch_count} - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    description='Automatically created by continuous processing',
                    batch_type='ai',
                    batch_size=len(pending_products)
                )
                
                # Create batch items
                batch_items = []
                for product in pending_products:
                    batch_item = BatchItem.objects.create(
                        batch=batch,
                        product=product,
                        batch_type='ai'
                    )
                    batch_items.append(batch_item)
                
                # Create assignments for each AI provider
                for provider in ai_providers:
                    assignment = BatchAssignment.objects.create(
                        batch=batch,
                        assignment_type='ai',
                        assignment_id=provider.id,
                        status='in_progress'
                    )
                    
                    # Create assignment items
                    for batch_item in batch_items:
                        BatchAssignmentItem.objects.create(
                            assignment=assignment,
                            batch_item=batch_item,
                            status='ai_in_progress'
                        )
                
                # Update product status
                product_ids = [p.id for p in pending_products]
                BaseProduct.objects.filter(id__in=product_ids).update(
                    processing_status='ai_in_progress',
                    updated_at=timezone.now()
                )
                
                # Process the batch
                processor = AIBatchProcessor([p.id for p in ai_providers])
                processor.process_batch(batch.id)
                
                self.stdout.write(f"  End time: {datetime.now().strftime('%H:%M:%S')}")
                self.stdout.write(self.style.SUCCESS(f'  ✓ Batch {batch_count} completed'))
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  ❌ Error creating batch: {e}'))
            
            # Delay between batches
            if batch_delay > 0:
                time.sleep(batch_delay)
    
    def _process_batch(self, batch, products, ai_providers, simulate_delay):
        """Process a batch of products with AI"""
        # Process each product with each provider
        for index, product in enumerate(products):
            product_num = index + 1
            total_products = len(products)
            
            self.stdout.write(f"  Product {product_num}/{total_products}: {product.style_desc}")
            
            # Get applicable attributes
            applicable_attrs = self._get_applicable_attributes(product)
            
            if not applicable_attrs:
                self.stdout.write(f"    ⚠ No applicable attributes found")
                continue
            
            self.stdout.write(f"    Processing {len(applicable_attrs)} attributes")
            
            # Process with each provider
            for provider in ai_providers:
                # Get batch item and assignment
                batch_item = BatchItem.objects.get(batch=batch, product=product)
                assignment = BatchAssignment.objects.get(
                    batch=batch,
                    assignment_type='ai',
                    assignment_id=provider.id
                )
                
                assignment_item = BatchAssignmentItem.objects.get(
                    assignment=assignment,
                    batch_item=batch_item
                )
                
                # Process each attribute
                for attr_info in applicable_attrs:
                    # Generate AI suggestion
                    suggested_value = self._generate_ai_suggestion(product, attr_info, provider)
                    confidence = round(random.uniform(0.7, 0.95), 4)
                    
                    # Save annotation
                    ProductAnnotation.objects.create(
                        product=product,
                        attribute_id=attr_info['id'],
                        value=suggested_value,
                        source_type='ai',
                        source_id=provider.id,
                        confidence_score=confidence,
                        batch_item=batch_item
                    )
                    
                    # Simulate processing delay
                    if simulate_delay > 0:
                        time.sleep(simulate_delay)
                
                # Update assignment item
                assignment_item.status = 'ai_done'
                assignment_item.completed_at = timezone.now()
                assignment_item.save()
                
                # Update assignment progress
                assignment_items = BatchAssignmentItem.objects.filter(assignment=assignment)
                completed_items = assignment_items.filter(status='ai_done').count()
                total_items = assignment_items.count()
                
                progress = (completed_items / total_items * 100) if total_items > 0 else 0
                assignment.progress = progress
                
                if completed_items == total_items:
                    assignment.status = 'completed'
                
                assignment.save()
            
            # Simulate delay between products
            time.sleep(0.05)
        
        # Update product statuses
        for product in products:
            batch_item = BatchItem.objects.get(batch=batch, product=product)
            
            # Check if all AI assignments are done
            all_done = True
            assignment_items = BatchAssignmentItem.objects.filter(batch_item=batch_item)
            
            for item in assignment_items:
                if item.status != 'ai_done':
                    all_done = False
                    break
            
            if all_done:
                product.processing_status = 'ai_done'
                product.save()
    
    def _ensure_pending_products(self, desired_pending: int):
        """Bootstrap demo data so the command can run on empty databases."""
        current_pending = BaseProduct.objects.filter(processing_status__in=['pending', 'pending_ai']).count()
        missing = max(0, desired_pending - current_pending)
        if missing <= 0:
            return
        
        factory = SampleProductFactory()
        created = factory.create_products(missing)
        if created:
            self.stdout.write(
                self.style.SUCCESS(f'Generated {created} synthetic pending products for processing queue.')
            )
    
    def _get_applicable_attributes(self, product):
        """Get attributes applicable to a product"""
        if not product.subclass:
            return []
        
        attributes = []
        
        # Get global attributes
        global_attrs = AttributeGlobalMap.objects.all().select_related('attribute')
        for map_obj in global_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name
            })
        
        # Get subclass-specific attributes
        subclass_attrs = AttributeSubclassMap.objects.filter(
            subclass=product.subclass
        ).select_related('attribute')
        
        for map_obj in subclass_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name
            })
        
        return attributes
    
    def _generate_ai_suggestion(self, product, attribute, provider):
        """Generate AI suggestions - replace with actual AI API calls"""
        if attribute['name'] == 'Color':
            colors = ['Red', 'Blue', 'Green', 'Black', 'White', 'Yellow', 'Pink', 'Purple', 'Orange', 'Gray']
            return random.choice(colors)
        elif attribute['name'] == 'Size':
            sizes = ['XS', 'S', 'M', 'L', 'XL', 'XXL']
            return random.choice(sizes)
        elif attribute['name'] == 'Material':
            materials = ['Cotton', 'Polyester', 'Silk', 'Wool', 'Linen', 'Denim', 'Leather', 'Nylon']
            return random.choice(materials)
        elif attribute['name'] == 'Gender':
            return random.choice(['Men', 'Women', 'Unisex'])
        elif attribute['name'] == 'Season':
            seasons = ['Spring', 'Summer', 'Fall', 'Winter', 'All Season']
            return random.choice(seasons)
        elif attribute['name'] == 'Pattern':
            patterns = ['Solid', 'Striped', 'Printed', 'Floral', 'Checkered', 'Plaid', 'Graphic']
            return random.choice(patterns)
        elif attribute['name'] == 'Fit':
            fits = ['Slim', 'Regular', 'Loose', 'Oversized', 'Skinny']
            return random.choice(fits)
        elif attribute['name'] == 'Occasion':
            occasions = ['Casual', 'Formal', 'Business', 'Sports', 'Evening']
            return random.choice(occasions)
        else:
            return f"AI suggested value for {attribute['name']}"


class SampleProductFactory:
    """Lightweight seeder for the AI processing command."""

    templates = [
        {
            'style_prefix': 'AUTO-TOP',
            'division': 'Men',
            'department': 'Apparel',
            'subdepartment': 'Tops',
            'product_class': 'T-Shirts',
            'subclass': 'Essential',
            'size_key': 'tops',
            'style_desc': 'Auto Performance Tee',
        },
        {
            'style_prefix': 'AUTO-BOT',
            'division': 'Men',
            'department': 'Apparel',
            'subdepartment': 'Bottoms',
            'product_class': 'Joggers',
            'subclass': 'Athletic',
            'size_key': 'bottoms',
            'style_desc': 'Auto Knit Jogger',
        },
        {
            'style_prefix': 'AUTO-WM',
            'division': 'Women',
            'department': 'Apparel',
            'subdepartment': 'Dresses',
            'product_class': 'Shift Dresses',
            'subclass': 'Everyday',
            'size_key': 'dresses',
            'style_desc': 'Auto Shift Dress',
        },
    ]

    size_map = {
        'tops': ['XS', 'S', 'M', 'L', 'XL'],
        'bottoms': ['26', '28', '30', '32', '34'],
        'dresses': ['2', '4', '6', '8', '10', '12'],
    }

    colors = ['red', 'blue', 'black', 'white', 'olive', 'charcoal']

    def create_products(self, count: int) -> int:
        created = 0
        for index in range(count):
            template = random.choice(self.templates)
            style_id = f"{template['style_prefix']}{(index + 1):04d}"
            department, _ = Department.objects.get_or_create(name=template['department'])
            subdept, _ = SubDepartment.objects.get_or_create(name=template['subdepartment'])
            product_class, _ = Class.objects.get_or_create(name=template['product_class'])
            subclass, _ = SubClass.objects.get_or_create(name=template['subclass'])

            color = random.choice(self.colors)
            size = random.choice(self.size_map[template['size_key']])

            product, was_created = BaseProduct.objects.get_or_create(
                style_id=style_id,
                ingestion_batch=1,
                defaults={
                    'style_desc': template['style_desc'],
                    'processing_status': 'pending',
                    'department': department,
                    'dept_name': department.name,
                    'subdepartment': subdept,
                    'subdept_name': subdept.name,
                    'class_field': product_class,
                    'class_name': product_class.name,
                    'subclass': subclass,
                    'subclass_name': subclass.name,
                    'ingestion_batch': 1,
                    'is_active': True,
                },
            )

            if was_created:
                product_color, _ = ProductColor.objects.get_or_create(
                    base_product=product,
                    color_id=color.upper(),
                    defaults={'color_desc': color.title()},
                )
                ProductSize.objects.get_or_create(
                    product_color=product_color,
                    size_desc=size,
                    dim_desc=None,
                )
                ProductImage.objects.get_or_create(
                    product_color=product_color,
                    image_url=f'https://placehold.co/400x600?text={style_id}',
                )
                created += 1

        return created