# products/serializers.py
from rest_framework import serializers
from django.contrib.auth.models import User
from .models import *
from collections import Counter, defaultdict

BATCH_ITEM_STATUS_CLIENT_MAP = {
    'pending_human': 'not_started',
    'human_in_progress': 'in_progress',
    'human_done': 'done',
}

PRODUCT_STATUS_CLIENT_MAP = {
    'pending': 'pending_ai',
    'pending_ai': 'pending_ai',
    'ai_in_progress': 'ai_running',
    'ai_failed': 'ai_failed',
    'ai_done': 'ai_done',
    'pending_human': 'assigned',
    'human_in_progress': 'in_review',
    'human_done': 'reviewed',
}


def map_product_status_to_client(status: str) -> str:
    """Map database processing status to client-facing status codes."""
    return PRODUCT_STATUS_CLIENT_MAP.get(status, 'pending_ai')


def build_attribute_options_map(attribute_ids):
    options = AttributeOption.objects.filter(
        attribute_id__in=attribute_ids,
        attribute__is_active=True
    )
    option_map = defaultdict(list)
    for option in options:
        option_map[option.attribute_id].append(option.option_value)
    return option_map


class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'role']
        read_only_fields = fields
    
    def get_role(self, obj):
        if obj.groups.filter(name='Admin').exists():
            return 'admin'
        elif obj.groups.filter(name='Annotator').exists():
            return 'annotator'
        return 'user'


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = '__all__'
        read_only_fields = ['id']


class SubDepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubDepartment
        fields = '__all__'
        read_only_fields = ['id']


class ClassSerializer(serializers.ModelSerializer):
    class Meta:
        model = Class
        fields = '__all__'
        read_only_fields = ['id']


class SubClassSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubClass
        fields = '__all__'
        read_only_fields = ['id']


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = '__all__'
        read_only_fields = ['id']


class ProductSizeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductSize
        fields = '__all__'
        read_only_fields = ['id']


class ProductColorSerializer(serializers.ModelSerializer):
    sizes = ProductSizeSerializer(many=True, read_only=True)
    images = ProductImageSerializer(many=True, read_only=True)

    class Meta:
        model = ProductColor
        fields = '__all__'
        read_only_fields = ['id']


class AttributeMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeMaster
        fields = '__all__'
        read_only_fields = ['id']


class AttributeOptionSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source='attribute.attribute_name', read_only=True)
    
    class Meta:
        model = AttributeOption
        fields = '__all__'
        read_only_fields = ['id']


class AttributeSubclassMapSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source='attribute.attribute_name', read_only=True)
    subclass_name = serializers.CharField(source='subclass.name', read_only=True)
    
    class Meta:
        model = AttributeSubclassMap
        fields = '__all__'
        read_only_fields = ['id']


class ProductSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source='department.name', read_only=True)
    subdepartment_name = serializers.CharField(source='subdepartment.name', read_only=True)
    class_name = serializers.CharField(source='class_field.name', read_only=True)
    subclass_name = serializers.CharField(source='subclass.name', read_only=True)
    primary_image = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()
    subcategory = serializers.SerializerMethodField()
    subcategory_name = serializers.SerializerMethodField()
    image_urls = serializers.SerializerMethodField()
    color_id = serializers.SerializerMethodField()
    color_desc = serializers.SerializerMethodField()
    size_desc = serializers.SerializerMethodField()
    dim_desc = serializers.SerializerMethodField()
    colors = ProductColorSerializer(many=True, read_only=True)
    
    class Meta:
        model = BaseProduct
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_primary_image(self, obj):
        return obj.primary_image_url
    
    def get_name(self, obj):
        return obj.style_desc or obj.style_id
    
    def get_description(self, obj):
        return obj.style_description or ''
    
    def get_status(self, obj):
        return map_product_status_to_client(obj.processing_status)
    
    def get_category(self, obj):
        return obj.class_field.id if obj.class_field else None
    
    def get_category_name(self, obj):
        return obj.class_field.name if obj.class_field else None
    
    def get_subcategory(self, obj):
        return obj.subclass.id if obj.subclass else None
    
    def get_subcategory_name(self, obj):
        return obj.subclass.name if obj.subclass else None
    
    def get_image_urls(self, obj):
        # Flatten images across all color variants
        urls = []
        for color in obj.colors.all():
            for image in color.images.all():
                urls.append(image.image_url)
        return urls

    def get_color_id(self, obj):
        return obj.color_id

    def get_color_desc(self, obj):
        return obj.color_desc

    def get_size_desc(self, obj):
        return obj.size_desc

    def get_dim_desc(self, obj):
        return obj.dim_desc


class ProductDetailSerializer(ProductSerializer):
    applicable_attributes = serializers.SerializerMethodField()
    ai_annotations = serializers.SerializerMethodField()
    human_annotations = serializers.SerializerMethodField()
    current_batches = serializers.SerializerMethodField()
    
    class Meta:
        model = BaseProduct
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_primary_image(self, obj):
        return obj.primary_image_url
    
    def get_applicable_attributes(self, obj):
        """Get attributes applicable to this product's subclass"""
        if not obj.subclass:
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
            subclass=obj.subclass,
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
    
    def get_ai_annotations(self, obj):
        annotations = ProductAnnotation.objects.filter(
            product=obj,
            source_type='ai',
            attribute__is_active=True
        ).select_related('attribute')
        
        result = []
        for ann in annotations:
            result.append({
                'id': ann.id,
                'attribute_id': ann.attribute.id,
                'attribute_name': ann.attribute.attribute_name,
                'value': ann.value,
                'source_id': ann.source_id,
                'source_name': ann.source_name,
                'confidence_score': float(ann.confidence_score) if ann.confidence_score else None,
                'created_at': ann.created_at
            })
        return result
    
    def get_human_annotations(self, obj):
        annotations = ProductAnnotation.objects.filter(
            product=obj,
            source_type='human',
            attribute__is_active=True
        ).select_related('attribute')
        
        result = []
        for ann in annotations:
            result.append({
                'id': ann.id,
                'attribute_id': ann.attribute.id,
                'attribute_name': ann.attribute.attribute_name,
                'value': ann.value,
                'source_id': ann.source_id,
                'source_name': ann.source_name,
                'created_at': ann.created_at
            })
        return result
    
    def get_current_batches(self, obj):
        """Get current batch assignments for this product"""
        batch_items = BatchItem.objects.filter(product=obj).select_related('batch')
        
        result = []
        for batch_item in batch_items:
            assignments = BatchAssignment.objects.filter(batch=batch_item.batch)
            
            for assignment in assignments:
                try:
                    assignment_item = BatchAssignmentItem.objects.get(
                        assignment=assignment,
                        batch_item=batch_item
                    )
                    
                    result.append({
                        'batch_id': batch_item.batch.id,
                        'batch_name': batch_item.batch.name,
                        'batch_type': batch_item.batch.batch_type,
                        'assignment_id': assignment.id,
                        'assignment_type': assignment.assignment_type,
                        'assignee_name': assignment.assignee_name,
                        'status': assignment_item.status,
                        'progress': assignment.progress
                    })
                except BatchAssignmentItem.DoesNotExist:
                    continue
        
        return result


class AIProviderSerializer(serializers.ModelSerializer):
    """
    Serializer for AI providers.
    
    The actual database column for dynamic configuration is the JSONB field
    `config`.  For convenience, the API exposes a flatter write interface with
    `api_key`, `max_tokens`, and `temperature` fields.  These are written into
    the config object but are never returned directly (API keys stay server‑side).
    """
    
    # Convenience write-only fields that map into `config`
    api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    max_tokens = serializers.IntegerField(
        write_only=True, required=False, allow_null=True
    )
    temperature = serializers.FloatField(
        write_only=True, required=False, allow_null=True
    )
    prompt_template = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    # NEW: Custom provider configuration fields
    custom_endpoint = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    request_format = serializers.JSONField(
        write_only=True, required=False, allow_null=True
    )
    response_path = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True
    )
    headers_template = serializers.JSONField(
        write_only=True, required=False, allow_null=True
    )
    supports_vision = serializers.BooleanField(
        write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = AIProvider
        fields = '__all__'
        read_only_fields = ['id', 'created_at']
    
    def _merge_config_fields(self, instance, validated_data):
        """
        Merge flat fields (api_key, max_tokens, temperature) into the JSON
        config field while preserving any existing keys.
        """
        api_key = validated_data.pop('api_key', None)
        max_tokens = validated_data.pop('max_tokens', None)
        temperature = validated_data.pop('temperature', None)
        prompt_template = validated_data.pop('prompt_template', None)
        custom_endpoint = validated_data.pop('custom_endpoint', None)
        request_format = validated_data.pop('request_format', None)
        response_path = validated_data.pop('response_path', None)
        headers_template = validated_data.pop('headers_template', None)
        supports_vision = validated_data.pop('supports_vision', None) 
        
        # Start from existing config (for update) or an empty dict (for create)
        existing_config = {}
        if instance is not None and getattr(instance, 'config', None):
            # Make a shallow copy so we don't mutate instance.config directly
            existing_config = dict(instance.config)
        
        config = validated_data.get('config') or existing_config or {}
        
        if api_key:
            # We intentionally don't ever return this field to the client
            config['api_key'] = api_key
        
        if max_tokens is not None:
            config['max_tokens'] = max_tokens
        
        if temperature is not None:
            config['temperature'] = temperature
        
        # NEW: Save prompt template
        if prompt_template is not None:
            config['prompt_template'] = prompt_template

        # NEW: Custom provider fields
        if custom_endpoint is not None:
            config['custom_endpoint'] = custom_endpoint
        
        if request_format is not None:
            config['request_format'] = request_format
        
        if response_path is not None:
            config['response_path'] = response_path
        
        if headers_template is not None:
            config['headers_template'] = headers_template
        
        if supports_vision is not None:
            config['supports_vision'] = supports_vision    

        validated_data['config'] = config
        return validated_data
        
        
    
    def create(self, validated_data):
        # When creating, there is no existing instance yet
        validated_data = self._merge_config_fields(None, validated_data)
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        # On update we want to preserve existing config keys if they are not
        # explicitly overridden, and we only overwrite the API key if a new one
        # is provided.
        validated_data = self._merge_config_fields(instance, validated_data)
        return super().update(instance, validated_data)
    
    def to_representation(self, instance):
        """
        Override to include all custom fields in read responses (but never api_key)
        """
        data = super().to_representation(instance)
        
        # Add all config fields to response for display
        if instance.config:
            # Prompt template
            data['prompt_template'] = instance.config.get('prompt_template')
            
            # Standard parameters
            data['max_tokens_display'] = instance.config.get('max_tokens')
            data['temperature_display'] = instance.config.get('temperature')
            
            # Custom provider configuration
            data['custom_endpoint_display'] = instance.config.get('custom_endpoint')
            data['response_path_display'] = instance.config.get('response_path')
            data['headers_template_display'] = instance.config.get('headers_template')
            data['request_format_display'] = instance.config.get('request_format')
            data['supports_vision_display'] = instance.config.get('supports_vision', False)
        else:
            # Set defaults when config is None
            data['prompt_template'] = None
            data['max_tokens_display'] = None
            data['temperature_display'] = None
            data['custom_endpoint_display'] = None
            data['response_path_display'] = None
            data['headers_template_display'] = None
            data['request_format_display'] = None
            data['supports_vision_display'] = False
        
        # IMPORTANT: Never expose the API key in responses
        # The api_key field is write-only and should never be returned
        
        return data


class HumanAnnotatorSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    
    class Meta:
        model = HumanAnnotator
        fields = '__all__'
        read_only_fields = ['id']


class AnnotationBatchSerializer(serializers.ModelSerializer):
    actual_size = serializers.IntegerField(read_only=True)
    items_count = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    assignments_count = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    assigned_to = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    completed_count = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = AnnotationBatch
        fields = [
            'id', 'name', 'description', 'batch_size', 'batch_type',
            'created_at', 'updated_at', 'actual_size',
            'items_count', 'item_count', 'assignments_count',
            'status', 'progress', 'assigned_to',
            'assigned_to_name', 'completed_count', 'items',
            'display_name'
        ]
        read_only_fields = fields
    
    def get_display_name(self, obj):
        """Return a global counter display name per batch type."""
        if not obj.batch_type:
            return obj.name or "Batch"
        batch_number = AnnotationBatch.objects.filter(
            batch_type=obj.batch_type,
            id__lte=obj.id
        ).count()
        return f"{obj.batch_type.upper()} Batch #{batch_number}"

    def get_items_count(self, obj):
        return obj.batchitem_set.count()
    
    def get_assignments_count(self, obj):
        return BatchAssignment.objects.filter(batch=obj).count()
    
    def get_item_count(self, obj):
        return self.get_items_count(obj)
    
    def get_status(self, obj):
        assignments = BatchAssignment.objects.filter(batch=obj)
        if not assignments.exists():
            return 'pending'
        if assignments.filter(status='in_progress').exists():
            return 'in_progress'
        if assignments.filter(status='pending').exists():
            return 'pending'
        if assignments.filter(status='completed').count() == assignments.count():
            return 'completed'
        if assignments.filter(status='cancelled').count() == assignments.count():
            return 'cancelled'
        return assignments.first().status
    
    def get_progress(self, obj):
        assignments = BatchAssignment.objects.filter(batch=obj)
        progress_values = list(assignments.values_list('progress', flat=True))
        if not progress_values:
            return 0
        return float(sum(progress_values)) / len(progress_values)
    
    def get_assigned_to(self, obj):
        assignments = BatchAssignment.objects.filter(batch=obj)
        if assignments.count() == 1:
            return assignments.first().assignment_id
        return None
    
    def get_assigned_to_name(self, obj):
        assignments = BatchAssignment.objects.filter(batch=obj)
        if assignments.count() == 1:
            return assignments.first().assignee_name
        return None
    
    def get_completed_count(self, obj):
        assignment = self.context.get('assignment')
        if assignment:
            status_field = 'human_done' if assignment.assignment_type == 'human' else 'ai_done'
            return BatchAssignmentItem.objects.filter(
                assignment=assignment,
                status=status_field
            ).count()
        # Default to completed products in batch
        completed_status = 'human_done' if obj.batch_type == 'human' else 'ai_done'
        return BatchItem.objects.filter(
            batch=obj,
            product__processing_status=completed_status
        ).count()
    
    def get_items(self, obj):
        include_items = self.context.get('include_items', False)
        assignment = self.context.get('assignment')
        if not include_items or not assignment:
            return []
        
        items = BatchAssignmentItem.objects.filter(
            assignment=assignment
        ).select_related('batch_item__product').prefetch_related(
            'batch_item__product__colors',
            'batch_item__product__colors__images',
            'batch_item__product__colors__sizes',
        )
        
        serializer = AnnotatorBatchItemSerializer(
            items,
            many=True,
            context={'annotator_id': assignment.assignment_id}
        )
        return serializer.data


class BatchItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    batch_name = serializers.CharField(source='batch.name', read_only=True)
    batch_type_display = serializers.CharField(source='get_batch_type_display', read_only=True)
    
    class Meta:
        model = BatchItem
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class BatchAssignmentSerializer(serializers.ModelSerializer):
    assignee_name = serializers.CharField(read_only=True)
    batch_name = serializers.CharField(source='batch.name', read_only=True)
    items_count = serializers.SerializerMethodField()
    completed_items = serializers.SerializerMethodField()
    failure_count = serializers.SerializerMethodField()
    last_error = serializers.SerializerMethodField()
    
    class Meta:
        model = BatchAssignment
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_items_count(self, obj):
        return BatchAssignmentItem.objects.filter(assignment=obj).count()
    
    def get_completed_items(self, obj):
        if obj.assignment_type == 'ai':
            return BatchAssignmentItem.objects.filter(
                assignment=obj,
                status='ai_done'
            ).count()
        else:
            return BatchAssignmentItem.objects.filter(
                assignment=obj,
                status='human_done'
            ).count()

    def get_failure_count(self, obj):
        if obj.assignment_type != 'ai':
            return 0
        return AIProviderFailureLog.objects.filter(
            assignment_item__assignment=obj,
            is_resolved=False
        ).count()

    def get_last_error(self, obj):
        if obj.assignment_type != 'ai':
            return None
        log = AIProviderFailureLog.objects.filter(
            assignment_item__assignment=obj
        ).order_by('-created_at').first()
        return log.error_message if log else None


class BatchAssignmentItemSerializer(serializers.ModelSerializer):
    product_info = serializers.SerializerMethodField()
    assignment_info = serializers.SerializerMethodField()
    attribute_info = serializers.SerializerMethodField()
    
    class Meta:
        model = BatchAssignmentItem
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_product_info(self, obj):
        product = obj.batch_item.product
        return {
            'id': product.id,
            'style_id': product.style_id,
            'color_desc': product.color_desc,
            'size_desc': product.size_desc,
            'style_desc': product.style_desc,
            'primary_image': product.primary_image_url,
            'processing_status': product.processing_status
        }
    
    def get_assignment_info(self, obj):
        assignment = obj.assignment
        return {
            'id': assignment.id,
            'assignment_type': assignment.assignment_type,
            'assignee_name': assignment.assignee_name,
            'status': assignment.status,
            'progress': assignment.progress
        }
    
    def get_attribute_info(self, obj):
        """Get applicable attributes for this product"""
        product = obj.batch_item.product
        if not product.subclass:
            return []
        
        # Get existing annotations for this product
        existing_annotations = ProductAnnotation.objects.filter(
            product=product
        ).select_related('attribute')
        
        existing_attr_ids = {ann.attribute_id for ann in existing_annotations}
        
        attributes = []
        
        # Get global attributes
        global_attrs = AttributeGlobalMap.objects.filter(
            attribute__is_active=True
        ).select_related('attribute')
        for map_obj in global_attrs:
            attr = map_obj.attribute
            # Check if already annotated by this source
            existing_ann = existing_annotations.filter(
                attribute=attr,
                source_type=obj.assignment.assignment_type,
                source_id=obj.assignment.assignment_id
            ).first()
            
            attributes.append({
                'id': attr.id,
                'name': attr.attribute_name,
                'description': attr.description,
                'scope': 'global',
                'already_annotated': attr.id in existing_attr_ids,
                'current_value': existing_ann.value if existing_ann else None
            })
        
        # Get subclass-specific attributes
        subclass_attrs = AttributeSubclassMap.objects.filter(
            subclass=product.subclass,
            attribute__is_active=True
        ).select_related('attribute')
        
        for map_obj in subclass_attrs:
            attr = map_obj.attribute
            # Check if already annotated by this source
            existing_ann = existing_annotations.filter(
                attribute=attr,
                source_type=obj.assignment.assignment_type,
                source_id=obj.assignment.assignment_id
            ).first()
            
            attributes.append({
                'id': attr.id,
                'name': attr.attribute_name,
                'description': attr.description,
                'scope': 'subclass',
                'already_annotated': attr.id in existing_attr_ids,
                'current_value': existing_ann.value if existing_ann else None
            })
        
        return attributes


class AnnotatorBatchItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(source='batch_item.product', read_only=True)
    status = serializers.SerializerMethodField()
    ai_suggestions = serializers.SerializerMethodField()
    ai_consensus = serializers.SerializerMethodField()
    human_annotations = serializers.SerializerMethodField()
    applicable_attributes = serializers.SerializerMethodField()
    
    class Meta:
        model = BatchAssignmentItem
        fields = [
            'id', 'status', 'product',
            'ai_suggestions', 'ai_consensus',
            'human_annotations', 'applicable_attributes'
        ]
    
    def get_status(self, obj):
        return BATCH_ITEM_STATUS_CLIENT_MAP.get(obj.status, 'not_started')
    
    def _get_attribute_options(self, attribute_ids):
        return build_attribute_options_map(attribute_ids)
    
    def _get_applicable_attribute_queryset(self, product):
        global_attrs = list(
            AttributeGlobalMap.objects.filter(attribute__is_active=True).select_related('attribute')
        )
        subclass_attrs = []
        if product.subclass:
            subclass_attrs = list(
                AttributeSubclassMap.objects.filter(
                    subclass=product.subclass,
                    attribute__is_active=True
                ).select_related('attribute')
            )
        return global_attrs, subclass_attrs
    
    def get_ai_suggestions(self, obj):
        product = obj.batch_item.product
        annotations = ProductAnnotation.objects.filter(
            product=product,
            source_type='ai',
            attribute__is_active=True
        ).select_related('attribute')
        
        attribute_ids = {ann.attribute_id for ann in annotations}
        option_map = self._get_attribute_options(attribute_ids)
        
        providers = {
            provider.id: provider.name
            for provider in AIProvider.objects.filter(id__in={ann.source_id for ann in annotations})
        }
        
        suggestions = []
        for ann in annotations:
            suggestions.append({
                'id': ann.id,
                'product': product.id,
                'attribute': ann.attribute_id,
                'attribute_name': ann.attribute.attribute_name,
                'provider': ann.source_id,
                'provider_name': providers.get(ann.source_id, f'Provider {ann.source_id}'),
                'suggested_value': ann.value,
                'confidence_score': float(ann.confidence_score) if ann.confidence_score else None,
                'allowed_values': option_map.get(ann.attribute_id),
                'data_type': 'text'
            })
        return suggestions
    
    def get_ai_consensus(self, obj):
        suggestions = self.get_ai_suggestions(obj)
        by_attribute = defaultdict(list)
        for suggestion in suggestions:
            by_attribute[suggestion['attribute']].append(suggestion)
        
        consensus_list = []
        for attribute_id, entries in by_attribute.items():
            counter = Counter(entry['suggested_value'] for entry in entries if entry['suggested_value'])
            consensus_value, count = (counter.most_common(1)[0] if counter else (None, 0))
            attr_name = entries[0]['attribute_name'] if entries else ''
            consensus_list.append({
                'id': attribute_id,
                'attribute': attribute_id,
                'attribute_name': attr_name,
                'consensus_value': consensus_value or '',
                'data_type': 'text',
                'allowed_values': entries[0]['allowed_values'] if entries else None,
                'confidence': (count / len(entries)) if entries else None
            })
        return consensus_list
    
    def get_human_annotations(self, obj):
        product = obj.batch_item.product
        annotator_id = self.context.get('annotator_id')
        
        queryset = ProductAnnotation.objects.filter(
            product=product,
            source_type='human',
            attribute__is_active=True
        ).select_related('attribute')
        
        if annotator_id:
            queryset = queryset.filter(source_id=annotator_id)
        
        attribute_ids = queryset.values_list('attribute_id', flat=True).distinct()
        option_map = self._get_attribute_options(attribute_ids)
        
        annotations = []
        for ann in queryset:
            annotations.append({
                'id': ann.id,
                'attribute': ann.attribute_id,
                'attribute_name': ann.attribute.attribute_name,
                'product': product.id,
                'product_name': product.style_desc or product.style_id,
                'annotator': ann.source_id,
                'annotator_name': HumanAnnotator.objects.filter(id=ann.source_id).values_list(
                    'user__username', flat=True
                ).first() or f'Annotator {ann.source_id}',
                'annotated_value': ann.value,
                'status': 'approved' if obj.status == 'human_done' else 'suggested',
                'note': '',
                'allowed_values': option_map.get(ann.attribute_id),
                'ai_suggested_value': None,
                'batch_item': obj.id,
            })
        return annotations
    
    def get_applicable_attributes(self, obj):
        product = obj.batch_item.product
        global_attrs, subclass_attrs = self._get_applicable_attribute_queryset(product)
        attribute_ids = []
        attributes = []
        
        for map_obj in global_attrs:
            attribute_ids.append(map_obj.attribute.id)
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name,
                'data_type': 'text',
                'allowed_values': None,
            })
        
        for map_obj in subclass_attrs:
            attribute_ids.append(map_obj.attribute.id)
            attributes.append({
                'id': map_obj.attribute.id,
                'name': map_obj.attribute.attribute_name,
                'data_type': 'text',
                'allowed_values': None,
            })
        
        option_map = self._get_attribute_options(attribute_ids)
        for attr in attributes:
            attr['allowed_values'] = option_map.get(attr['id'])
        
        return attributes


class ProductAnnotationSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source='attribute.attribute_name', read_only=True)
    source_name = serializers.CharField(read_only=True)
    product_style_id = serializers.CharField(source='product.style_id', read_only=True)
    
    class Meta:
        model = ProductAnnotation
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class ProductAttributeSerializer(serializers.ModelSerializer):
    attribute_name = serializers.CharField(source='attribute.attribute_name', read_only=True)
    product_style_id = serializers.CharField(source='product.style_id', read_only=True)
    
    class Meta:
        model = ProductAttribute
        fields = '__all__'
        read_only_fields = ['id']


class MissingValueFlagSerializer(serializers.ModelSerializer):
    product_style_id = serializers.CharField(source='product.style_id', read_only=True)
    attribute_name = serializers.CharField(source='attribute.attribute_name', read_only=True)
    annotator_name = serializers.CharField(source='annotator.user.username', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.username', read_only=True, allow_null=True)
    
    class Meta:
        model = MissingValueFlag
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']


class AIProcessingControlSerializer(serializers.ModelSerializer):
    paused_by_name = serializers.CharField(source='paused_by.username', read_only=True, allow_null=True)
    
    class Meta:
        model = AIProcessingControl
        fields = '__all__'
        read_only_fields = ['id', 'last_updated']


# Request/Response Serializers
class CreateBatchRequestSerializer(serializers.Serializer):
    batch_type = serializers.ChoiceField(choices=['ai', 'human'])
    batch_size = serializers.IntegerField(min_value=1, default=10)
    name = serializers.CharField(required=False, allow_blank=True)
    
    # For AI batches
    ai_provider_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[]
    )
    
    # For Human batches
    annotator_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[]
    )
    force_create = serializers.BooleanField(default=False)


class CreateMultiBatchRequestSerializer(serializers.Serializer):
    batch_type = serializers.ChoiceField(choices=['ai', 'human'])
    total_batches = serializers.IntegerField(min_value=1, default=1)
    items_per_batch = serializers.IntegerField(min_value=1, default=10)
    
    # For AI batches
    ai_provider_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[]
    )
    
    # For Human batches
    annotator_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[]
    )
    force_create = serializers.BooleanField(default=False)
    
    # Filter parameters
    search = serializers.CharField(required=False, allow_blank=True, default='')
    class_filter = serializers.CharField(required=False, allow_blank=True, default='')
    subclass_filter = serializers.CharField(required=False, allow_blank=True, default='')
    department_filter = serializers.CharField(required=False, allow_blank=True, default='')
    subdepartment_filter = serializers.CharField(required=False, allow_blank=True, default='')

    # ADD THESE TWO NEW LINES:
    order_by = serializers.ChoiceField(choices=['id', 'created_at'], required=False, default='id')
    order_dir = serializers.ChoiceField(choices=['asc', 'desc'], required=False, default='asc')


class CreateAIBatchRequestSerializer(serializers.Serializer):
    batch_size = serializers.IntegerField(min_value=1, default=10)
    ai_provider_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[]
    )
    name = serializers.CharField(required=False, allow_blank=True)


class CreateHumanBatchRequestSerializer(serializers.Serializer):
    batch_size = serializers.IntegerField(min_value=1, default=10)
    annotator_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        default=[]
    )
    name = serializers.CharField(required=False, allow_blank=True)
    force_create = serializers.BooleanField(default=False)


class SubmitAnnotationRequestSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    attribute_id = serializers.IntegerField()
    value = serializers.CharField()
    batch_assignment_item_id = serializers.IntegerField()
    confidence_score = serializers.FloatField(
        required=False, 
        min_value=0.0, 
        max_value=1.0,
        allow_null=True
    )


class FlagMissingValueRequestSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    attribute_id = serializers.IntegerField()
    requested_value = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True)
    batch_assignment_item_id = serializers.IntegerField(required=False, allow_null=True)


class BatchProgressUpdateSerializer(serializers.Serializer):
    assignment_item_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=[
        'pending_ai', 'ai_in_progress', 'ai_failed', 'ai_done',
        'pending_human', 'human_in_progress', 'human_done'
    ])


class StartAutoAIProcessingSerializer(serializers.Serializer):
    batch_size = serializers.IntegerField(default=10, min_value=1)
    ai_provider_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False
    )


class AutoAssignHumanBatchSerializer(serializers.Serializer):
    batch_size = serializers.IntegerField(default=10, min_value=1)
    overlap_count = serializers.IntegerField(default=1, min_value=1, max_value=10)
    force_create = serializers.BooleanField(default=False)
    name = serializers.CharField(required=False, allow_blank=True)


class UpdateProductStatusSerializer(serializers.Serializer):
    product_ids = serializers.ListField(
        child=serializers.IntegerField(),
        min_length=1
    )
    new_status = serializers.ChoiceField(choices=[
        'pending', 'pending_ai', 'ai_in_progress', 'ai_failed', 'ai_done',
        'pending_human', 'human_in_progress', 'human_done'
    ])
