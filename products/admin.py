# products/admin.py
from django.contrib import admin
from django.contrib.auth.models import User, Group
from .models import *

# Note: We don't unregister or re-register User/Group since they're already registered
# by Django's auth app. We'll just register our custom models.

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'name']
    search_fields = ['name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(SubDepartment)
class SubDepartmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'name']
    search_fields = ['name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(Class)
class ClassAdmin(admin.ModelAdmin):
    list_display = ['id', 'name']
    search_fields = ['name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(SubClass)
class SubClassAdmin(admin.ModelAdmin):
    list_display = ['id', 'name']
    search_fields = ['name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(BaseProduct)
class BaseProductAdmin(admin.ModelAdmin):
    list_display = ['id', 'style_id', 'color_desc', 'size_desc', 'processing_status', 'created_at']
    list_filter = ['processing_status', 'department', 'created_at']
    search_fields = ['style_id', 'style_desc', 'color_desc']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 50
    fieldsets = (
        ('Basic Information', {
            'fields': ('style_id', 'style_desc', 'style_description')
        }),
        ('Taxonomy', {
            'fields': ('department', 'subdepartment', 'class_field', 'subclass')
        }),
        ('Additional Info', {
            'fields': ('ingestion_batch', 'is_active', 'processing_status')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ['id', 'product_color', 'image_url']
    list_filter = ['product_color']
    search_fields = ['product_color__base_product__style_id', 'image_url']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(AttributeMaster)
class AttributeMasterAdmin(admin.ModelAdmin):
    list_display = ['id', 'attribute_name', 'description', 'is_active']
    list_filter = ['is_active']
    search_fields = ['attribute_name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(AttributeOption)
class AttributeOptionAdmin(admin.ModelAdmin):
    list_display = ['id', 'attribute', 'option_value']
    list_filter = ['attribute']
    search_fields = ['attribute__attribute_name', 'option_value']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(AttributeGlobalMap)
class AttributeGlobalMapAdmin(admin.ModelAdmin):
    list_display = ['id', 'attribute']
    search_fields = ['attribute__attribute_name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(AttributeSubclassMap)
class AttributeSubclassMapAdmin(admin.ModelAdmin):
    list_display = ['id', 'attribute', 'subclass']
    list_filter = ['subclass']
    search_fields = ['attribute__attribute_name', 'subclass__name']
    readonly_fields = ['id']
    list_per_page = 20


@admin.register(AIProvider)
class AIProviderAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'service_name', 'model_name', 'is_active', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'service_name', 'model_name']
    readonly_fields = ['id', 'created_at']
    list_per_page = 20


@admin.register(HumanAnnotator)
class HumanAnnotatorAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'get_username', 'get_email']
    search_fields = ['user__username', 'user__email']
    readonly_fields = ['id']
    list_per_page = 20
    
    def get_username(self, obj):
        return obj.user.username
    get_username.short_description = 'Username'
    
    def get_email(self, obj):
        return obj.user.email
    get_email.short_description = 'Email'


@admin.register(AnnotationBatch)
class AnnotationBatchAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'batch_type', 'batch_size', 'created_at', 'updated_at']
    list_filter = ['batch_type', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 20


@admin.register(BatchItem)
class BatchItemAdmin(admin.ModelAdmin):
    list_display = ['id', 'batch', 'product', 'batch_type', 'created_at']
    list_filter = ['batch_type', 'batch', 'created_at']
    search_fields = ['batch__name', 'product__style_id']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 20


@admin.register(BatchAssignment)
class BatchAssignmentAdmin(admin.ModelAdmin):
    list_display = ['id', 'batch', 'assignment_type', 'get_assignee', 'status', 'progress', 'created_at']
    list_filter = ['assignment_type', 'status', 'batch', 'created_at']
    search_fields = ['batch__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 20
    
    def get_assignee(self, obj):
        return obj.assignee_name
    get_assignee.short_description = 'Assignee'


@admin.register(BatchAssignmentItem)
class BatchAssignmentItemAdmin(admin.ModelAdmin):
    list_display = ['id', 'assignment', 'batch_item', 'status', 'started_at', 'completed_at']
    list_filter = ['status', 'assignment', 'created_at']
    search_fields = ['assignment__batch__name', 'batch_item__product__style_id']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 20


@admin.register(ProductAnnotation)
class ProductAnnotationAdmin(admin.ModelAdmin):
    list_display = ['id', 'product', 'attribute', 'value', 'source_type', 'get_source', 'created_at']
    list_filter = ['source_type', 'attribute', 'created_at']
    search_fields = ['product__style_id', 'attribute__attribute_name', 'value']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 20
    
    def get_source(self, obj):
        return obj.source_name
    get_source.short_description = 'Source'


@admin.register(MissingValueFlag)
class MissingValueFlagAdmin(admin.ModelAdmin):
    list_display = ['id', 'product', 'attribute', 'annotator', 'status', 'requested_value', 'created_at']
    list_filter = ['status', 'attribute', 'created_at']
    search_fields = ['product__style_id', 'attribute__attribute_name', 'requested_value']
    readonly_fields = ['id', 'created_at', 'updated_at']
    list_per_page = 20


@admin.register(AIProcessingControl)
class AIProcessingControlAdmin(admin.ModelAdmin):
    list_display = ['id', 'is_paused', 'paused_at', 'paused_by', 'last_updated']
    readonly_fields = ['id', 'last_updated']
    list_per_page = 20
    
    def has_add_permission(self, request):
        return False
    
    def has_delete_permission(self, request, obj=None):
        return False
