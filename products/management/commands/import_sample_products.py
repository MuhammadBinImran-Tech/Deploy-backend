from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List

from django.core.management.base import BaseCommand, CommandError

from products.models import (
    Class as ProductClass,
    Department,
    BaseProduct,
    ProductColor,
    ProductSize,
    ProductImage,
    SubClass,
    SubDepartment,
)

DEFAULT_SAMPLE_DATA = [
    {
        'style_id': 'TSHIRT001',
        'color_id': 'RED',
        'size_desc': 'M',
        'style_desc': 'Basic Cotton T-Shirt',
        'color_desc': 'Red',
        'division': 'Men',
        'department': 'Apparel',
        'subdepartment': 'Tops',
        'product_class': 'T-Shirts',
        'subclass': 'Core Tees',
    },
    {
        'style_id': 'JEANS001',
        'color_id': 'BLUE',
        'size_desc': '32W 32L',
        'style_desc': 'Slim Fit Jeans',
        'color_desc': 'Blue',
        'division': 'Men',
        'department': 'Apparel',
        'subdepartment': 'Bottoms',
        'product_class': 'Jeans',
        'subclass': 'Slim Fit',
    },
    {
        'style_id': 'SHIRT001',
        'color_id': 'WHITE',
        'size_desc': 'L',
        'style_desc': 'Formal Dress Shirt',
        'color_desc': 'White',
        'division': 'Men',
        'department': 'Apparel',
        'subdepartment': 'Tops',
        'product_class': 'Shirts',
        'subclass': 'Dress Shirts',
    },
    {
        'style_id': 'HOODIE001',
        'color_id': 'BLACK',
        'size_desc': 'XL',
        'style_desc': 'Premium Hoodie',
        'color_desc': 'Black',
        'division': 'Women',
        'department': 'Apparel',
        'subdepartment': 'Outerwear',
        'product_class': 'Hoodies',
        'subclass': 'Premium',
    },
]


class Command(BaseCommand):
    help = 'Import sample products from JSON/CSV to speed up demos'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            help='Path to a JSON or CSV file containing products (see docs/sample_products.*)',
        )
        parser.add_argument(
            '--status',
            type=str,
            default='pending_ai',
            choices=[
                'pending_ai',
                'ai_in_progress',
                'ai_done',
                'pending_human',
                'human_in_progress',
                'human_done',
            ],
            help='Processing status to apply when the record does not specify one (default: pending_ai)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit the number of records imported from the file',
        )

    def handle(self, *args, **options):
        file_path = options.get('file')
        default_status = options['status']
        limit = options['limit']

        records = self._load_records(file_path) if file_path else DEFAULT_SAMPLE_DATA
        if limit:
            records = records[:limit]

        if not records:
            raise CommandError('No records found to import.')

        created = 0
        for entry in records:
            status = entry.get('processing_status', default_status)
            product = self._create_or_update_product(entry, status)
            if product:
                created += 1

        self.stdout.write(self.style.SUCCESS(f'✓ Imported {created} products'))

    # ------------------------------------------------------------------ helpers
    def _load_records(self, file_path: str) -> List[Dict]:
        path = Path(file_path)
        if not path.exists():
            self.stdout.write(self.style.WARNING(f'File "{path}" not found, using built-in samples.'))
            return DEFAULT_SAMPLE_DATA

        if path.suffix.lower() == '.json':
            with path.open(encoding='utf-8') as fh:
                payload = json.load(fh)
            if isinstance(payload, dict):
                payload = payload.get('products', [])
            if not isinstance(payload, list):
                raise CommandError('JSON file must contain a list of products or {"products": [...]}.')
            return payload

        if path.suffix.lower() in {'.csv', '.tsv'}:
            delimiter = '\t' if path.suffix.lower() == '.tsv' else ','
            with path.open(newline='', encoding='utf-8') as fh:
                reader = csv.DictReader(fh, delimiter=delimiter)
                return list(reader)

        raise CommandError('Unsupported file extension. Use .json, .csv or .tsv.')

    def _create_or_update_product(self, data: Dict, status: str):
        required_fields = [
            'style_id',
            'color_id',
            'size_desc',
            'style_desc',
            'color_desc',
            'division',
            'department',
            'subdepartment',
            'product_class',
            'subclass',
        ]
        missing = [field for field in required_fields if not data.get(field)]
        if missing:
            self.stdout.write(self.style.WARNING(f"Skipping record missing fields: {', '.join(missing)}"))
            return None

        department, _ = Department.objects.get_or_create(name=data['department'])
        subdept, _ = SubDepartment.objects.get_or_create(name=data['subdepartment'])
        product_class, _ = ProductClass.objects.get_or_create(name=data['product_class'])
        subclass, _ = SubClass.objects.get_or_create(name=data['subclass'])

        ingestion_batch = data.get('ingestion_batch', 1)
        dim_desc = data.get('dim_desc')

        product, created = BaseProduct.objects.get_or_create(
            style_id=data['style_id'],
            ingestion_batch=ingestion_batch,
            defaults={
                'style_desc': data['style_desc'],
                'processing_status': status,
                'department': department,
                'dept_name': department.name,
                'subdepartment': subdept,
                'subdept_name': subdept.name,
                'class_field': product_class,
                'class_name': product_class.name,
                'subclass': subclass,
                'subclass_name': subclass.name,
                'ingestion_batch': ingestion_batch,
                'is_active': True,
            },
        )

        # Create/update color + size + image records (new schema)
        color, _ = ProductColor.objects.get_or_create(
            base_product=product,
            color_id=data['color_id'],
            defaults={'color_desc': data.get('color_desc')},
        )
        if data.get('color_desc') and not color.color_desc:
            color.color_desc = data.get('color_desc')
            color.save(update_fields=['color_desc'])

        ProductSize.objects.get_or_create(
            product_color=color,
            size_desc=data.get('size_desc'),
            dim_desc=dim_desc,
        )

        ProductImage.objects.get_or_create(
            product_color=color,
            image_url=data.get(
                'image_url',
                f'https://placehold.co/600x800?text={data["style_id"]}',
            ),
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f'  • {product.style_desc} ({product.color_desc}) [{product.processing_status}]'
                )
            )

        return product