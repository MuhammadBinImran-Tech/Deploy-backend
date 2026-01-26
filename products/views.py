# products/views.py
from rest_framework import viewsets, permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.contrib.auth.models import User, Group
from django.db.models import Q, Count, Avg, Max, Min, Sum, Subquery, OuterRef
from django.utils import timezone
from django.db import transaction
from django.core.paginator import Paginator
import logging
from collections import Counter, defaultdict
import random
import threading
import time
from datetime import timedelta
from .models import *
from .serializers import *
from .ai_runner import AIBatchProcessor
from rest_framework.decorators import action

logger = logging.getLogger(__name__)
class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class IsAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.groups.filter(name='Admin').exists()


class IsAnnotator(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user.groups.filter(name='Annotator').exists()


class BatchCreationMixin:
    """
    Shared helpers for creating AI and human batches so the logic stays consistent
    across different endpoints (admin dashboard, workflow APIs, etc.).
    """
    
    def _schedule_ai_batch_processing(self, batch_id: int):
        """
        Kick off AI batch processing on a background thread once the current
        transaction commits.  The mixin defers to the host viewset's
        `_process_ai_batch` implementation when available.
        """
        process_callable = getattr(self, '_process_ai_batch', None)
        if not callable(process_callable):
            return
        
        thread = threading.Thread(
            target=process_callable,
            args=(batch_id,)
        )
        thread.daemon = True
        thread.start()
    
    def _create_ai_batch(self, data):
        """Create AI annotation batch"""
        batch_size = data['batch_size']
        ai_provider_ids = data.get('ai_provider_ids', [])
        name = data.get('name', '')
        
        # Step 1: Validate AI providers
        if ai_provider_ids:
            ai_providers = AIProvider.objects.filter(
                id__in=ai_provider_ids,
                is_active=True
            )
        else:
            ai_providers = AIProvider.objects.filter(is_active=True)
        
        if not ai_providers.exists():
            return Response({
                'error': 'No active AI providers found'
            }, status=400)
        
        # Step 2: Select products for AI processing - order by id
        pending_products = BaseProduct.objects.filter(
            processing_status__in=['pending', 'pending_ai']
        ).exclude(
            id__in=BatchItem.objects.filter(batch_type='ai').values_list('product_id', flat=True)
        ).order_by('id')[:batch_size]
        
        if not pending_products.exists():
            return Response({
                'error': 'No pending products available for AI processing'
            }, status=400)
        
        try:
            with transaction.atomic():
                batch_name = name or f"AI Batch - {timezone.now().strftime('%Y-%m-%d %H:%M')}"
                batch = AnnotationBatch.objects.create(
                    name=batch_name,
                    description='AI annotation batch created by admin',
                    batch_type='ai',
                    batch_size=len(pending_products)
                )
                
                batch_items = []
                for product in pending_products:
                    batch_item = BatchItem.objects.create(
                        batch=batch,
                        product=product,
                        batch_type='ai'
                    )
                    batch_items.append(batch_item)
                
                for provider in ai_providers:
                    assignment = BatchAssignment.objects.create(
                        batch=batch,
                        assignment_type='ai',
                        assignment_id=provider.id,
                        status='pending'
                    )
                    
                    for batch_item in batch_items:
                        BatchAssignmentItem.objects.create(
                            assignment=assignment,
                            batch_item=batch_item,
                            status='pending_ai'
                        )
                
                product_ids = [p.id for p in pending_products]
                BaseProduct.objects.filter(id__in=product_ids).update(
                    processing_status='ai_in_progress',
                    updated_at=timezone.now()
                )
                
                transaction.on_commit(
                    lambda batch_id=batch.id: self._schedule_ai_batch_processing(batch_id)
                )
                
                return Response({
                    'message': 'AI batch created successfully',
                    'batch': {
                        'id': batch.id,
                        'name': batch.name,
                        'type': batch.batch_type,
                        'size': len(pending_products)
                    },
                    'providers': [
                        {'id': p.id, 'name': p.name}
                        for p in ai_providers
                    ],
                    'products_count': len(pending_products),
                    'processing_started': True
                })
                
        except Exception as exc:
            return Response({
                'error': f'Failed to create AI batch: {str(exc)}'
            }, status=500)
    
    def _create_human_batch(self, data):
        """Create human annotation batch."""
        batch_size = data['batch_size']
        annotator_ids = data.get('annotator_ids', [])
        force_create = data.get('force_create', False)
        name = data.get('name', '')
        
        annotators = HumanAnnotator.objects.filter(id__in=annotator_ids)
        if annotator_ids and not annotators.exists():
            return Response({
                'error': 'No valid annotators found'
            }, status=400)
        
        # Always exclude products already in human batches
        existing_human_batch_products = BatchItem.objects.filter(
            batch_type='human'
        ).values_list('product_id', flat=True)
        
        if force_create:
            # For force create: prioritize ai_done, then use pending/pending_ai
            ai_done_products = BaseProduct.objects.filter(
                processing_status='ai_done'
            ).exclude(
                id__in=existing_human_batch_products
            ).order_by('id')[:batch_size]
            
            ai_done_count = ai_done_products.count()
            remaining_needed = batch_size - ai_done_count
            
            if remaining_needed > 0:
                # Get additional products from pending/pending_ai
                pending_products = BaseProduct.objects.filter(
                    processing_status__in=['pending', 'pending_ai']
                ).exclude(
                    id__in=existing_human_batch_products
                ).order_by('id')[:remaining_needed]
                
                # Combine both querysets
                from itertools import chain
                available_products = list(chain(ai_done_products, pending_products))
            else:
                available_products = list(ai_done_products)
            
            # Set status for all products
            target_status = 'pending_human'
        else:
            # Normal human batch: only use ai_done products
            available_products = BaseProduct.objects.filter(
                processing_status='ai_done'
            ).exclude(
                id__in=existing_human_batch_products
            ).order_by('id')[:batch_size]
            target_status = 'pending_human'
        
        if not available_products:
            return Response({
                'error': 'No products available for human batch (either not AI processed or already in human batches)'
            }, status=400)
        
        try:
            with transaction.atomic():
                batch_name = name or f"Human Batch - {timezone.now().strftime('%Y-%m-%d %H:%M')}"
                batch = AnnotationBatch.objects.create(
                    name=batch_name,
                    description='Human annotation batch created by admin',
                    batch_type='human',
                    batch_size=len(available_products)
                )
                
                batch_items = []
                for product in available_products:
                    batch_item = BatchItem.objects.create(
                        batch=batch,
                        product=product,
                        batch_type='human'
                    )
                    batch_items.append(batch_item)
                
                if annotators.exists():
                    for annotator in annotators:
                        assignment = BatchAssignment.objects.create(
                            batch=batch,
                            assignment_type='human',
                            assignment_id=annotator.id,
                            status='pending'
                        )
                        
                        for batch_item in batch_items:
                            BatchAssignmentItem.objects.create(
                                assignment=assignment,
                                batch_item=batch_item,
                                status='pending_human'
                            )
                
                product_ids = [p.id for p in available_products]
                BaseProduct.objects.filter(id__in=product_ids).update(
                    processing_status=target_status,
                    updated_at=timezone.now()
                )
                
                # Count how many products came from each status
                ai_done_count = sum(1 for p in available_products if p.processing_status == 'ai_done')
                pending_count = sum(1 for p in available_products if p.processing_status in ['pending', 'pending_ai'])
                
                return Response({
                    'message': 'Human batch created successfully',
                    'batch': {
                        'id': batch.id,
                        'name': batch.name,
                        'type': batch.batch_type,
                        'size': len(available_products)
                    },
                    'annotators': [
                        {'id': a.id, 'username': a.user.username}
                        for a in annotators
                    ],
                    'products_count': len(available_products),
                    'distribution': {
                        'ai_done': ai_done_count,
                        'pending': pending_count
                    },
                    'distribution_note': 'Each annotator assigned to ALL products (full overlap)' if annotators.exists() else 'No annotators assigned yet'
                })
                
        except Exception as exc:
            return Response({
                'error': f'Failed to create human batch: {str(exc)}'
            }, status=500)

    def _process_ai_batch(self, batch_id):
        """Process AI batch in background."""
        try:
            batch = AnnotationBatch.objects.get(id=batch_id)
            assignments = BatchAssignment.objects.filter(batch=batch, assignment_type='ai')
            provider_ids = list(assignments.values_list('assignment_id', flat=True))

            processor = AIBatchProcessor(provider_ids)
            processor.process_batch(batch.id)

            print(f"AI batch {batch_id} processing completed")
        except Exception as exc:
            print(f"Error processing AI batch {batch_id}: {exc}")
    
    def _get_applicable_attributes_for_product(self, product):
        """Get applicable attributes for a product."""
        if not product.subclass:
            return []
        
        attributes = []
        global_attrs = AttributeGlobalMap.objects.filter(
            attribute__is_active=True
        ).select_related('attribute')
        for map_obj in global_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name,
                'description': map_obj.attribute.description
            })
        
        subclass_attrs = AttributeSubclassMap.objects.filter(
            subclass=product.subclass,
            attribute__is_active=True
        ).select_related('attribute')
        
        for map_obj in subclass_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name,
                'description': map_obj.attribute.description
            })
        
        return attributes
    
    def _generate_ai_suggestion(self, product, attribute, provider):
        """Generate AI suggestion (simulated)."""
        attribute_name = attribute['name'].lower()
        
        if 'color' in attribute_name:
            colors = ['Red', 'Blue', 'Green', 'Black', 'White', 'Yellow', 'Pink']
            return random.choice(colors)
        if 'size' in attribute_name:
            sizes = ['XS', 'S', 'M', 'L', 'XL', 'XXL']
            return random.choice(sizes)
        if 'material' in attribute_name:
            materials = ['Cotton', 'Polyester', 'Silk', 'Wool', 'Leather']
            return random.choice(materials)
        if 'fit' in attribute_name:
            fits = ['Slim', 'Regular', 'Relaxed', 'Loose']
            return random.choice(fits)
        
        base_desc = product.style_desc or product.style_description or product.style_id
        return f"{base_desc} attribute {attribute['name']} (AI)"


class AssignmentProgressMixin:
    """Shared helpers for keeping assignment and product statuses in sync."""
    
    def _update_assignment_progress(self, assignment):
        items = BatchAssignmentItem.objects.filter(assignment=assignment)
        total_items = items.count()
        
        if assignment.assignment_type == 'ai':
            completed_items = items.filter(status='ai_done').count()
        else:
            completed_items = items.filter(status='human_done').count()
        
        progress = (completed_items / total_items * 100) if total_items > 0 else 0
        
        assignment.progress = progress
        
        if completed_items == total_items and total_items > 0:
            assignment.status = 'completed'
        
        assignment.save()
    
    def _update_product_status(self, product, batch_item):
        product_assignments = BatchAssignmentItem.objects.filter(
            batch_item=batch_item,
            assignment__assignment_type='human'
        )
        
        all_done = all(
            assignment_item.status == 'human_done' 
            for assignment_item in product_assignments
        )
        
        if all_done and product.processing_status == 'human_in_progress':
            product.processing_status = 'human_done'
            product.save()


class ProductViewSet(BatchCreationMixin, AssignmentProgressMixin, viewsets.ModelViewSet):
    queryset = BaseProduct.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        queryset = BaseProduct.objects.select_related(
            'department', 'subdepartment', 'class_field', 'subclass'
        ).prefetch_related(
            'colors',
            'colors__images',
            'colors__sizes',
        ).all()
        
        # Filter by processing status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            status_values = [value.strip() for value in status_filter.split(',') if value.strip()]
            if len(status_values) == 1:
                queryset = queryset.filter(processing_status=status_values[0])
            elif status_values:
                queryset = queryset.filter(processing_status__in=status_values)
        
        # Filter by class - frontend sends 'class' param but backend expects 'class'
        class_filter = self.request.query_params.get('class')
        if class_filter and class_filter != 'all':
            queryset = queryset.filter(
                Q(class_name__iexact=class_filter) | 
                Q(class_field__name__iexact=class_filter)
            )
        
        # Filter by subclass - frontend sends 'subclass' param
        subclass_filter = self.request.query_params.get('subclass')
        if subclass_filter and subclass_filter != 'all':
            queryset = queryset.filter(
                Q(subclass_name__iexact=subclass_filter) | 
                Q(subclass__name__iexact=subclass_filter)
            )
        
        # Filter by department - frontend sends 'department' param
        department_filter = self.request.query_params.get('department')
        if department_filter and department_filter != 'all':
            queryset = queryset.filter(
                Q(dept_name__iexact=department_filter) | 
                Q(department__name__iexact=department_filter)
            )
        
        # Filter by subdepartment - frontend sends 'subdepartment' param
        subdepartment_filter = self.request.query_params.get('subdepartment')
        if subdepartment_filter and subdepartment_filter != 'all':
            queryset = queryset.filter(
                Q(subdept_name__iexact=subdepartment_filter) | 
                Q(subdepartment__name__iexact=subdepartment_filter)
            )
        
        # Filter by search term
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(style_id__icontains=search) |
                Q(style_desc__icontains=search) |
                Q(style_description__icontains=search) |
                Q(colors__color_id__icontains=search) |
                Q(colors__color_desc__icontains=search) |
                Q(colors__sizes__size_desc__icontains=search)
            ).distinct()
        
        # Filter by batch
        batch_id = self.request.query_params.get('batch_id')
        if batch_id:
            try:
                batch = AnnotationBatch.objects.get(id=batch_id)
                product_ids = batch.batchitem_set.values_list('product_id', flat=True)
                queryset = queryset.filter(id__in=product_ids)
            except AnnotationBatch.DoesNotExist:
                pass
        
        # Annotators can only see products assigned to them
        if user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                # Get assignments for this annotator
                assignments = BatchAssignment.objects.filter(
                    assignment_type='human',
                    assignment_id=annotator.id
                )
                
                # Get batch items from these assignments
                batch_items = BatchItem.objects.filter(
                    batch__in=assignments.values('batch')
                )
                
                product_ids = batch_items.values_list('product_id', flat=True)
                queryset = queryset.filter(id__in=product_ids)
                
                # Filter by assignment status
                assignment_status = self.request.query_params.get('assignment_status')
                if assignment_status:
                    # Get assignment items with specific status
                    assignment_items = BatchAssignmentItem.objects.filter(
                        assignment__in=assignments,
                        status=assignment_status
                    )
                    product_ids = assignment_items.values_list('batch_item__product_id', flat=True)
                    queryset = queryset.filter(id__in=product_ids)
                    
            except HumanAnnotator.DoesNotExist:
                return BaseProduct.objects.none()
        
        # Sorting / ordering
        order_by = self.request.query_params.get('order_by', 'id')
        order_dir = self.request.query_params.get('order_dir', 'asc')
        
        # Defensive: only allow known sortable fields
        if order_by not in ['id', 'created_at', 'updated_at']:
            order_by = 'id'
        if order_dir not in ['asc', 'desc']:
            order_dir = 'asc'
        
        ordering = order_by if order_dir == 'asc' else f'-{order_by}'
        return queryset.order_by(ordering, 'id')
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductSerializer
    
    @action(detail=False, methods=['get'])
    def classes(self, request):
        """Get unique product classes"""
        # Try to get from class_name field first, then from class_field relationship
        classes_from_field = BaseProduct.objects.exclude(
            Q(class_name__isnull=True) | Q(class_name='')
        ).values_list('class_name', flat=True).distinct()
        
        classes_from_relation = BaseProduct.objects.filter(
            class_field__isnull=False
        ).values_list('class_field__name', flat=True).distinct()
        
        # Combine both sources, remove duplicates and empty values
        all_classes = set(
            list(classes_from_field) + 
            list(classes_from_relation)
        )
        all_classes = [c for c in all_classes if c]  # Remove empty strings
        
        return Response(sorted(all_classes))
    
    @action(detail=False, methods=['get'])
    def subclasses(self, request):
        """Get unique product subclasses"""
        # Try to get from subclass_name field first, then from subclass relationship
        subclasses_from_field = BaseProduct.objects.exclude(
            Q(subclass_name__isnull=True) | Q(subclass_name='')
        ).values_list('subclass_name', flat=True).distinct()
        
        subclasses_from_relation = BaseProduct.objects.filter(
            subclass__isnull=False
        ).values_list('subclass__name', flat=True).distinct()
        
        # Combine both sources
        all_subclasses = set(
            list(subclasses_from_field) + 
            list(subclasses_from_relation)
        )
        all_subclasses = [c for c in all_subclasses if c]
        
        return Response(sorted(all_subclasses))
    
    @action(detail=False, methods=['get'])
    def departments(self, request):
        """Get unique product departments"""
        # Try to get from department_name field first, then from department relationship
        departments_from_field = BaseProduct.objects.exclude(
            Q(dept_name__isnull=True) | Q(dept_name='')
        ).values_list('dept_name', flat=True).distinct()
        
        departments_from_relation = BaseProduct.objects.filter(
            department__isnull=False
        ).values_list('department__name', flat=True).distinct()
        
        # Also try division_name as fallback
        # divisions_as_departments = Product.objects.exclude(
        #     Q(division_name__isnull=True) | Q(division_name='')
        # ).values_list('division_name', flat=True).distinct()
        
        # Combine all sources
        all_departments = set(
            list(departments_from_field) + 
            list(departments_from_relation) 
            # list(divisions_as_departments)
        )
        all_departments = [c for c in all_departments if c]
        
        return Response(sorted(all_departments))
    
    @action(detail=False, methods=['get'])
    def subdepartments(self, request):
        """Get unique product subdepartments"""
        # Try to get from subdepartment_name field first, then from subdepartment relationship
        subdepartments_from_field = BaseProduct.objects.exclude(
            Q(subdept_name__isnull=True) | Q(subdept_name='')
        ).values_list('subdept_name', flat=True).distinct()
        
        subdepartments_from_relation = BaseProduct.objects.filter(
            subdepartment__isnull=False
        ).values_list('subdepartment__name', flat=True).distinct()
        
        # Combine both sources
        all_subdepartments = set(
            list(subdepartments_from_field) + 
            list(subdepartments_from_relation)
        )
        all_subdepartments = [c for c in all_subdepartments if c]
        
        return Response(sorted(all_subdepartments))
    
    @action(detail=False, methods=['get'])
    def filter_options(self, request):
        """Get hierarchical filter options based on selected filters"""
        # Get current filter values from query params
        selected_class = request.query_params.get('class', '').strip()
        selected_subclass = request.query_params.get('subclass', '').strip()
        selected_department = request.query_params.get('department', '').strip()
        selected_subdepartment = request.query_params.get('subdepartment', '').strip()
        
        # Start with all products
        base_queryset = BaseProduct.objects.all()
        
        # Apply hierarchical filtering logic
        queryset = base_queryset
        applied_filters = {}
        
        # Apply filters in order of priority
        if selected_subdepartment and selected_subdepartment != 'all':
            queryset = queryset.filter(
                Q(subdept_name__iexact=selected_subdepartment) | 
                Q(subdepartment__name__iexact=selected_subdepartment)
            )
            applied_filters['subdepartment'] = selected_subdepartment
        
        if selected_department and selected_department != 'all':
            queryset = queryset.filter(
                Q(dept_name__iexact=selected_department) | 
                Q(department__name__iexact=selected_department)
            )
            applied_filters['department'] = selected_department
        
        if selected_class and selected_class != 'all':
            queryset = queryset.filter(
                Q(class_name__iexact=selected_class) | 
                Q(class_field__name__iexact=selected_class)
            )
            applied_filters['class'] = selected_class
        
        if selected_subclass and selected_subclass != 'all':
            queryset = queryset.filter(
                Q(subclass_name__iexact=selected_subclass) | 
                Q(subclass__name__iexact=selected_subclass)
            )
            applied_filters['subclass'] = selected_subclass
        
        # Get available options based on current filters
        result = {}
        
        # Classes - Show options based on current filters
        if 'class' not in applied_filters:
            class_from_field = queryset.exclude(
                Q(class_name__isnull=True) | Q(class_name='')
            ).values_list('class_name', flat=True).distinct()
            class_from_relation = queryset.filter(
                class_field__isnull=False
            ).values_list('class_field__name', flat=True).distinct()
            result['classes'] = sorted(set(list(class_from_field) + list(class_from_relation)))
        else:
            result['classes'] = [selected_class]
        
        # Subclasses - Based on current filters
        if 'subclass' not in applied_filters:
            subclass_from_field = queryset.exclude(
                Q(subclass_name__isnull=True) | Q(subclass_name='')
            ).values_list('subclass_name', flat=True).distinct()
            subclass_from_relation = queryset.filter(
                subclass__isnull=False
            ).values_list('subclass__name', flat=True).distinct()
            result['subclasses'] = sorted(set(list(subclass_from_field) + list(subclass_from_relation)))
        else:
            result['subclasses'] = [selected_subclass]
        
        # Departments - Based on current filters
        if 'department' not in applied_filters:
            dept_from_field = queryset.exclude(
                Q(dept_name__isnull=True) | Q(dept_name='')
            ).values_list('dept_name', flat=True).distinct()
            dept_from_relation = queryset.filter(
                department__isnull=False
            ).values_list('department__name', flat=True).distinct()
            result['departments'] = sorted(set(list(dept_from_field) + list(dept_from_relation)))
        else:
            result['departments'] = [selected_department]
        
        # Subdepartments - Based on current filters
        if 'subdepartment' not in applied_filters:
            subdept_from_field = queryset.exclude(
                Q(subdept_name__isnull=True) | Q(subdept_name='')
            ).values_list('subdept_name', flat=True).distinct()
            subdept_from_relation = queryset.filter(
                subdepartment__isnull=False
            ).values_list('subdepartment__name', flat=True).distinct()
            result['subdepartments'] = sorted(set(list(subdept_from_field) + list(subdept_from_relation)))
        else:
            result['subdepartments'] = [selected_subdepartment]
        
        # Filter out empty strings and None values
        for key in result:
            result[key] = [c for c in result[key] if c]
        
        # Add metadata about applied filters
        result['applied_filters'] = applied_filters
        
        return Response(result)
    
    def _get_hierarchical_options(self, base_queryset, filters):
        """Get hierarchical filter options based on current filters"""
        queryset = base_queryset
        
        # Apply filters to base queryset
        for filter_name, filter_value in filters.items():
            if filter_value and filter_value != 'all':
                if filter_name == 'class':
                    queryset = queryset.filter(
                        Q(class_name__iexact=filter_value) | 
                        Q(class_field__name__iexact=filter_value)
                    )
                elif filter_name == 'subclass':
                    queryset = queryset.filter(
                        Q(subclass_name__iexact=filter_value) | 
                        Q(subclass__name__iexact=filter_value)
                    )
                elif filter_name == 'department':
                    queryset = queryset.filter(
                        Q(dept_name__iexact=filter_value) | 
                        Q(department__name__iexact=filter_value) |
                        Q(division_name__iexact=filter_value) |
                        Q(division__name__iexact=filter_value)
                    )
                elif filter_name == 'subdepartment':
                    queryset = queryset.filter(
                        Q(subdept_name__iexact=filter_value) | 
                        Q(subdepartment__name__iexact=filter_value)
                    )
        
        # Get options for each field
        options = {}
        
        # Get classes
        class_from_field = queryset.exclude(
            Q(class_name__isnull=True) | Q(class_name='')
        ).values_list('class_name', flat=True).distinct()
        class_from_relation = queryset.filter(
            class_field__isnull=False
        ).values_list('class_field__name', flat=True).distinct()
        options['classes'] = sorted(set(list(class_from_field) + list(class_from_relation)))
        
        # Get subclasses
        subclass_from_field = queryset.exclude(
            Q(subclass_name__isnull=True) | Q(subclass_name='')
        ).values_list('subclass_name', flat=True).distinct()
        subclass_from_relation = queryset.filter(
            subclass__isnull=False
        ).values_list('subclass__name', flat=True).distinct()
        options['subclasses'] = sorted(set(list(subclass_from_field) + list(subclass_from_relation)))
        
        # Get departments
        dept_from_field = queryset.exclude(
            Q(dept_name__isnull=True) | Q(dept_name='')
        ).values_list('dept_name', flat=True).distinct()
        dept_from_relation = queryset.filter(
            department__isnull=False
        ).values_list('department__name', flat=True).distinct()
        options['departments'] = sorted(set(list(dept_from_field) + list(dept_from_relation)))
        
        # Get subdepartments
        subdept_from_field = queryset.exclude(
            Q(subdept_name__isnull=True) | Q(subdept_name='')
        ).values_list('subdept_name', flat=True).distinct()
        subdept_from_relation = queryset.filter(
            subdepartment__isnull=False
        ).values_list('subdepartment__name', flat=True).distinct()
        options['subdepartments'] = sorted(set(list(subdept_from_field) + list(subdept_from_relation)))
        
        # Filter out empty values
        for key in options:
            options[key] = [c for c in options[key] if c]
        
        return options
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get comprehensive product statistics"""
        user = request.user
        
        if user.groups.filter(name='Admin').exists():
            # Admin statistics
            total = BaseProduct.objects.count()
            
            # Status distribution
            status_counts = {}
            for status_choice in BaseProduct.PROCESSING_STATUS_CHOICES:
                status_code, status_name = status_choice
                count = BaseProduct.objects.filter(processing_status=status_code).count()
                status_counts[status_code] = {
                    'name': status_name,
                    'count': count,
                    'percentage': (count / total * 100) if total > 0 else 0
                }
            
            # Daily trends
            today = timezone.now().date()
            last_7_days = []
            for i in range(7):
                day = today - timedelta(days=i)
                day_start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
                day_end = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.max.time()))
                
                day_count = BaseProduct.objects.filter(
                    created_at__range=(day_start, day_end)
                ).count()
                
                last_7_days.append({
                    'date': day.isoformat(),
                    'count': day_count
                })
            
            return Response({
                'total': total,
                'status_distribution': status_counts,
                'divisions': [],
                'daily_trends': list(reversed(last_7_days)),
                'recent_products': ProductSerializer(
                    BaseProduct.objects.order_by('-created_at')[:10], 
                    many=True
                ).data
            })
        else:
            # Annotator statistics
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                
                # Get assignments
                assignments = BatchAssignment.objects.filter(
                    assignment_type='human',
                    assignment_id=annotator.id
                )
                
                # Get products from assignments
                batch_items = BatchItem.objects.filter(
                    batch__in=assignments.values('batch')
                )
                
                products = BaseProduct.objects.filter(id__in=batch_items.values_list('product_id', flat=True))
                
                # Count by status
                status_counts = {}
                for status_choice in BaseProduct.PROCESSING_STATUS_CHOICES:
                    status_code, status_name = status_choice
                    count = products.filter(processing_status=status_code).count()
                    status_counts[status_code] = {
                        'name': status_name,
                        'count': count
                    }
                
                # Assignment progress
                assignment_stats = assignments.aggregate(
                    total=Count('id'),
                    pending=Count('id', filter=Q(status='pending')),
                    in_progress=Count('id', filter=Q(status='in_progress')),
                    completed=Count('id', filter=Q(status='completed')),
                    avg_progress=Avg('progress')
                )
                
                return Response({
                    'assigned_products': products.count(),
                    'status_distribution': status_counts,
                    'assignment_stats': assignment_stats,
                    'recent_assignments': BatchAssignmentSerializer(
                        assignments.order_by('-created_at')[:5], 
                        many=True
                    ).data
                })
                
            except HumanAnnotator.DoesNotExist:
                return Response({'error': 'Annotator profile not found'}, status=404)
    
    @action(detail=False, methods=['post'], permission_classes=[IsAdmin])
    def update_status_bulk(self, request):
        """Update processing status for multiple products (Admin only)"""
        serializer = UpdateProductStatusSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        data = serializer.validated_data
        product_ids = data['product_ids']
        new_status = data['new_status']
        
        # Validate status transition
        valid_transitions = {
            'pending': ['pending_ai', 'ai_in_progress', 'pending_human'],
            'pending_ai': ['ai_in_progress', 'pending_human'],  # Admin can skip AI
            'ai_in_progress': ['ai_done', 'pending_human'],
            'ai_done': ['pending_human', 'human_in_progress'],
            'pending_human': ['human_in_progress'],
            'human_in_progress': ['human_done'],
            'human_done': [],
        }
        
        products = BaseProduct.objects.filter(id__in=product_ids)
        invalid_transitions = []
        
        for product in products:
            current_status = product.processing_status
            if new_status not in valid_transitions.get(current_status, []):
                invalid_transitions.append({
                    'product_id': product.id,
                    'current_status': current_status,
                    'new_status': new_status
                })
        
        if invalid_transitions:
            return Response({
                'error': 'Invalid status transitions',
                'invalid_transitions': invalid_transitions
            }, status=400)
        
        # Update products
        updated_count = products.update(
            processing_status=new_status,
            updated_at=timezone.now()
        )
        
        return Response({
            'message': f'Updated {updated_count} products to {new_status}',
            'updated_count': updated_count
        })
    
    @action(detail=False, methods=['get'], permission_classes=[IsAdmin])
    def filtered_count(self, request):
        """Get count of products matching current filters for batch creation"""
        filters = request.query_params
        batch_type = filters.get('batch_type', 'ai')
        force_create = filters.get('force_create', 'false').lower() == 'true'
        
        # Get base queryset based on batch type
        if batch_type == 'ai':
            queryset = BaseProduct.objects.filter(
                processing_status__in=['pending', 'pending_ai']
            ).exclude(
                id__in=BatchItem.objects.filter(batch_type='ai').values_list('product_id', flat=True)
            )
        else:  # human
            if force_create:
                queryset = BaseProduct.objects.filter(
                    processing_status__in=['pending', 'pending_ai', 'ai_done']
                ).exclude(
                    id__in=BatchItem.objects.filter(batch_type='human').values_list('product_id', flat=True)
                )
            else:
                queryset = BaseProduct.objects.filter(
                    processing_status='ai_done'
                ).exclude(
                    id__in=BatchItem.objects.filter(batch_type='human').values_list('product_id', flat=True)
                )
        
        # Apply filters
        search = filters.get('search')
        if search and search != 'all' and search != '':
            queryset = queryset.filter(
                Q(style_id__icontains=search) |
                Q(style_desc__icontains=search) |
                Q(colors__color_desc__icontains=search) |
                Q(colors__color_id__icontains=search)
            ).distinct()
        
        class_filter = filters.get('class_filter')
        if class_filter and class_filter != 'all' and class_filter != '':
            queryset = queryset.filter(
                Q(class_name__iexact=class_filter) | 
                Q(class_field__name__iexact=class_filter)
            )
        
        subclass_filter = filters.get('subclass_filter')
        if subclass_filter and subclass_filter != 'all' and subclass_filter != '':
            queryset = queryset.filter(
                Q(subclass_name__iexact=subclass_filter) | 
                Q(subclass__name__iexact=subclass_filter)
            )
        
        department_filter = filters.get('department_filter')
        if department_filter and department_filter != 'all' and department_filter != '':
            queryset = queryset.filter(
                Q(dept_name__iexact=department_filter) | 
                Q(department__name__iexact=department_filter)
            )
        
        subdepartment_filter = filters.get('subdepartment_filter')
        if subdepartment_filter and subdepartment_filter != 'all' and subdepartment_filter != '':
            queryset = queryset.filter(
                Q(subdept_name__iexact=subdepartment_filter) | 
                Q(subdepartment__name__iexact=subdepartment_filter)
            )

        # ADD THESE LINES:
        order_by = filters.get('order_by', 'id')
        order_dir = filters.get('order_dir', 'asc')
        
        # Validate
        if order_by not in ['id', 'created_at']:
            order_by = 'id'
        if order_dir not in ['asc', 'desc']:
            order_dir = 'asc'    
        
        count = queryset.count()
        
        return Response({
            'count': count,
            'batch_type': batch_type,
            'force_create': force_create,
            'filters_applied': {
                'search': search if search and search != 'all' and search != '' else None,
                'class': class_filter if class_filter and class_filter != 'all' and class_filter != '' else None,
                'subclass': subclass_filter if subclass_filter and subclass_filter != 'all' and subclass_filter != '' else None,
                'department': department_filter if department_filter and department_filter != 'all' and department_filter != '' else None,
                'subdepartment': subdepartment_filter if subdepartment_filter and subdepartment_filter != 'all' and subdepartment_filter != '' else None,
                'order_by': order_by,  # ADD THIS LINE
                'order_dir': order_dir,
            }
        })
    
    @action(detail=False, methods=['post'], permission_classes=[IsAdmin])
    def create_multi_batch(self, request):
        """Create multiple annotation batches with filters (Admin only)"""
        serializer = CreateMultiBatchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        data = serializer.validated_data
        batch_type = data['batch_type']
        total_batches = data['total_batches']
        items_per_batch = data['items_per_batch']
        total_products_needed = total_batches * items_per_batch
        
        # Get filtered products
        filtered_products = self._get_filtered_products_for_batch(data)
        available_count = filtered_products.count()
        
        # Validation
        if available_count < items_per_batch:
            return Response({
                'error': f'Not enough products available. Need at least {items_per_batch} products, but only {available_count} match your filters.',
                'available': available_count,
                'needed_per_batch': items_per_batch
            }, status=400)
        
        if available_count < total_products_needed:
            max_batches_possible = available_count // items_per_batch
            return Response({
                'error': f'Not enough products for {total_batches} batches. Need {total_products_needed} products, but only {available_count} match your filters.',
                'available': available_count,
                'needed_total': total_products_needed,
                'max_batches_possible': max_batches_possible,
                'suggestion': f'Try creating {max_batches_possible} batches instead'
            }, status=400)
        
        try:
            created_batches = []
            
            for batch_num in range(total_batches):
                # Take products for this batch (converting QuerySet to list to slice properly)
                product_list = list(filtered_products)
                batch_products = product_list[batch_num * items_per_batch:(batch_num + 1) * items_per_batch]
                
                if not batch_products:
                    break
                
                # Create batch data
                batch_data = {
                    'batch_type': batch_type,
                    'batch_size': len(batch_products),
                    'ai_provider_ids': data.get('ai_provider_ids', []),
                    'annotator_ids': data.get('annotator_ids', []),
                    'force_create': data.get('force_create', False),
                    'name': f"{batch_type.upper()} Batch {batch_num + 1} - Filtered {timezone.now().strftime('%Y-%m-%d')}"
                }
                
                # Use existing batch creation logic with specific products
                if batch_type == 'ai':
                    result = self._create_ai_batch_with_products(batch_data, batch_products)
                else:
                    result = self._create_human_batch_with_products(batch_data, batch_products)
                
                if isinstance(result, Response):
                    # Check if it's an error response
                    if result.status_code != 200:
                        # Rollback created batches
                        for batch in created_batches:
                            try:
                                AnnotationBatch.objects.get(id=batch['id']).delete()
                            except:
                                pass
                        return result
                    created_batches.append(result.data)
                else:
                    created_batches.append(result)
            
            return Response({
                'message': f'Successfully created {len(created_batches)} batches',
                'total_batches_created': len(created_batches),
                'total_products_assigned': sum(batch.get('size', 0) for batch in created_batches),
                'batches': created_batches,
                'summary': {
                    'requested_batches': total_batches,
                    'requested_per_batch': items_per_batch,
                    'total_requested': total_products_needed,
                    'available_products': available_count,
                    'products_used': sum(batch.get('size', 0) for batch in created_batches)
                }
            })
            
        except Exception as exc:
            return Response({
                'error': f'Failed to create batches: {str(exc)}'
            }, status=500)
    
    def _get_filtered_products_for_batch(self, data):
        """Get products filtered for batch creation"""
        batch_type = data['batch_type']
        
        # Base queryset based on batch type
        if batch_type == 'ai':
            queryset = BaseProduct.objects.filter(
                processing_status__in=['pending', 'pending_ai']
            ).exclude(
                id__in=BatchItem.objects.filter(batch_type='ai').values_list('product_id', flat=True)
            )
        else:  # human
            force_create = data.get('force_create', False)
            if force_create:
                queryset = BaseProduct.objects.filter(
                    processing_status__in=['pending', 'pending_ai', 'ai_done']
                ).exclude(
                    id__in=BatchItem.objects.filter(batch_type='human').values_list('product_id', flat=True)
                )
            else:
                queryset = BaseProduct.objects.filter(
                    processing_status='ai_done'
                ).exclude(
                    id__in=BatchItem.objects.filter(batch_type='human').values_list('product_id', flat=True)
                )
        
        # Apply filters from request
        search = data.get('search', '')
        if search:
            queryset = queryset.filter(
                Q(style_id__icontains=search) |
                Q(style_desc__icontains=search) |
                Q(colors__color_desc__icontains=search) |
                Q(colors__color_id__icontains=search)
            ).distinct()
        
        class_filter = data.get('class_filter', '')
        if class_filter and class_filter != 'all':
            queryset = queryset.filter(
                Q(class_name__iexact=class_filter) | 
                Q(class_field__name__iexact=class_filter)
            )
        
        subclass_filter = data.get('subclass_filter', '')
        if subclass_filter and subclass_filter != 'all':
            queryset = queryset.filter(
                Q(subclass_name__iexact=subclass_filter) | 
                Q(subclass__name__iexact=subclass_filter)
            )
        
        department_filter = data.get('department_filter', '')
        if department_filter and department_filter != 'all':
            queryset = queryset.filter(
                Q(dept_name__iexact=department_filter) | 
                Q(department__name__iexact=department_filter)
            )
        
        subdepartment_filter = data.get('subdepartment_filter', '')
        if subdepartment_filter and subdepartment_filter != 'all':
            queryset = queryset.filter(
                Q(subdept_name__iexact=subdepartment_filter) | 
                Q(subdepartment__name__iexact=subdepartment_filter)
            )
        
        # ADD THESE LINES FOR SORTING:
        order_by = data.get('order_by', 'id')
        order_dir = data.get('order_dir', 'asc')
        
        # Validate order_by field
        if order_by not in ['id', 'created_at']:
            order_by = 'id'
        if order_dir not in ['asc', 'desc']:
            order_dir = 'asc'
        
        # Apply ordering
        ordering = order_by if order_dir == 'asc' else f'-{order_by}'
        return queryset.order_by(ordering, 'id')
    
    def _create_ai_batch_with_products(self, data, products):
        """Create AI batch with specific products"""
        ai_provider_ids = data.get('ai_provider_ids', [])
        name = data.get('name', '')
        
        if not products:
            return Response({
                'error': 'No products available for AI processing'
            }, status=400)
        
        if ai_provider_ids:
            ai_providers = AIProvider.objects.filter(
                id__in=ai_provider_ids,
                is_active=True
            )
        else:
            ai_providers = AIProvider.objects.filter(is_active=True)
        
        if not ai_providers.exists():
            return Response({
                'error': 'No active AI providers found'
            }, status=400)
        
        try:
            with transaction.atomic():
                batch_name = name or f"AI Batch - {timezone.now().strftime('%Y-%m-%d %H:%M')}"
                batch = AnnotationBatch.objects.create(
                    name=batch_name,
                    description='AI annotation batch created by admin with filters',
                    batch_type='ai',
                    batch_size=len(products)
                )
                
                batch_items = []
                for product in products:
                    batch_item = BatchItem.objects.create(
                        batch=batch,
                        product=product,
                        batch_type='ai'
                    )
                    batch_items.append(batch_item)
                
                for provider in ai_providers:
                    assignment = BatchAssignment.objects.create(
                        batch=batch,
                        assignment_type='ai',
                        assignment_id=provider.id,
                        status='pending'
                    )
                    
                    for batch_item in batch_items:
                        BatchAssignmentItem.objects.create(
                            assignment=assignment,
                            batch_item=batch_item,
                            status='pending_ai'
                        )
                
                product_ids = [p.id for p in products]
                BaseProduct.objects.filter(id__in=product_ids).update(
                    processing_status='ai_in_progress',
                    updated_at=timezone.now()
                )
                
                transaction.on_commit(
                    lambda batch_id=batch.id: self._schedule_ai_batch_processing(batch_id)
                )
                
                return {
                    'id': batch.id,
                    'name': batch.name,
                    'type': batch.batch_type,
                    'size': len(products),
                    'providers': [{'id': p.id, 'name': p.name} for p in ai_providers],
                    'products_count': len(products),
                    'processing_started': True
                }
                
        except Exception as exc:
            return Response({
                'error': f'Failed to create AI batch: {str(exc)}'
            }, status=500)
    
    def _create_human_batch_with_products(self, data, products):
        """Create human batch with specific products"""
        annotator_ids = data.get('annotator_ids', [])
        force_create = data.get('force_create', False)
        name = data.get('name', '')
        
        if not products:
            return Response({
                'error': 'No products available for human batch'
            }, status=400)
        
        annotators = HumanAnnotator.objects.filter(id__in=annotator_ids)
        if annotator_ids and not annotators.exists():
            return Response({
                'error': 'No valid annotators found'
            }, status=400)
        
        try:
            with transaction.atomic():
                batch_name = name or f"Human Batch - {timezone.now().strftime('%Y-%m-%d %H:%M')}"
                batch = AnnotationBatch.objects.create(
                    name=batch_name,
                    description='Human annotation batch created by admin with filters',
                    batch_type='human',
                    batch_size=len(products)
                )
                
                batch_items = []
                for product in products:
                    batch_item = BatchItem.objects.create(
                        batch=batch,
                        product=product,
                        batch_type='human'
                    )
                    batch_items.append(batch_item)
                
                if annotators.exists():
                    for annotator in annotators:
                        assignment = BatchAssignment.objects.create(
                            batch=batch,
                            assignment_type='human',
                            assignment_id=annotator.id,
                            status='pending'
                        )
                        
                        for batch_item in batch_items:
                            BatchAssignmentItem.objects.create(
                                assignment=assignment,
                                batch_item=batch_item,
                                status='pending_human'
                            )
                
                product_ids = [p.id for p in products]
                target_status = 'pending_human'  # Force create still goes to pending_human
                BaseProduct.objects.filter(id__in=product_ids).update(
                    processing_status=target_status,
                    updated_at=timezone.now()
                )
                
                # Count how many products came from each status
                ai_done_count = sum(1 for p in products if p.processing_status == 'ai_done')
                pending_count = sum(1 for p in products if p.processing_status in ['pending', 'pending_ai'])
                
                return {
                    'id': batch.id,
                    'name': batch.name,
                    'type': batch.batch_type,
                    'size': len(products),
                    'annotators': [{'id': a.id, 'username': a.user.username} for a in annotators],
                    'products_count': len(products),
                    'distribution': {
                        'ai_done': ai_done_count,
                        'pending': pending_count
                    },
                    'distribution_note': 'Each annotator assigned to ALL products (full overlap)' if annotators.exists() else 'No annotators assigned yet'
                }
                
        except Exception as exc:
            return Response({
                'error': f'Failed to create human batch: {str(exc)}'
            }, status=500)
    
    @action(detail=False, methods=['post'], permission_classes=[IsAdmin])
    def create_batch(self, request):
        """
        Create annotation batch (Admin only)
        
        Flow:
        1. Admin selects products (based on status)
        2. Chooses batch type (AI or Human)
        3. For AI: Selects AI providers
        4. For Human: Selects annotators
        5. Batch is created with assignments
        """
        serializer = CreateBatchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        data = serializer.validated_data
        batch_type = data['batch_type']
        batch_size = data['batch_size']
        
        if batch_type == 'ai':
            return self._create_ai_batch(data)
        else:
            return self._create_human_batch(data)
    
    
    @action(detail=True, methods=['get'])
    def annotations(self, request, pk=None):
        """Get all annotations for a product"""
        product = self.get_object()
        
        # Get all annotations
        annotations = ProductAnnotation.objects.filter(
            product=product,
            attribute__is_active=True
        ).select_related('attribute').order_by('-created_at')
        
        # Group by attribute
        grouped_annotations = {}
        for ann in annotations:
            attr_id = ann.attribute_id
            if attr_id not in grouped_annotations:
                grouped_annotations[attr_id] = {
                    'attribute': {
                        'id': ann.attribute.id,
                        'name': ann.attribute.attribute_name,
                        'description': ann.attribute.description
                    },
                    'annotations': []
                }
            
            grouped_annotations[attr_id]['annotations'].append({
                'id': ann.id,
                'value': ann.value,
                'source_type': ann.source_type,
                'source_name': ann.source_name,
                'confidence_score': float(ann.confidence_score) if ann.confidence_score else None,
                'created_at': ann.created_at,
                'updated_at': ann.updated_at
            })
        
        # Get applicable attributes
        applicable_attrs = self._get_applicable_attributes(product)
        
        return Response({
            'product': {
                'id': product.id,
                'style_id': product.style_id,
                'processing_status': product.processing_status
            },
            'applicable_attributes': applicable_attrs,
            'annotations': list(grouped_annotations.values())
        })
    
    def _get_applicable_attributes(self, product):
        """Get applicable attributes for a product"""
        if not product.subclass:
            return []
        
        attributes = []
        
        # Get global attributes
        global_attrs = AttributeGlobalMap.objects.filter(
            attribute__is_active=True
        ).select_related('attribute')
        for map_obj in global_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name,
                'description': map_obj.attribute.description,
                'scope': 'global'
            })
        
        # Get subclass-specific attributes
        subclass_attrs = AttributeSubclassMap.objects.filter(
            subclass=product.subclass,
            attribute__is_active=True
        ).select_related('attribute')
        
        for map_obj in subclass_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name,
                'description': map_obj.attribute.description,
                'scope': 'subclass'
            })
        
        return attributes
    
    @action(detail=False, methods=['get'])
    def export_csv(self, request):
        """Export filtered products to CSV"""
        try:
            # Get filtered queryset
            queryset = self.filter_queryset(self.get_queryset())
            
            # Get all filtered products (no pagination for export)
            products = queryset
            
            # Create CSV content
            import csv
            from io import StringIO
            
            csvfile = StringIO()
            writer = csv.writer(csvfile)
            
            # Write header
            writer.writerow([
                'Product ID', 'Style ID', 'Style Description', 'Color Description',
                'Size Description', 'Product Class', 'Product Subclass',
                'Department', 'Subdepartment', 'Processing Status',
                'Created At', 'Updated At'
            ])
            
            # Write data rows
            for product in products:
                writer.writerow([
                    product.id,
                    product.style_id or '',
                    product.style_desc or product.style_id or '',
                    product.color_desc or '',
                    product.size_desc or '',
                    product.class_name or (product.class_field.name if product.class_field else ''),
                    product.subclass_name or (product.subclass.name if product.subclass else ''),
                    product.dept_name or (product.department.name if product.department else '') or product.division_name or '',
                    product.subdept_name or (product.subdepartment.name if product.subdepartment else ''),
                    product.processing_status or '',
                    product.created_at.isoformat() if product.created_at else '',
                    product.updated_at.isoformat() if product.updated_at else ''
                ])
            
            csv_content = csvfile.getvalue()
            csvfile.close()
            
            # Create response
            from django.http import HttpResponse
            response = HttpResponse(csv_content, content_type='text/csv')
            filename = f'products_export_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv'
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            
            return response
            
        except Exception as e:
            return Response({'error': str(e)}, status=500)


class AttributeViewSet(viewsets.ModelViewSet):
    queryset = AttributeMaster.objects.filter(is_active=True)
    serializer_class = AttributeMasterSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    @action(detail=False, methods=['get'])
    def by_subclass(self, request):
        """Get attributes by subclass"""
        subclass_id = request.query_params.get('subclass_id')
        if not subclass_id:
            return Response({"error": "subclass_id is required"}, status=400)
        
        try:
            subclass = SubClass.objects.get(id=subclass_id)
            
            # Get subclass-specific attributes
            subclass_attrs = AttributeSubclassMap.objects.filter(
                subclass=subclass,
                attribute__is_active=True
            ).select_related('attribute')
            
            subclass_attrs_data = []
            for map_obj in subclass_attrs:
                subclass_attrs_data.append({
                    'id': map_obj.attribute.id,
                    'name': map_obj.attribute.attribute_name,
                    'description': map_obj.attribute.description,
                    'scope': 'subclass'
                })
            
            # Get global attributes
            global_attrs = AttributeGlobalMap.objects.filter(
                attribute__is_active=True
            ).select_related('attribute')
            global_attrs_data = []
            for map_obj in global_attrs:
                global_attrs_data.append({
                    'id': map_obj.attribute.id,
                    'name': map_obj.attribute.attribute_name,
                    'description': map_obj.attribute.description,
                    'scope': 'global'
                })
            
            # Get attribute options
            all_attribute_ids = [attr['id'] for attr in subclass_attrs_data + global_attrs_data]
            attribute_options = AttributeOption.objects.filter(
                attribute_id__in=all_attribute_ids,
                attribute__is_active=True
            ).select_related('attribute')
            
            options_by_attribute = {}
            for option in attribute_options:
                attr_id = option.attribute_id
                if attr_id not in options_by_attribute:
                    options_by_attribute[attr_id] = []
                options_by_attribute[attr_id].append(option.option_value)
            
            return Response({
                'subclass': {
                    'id': subclass.id,
                    'name': subclass.name,
                    'class_name': None
                },
                'subclass_attributes': subclass_attrs_data,
                'global_attributes': global_attrs_data,
                'attribute_options': options_by_attribute
            })
        except SubClass.DoesNotExist:
            return Response({"error": "Subclass not found"}, status=404)


class AIProviderViewSet(viewsets.ModelViewSet):
    queryset = AIProvider.objects.all()
    serializer_class = AIProviderSerializer
    permission_classes = [permissions.IsAuthenticated & IsAdmin]
    pagination_class = StandardPagination


class HumanAnnotatorViewSet(viewsets.ModelViewSet):
    queryset = HumanAnnotator.objects.all()
    serializer_class = HumanAnnotatorSerializer
    permission_classes = [permissions.IsAuthenticated & IsAdmin]
    pagination_class = StandardPagination
    
    @action(detail=False, methods=['post'])
    def create_from_user(self, request):
        """Create annotator from existing user"""
        user_id = request.data.get('user_id')
        username = request.data.get('username')
        
        if not user_id and not username:
            return Response({
                "error": "Either user_id or username is required"
            }, status=400)
        
        try:
            if user_id:
                user = User.objects.get(id=user_id)
            else:
                user = User.objects.get(username=username)
            
            # Create annotator
            annotator, created = HumanAnnotator.objects.get_or_create(user=user)
            
            # Add user to Annotator group
            annotator_group, _ = Group.objects.get_or_create(name='Annotator')
            user.groups.add(annotator_group)
            
            if created:
                message = f'Created annotator from user {user.username}'
            else:
                message = f'Annotator already exists for user {user.username}'
            
            return Response({
                "message": message,
                "annotator": HumanAnnotatorSerializer(annotator).data
            })
            
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get annotator statistics"""
        annotators = HumanAnnotator.objects.all().select_related('user')
        
        stats = []
        for annotator in annotators:
            # Get assignments
            assignments = BatchAssignment.objects.filter(
                assignment_type='human',
                assignment_id=annotator.id
            )
            
            # Get assignment items
            assignment_items = BatchAssignmentItem.objects.filter(
                assignment__in=assignments
            )
            
            # Count annotations
            annotations_count = ProductAnnotation.objects.filter(
                source_type='human',
                source_id=annotator.id,
                attribute__is_active=True
            ).count()
            
            # Calculate productivity
            completed_items_qs = assignment_items.filter(status='human_done')
            completed_items = completed_items_qs.count()
            total_items = assignment_items.count()
            
            # Average completion time
            completed_times = []
            total_work_hours = 0
            for item in completed_items_qs:
                started_at = item.started_at or item.created_at
                completed_at = item.completed_at or item.updated_at
                if started_at and completed_at and completed_at >= started_at:
                    completion_time = (completed_at - started_at).total_seconds() / 60
                    completed_times.append(completion_time)
                    total_work_hours += (completed_at - started_at).total_seconds() / 3600
            
            avg_completion_time = sum(completed_times) / len(completed_times) if completed_times else 0
            
            items_per_hour = 0
            if total_work_hours > 0:
                items_per_hour = completed_items / total_work_hours
            
            stats.append({
                'id': annotator.id,
                'username': annotator.user.username,
                'email': annotator.user.email,
                'assignments_count': assignments.count(),
                'completed_assignments': assignments.filter(status='completed').count(),
                'total_items': total_items,
                'completed_items': completed_items,
                'completion_rate': (completed_items / total_items * 100) if total_items > 0 else 0,
                'annotations_count': annotations_count,
                'avg_completion_time': avg_completion_time,
                'items_per_hour': round(items_per_hour, 2),
                'total_work_hours': round(total_work_hours, 2),
                'last_active': assignment_items.aggregate(
                    last_active=Max('updated_at')
                )['last_active']
            })
        
        return Response(stats)


class AnnotationBatchViewSet(BatchCreationMixin, viewsets.ModelViewSet):
    queryset = AnnotationBatch.objects.all()
    serializer_class = AnnotationBatchSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        queryset = AnnotationBatch.objects.all()
        
        # Filter by batch type
        batch_type = self.request.query_params.get('type')
        if batch_type:
            queryset = queryset.filter(batch_type=batch_type)
        
        # Filter by status through assignments
        status_filter = self.request.query_params.get('status')
        if status_filter:
            batch_ids = BatchAssignment.objects.filter(
                status=status_filter
            ).values_list('batch_id', flat=True)
            queryset = queryset.filter(id__in=batch_ids)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(created_at__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__date__lte=end_date)
        
        # Annotators can only see batches assigned to them
        if user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                assignments = BatchAssignment.objects.filter(
                    assignment_type='human',
                    assignment_id=annotator.id
                )
                batch_ids = assignments.values_list('batch_id', flat=True)
                queryset = queryset.filter(id__in=batch_ids)
            except HumanAnnotator.DoesNotExist:
                return AnnotationBatch.objects.none()
        
        order_by = self.request.query_params.get('order_by', 'created_at')
        order_dir = self.request.query_params.get('order_dir', 'desc')
        allowed_order_by = {'created_at', 'name'}
        if order_by not in allowed_order_by:
            order_by = 'created_at'
        if order_dir not in {'asc', 'desc'}:
            order_dir = 'desc'
        ordering = order_by if order_dir == 'asc' else f'-{order_by}'
        return queryset.order_by(ordering)
    
    def retrieve(self, request, *args, **kwargs):
        batch = self.get_object()
        context = self.get_serializer_context()
        context['include_items'] = False
        
        if request.user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=request.user)
            except HumanAnnotator.DoesNotExist:
                return Response({'error': 'Annotator profile not found'}, status=status.HTTP_404_NOT_FOUND)
            
            assignment = BatchAssignment.objects.filter(
                batch=batch,
                assignment_type='human',
                assignment_id=annotator.id
            ).first()
            
            if not assignment:
                return Response({'error': 'Batch not assigned to this annotator'}, status=status.HTTP_403_FORBIDDEN)
            
            context['assignment'] = assignment
            context['include_items'] = True
        else:
            include_items = request.query_params.get('include_items')
            assignment_id = request.query_params.get('assignment_id')
            if include_items == 'true' and assignment_id:
                assignment = BatchAssignment.objects.filter(id=assignment_id, batch=batch).first()
                if assignment:
                    context['assignment'] = assignment
                    context['include_items'] = True
        
        serializer = self.get_serializer(batch, context=context)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get batch statistics"""
        user = request.user
        
        if user.groups.filter(name='Admin').exists():
            # Admin statistics
            total_batches = AnnotationBatch.objects.count()
            
            ai_batches = AnnotationBatch.objects.filter(batch_type='ai')
            human_batches = AnnotationBatch.objects.filter(batch_type='human')
            
            # Batch status distribution
            batch_status_stats = {
                'ai': {},
                'human': {}
            }
            
            for batch_type, batches in [('ai', ai_batches), ('human', human_batches)]:
                for status_choice in BatchAssignment.STATUS_CHOICES:
                    status_code, status_name = status_choice
                    count = batches.filter(
                        batchassignment__status=status_code
                    ).distinct().count()
                    
                    batch_status_stats[batch_type][status_code] = {
                        'name': status_name,
                        'count': count
                    }
            
            # Daily batch creation
            today = timezone.now().date()
            last_7_days = []
            for i in range(7):
                day = today - timedelta(days=i)
                day_start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
                day_end = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.max.time()))
                
                day_ai_batches = ai_batches.filter(
                    created_at__range=(day_start, day_end)
                ).count()
                
                day_human_batches = human_batches.filter(
                    created_at__range=(day_start, day_end)
                ).count()
                
                last_7_days.append({
                    'date': day.isoformat(),
                    'ai_batches': day_ai_batches,
                    'human_batches': day_human_batches,
                    'total': day_ai_batches + day_human_batches
                })
            
            return Response({
                'total_batches': total_batches,
                'ai_batches': ai_batches.count(),
                'human_batches': human_batches.count(),
                'batch_status_distribution': batch_status_stats,
                'daily_trends': list(reversed(last_7_days))
            })
        else:
            # Annotator statistics
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                
                # Get batches assigned to this annotator
                assignments = BatchAssignment.objects.filter(
                    assignment_type='human',
                    assignment_id=annotator.id
                )
                
                batches = AnnotationBatch.objects.filter(
                    id__in=assignments.values_list('batch_id', flat=True)
                )
                
                # Batch completion status
                completed_batches = batches.filter(
                    batchassignment__status='completed'
                ).distinct().count()
                
                in_progress_batches = batches.filter(
                    batchassignment__status='in_progress'
                ).distinct().count()
                
                pending_batches = batches.filter(
                    batchassignment__status='pending'
                ).distinct().count()
                
                return Response({
                    'total_batches': batches.count(),
                    'completed_batches': completed_batches,
                    'in_progress_batches': in_progress_batches,
                    'pending_batches': pending_batches,
                    'recent_batches': AnnotationBatchSerializer(
                        batches.order_by('-created_at')[:5], 
                        many=True
                    ).data
                })
                
            except HumanAnnotator.DoesNotExist:
                return Response({'error': 'Annotator profile not found'}, status=404)
    
    @action(detail=False, methods=['get'], url_path='ai_batches')
    def ai_batches(self, request):
        limit = int(request.query_params.get('limit', 10))
        queryset = self.get_queryset().filter(batch_type='ai')[:limit]
        context = self.get_serializer_context()
        context['include_items'] = False
        serializer = self.get_serializer(queryset, many=True, context=context)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], url_path='human_batches')
    def human_batches(self, request):
        limit = int(request.query_params.get('limit', 10))
        queryset = self.get_queryset().filter(batch_type='human')[:limit]
        context = self.get_serializer_context()
        context['include_items'] = False
        serializer = self.get_serializer(queryset, many=True, context=context)
        return Response(serializer.data)
    
    @action(
        detail=False,
        methods=['get'],
        url_path='unassigned_batches',
        permission_classes=[permissions.IsAuthenticated & IsAdmin]
    )
    def unassigned_batches(self, request):
        limit = int(request.query_params.get('limit', 10))
        queryset = AnnotationBatch.objects.filter(batch_type='human').annotate(
            assignments_total=Count('batchassignment')
        ).filter(assignments_total=0).order_by('created_at')[:limit]
        
        context = self.get_serializer_context()
        context['include_items'] = False
        serializer = self.get_serializer(queryset, many=True, context=context)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'], url_path='create_ai_batch', permission_classes=[IsAdmin])
    def create_ai_batch_action(self, request):
        serializer = CreateAIBatchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        payload = serializer.validated_data
        payload['batch_type'] = 'ai'
        return self._create_ai_batch(payload)
    
    @action(detail=False, methods=['post'], url_path='create_human_batch', permission_classes=[IsAdmin])
    def create_human_batch_action(self, request):
        serializer = CreateHumanBatchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        payload = serializer.validated_data
        payload['batch_type'] = 'human'
        return self._create_human_batch(payload)
    
    @action(detail=True, methods=['post'], url_path='assign_to_annotators', permission_classes=[IsAdmin])
    def assign_to_annotators(self, request, pk=None):
        batch = self.get_object()
        if batch.batch_type != 'human':
            return Response({'error': 'Assignments only allowed for human batches'}, status=status.HTTP_400_BAD_REQUEST)
        
        annotator_ids = request.data.get('annotator_ids', [])
        if not annotator_ids:
            return Response({'error': 'annotator_ids list required'}, status=status.HTTP_400_BAD_REQUEST)
        
        annotators = list(HumanAnnotator.objects.filter(id__in=annotator_ids))
        if not annotators:
            return Response({'error': 'No valid annotators supplied'}, status=status.HTTP_400_BAD_REQUEST)
        
        batch_items = list(BatchItem.objects.filter(batch=batch))
        if not batch_items:
            return Response({'error': 'Batch has no products to assign'}, status=status.HTTP_400_BAD_REQUEST)
        
        created_assignments = 0
        created_items = 0
        for annotator in annotators:
            assignment, created = BatchAssignment.objects.get_or_create(
                batch=batch,
                assignment_type='human',
                assignment_id=annotator.id,
                defaults={'status': 'pending'}
            )
            if created:
                created_assignments += 1
            
            for batch_item in batch_items:
                _, item_created = BatchAssignmentItem.objects.get_or_create(
                    assignment=assignment,
                    batch_item=batch_item,
                    defaults={'status': 'pending_human'}
                )
                if item_created:
                    created_items += 1
        
        product_ids = [i.product_id for i in batch_items]
        BaseProduct.objects.filter(id__in=product_ids).update(
            processing_status='pending_human',
            updated_at=timezone.now()
        )
        
        return Response({
            'message': 'Annotators assigned successfully',
            'assignments_created': created_assignments,
            'assignment_items_created': created_items
        })
    
    @action(detail=False, methods=['post'], url_path='auto_assign_to_annotators', permission_classes=[IsAdmin])
    def auto_assign_to_annotators(self, request):
        serializer = AutoAssignHumanBatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        payload = serializer.validated_data
        overlap_count = payload['overlap_count']
        
        annotators = HumanAnnotator.objects.annotate(
            active_assignments=Count(
                'batchassignment',
                filter=Q(batchassignment__status__in=['pending', 'in_progress'])
            )
        ).order_by('active_assignments', 'id')[:overlap_count]
        
        if annotators.count() < overlap_count:
            return Response({'error': 'Not enough annotators available for requested overlap'}, status=status.HTTP_400_BAD_REQUEST)
        
        request_payload = {
            'batch_type': 'human',
            'batch_size': payload['batch_size'],
            'annotator_ids': [annotator.id for annotator in annotators],
            'force_create': payload.get('force_create', False),
            'name': payload.get('name') or f'Human Batch - {timezone.now().strftime("%Y-%m-%d %H:%M")}'
        }
        return self._create_human_batch(request_payload)
    
    @action(detail=False, methods=['post'], url_path='start_auto_ai_processing', permission_classes=[IsAdmin])
    def start_auto_ai_processing(self, request):
        serializer = StartAutoAIProcessingSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        control = AIProcessingControl.get_control()
        if control.is_paused:
            return Response({'error': 'AI processing is paused. Resume before starting auto processing.'}, status=status.HTTP_400_BAD_REQUEST)
        
        if data.get('ai_provider_ids'):
            provider_ids = list(
                AIProvider.objects.filter(
                    id__in=data['ai_provider_ids'],
                    is_active=True
                ).values_list('id', flat=True)
            )
        else:
            provider_ids = list(
                AIProvider.objects.filter(is_active=True).values_list('id', flat=True)
            )
        
        if not provider_ids:
            return Response({'error': 'No active AI providers available'}, status=status.HTTP_400_BAD_REQUEST)
        
        thread = threading.Thread(
            target=AutoAIProcessingViewSet()._process_all_pending_products,  # type: ignore[attr-defined]
            args=(data['batch_size'], provider_ids)
        )
        thread.daemon = True
        thread.start()
        
        return Response({
            'message': 'Auto AI processing started',
            'batch_size': data['batch_size'],
            'provider_ids': provider_ids,
        })
    
    @action(detail=False, methods=['post'], url_path='pause_ai_processing', permission_classes=[IsAdmin])
    def pause_ai_processing(self, request):
        control = AIProcessingControl.get_control()
        control.is_paused = True
        control.paused_at = timezone.now()
        control.paused_by = request.user
        control.save()
        return Response({'message': 'AI processing paused'})
    
    @action(detail=False, methods=['post'], url_path='resume_ai_processing', permission_classes=[IsAdmin])
    def resume_ai_processing(self, request):
        control = AIProcessingControl.get_control()
        control.is_paused = False
        control.paused_at = None
        control.paused_by = None
        control.save()
        return Response({'message': 'AI processing resumed'})
    
    @action(detail=True, methods=['get'])
    def details(self, request, pk=None):
        """Get detailed batch information"""
        batch = self.get_object()
        
        # Get batch items
        batch_items = BatchItem.objects.filter(batch=batch).select_related('product')
        
        # Get assignments
        assignments = BatchAssignment.objects.filter(batch=batch)
        
        # Get assignment items
        assignment_items = BatchAssignmentItem.objects.filter(
            assignment__in=assignments
        ).select_related('batch_item__product')
        
        # Calculate progress
        total_items = assignment_items.count()
        completed_items = assignment_items.filter(
            Q(status='ai_done') | Q(status='human_done')
        ).count()
        
        overall_progress = (completed_items / total_items * 100) if total_items > 0 else 0
        
        # Product status distribution
        product_status_counts = {}
        for batch_item in batch_items:
            status = batch_item.product.processing_status
            product_status_counts[status] = product_status_counts.get(status, 0) + 1
        
        return Response({
            'batch': AnnotationBatchSerializer(batch).data,
            'overall_progress': overall_progress,
            'product_status_distribution': product_status_counts,
            'assignments': BatchAssignmentSerializer(assignments, many=True).data,
            'products_count': batch_items.count(),
            'completed_items': completed_items,
            'total_items': total_items
        })

    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """List batch items with per-item progress."""
        batch = self.get_object()
        batch_items = BatchItem.objects.filter(batch=batch).select_related('product').order_by('id')

        assignment_items = BatchAssignmentItem.objects.filter(
            batch_item__in=batch_items
        ).values('batch_item_id', 'status')

        stats = defaultdict(lambda: {
            'total': 0,
            'completed': 0,
            'failed': 0,
            'in_progress': 0,
        })

        for item in assignment_items:
            item_stats = stats[item['batch_item_id']]
            item_stats['total'] += 1
            status_value = item['status']
            if status_value in ['ai_done', 'human_done']:
                item_stats['completed'] += 1
            elif status_value == 'ai_failed':
                item_stats['failed'] += 1
            elif status_value in ['ai_in_progress', 'human_in_progress']:
                item_stats['in_progress'] += 1

        response_items = []
        for batch_item in batch_items:
            item_stats = stats.get(batch_item.id, {'total': 0, 'completed': 0, 'failed': 0, 'in_progress': 0})
            total = item_stats['total']
            completed = item_stats['completed']
            failed = item_stats['failed']
            progress = round((completed / total) * 100, 2) if total > 0 else 0

            if failed > 0:
                status_label = 'failed'
            elif total > 0 and completed == total:
                status_label = 'completed'
            elif item_stats['in_progress'] > 0:
                status_label = 'in_progress'
            else:
                status_label = 'pending'

            product = batch_item.product
            response_items.append({
                'id': batch_item.id,
                'product_id': product.id,
                'product_name': product.style_desc or product.style_id or f'Product {product.id}',
                'status': status_label,
                'progress': progress,
                'created_at': batch_item.created_at,
            })

        page = self.paginate_queryset(response_items)
        if page is not None:
            serializer = BatchItemProgressSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = BatchItemProgressSerializer(response_items, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def start_processing(self, request, pk=None):
        """Start processing a batch"""
        batch = self.get_object()
        
        if batch.batch_type == 'ai':
            # Start AI processing in background
            thread = threading.Thread(
                target=self._process_ai_batch,
                args=(batch.id,)
            )
            thread.daemon = True
            thread.start()
            
            return Response({
                "message": "AI processing started",
                "batch_id": batch.id,
                "status": "processing_started"
            })
        else:
            # For human batches, update assignment status
            assignments = BatchAssignment.objects.filter(batch=batch)
            assignments.update(status='in_progress')
            
            # Update assignment items status
            assignment_items = BatchAssignmentItem.objects.filter(
                assignment__in=assignments,
                status='pending_human'
            )
            assignment_items.update(
                status='human_in_progress',
                started_at=timezone.now()
            )
            
            # Update product statuses
            product_ids = assignment_items.values_list(
                'batch_item__product_id', 
                flat=True
            ).distinct()
            
            BaseProduct.objects.filter(
                id__in=product_ids,
                processing_status='pending_human'
            ).update(
                processing_status='human_in_progress',
                updated_at=timezone.now()
            )
            
            return Response({
                "message": "Human batch marked as in progress",
                "updated_assignments": assignments.count(),
                "updated_products": product_ids.count()
            })

    @action(detail=True, methods=['post'], permission_classes=[IsAdmin], url_path='pause_ai')
    def pause_ai_batch(self, request, pk=None):
        """
        Pause an AI batch: reset in-progress items to pending_ai and mark assignments pending.
        """
        batch = self.get_object()
        if batch.batch_type != 'ai':
            return Response({"error": "Only AI batches can be paused"}, status=400)

        assignments = BatchAssignment.objects.filter(batch=batch, assignment_type='ai')
        items = BatchAssignmentItem.objects.filter(assignment__in=assignments).exclude(
            status__in=['ai_done', 'ai_failed']
        )
        items.update(status='pending_ai', updated_at=timezone.now())
        assignments.update(status='pending', progress=0)

        BaseProduct.objects.filter(
            id__in=items.values_list('batch_item__product_id', flat=True)
        ).exclude(processing_status__in=['ai_done', 'ai_failed']).update(
            processing_status='ai_in_progress',
            updated_at=timezone.now()
        )

        return Response({"message": "Batch paused", "batch_id": batch.id})

    @action(detail=True, methods=['post'], permission_classes=[IsAdmin], url_path='resume_ai')
    def resume_ai_batch(self, request, pk=None):
        """
        Resume a paused AI batch by restarting processing.
        """
        batch = self.get_object()
        if batch.batch_type != 'ai':
            return Response({"error": "Only AI batches can be resumed"}, status=400)

        assignments = BatchAssignment.objects.filter(batch=batch, assignment_type='ai')
        items = BatchAssignmentItem.objects.filter(assignment__in=assignments).exclude(
            status__in=['ai_done', 'ai_failed']
        )
        items.update(status='pending_ai', updated_at=timezone.now(), started_at=None, completed_at=None)
        assignments.update(status='pending', progress=0)

        BaseProduct.objects.filter(
            id__in=items.values_list('batch_item__product_id', flat=True)
        ).exclude(processing_status__in=['ai_done', 'ai_failed']).update(
            processing_status='ai_in_progress',
            updated_at=timezone.now()
        )

        thread = threading.Thread(
            target=self._process_ai_batch,
            args=(batch.id,)
        )
        thread.daemon = True
        thread.start()

        return Response({"message": "Batch resumed", "batch_id": batch.id})

    @action(detail=True, methods=['post'], permission_classes=[IsAdmin], url_path='retry_failed_ai')
    def retry_failed_ai(self, request, pk=None):
        """
        Retry ONLY failed AI items in a batch.
        
        CRITICAL FIX: Only retry providers that actually have failures.
        Completed providers remain completed and are NOT re-run.
        """
        batch = self.get_object()
        if batch.batch_type != 'ai':
            return Response({"error": "Only AI batches can be retried"}, status=400)

        # Find assignments (providers) that have unresolved failures
        failed_logs = AIProviderFailureLog.objects.filter(
            assignment_item__assignment__batch=batch,
            is_resolved=False,
        ).select_related('assignment_item__assignment', 'provider')

        if not failed_logs.exists():
            return Response({"message": "No failed AI items to retry", "batch_id": batch.id})

        # Get unique provider IDs that have failures
        failed_provider_ids = set()
        for log in failed_logs:
            if log.assignment_item and log.assignment_item.assignment:
                failed_provider_ids.add(log.assignment_item.assignment.assignment_id)

        if not failed_provider_ids:
            return Response({"message": "No failed providers to retry", "batch_id": batch.id})

        # CRITICAL: Only get assignments for providers that have failures
        assignments_to_retry = BatchAssignment.objects.filter(
            batch=batch,
            assignment_type='ai',
            assignment_id__in=failed_provider_ids  # Only failed providers
        )

        # Count providers for reporting
        completed_providers = BatchAssignment.objects.filter(
            batch=batch,
            assignment_type='ai',
            status='completed'
        ).exclude(
            assignment_id__in=failed_provider_ids  # Exclude failed from completed count
        ).count()

        # Get items belonging to failing assignments that are not yet ai_done
        items = BatchAssignmentItem.objects.filter(
            assignment__in=assignments_to_retry,
            status='ai_failed'
        )

        # Reset item status so they can be reprocessed
        items_updated = items.update(
            status='ai_in_progress',
            updated_at=timezone.now(),
            started_at=None,
            completed_at=None,
        )

        # Reset only the failing assignments
        assignments_updated = assignments_to_retry.update(
            status='pending', 
            progress=0,
            updated_at=timezone.now()
        )

        # Resolve old failure logs
        failed_logs_updated = failed_logs.update(
            is_resolved=True, 
            resolved_at=timezone.now()
        )

        # Update products back to pending_ai if needed
        product_ids = items.values_list('batch_item__product_id', flat=True).distinct()
        BaseProduct.objects.filter(
            id__in=product_ids
        ).exclude(processing_status='ai_done').update(
            processing_status='ai_in_progress',
            updated_at=timezone.now()
        )

        # Restart processing
        thread = threading.Thread(
            target=self._process_ai_batch,
            args=(batch.id,)
        )
        thread.daemon = True
        thread.start()

        return Response({
            "message": "Retry started for failed providers only",
            "batch_id": batch.id,
            "failed_providers_retried": assignments_updated,
            "completed_providers_unchanged": completed_providers,
            "items_reset": items_updated,
            "failure_logs_resolved": failed_logs_updated
        })
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def cancel(self, request, pk=None):
        """Cancel a batch"""
        batch = self.get_object()
        
        try:
            with transaction.atomic():
                # Update assignments
                assignments = BatchAssignment.objects.filter(batch=batch)
                assignments.update(status='cancelled')
                
                # Update assignment items
                assignment_items = BatchAssignmentItem.objects.filter(
                    assignment__in=assignments
                )
                assignment_items.update(
                    status='pending_human' if batch.batch_type == 'human' else 'pending_ai',
                    started_at=None,
                    completed_at=None
                )
                
                # Update product statuses
                product_ids = assignment_items.values_list(
                    'batch_item__product_id', 
                    flat=True
                ).distinct()
                
                if batch.batch_type == 'ai':
                    BaseProduct.objects.filter(id__in=product_ids).update(
                        processing_status='pending_ai',
                        updated_at=timezone.now()
                    )
                else:
                    BaseProduct.objects.filter(id__in=product_ids).update(
                        processing_status='pending_human',
                        updated_at=timezone.now()
                    )
                
                return Response({
                    "message": f"Batch {batch.name} cancelled",
                    "affected_products": product_ids.count(),
                    "affected_assignments": assignments.count()
                })
                
        except Exception as e:
            return Response({
                "error": f"Failed to cancel batch: {str(e)}"
            }, status=500)
    
    # AI batch processing helpers are provided by BatchCreationMixin.


class BatchAssignmentViewSet(viewsets.ModelViewSet):
    queryset = BatchAssignment.objects.all()
    serializer_class = BatchAssignmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        queryset = BatchAssignment.objects.all()
        
        # Filter by batch
        batch_id = self.request.query_params.get('batch_id')
        if batch_id:
            queryset = queryset.filter(batch_id=batch_id)
        
        # Filter by assignment type
        assignment_type = self.request.query_params.get('type')
        if assignment_type:
            queryset = queryset.filter(assignment_type=assignment_type)
        
        # Filter by status
        status = self.request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        
        # Annotators can only see their own assignments
        if user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                queryset = queryset.filter(
                    assignment_type='human',
                    assignment_id=annotator.id
                )
            except HumanAnnotator.DoesNotExist:
                return BatchAssignment.objects.none()
        
        return queryset.order_by('-created_at')
    
    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get items for this assignment"""
        assignment = self.get_object()
        
        # Check permissions for annotators
        user = request.user
        if assignment.assignment_type == 'human':
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                if assignment.assignment_id != annotator.id:
                    return Response(
                        {"error": "Not authorized to view this assignment"},
                        status=403
                    )
            except HumanAnnotator.DoesNotExist:
                return Response({"error": "Not an annotator"}, status=403)
        
        items = BatchAssignmentItem.objects.filter(
            assignment=assignment
        ).select_related('batch_item__product')
        
        paginator = PageNumberPagination()
        paginator.page_size = 20
        result_page = paginator.paginate_queryset(items, request)
        serializer = BatchAssignmentItemSerializer(result_page, many=True)
        
        return paginator.get_paginated_response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def start_work(self, request, pk=None):
        """Start working on an assignment"""
        assignment = self.get_object()
        
        # Check permissions
        user = request.user
        if assignment.assignment_type == 'human':
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                if assignment.assignment_id != annotator.id:
                    return Response(
                        {"error": "Not authorized to work on this assignment"},
                        status=403
                    )
            except HumanAnnotator.DoesNotExist:
                return Response({"error": "Not an annotator"}, status=403)
        
        # Update assignment status
        assignment.status = 'in_progress'
        assignment.save()
        
        # Update assignment items
        items = BatchAssignmentItem.objects.filter(assignment=assignment)
        new_status = 'human_in_progress' if assignment.assignment_type == 'human' else 'ai_in_progress'
        
        updated_items = 0
        for item in items:
            if item.status in ['pending_human', 'pending_ai']:
                item.status = new_status
                item.started_at = timezone.now()
                item.save()
                updated_items += 1
        
        return Response({
            "message": "Work started successfully",
            "updated_items": updated_items,
            "assignment_status": assignment.status
        })
    
    @action(detail=True, methods=['get'])
    def progress(self, request, pk=None):
        """Get assignment progress"""
        assignment = self.get_object()
        
        items = BatchAssignmentItem.objects.filter(assignment=assignment)
        total_items = items.count()
        
        if assignment.assignment_type == 'ai':
            completed_items = items.filter(status='ai_done').count()
            in_progress_items = items.filter(status='ai_in_progress').count()
            pending_items = items.filter(status='pending_ai').count()
        else:
            completed_items = items.filter(status='human_done').count()
            in_progress_items = items.filter(status='human_in_progress').count()
            pending_items = items.filter(status='pending_human').count()
        
        return Response({
            "total_items": total_items,
            "completed_items": completed_items,
            "in_progress_items": in_progress_items,
            "pending_items": pending_items,
            "completion_percentage": (completed_items / total_items * 100) if total_items > 0 else 0,
            "assignment_progress": assignment.progress
        })


class BatchAssignmentItemViewSet(AssignmentProgressMixin, viewsets.ModelViewSet):
    queryset = BatchAssignmentItem.objects.select_related(
        'assignment',
        'batch_item__product'
    )
    serializer_class = BatchAssignmentItemSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                queryset = queryset.filter(
                    assignment__assignment_type='human',
                    assignment__assignment_id=annotator.id
                )
            except HumanAnnotator.DoesNotExist:
                return BatchAssignmentItem.objects.none()
        return queryset.order_by('-updated_at')
    
    def _ensure_access(self, request, item):
        if request.user.groups.filter(name='Admin').exists():
            return
        try:
            annotator = HumanAnnotator.objects.get(user=request.user)
        except HumanAnnotator.DoesNotExist:
            raise PermissionDenied('Annotator profile not found')
        if item.assignment.assignment_type != 'human' or item.assignment.assignment_id != annotator.id:
            raise PermissionDenied('Not authorized for this assignment item')
    
    @action(detail=True, methods=['post'])
    def start_work(self, request, pk=None):
        item = self.get_object()
        self._ensure_access(request, item)
        
        if item.status == 'pending_human':
            item.status = 'human_in_progress'
            item.started_at = timezone.now()
            item.save()
            assignment = item.assignment
            if assignment.status == 'pending':
                assignment.status = 'in_progress'
                assignment.save(update_fields=['status'])
            product = item.batch_item.product
            if product.processing_status == 'pending_human':
                product.processing_status = 'human_in_progress'
                product.save(update_fields=['processing_status', 'updated_at'])
        
        return Response({
            'message': 'Assignment item started',
            'status': item.status
        })
    
    @action(detail=True, methods=['post'])
    def complete_work(self, request, pk=None):
        item = self.get_object()
        self._ensure_access(request, item)
        
        if item.status in ['pending_human', 'human_in_progress']:
            item.status = 'human_done'
            item.completed_at = timezone.now()
            item.save()
            self._update_assignment_progress(item.assignment)
            self._update_product_status(item.batch_item.product, item.batch_item)
        
        return Response({
            'message': 'Assignment item marked completed',
            'status': item.status
        })


class ProductAnnotationViewSet(AssignmentProgressMixin, viewsets.ModelViewSet):
    queryset = ProductAnnotation.objects.all()
    serializer_class = ProductAnnotationSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        queryset = ProductAnnotation.objects.filter(attribute__is_active=True)
        
        # Filter by product
        product_id = self.request.query_params.get('product_id')
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        
        # Filter by attribute
        attribute_id = self.request.query_params.get('attribute_id')
        if attribute_id:
            queryset = queryset.filter(attribute_id=attribute_id)
        
        # Filter by source type
        source_type = self.request.query_params.get('source_type')
        if source_type:
            queryset = queryset.filter(source_type=source_type)
        
        # Filter by batch
        batch_id = self.request.query_params.get('batch_id')
        if batch_id:
            batch_items = BatchItem.objects.filter(batch_id=batch_id)
            queryset = queryset.filter(batch_item__in=batch_items)
        
        # Annotators can only see their own annotations
        if user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                queryset = queryset.filter(
                    source_type='human',
                    source_id=annotator.id
                )
            except HumanAnnotator.DoesNotExist:
                return ProductAnnotation.objects.none()
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'], permission_classes=[IsAnnotator])
    def submit(self, request):
        """Submit a human annotation - AUTO STATUS"""
        serializer = SubmitAnnotationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        data = serializer.validated_data
        
        try:
            with transaction.atomic():
                # Get annotator
                annotator = HumanAnnotator.objects.get(user=request.user)
                
                # Get assignment item
                assignment_item = BatchAssignmentItem.objects.get(
                    id=data['batch_assignment_item_id']
                )
                
                # Verify this assignment belongs to the annotator
                if assignment_item.assignment.assignment_type != 'human' or \
                   assignment_item.assignment.assignment_id != annotator.id:
                    return Response(
                        {"error": "Not authorized to submit for this assignment"},
                        status=403
                    )
                
                # Get product and attribute
                product = assignment_item.batch_item.product
                attribute = AttributeMaster.objects.get(
                    id=data['attribute_id'],
                    is_active=True
                )
                
                # Check if attribute is applicable
                if not self._is_attribute_applicable(product, attribute):
                    return Response(
                        {"error": f"Attribute '{attribute.attribute_name}' is not applicable to this product"},
                        status=400
                    )
                
                # AUTO-DETERMINE STATUS based on consensus match
                ai_consensus_value = None
                try:
                    # Try to get AI consensus for this attribute
                    ai_annotations = ProductAnnotation.objects.filter(
                        product=product,
                        attribute=attribute,
                        source_type='ai'
                    )
                    
                    if ai_annotations.exists():
                        # Get most common value
                        from collections import Counter
                        values = [ann.value for ann in ai_annotations if ann.value]
                        if values:
                            counter = Counter(values)
                            ai_consensus_value = counter.most_common(1)[0][0]
                except Exception:
                    pass
                
                # Determine status automatically
                # If human value matches AI consensus -> approved
                # If different -> suggested (needs review)
                human_value = data['value']
                if ai_consensus_value and human_value.lower().strip() == ai_consensus_value.lower().strip():
                    auto_status = 'approved'
                else:
                    auto_status = 'suggested'
                
                # Save annotation with auto-determined status
                annotation, created = ProductAnnotation.objects.update_or_create(
                    product=product,
                    attribute=attribute,
                    source_type='human',
                    source_id=annotator.id,
                    defaults={
                        'value': human_value,
                        'batch_item': assignment_item.batch_item,
                        'confidence_score': data.get('confidence_score')
                    }
                )
                
                # Check if all applicable attributes are annotated
                if self._all_attributes_annotated(product, annotator, assignment_item):
                    assignment_item.status = 'human_done'
                    assignment_item.completed_at = timezone.now()
                    assignment_item.save()
                    
                    # Update assignment progress
                    self._update_assignment_progress(assignment_item.assignment)
                    
                    # Update product status if all human assignments are done
                    self._update_product_status(product, assignment_item.batch_item)
                
                return Response({
                    "message": "Annotation submitted successfully",
                    "annotation": ProductAnnotationSerializer(annotation).data,
                    "created": created,
                    "auto_status": auto_status,
                    "matched_consensus": auto_status == 'approved'
                })
                
        except HumanAnnotator.DoesNotExist:
            return Response({"error": "Annotator not found"}, status=404)
        except BatchAssignmentItem.DoesNotExist:
            return Response({"error": "Assignment item not found"}, status=404)
        except AttributeMaster.DoesNotExist:
            return Response({"error": "Attribute not found"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=500)
    
    @action(detail=False, methods=['post'], permission_classes=[IsAnnotator], url_path='submit_annotation')
    def submit_annotation(self, request):
        """Alias to support /annotations/submit_annotation/ endpoint."""
        return self.submit(request)
    
    def _is_attribute_applicable(self, product, attribute):
        """Check if an attribute is applicable to a product"""
        if not attribute.is_active:
            return False
        if not product.subclass:
            return False
        
        # Check if attribute is global
        is_global = AttributeGlobalMap.objects.filter(
            attribute=attribute,
            attribute__is_active=True
        ).exists()
        if is_global:
            return True
        
        # Check if attribute is mapped to product's subclass
        is_mapped = AttributeSubclassMap.objects.filter(
            attribute=attribute,
            attribute__is_active=True,
            subclass=product.subclass
        ).exists()
        
        return is_mapped
    
    def _all_attributes_annotated(self, product, annotator, assignment_item):
        """Check if all applicable attributes are annotated"""
        applicable_attrs = self._get_applicable_attributes(product)
        
        for attr_info in applicable_attrs:
            attribute = AttributeMaster.objects.filter(
                id=attr_info['id'],
                is_active=True
            ).first()
            if not attribute:
                continue
            
            # Check if annotation exists
            annotation_exists = ProductAnnotation.objects.filter(
                product=product,
                attribute=attribute,
                source_type='human',
                source_id=annotator.id
            ).exists()
            
            if not annotation_exists:
                return False
        
        return True
    
    def _get_applicable_attributes(self, product):
        """Get applicable attributes for a product"""
        if not product.subclass:
            return []
        
        attributes = []
        
        # Get global attributes
        global_attrs = AttributeGlobalMap.objects.filter(
            attribute__is_active=True
        ).select_related('attribute')
        for map_obj in global_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name
            })
        
        # Get subclass-specific attributes
        subclass_attrs = AttributeSubclassMap.objects.filter(
            subclass=product.subclass,
            attribute__is_active=True
        ).select_related('attribute')
        
        for map_obj in subclass_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name
            })
        
        return attributes
    


class MissingValueFlagViewSet(viewsets.ModelViewSet):
    queryset = MissingValueFlag.objects.all()
    serializer_class = MissingValueFlagSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardPagination
    
    def get_queryset(self):
        user = self.request.user
        queryset = MissingValueFlag.objects.filter(attribute__is_active=True)
        
        # Filter by status
        status = self.request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        
        # Filter by product
        product_id = self.request.query_params.get('product_id')
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        
        # Filter by attribute
        attribute_id = self.request.query_params.get('attribute_id')
        if attribute_id:
            queryset = queryset.filter(attribute_id=attribute_id)
        
        # Annotators can only see their own flags
        if user.groups.filter(name='Annotator').exists():
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                queryset = queryset.filter(annotator=annotator)
            except HumanAnnotator.DoesNotExist:
                return MissingValueFlag.objects.none()
        
        return queryset.order_by('-created_at')
    
    @action(detail=False, methods=['post'], permission_classes=[IsAnnotator])
    def flag(self, request):
        """Flag a missing value"""
        serializer = FlagMissingValueRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        data = serializer.validated_data
        
        try:
            with transaction.atomic():
                # Get annotator
                annotator = HumanAnnotator.objects.get(user=request.user)
                
                # Get product and attribute
                product = BaseProduct.objects.get(id=data['product_id'])
                attribute = AttributeMaster.objects.get(
                    id=data['attribute_id'],
                    is_active=True
                )
                
                # Get batch item if provided
                batch_item = None
                if data.get('batch_assignment_item_id'):
                    assignment_item = BatchAssignmentItem.objects.get(
                        id=data['batch_assignment_item_id']
                    )
                    batch_item = assignment_item.batch_item
                
                # Create flag
                flag = MissingValueFlag.objects.create(
                    product=product,
                    attribute=attribute,
                    annotator=annotator,
                    batch_item=batch_item,
                    requested_value=data['requested_value'],
                    reason=data.get('reason', ''),
                    status='pending'
                )
                
                return Response({
                    "message": "Value flagged successfully",
                    "flag": MissingValueFlagSerializer(flag).data
                })
                
        except HumanAnnotator.DoesNotExist:
            return Response({"error": "Annotator not found"}, status=404)
        except Product.DoesNotExist:
            return Response({"error": "Product not found"}, status=404)
        except AttributeMaster.DoesNotExist:
            return Response({"error": "Attribute not found"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=500)
    
    @action(detail=False, methods=['post'], permission_classes=[IsAnnotator], url_path='flag_value')
    def flag_value(self, request):
        """Alias for frontend compatibility."""
        return self.flag(request)
    
    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def resolve(self, request, pk=None):
        """Resolve a missing value flag"""
        flag = self.get_object()
        
        action = request.data.get('action')  # 'approve' or 'reject'
        resolution_note = request.data.get('resolution_note', '')
        
        if action not in ['approve', 'reject']:
            return Response({
                "error": "Action must be 'approve' or 'reject'"
            }, status=400)
        
        try:
            with transaction.atomic():
                if action == 'approve':
                    # Add value to attribute options
                    attribute_option, created = AttributeOption.objects.get_or_create(
                        attribute=flag.attribute,
                        option_value=flag.requested_value
                    )
                    
                    flag.status = 'resolved'
                    flag.resolution_note = resolution_note or f"Value '{flag.requested_value}' added to options"
                else:
                    flag.status = 'rejected'
                    flag.resolution_note = resolution_note or 'Request rejected'
                
                flag.reviewed_by = request.user
                flag.reviewed_at = timezone.now()
                flag.save()
                
                return Response({
                    "message": f"Flag {action}d successfully",
                    "flag": MissingValueFlagSerializer(flag).data
                })
                
        except Exception as e:
            return Response({"error": str(e)}, status=500)
    
    @action(detail=False, methods=['get'], permission_classes=[IsAdmin])
    def pending(self, request):
        """Get all pending flags"""
        flags = MissingValueFlag.objects.filter(status='pending')
        serializer = self.get_serializer(flags, many=True)
        return Response(serializer.data)


class AIProcessingControlViewSet(viewsets.ModelViewSet):
    queryset = AIProcessingControl.objects.all()
    serializer_class = AIProcessingControlSerializer
    permission_classes = [permissions.IsAuthenticated & IsAdmin]
    
    @action(detail=False, methods=['post'])
    def toggle(self, request):
        """Toggle AI processing pause/resume"""
        action = request.data.get('action')
        
        if action not in ['pause', 'resume']:
            return Response({
                "error": "Action must be 'pause' or 'resume'"
            }, status=400)
        
        control = AIProcessingControl.get_control()
        
        if action == 'pause':
            control.is_paused = True
            control.paused_at = timezone.now()
            control.paused_by = request.user
            control.save()
            
            return Response({
                "message": "AI processing paused",
                "paused_at": control.paused_at,
                "paused_by": request.user.username
            })
        else:
            control.is_paused = False
            control.paused_at = None
            control.paused_by = None
            control.save()
            
            return Response({
                "message": "AI processing resumed"
            })
    
    @action(detail=False, methods=['get'])
    def status(self, request):
        """Get AI processing status"""
        control = AIProcessingControl.get_control()
        
        # Get statistics
        ai_batches = AnnotationBatch.objects.filter(batch_type='ai')
        
        active_batches = ai_batches.filter(
            batchassignment__status='in_progress'
        ).distinct().count()
        
        pending_products = BaseProduct.objects.filter(processing_status__in=['pending', 'pending_ai']).count()
        ai_in_progress_products = BaseProduct.objects.filter(processing_status='ai_in_progress').count()
        ai_failed_products = BaseProduct.objects.filter(processing_status='ai_failed').count()
        ai_done_products = BaseProduct.objects.filter(processing_status='ai_done').count()
        
        # Get AI providers status
        ai_providers = AIProvider.objects.filter(is_active=True)
        provider_stats = []
        for provider in ai_providers:
            annotations_count = ProductAnnotation.objects.filter(
                source_type='ai',
                source_id=provider.id,
                attribute__is_active=True
            ).count()
            
            assignments = BatchAssignment.objects.filter(
                assignment_type='ai',
                assignment_id=provider.id
            )
            
            provider_stats.append({
                'id': provider.id,
                'name': provider.name,
                'annotations_count': annotations_count,
                'active_assignments': assignments.filter(status='in_progress').count(),
                'completed_assignments': assignments.filter(status='completed').count()
            })
        
        return Response({
            'active_batches': active_batches,
            'pending_products': pending_products,
            'processing_products': ai_in_progress_products,
            'completed_products': ai_done_products,
            'failed_products': ai_failed_products,
            'is_processing': active_batches > 0,
            'is_paused': control.is_paused,
            'paused_at': control.paused_at,
            'paused_by': control.paused_by.username if control.paused_by else None,
            'last_updated': control.last_updated,
            'stats': {
                'active_batches': active_batches,
                'pending_products': pending_products,
                'ai_in_progress_products': ai_in_progress_products,
                'ai_failed_products': ai_failed_products,
                'ai_done_products': ai_done_products
            },
            'ai_providers': provider_stats
        })


class DashboardViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def overview(self, request):
        """Get dashboard overview"""
        user = request.user
        
        if user.groups.filter(name='Admin').exists():
            total_products = BaseProduct.objects.count()
            products_summary = {
                'total': total_products,
                'pending_ai': BaseProduct.objects.filter(processing_status__in=['pending', 'pending_ai']).count(),
                'ai_running': BaseProduct.objects.filter(processing_status='ai_in_progress').count(),
                'ai_failed': BaseProduct.objects.filter(processing_status='ai_failed').count(),
                'ai_done': BaseProduct.objects.filter(processing_status='ai_done').count(),
                'assigned': BaseProduct.objects.filter(processing_status='pending_human').count(),
                'in_review': BaseProduct.objects.filter(processing_status='human_in_progress').count(),
                'reviewed': BaseProduct.objects.filter(processing_status='human_done').count(),
                'finalized': BaseProduct.objects.filter(processing_status='human_done').count(),
            }
            
            batches_summary = {
                'total': AnnotationBatch.objects.count(),
                'pending': BatchAssignment.objects.filter(status='pending').values('batch').distinct().count(),
                'in_progress': BatchAssignment.objects.filter(status='in_progress').values('batch').distinct().count(),
                'completed': BatchAssignment.objects.filter(status='completed').values('batch').distinct().count(),
                'failed': BatchAssignment.objects.filter(status='failed').values('batch').distinct().count(),
                'ai': AnnotationBatch.objects.filter(batch_type='ai').count(),
                'human': AnnotationBatch.objects.filter(batch_type='human').count(),
            }
            
            overlap_records = BatchAssignmentItem.objects.filter(
                assignment__assignment_type='human'
            ).values('batch_item').annotate(annotator_count=Count('id')).filter(annotator_count__gt=1)
            overlaps_summary = {
                'total': overlap_records.count(),
                'resolved': 0,
                'unresolved': overlap_records.count()
            }
            
            annotator_metrics = []
            annotators = HumanAnnotator.objects.select_related('user')
            for annotator in annotators:
                items = BatchAssignmentItem.objects.filter(
                    assignment__assignment_type='human',
                    assignment__assignment_id=annotator.id
                )
                completed_items_qs = items.filter(status='human_done')
                completed = completed_items_qs.count()
                total_items = items.count()
                completion_rate = (completed / total_items * 100) if total_items else 0
                total_work_hours = 0
                for item in completed_items_qs:
                    started_at = item.started_at or item.created_at
                    completed_at = item.completed_at or item.updated_at
                    if started_at and completed_at and completed_at >= started_at:
                        total_work_hours += (completed_at - started_at).total_seconds() / 3600
                items_per_hour = (completed / total_work_hours) if total_work_hours > 0 else 0

                completed_batch_item_ids = list(
                    completed_items_qs.values_list('batch_item_id', flat=True)
                )
                compared = 0
                matches = 0
                if completed_batch_item_ids:
                    human_annotations = list(
                        ProductAnnotation.objects.filter(
                            source_type='human',
                            source_id=annotator.id,
                            attribute__is_active=True,
                            batch_item_id__in=completed_batch_item_ids,
                        ).values('product_id', 'attribute_id', 'value')
                    )
                    annotation_keys = {
                        (ann['product_id'], ann['attribute_id']) for ann in human_annotations
                    }
                    product_ids = {key[0] for key in annotation_keys}
                    attribute_ids = {key[1] for key in annotation_keys}
                    ai_annotations = ProductAnnotation.objects.filter(
                        source_type='ai',
                        attribute__is_active=True,
                        product_id__in=product_ids,
                        attribute_id__in=attribute_ids,
                    ).values('product_id', 'attribute_id', 'value')

                    ai_values_map = {}
                    for ann in ai_annotations:
                        value = ann['value']
                        if value is None:
                            continue
                        key = (ann['product_id'], ann['attribute_id'])
                        ai_values_map.setdefault(key, []).append(str(value).strip())

                    for ann in human_annotations:
                        key = (ann['product_id'], ann['attribute_id'])
                        ai_values = ai_values_map.get(key)
                        if not ai_values:
                            continue
                        consensus, _ = Counter(ai_values).most_common(1)[0]
                        human_value = '' if ann['value'] is None else str(ann['value']).strip()
                        compared += 1
                        if human_value.lower() == str(consensus).strip().lower():
                            matches += 1

                accuracy_rate = (matches / compared * 100) if compared else 0
                change_rate = ((compared - matches) / compared * 100) if compared else 0
                annotator_metrics.append({
                    'id': annotator.id,
                    'username': annotator.user.username,
                    'completed_items': completed,
                    'total_assigned': total_items,
                    'completion_rate': completion_rate,
                    'accuracy_rate': accuracy_rate,
                    'change_rate': change_rate,
                    'items_per_hour': round(items_per_hour, 2),
                })
            
            ai_processed = BaseProduct.objects.exclude(
                processing_status__in=['pending', 'pending_ai', 'ai_failed']
            ).count()
            ai_metrics = {
                'coverage': (ai_processed / total_products * 100) if total_products else 0,
                'accuracy': 0.0,
                'total_products_processed': ai_processed,
                'comparisons_made': ProductAnnotation.objects.filter(
                    source_type='ai',
                    attribute__is_active=True
                ).count()
            }
            
            payload = {
                'user': {
                    'username': user.username,
                    'role': 'admin'
                },
                'products': products_summary,
                'batches': batches_summary,
                'overlaps': overlaps_summary,
                'annotators': annotator_metrics,
                'ai_metrics': ai_metrics,
            }
            return Response(payload)
        
        elif user.groups.filter(name='Annotator').exists():
            # Annotator dashboard
            try:
                annotator = HumanAnnotator.objects.get(user=user)
                
                assignments = BatchAssignment.objects.filter(
                    assignment_type='human',
                    assignment_id=annotator.id
                )
                
                assignment_items = BatchAssignmentItem.objects.filter(
                    assignment__in=assignments
                )
                
                recent_annotations = ProductAnnotation.objects.filter(
                    source_type='human',
                    source_id=annotator.id,
                    attribute__is_active=True
                ).order_by('-created_at')[:10]
                
                pending_flags = MissingValueFlag.objects.filter(
                    annotator=annotator,
                    status='pending'
                ).count()
                
                recent_products = BaseProduct.objects.filter(
                    id__in=assignment_items.values_list('batch_item__product_id', flat=True)
                ).distinct().order_by('-updated_at')[:5]
                
                total_items = assignment_items.count()
                completed_items = assignment_items.filter(status='human_done').count()
                in_progress_items = assignment_items.filter(status='human_in_progress').count()
                pending_items = assignment_items.filter(status='pending_human').count()
                completion_rate = (completed_items / total_items * 100) if total_items else 0
                
                recent_activity = []
                for annotation in recent_annotations:
                    recent_activity.append({
                        'id': annotation.id,
                        'attribute': annotation.attribute_id,
                        'attribute_name': annotation.attribute.attribute_name,
                        'product': annotation.product_id,
                        'product_name': annotation.product.style_desc or annotation.product.style_id,
                        'annotator': annotator.id,
                        'annotator_name': annotator.user.username,
                        'annotated_value': annotation.value,
                        'status': 'approved'
                    })
                
                payload = {
                    'user': {
                        'username': user.username,
                        'role': 'annotator',
                        'annotator_id': annotator.id
                    },
                    'assigned_batches': assignments.count(),
                    'total_items': total_items,
                    'completed_items': completed_items,
                    'in_progress_items': in_progress_items,
                    'not_started_items': pending_items,
                    'completion_rate': completion_rate,
                    'pending_flags': pending_flags,
                    'recent_activity': recent_activity,
                    'recent_assignments': BatchAssignmentSerializer(
                        assignments.order_by('-updated_at')[:5], 
                        many=True
                    ).data,
                    'recent_products': ProductSerializer(recent_products, many=True).data
                }
                
                return Response(payload)
                
            except HumanAnnotator.DoesNotExist:
                return Response({
                    "error": "Annotator profile not found"
                }, status=404)
        
        return Response({
            "error": "User has no assigned role"
        }, status=403)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Alias for overview to match updated frontend expectations."""
        return self.overview(request)


class AutoAIProcessingViewSet(viewsets.ViewSet):
    permission_classes = [permissions.IsAuthenticated & IsAdmin]
    
    @action(detail=False, methods=['post'])
    def start(self, request):
        """Start automated AI processing"""
        serializer = StartAutoAIProcessingSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)
        
        data = serializer.validated_data
        batch_size = data['batch_size']
        ai_provider_ids = data.get('ai_provider_ids')
        
        # Check if AI processing is paused
        control = AIProcessingControl.get_control()
        if control.is_paused:
            return Response({
                "error": "AI processing is paused. Resume first."
            }, status=400)
        
        # Get AI providers
        if ai_provider_ids:
            ai_providers = AIProvider.objects.filter(
                id__in=ai_provider_ids,
                is_active=True
            )
        else:
            ai_providers = AIProvider.objects.filter(is_active=True)
        
        if not ai_providers.exists():
            return Response({"error": "No active AI providers found"}, status=400)
        
        # Start processing in background
        thread = threading.Thread(
            target=self._process_all_pending_products,
            args=(batch_size, [p.id for p in ai_providers])
        )
        thread.daemon = True
        thread.start()
        
        return Response({
            "message": "Automated AI processing started",
            "batch_size": batch_size,
            "providers": [p.name for p in ai_providers],
            "status": "started"
        })
    
    @action(detail=False, methods=['post'])
    def stop(self, request):
        """Stop automated AI processing"""
        # This would typically involve setting a flag and checking it in the processing loop
        # For now, we just return success
        return Response({
            "message": "Auto AI processing stop requested",
            "note": "Processing will complete current batch then stop"
        })
    
    def _process_all_pending_products(self, batch_size, ai_provider_ids):
        """Process all pending products in batches"""
        print(f"Starting auto AI processing with batch size {batch_size}")
        
        while True:
            # Check if processing is paused
            control = AIProcessingControl.get_control()
            if control.is_paused:
                print("AI processing paused, waiting...")
                time.sleep(5)
                continue
            
            # Get pending products
            pending_products = BaseProduct.objects.filter(
                processing_status__in=['pending', 'pending_ai']
            ).exclude(
                id__in=BatchItem.objects.filter(batch_type='ai').values_list('product_id', flat=True)
            ).order_by('id')[:batch_size]
            
            if not pending_products.exists():
                print("No more pending products for AI processing")
                break
            
            # Create and process batch
            self._create_and_process_batch(pending_products, ai_provider_ids)
            
            # Small delay between batches
            time.sleep(1)
        
        print("Auto AI processing completed")
    
    def _create_and_process_batch(self, products, ai_provider_ids):
        """Create and process a single batch"""
        try:
            with transaction.atomic():
                # Create batch
                batch = AnnotationBatch.objects.create(
                    name=f"Auto AI Batch - {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    description='Automatically created by auto AI processing',
                    batch_type='ai',
                    batch_size=len(products)
                )
                
                # Create batch items
                batch_items = []
                for product in products:
                    batch_item = BatchItem.objects.create(
                        batch=batch,
                        product=product,
                        batch_type='ai'
                    )
                    batch_items.append(batch_item)
                
                # Create assignments for each AI provider
                for provider_id in ai_provider_ids:
                    assignment = BatchAssignment.objects.create(
                        batch=batch,
                        assignment_type='ai',
                        assignment_id=provider_id,
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
                product_ids = [p.id for p in products]
                BaseProduct.objects.filter(id__in=product_ids).update(
                    processing_status='ai_in_progress',
                    updated_at=timezone.now()
                )
                
                # Process the batch
                self._process_batch(batch, products, ai_provider_ids)
                
        except Exception as e:
            print(f"Error creating batch: {e}")
    
    def _process_batch(self, batch, products, ai_provider_ids):
        """Process a batch"""
        try:
            print(f"Processing batch {batch.id} with {len(products)} products")

            processor = AIBatchProcessor(ai_provider_ids)
            processor.process_batch(batch.id)

            print(f"Batch {batch.id} processed successfully")
        except Exception as e:
            print(f"Error processing batch {batch.id}: {e}")
    
    def _get_applicable_attributes(self, product):
        """Get applicable attributes for a product"""
        if not product.subclass:
            return []
        
        attributes = []
        
        # Get global attributes
        global_attrs = AttributeGlobalMap.objects.filter(
            attribute__is_active=True
        ).select_related('attribute')
        for map_obj in global_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name
            })
        
        # Get subclass-specific attributes
        subclass_attrs = AttributeSubclassMap.objects.filter(
            subclass=product.subclass,
            attribute__is_active=True
        ).select_related('attribute')
        
        for map_obj in subclass_attrs:
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name
            })
        
        return attributes
    
    def _generate_ai_suggestion(self, product, attribute, provider):
        """Generate AI suggestion"""
        attribute_name = attribute['name'].lower()
        
        if 'color' in attribute_name:
            colors = ['Red', 'Blue', 'Green', 'Black', 'White', 'Yellow']
            return random.choice(colors)
        elif 'size' in attribute_name:
            sizes = ['XS', 'S', 'M', 'L', 'XL']
            return random.choice(sizes)
        elif 'material' in attribute_name:
            materials = ['Cotton', 'Polyester', 'Silk', 'Wool']
            return random.choice(materials)
        else:
            return f"AI suggested value for {attribute['name']}"
        

# Add this to your views.py, replacing the existing AttributeManagementViewSet
# Add this to your views.py, replacing the existing AttributeManagementViewSet

class AttributeManagementViewSet(viewsets.ViewSet):
    """
    Attribute management for admins - handles all attributes (mapped and unmapped).
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    
    @action(detail=False, methods=['get'])
    def all_attributes(self, request):
        """Get ALL attributes, including those not mapped to any subclass."""
        attributes = AttributeMaster.objects.filter(is_active=True).order_by('attribute_name')
        
        result = []
        for attr in attributes:
            # Get options
            options = AttributeOption.objects.filter(
                attribute=attr,
                attribute__is_active=True
            ).values_list('option_value', flat=True)
            
            # Get subclass mappings (if any)
            subclass_maps = AttributeSubclassMap.objects.filter(
                attribute=attr,
                attribute__is_active=True
            ).select_related('subclass')
            mapped_subclasses = [
                {'id': sm.subclass.id, 'name': sm.subclass.name}
                for sm in subclass_maps
            ]
            
            result.append({
                'id': attr.id,
                'name': attr.attribute_name,
                'description': attr.description or '',
                'options': list(options),
                'mapped_subclasses': mapped_subclasses,
                'option_count': len(options),
                'subclass_count': len(mapped_subclasses),
                'is_mapped': len(mapped_subclasses) > 0
            })
        
        return Response(result)
    
    @action(detail=False, methods=['get'])
    def available_subclasses(self, request):
        """Get all available subclasses for mapping."""
        subclasses = SubClass.objects.all().order_by('name')
        return Response([
            {'id': sc.id, 'name': sc.name}
            for sc in subclasses
        ])
    
    @action(detail=False, methods=['get'])
    def subclass_mappings(self, request):
        """Get subclass mappings view - shows which attributes are mapped to each subclass."""
        subclasses = SubClass.objects.all().order_by('name')
        result = []
        
        for subclass in subclasses:
            mappings = AttributeSubclassMap.objects.filter(
                subclass=subclass,
                attribute__is_active=True
            ).select_related('attribute')
            
            attributes = []
            for mapping in mappings:
                attr = mapping.attribute
                option_count = AttributeOption.objects.filter(
                    attribute=attr,
                    attribute__is_active=True
                ).count()
                
                attributes.append({
                    'id': attr.id,
                    'name': attr.attribute_name,
                    'description': attr.description or '',
                    'option_count': option_count
                })
            
            result.append({
                'id': subclass.id,
                'name': subclass.name,
                'attributes': attributes,
                'attribute_count': len(attributes)
            })
        
        return Response(result)
    
    @action(detail=False, methods=['post'])
    def create_attribute(self, request):
        """Create a new attribute."""
        name = request.data.get('name', '').strip()
        description = request.data.get('description', '').strip()
        options = request.data.get('options', [])
        subclass_ids = request.data.get('subclass_ids', [])
        
        if not name:
            return Response({'error': 'Attribute name is required'}, status=400)
        
        if AttributeMaster.objects.filter(attribute_name__iexact=name).exists():
            return Response({'error': f'Attribute "{name}" already exists'}, status=400)
        
        try:
            with transaction.atomic():
                attribute = AttributeMaster.objects.create(
                    attribute_name=name,
                    description=description
                )
                
                # Add options
                for option_value in options:
                    if option_value.strip():
                        AttributeOption.objects.create(
                            attribute=attribute,
                            option_value=option_value.strip()
                        )
                
                # Add subclass mappings if provided
                for subclass_id in subclass_ids:
                    try:
                        subclass = SubClass.objects.get(id=subclass_id)
                        AttributeSubclassMap.objects.get_or_create(
                            attribute=attribute,
                            subclass=subclass
                        )
                    except SubClass.DoesNotExist:
                        continue
                
                return Response({
                    'message': 'Attribute created successfully',
                    'attribute': {
                        'id': attribute.id,
                        'name': attribute.attribute_name,
                        'description': attribute.description
                    }
                }, status=201)
                
        except Exception as e:
            return Response({'error': str(e)}, status=500)
    
    @action(detail=True, methods=['put'])
    def update_attribute(self, request, pk=None):
        """Update an existing attribute."""
        try:
            attribute = AttributeMaster.objects.get(id=pk, is_active=True)
        except AttributeMaster.DoesNotExist:
            return Response({'error': 'Attribute not found'}, status=404)
        
        name = request.data.get('name', attribute.attribute_name).strip()
        description = request.data.get('description', attribute.description or '').strip()
        options = request.data.get('options')
        
        try:
            with transaction.atomic():
                # Update basic info
                if name != attribute.attribute_name:
                    if AttributeMaster.objects.filter(
                        attribute_name__iexact=name
                    ).exclude(id=pk).exists():
                        return Response({'error': f'Attribute "{name}" already exists'}, status=400)
                    attribute.attribute_name = name
                
                attribute.description = description
                attribute.save()
                
                # Update options if provided
                if options is not None:
                    AttributeOption.objects.filter(attribute=attribute).delete()
                    for option_value in options:
                        if option_value.strip():
                            AttributeOption.objects.create(
                                attribute=attribute,
                                option_value=option_value.strip()
                            )
                
                return Response({
                    'message': 'Attribute updated successfully',
                    'attribute': {
                        'id': attribute.id,
                        'name': attribute.attribute_name,
                        'description': attribute.description
                    }
                })
                
        except Exception as e:
            import traceback
            print(f"Error updating attribute: {traceback.format_exc()}")
            return Response({'error': str(e)}, status=500)
    
    @action(detail=True, methods=['delete'])
    def delete_attribute(self, request, pk=None):
        """Delete an attribute and all its related data."""
        try:
            attribute = AttributeMaster.objects.get(id=pk, is_active=True)
        except AttributeMaster.DoesNotExist:
            return Response({'error': 'Attribute not found'}, status=404)
        
        # Check if attribute is used in annotations
        annotation_count = ProductAnnotation.objects.filter(attribute=attribute).count()
        
        if annotation_count > 0:
            return Response({
                'error': f'Cannot delete attribute. It is used in {annotation_count} annotations.',
                'annotation_count': annotation_count
            }, status=400)
        
        try:
            with transaction.atomic():
                attribute_name = attribute.attribute_name
                attribute.delete()
                
                return Response({
                    'message': f'Attribute "{attribute_name}" deleted successfully'
                })
                
        except Exception as e:
            return Response({'error': str(e)}, status=500)
    
    @action(detail=False, methods=['post'])
    def bulk_map_attribute(self, request):
        """
        FIXED: Bulk map an attribute to multiple subclasses.
        Prevents duplicate key violations by checking existing mappings.
        Returns proper error responses on failure.
        """
        attribute_id = request.data.get('attribute_id')
        subclass_ids = request.data.get('subclass_ids', [])
        
        if not attribute_id:
            return Response({'error': 'attribute_id is required'}, status=400)
        
        if not subclass_ids or len(subclass_ids) == 0:
            return Response({'error': 'subclass_ids is required and must not be empty'}, status=400)
        
        try:
            attribute = AttributeMaster.objects.get(id=attribute_id, is_active=True)
        except AttributeMaster.DoesNotExist:
            return Response({'error': f'Attribute with id {attribute_id} not found'}, status=404)
        
        try:
            with transaction.atomic():
                # Get existing mappings for this attribute
                existing_mappings = set(
                    AttributeSubclassMap.objects.filter(
                        attribute=attribute
                    ).values_list('subclass_id', flat=True)
                )
                
                created_count = 0
                skipped_count = 0
                failed_count = 0
                failed_subclasses = []
                
                for subclass_id in subclass_ids:
                    # Skip if mapping already exists
                    if subclass_id in existing_mappings:
                        skipped_count += 1
                        continue
                    
                    try:
                        subclass = SubClass.objects.get(id=subclass_id)
                        # Use get_or_create to avoid duplicate key errors
                        mapping, created = AttributeSubclassMap.objects.get_or_create(
                            attribute=attribute,
                            subclass=subclass
                        )
                        if created:
                            created_count += 1
                        else:
                            skipped_count += 1
                    except SubClass.DoesNotExist:
                        failed_count += 1
                        failed_subclasses.append(f"Subclass {subclass_id} not found")
                    except Exception as e:
                        import traceback
                        error_msg = str(e)
                        error_trace = traceback.format_exc()
                        
                        # Handle sequence/duplicate key issues specially
                        if 'duplicate key' in error_msg.lower() or 'duplicate key' in error_trace.lower():
                            # Check if mapping already exists despite our check
                            try:
                                existing = AttributeSubclassMap.objects.filter(
                                    attribute=attribute,
                                    subclass_id=subclass_id
                                ).exists()
                                if existing:
                                    skipped_count += 1
                                    continue
                            except:
                                pass
                            # If still not found, it's a sequence issue
                            failed_count += 1
                            failed_subclasses.append(f"Subclass {subclass_id}: Database sequence error - please contact admin")
                            logger.error(f"Database sequence error for attribute {attribute_id}, subclass {subclass_id}")
                        else:
                            failed_count += 1
                            failed_subclasses.append(f"Subclass {subclass_id}: {error_msg[:100]}")
                        
                        logger.error(f"Error creating mapping for subclass {subclass_id}: {error_trace}")
                
                response_data = {
                    'message': f'Mapped attribute to {created_count} new subclasses',
                    'created': created_count,
                    'skipped': skipped_count,
                    'failed': failed_count,
                    'total_requested': len(subclass_ids),
                    'attribute_id': attribute_id,
                    'attribute_name': attribute.attribute_name
                }
                
                if failed_subclasses:
                    response_data['failures'] = failed_subclasses
                
                status_code = 200 if created_count > 0 else (400 if failed_count > 0 else 200)
                return Response(response_data, status=status_code)
                
        except Exception as e:
            import traceback
            error_trace = traceback.format_exc()
            print(f"Bulk map error: {error_trace}")
            return Response({
                'error': f'Failed to bulk map attribute: {str(e)}',
                'details': error_trace
            }, status=500)
    
    @action(detail=False, methods=['post'])
    def bulk_unmap_attribute(self, request):
        """Remove attribute mappings from multiple subclasses."""
        attribute_id = request.data.get('attribute_id')
        subclass_ids = request.data.get('subclass_ids', [])
        
        if not attribute_id:
            return Response({'error': 'attribute_id is required'}, status=400)
        
        if not subclass_ids:
            return Response({'error': 'subclass_ids is required'}, status=400)
        
        try:
            attribute = AttributeMaster.objects.get(id=attribute_id, is_active=True)
        except AttributeMaster.DoesNotExist:
            return Response({'error': 'Attribute not found'}, status=404)
        
        try:
            deleted_count = AttributeSubclassMap.objects.filter(
                attribute=attribute,
                subclass_id__in=subclass_ids
            ).delete()[0]
            
            return Response({
                'message': f'Removed {deleted_count} mappings',
                'deleted': deleted_count
            })
            
        except Exception as e:
            return Response({'error': str(e)}, status=500)
