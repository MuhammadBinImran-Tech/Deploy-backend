# products/models.py
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Department(models.Model):
    """lu_department table"""
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    
    class Meta:
        db_table = 'lu_department'
        managed = False
        verbose_name = 'Department'
        verbose_name_plural = 'Departments'
    
    def __str__(self):
        return self.name


class SubDepartment(models.Model):
    """lu_subdepartment table"""
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    
    class Meta:
        db_table = 'lu_subdepartment'
        managed = False
        verbose_name = 'Sub Department'
        verbose_name_plural = 'Sub Departments'
    
    def __str__(self):
        return self.name


class Class(models.Model):
    """lu_class table"""
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    
    class Meta:
        db_table = 'lu_class'
        managed = False
        verbose_name = 'Class'
        verbose_name_plural = 'Classes'
    
    def __str__(self):
        return self.name


class SubClass(models.Model):
    """lu_subclass table"""
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    
    class Meta:
        db_table = 'lu_subclass'
        managed = False
        verbose_name = 'Sub Class'
        verbose_name_plural = 'Sub Classes'
    
    def __str__(self):
        return self.name


class RawProductData(models.Model):
    """tbl_raw_product_data table"""
    id = models.BigAutoField(primary_key=True)
    division = models.CharField(max_length=200, null=True, blank=True)
    style_id = models.CharField(max_length=200)
    color_id = models.CharField(max_length=200, null=True, blank=True)
    size_desc = models.CharField(max_length=200, null=True, blank=True)
    dim_desc = models.CharField(max_length=100, null=True, blank=True)
    raw_payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'tbl_raw_product_data'
        managed = False
        verbose_name = 'Raw Product Data'
        verbose_name_plural = 'Raw Product Data'
        unique_together = ('division', 'style_id', 'color_id', 'size_desc', 'dim_desc')
    
    def __str__(self):
        return f"{self.style_id} - {self.color_id or 'N/A'} - {self.size_desc or 'N/A'}"


class BaseProduct(models.Model):
    """tbl_base_product table"""
    PROCESSING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('pending_ai', 'Pending AI'),
        ('ai_in_progress', 'AI In Progress'),
        ('ai_failed', 'AI Failed'),
        ('ai_done', 'AI Done'),
        ('pending_human', 'Pending Human'),
        ('human_in_progress', 'Human In Progress'),
        ('human_done', 'Human Done'),
    ]

    id = models.BigAutoField(primary_key=True)

    ingestion_batch = models.IntegerField()
    is_active = models.BooleanField(default=True)
    style_id = models.CharField(max_length=200)
    style_desc = models.TextField(null=True, blank=True)
    style_description = models.TextField(null=True, blank=True)

    # Foreign keys + denormalized names (per DDL)
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='dept_id',
        related_name='base_products',
    )
    dept_name = models.CharField(max_length=200, null=True, blank=True)

    subdepartment = models.ForeignKey(
        SubDepartment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='subdept_id',
        related_name='base_products',
    )
    subdept_name = models.CharField(max_length=200, null=True, blank=True)

    class_field = models.ForeignKey(
        Class,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='class_id',
        related_name='base_products',
    )
    class_name = models.CharField(max_length=200, null=True, blank=True)

    subclass = models.ForeignKey(
        SubClass,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='subclass_id',
        related_name='base_products',
    )
    subclass_name = models.CharField(max_length=200, null=True, blank=True)

    processing_status = models.CharField(
        max_length=30,
        choices=PROCESSING_STATUS_CHOICES,
        default='pending'
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_base_product'
        managed = False
        verbose_name = 'Base Product'
        verbose_name_plural = 'Base Products'
        unique_together = ('style_id', 'ingestion_batch')
    
    def __str__(self):
        return f"{self.style_id} (batch {self.ingestion_batch})"
    
    @property
    def primary_image_url(self):
        """Get the first image URL across all colors for this base product."""
        first_color = self.colors.all().prefetch_related('images').first()
        if not first_color:
            return None
        first_image = first_color.images.first()
        return first_image.image_url if first_image else None

    # Compatibility helpers (old frontend/API expects these fields)
    @property
    def color_id(self):
        first_color = self.colors.first()
        return first_color.color_id if first_color else None

    @property
    def color_desc(self):
        first_color = self.colors.first()
        return first_color.color_desc if first_color else None

    @property
    def size_desc(self):
        first_color = self.colors.all().prefetch_related('sizes').first()
        if not first_color:
            return None
        first_size = first_color.sizes.first()
        return first_size.size_desc if first_size else None

    @property
    def dim_desc(self):
        first_color = self.colors.all().prefetch_related('sizes').first()
        if not first_color:
            return None
        first_size = first_color.sizes.first()
        return first_size.dim_desc if first_size else None


class ProductColor(models.Model):
    """tbl_product_color table"""
    id = models.BigAutoField(primary_key=True)
    base_product = models.ForeignKey(
        BaseProduct,
        on_delete=models.CASCADE,
        db_column='base_product_id',
        related_name='colors',
    )
    color_id = models.CharField(max_length=200)
    color_desc = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tbl_product_color'
        managed = False
        verbose_name = 'Product Color'
        verbose_name_plural = 'Product Colors'
        unique_together = ('base_product', 'color_id')

    def __str__(self):
        return f"{self.base_product.style_id} - {self.color_id}"


class ProductSize(models.Model):
    """tbl_product_size table"""
    id = models.BigAutoField(primary_key=True)
    product_color = models.ForeignKey(
        ProductColor,
        on_delete=models.CASCADE,
        db_column='product_color_id',
        related_name='sizes',
    )
    size_desc = models.CharField(max_length=200, null=True, blank=True)
    dim_desc = models.CharField(max_length=100, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tbl_product_size'
        managed = False
        verbose_name = 'Product Size'
        verbose_name_plural = 'Product Sizes'
        unique_together = ('product_color', 'size_desc', 'dim_desc')

    def __str__(self):
        return f"{self.product_color.base_product.style_id} - {self.size_desc or 'N/A'}"


class ProductImage(models.Model):
    """tbl_product_images table"""
    id = models.BigAutoField(primary_key=True)
    product_color = models.ForeignKey(
        ProductColor,
        on_delete=models.CASCADE,
        db_column='product_color_id',
        related_name='images',
    )
    image_url = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_product_images'
        managed = False
        verbose_name = 'Product Image'
        verbose_name_plural = 'Product Images'
        unique_together = ('product_color', 'image_url')
    
    def __str__(self):
        return f"Image for {self.product_color.base_product.style_id}"


class AttributeMaster(models.Model):
    """tbl_attribute_master table"""
    id = models.BigAutoField(primary_key=True)
    attribute_name = models.CharField(max_length=200, unique=True)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'tbl_attribute_master'
        managed = False
        verbose_name = 'Attribute Master'
        verbose_name_plural = 'Attribute Masters'
    
    def __str__(self):
        return self.attribute_name


class AttributeOption(models.Model):
    """tbl_attribute_options table"""
    id = models.BigAutoField(primary_key=True)
    attribute = models.ForeignKey(AttributeMaster, on_delete=models.CASCADE, db_column='attribute_id')
    option_value = models.CharField(max_length=200)
    
    class Meta:
        db_table = 'tbl_attribute_options'
        managed = False
        verbose_name = 'Attribute Option'
        verbose_name_plural = 'Attribute Options'
        unique_together = ('attribute', 'option_value')
    
    def __str__(self):
        return f"{self.attribute.attribute_name}: {self.option_value}"


class AttributeGlobalMap(models.Model):
    """tbl_attribute_global_map table"""
    id = models.BigAutoField(primary_key=True)
    attribute = models.ForeignKey(AttributeMaster, on_delete=models.CASCADE, db_column='attribute_id', unique=True)
    
    class Meta:
        db_table = 'tbl_attribute_global_map'
        managed = False
        verbose_name = 'Global Attribute'
        verbose_name_plural = 'Global Attributes'
    
    def __str__(self):
        return f"Global: {self.attribute.attribute_name}"


class AttributeSubclassMap(models.Model):
    """tbl_attribute_subclass_map table"""
    id = models.BigAutoField(primary_key=True)
    attribute = models.ForeignKey(AttributeMaster, on_delete=models.CASCADE, db_column='attribute_id')
    subclass = models.ForeignKey(SubClass, on_delete=models.CASCADE, db_column='subclass_id')
    
    class Meta:
        db_table = 'tbl_attribute_subclass_map'
        managed = False
        verbose_name = 'Subclass Attribute Map'
        verbose_name_plural = 'Subclass Attribute Maps'
        unique_together = ('attribute', 'subclass')
    
    def __str__(self):
        return f"{self.subclass.name} -> {self.attribute.attribute_name}"


class ProductAttribute(models.Model):
    """tbl_product_attributes table"""
    SOURCE_CHOICES = [
        ('ai', 'AI'),
        ('human', 'Human'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    product = models.ForeignKey(BaseProduct, on_delete=models.CASCADE, db_column='product_id')
    attribute = models.ForeignKey(AttributeMaster, on_delete=models.CASCADE, db_column='attribute_id')
    value = models.CharField(max_length=500)
    source = models.CharField(max_length=50, choices=SOURCE_CHOICES)
    
    class Meta:
        db_table = 'tbl_product_attributes'
        managed = False
        verbose_name = 'Product Attribute'
        verbose_name_plural = 'Product Attributes'
        unique_together = ('product', 'attribute', 'source')
    
    def __str__(self):
        return f"{self.product.style_id} - {self.attribute.attribute_name}: {self.value} ({self.source})"


class AIProvider(models.Model):
    """tbl_ai_provider table"""
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200, unique=True)
    service_name = models.CharField(max_length=200, null=True, blank=True)
    model_name = models.CharField(max_length=200, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    config = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'tbl_ai_provider'
        managed = False
        verbose_name = 'AI Provider'
        verbose_name_plural = 'AI Providers'
    
    def __str__(self):
        return self.name


class HumanAnnotator(models.Model):
    """tbl_human_annotator table"""
    id = models.BigAutoField(primary_key=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, db_column='user_id')
    
    class Meta:
        db_table = 'tbl_human_annotator'
        managed = False
        verbose_name = 'Human Annotator'
        verbose_name_plural = 'Human Annotators'
    
    def __str__(self):
        return f"{self.user.username} (ID: {self.id})"


class AnnotationBatch(models.Model):
    """tbl_annotation_batch table"""
    BATCH_TYPE_CHOICES = [
        ('ai', 'AI'),
        ('human', 'Human'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    name = models.CharField(max_length=200)
    description = models.TextField(null=True, blank=True)
    batch_size = models.IntegerField(default=10)
    batch_type = models.CharField(max_length=20, choices=BATCH_TYPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_annotation_batch'
        managed = False
        verbose_name = 'Annotation Batch'
        verbose_name_plural = 'Annotation Batches'
    
    def __str__(self):
        return f"{self.name} ({self.get_batch_type_display()})"
    
    @property
    def actual_size(self):
        """Get actual number of products in batch"""
        return self.batchitem_set.count()


class BatchItem(models.Model):
    """tbl_batch_item table"""
    BATCH_TYPE_CHOICES = [
        ('ai', 'AI'),
        ('human', 'Human'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    batch = models.ForeignKey(AnnotationBatch, on_delete=models.CASCADE, db_column='batch_id')
    product = models.ForeignKey(BaseProduct, on_delete=models.CASCADE, db_column='product_id')
    batch_type = models.CharField(max_length=20, choices=BATCH_TYPE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_batch_item'
        managed = False
        verbose_name = 'Batch Item'
        verbose_name_plural = 'Batch Items'
        unique_together = ('product', 'batch_type')
        indexes = [
            models.Index(fields=['batch', 'batch_type']),
        ]
    
    def __str__(self):
        return f"{self.product.style_id} in {self.batch.name}"


class BatchAssignment(models.Model):
    """tbl_batch_assignment table"""
    ASSIGNMENT_TYPE_CHOICES = [
        ('ai', 'AI'),
        ('human', 'Human'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    batch = models.ForeignKey(AnnotationBatch, on_delete=models.CASCADE, db_column='batch_id')
    assignment_type = models.CharField(max_length=20, choices=ASSIGNMENT_TYPE_CHOICES)
    assignment_id = models.BigIntegerField()  # AI Provider ID or Human Annotator ID
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_batch_assignment'
        managed = False
        verbose_name = 'Batch Assignment'
        verbose_name_plural = 'Batch Assignments'
        unique_together = ('batch', 'assignment_type', 'assignment_id')
    
    def __str__(self):
        if self.assignment_type == 'ai':
            try:
                provider = AIProvider.objects.get(id=self.assignment_id)
                return f"AI Assignment: {provider.name} for {self.batch.name}"
            except AIProvider.DoesNotExist:
                return f"AI Assignment ID {self.assignment_id} for {self.batch.name}"
        else:
            try:
                annotator = HumanAnnotator.objects.get(id=self.assignment_id)
                return f"Human Assignment: {annotator.user.username} for {self.batch.name}"
            except HumanAnnotator.DoesNotExist:
                return f"Human Assignment ID {self.assignment_id} for {self.batch.name}"
    
    @property
    def assignee_name(self):
        """Get name of the assignee"""
        if self.assignment_type == 'ai':
            try:
                return AIProvider.objects.get(id=self.assignment_id).name
            except AIProvider.DoesNotExist:
                return f"AI Provider {self.assignment_id}"
        else:
            try:
                return HumanAnnotator.objects.get(id=self.assignment_id).user.username
            except HumanAnnotator.DoesNotExist:
                return f"Annotator {self.assignment_id}"


class BatchAssignmentItem(models.Model):
    """tbl_batch_assignment_item table"""
    STATUS_CHOICES = [
        ('pending_ai', 'Pending AI'),
        ('ai_in_progress', 'AI In Progress'),
        ('ai_failed', 'AI Failed'),
        ('ai_done', 'AI Done'),
        ('pending_human', 'Pending Human'),
        ('human_in_progress', 'Human In Progress'),
        ('human_done', 'Human Done'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    assignment = models.ForeignKey(BatchAssignment, on_delete=models.CASCADE, db_column='assignment_id')
    batch_item = models.ForeignKey(BatchItem, on_delete=models.CASCADE, db_column='batch_item_id')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_batch_assignment_item'
        managed = False
        verbose_name = 'Batch Assignment Item'
        verbose_name_plural = 'Batch Assignment Items'
        unique_together = ('assignment', 'batch_item')
    
    def __str__(self):
        return f"Assignment Item: {self.batch_item.product.style_id} - {self.status}"
    
    def save(self, *args, **kwargs):
        # Set started_at when status changes to in_progress
        if self.pk:
            old_status = BatchAssignmentItem.objects.get(pk=self.pk).status
            if old_status in ['pending_ai', 'pending_human'] and self.status in ['ai_in_progress', 'human_in_progress']:
                self.started_at = timezone.now()
        
        # Set completed_at when status changes to done
        if self.pk:
            old_status = BatchAssignmentItem.objects.get(pk=self.pk).status
            if self.status in ['ai_done', 'human_done'] and old_status not in ['ai_done', 'human_done']:
                self.completed_at = timezone.now()
        
        super().save(*args, **kwargs)


class ProductAnnotation(models.Model):
    """tbl_product_annotations table"""
    SOURCE_TYPE_CHOICES = [
        ('ai', 'AI'),
        ('human', 'Human'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    product = models.ForeignKey(BaseProduct, on_delete=models.CASCADE, db_column='product_id')
    attribute = models.ForeignKey(AttributeMaster, on_delete=models.CASCADE, db_column='attribute_id')
    value = models.CharField(max_length=500)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPE_CHOICES)
    source_id = models.BigIntegerField()  # AI Provider ID or Human Annotator ID
    confidence_score = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    batch_item = models.ForeignKey(BatchItem, on_delete=models.SET_NULL, null=True, blank=True, db_column='batch_item_id')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_product_annotations'
        managed = False
        verbose_name = 'Product Annotation'
        verbose_name_plural = 'Product Annotations'
        unique_together = ('product', 'attribute', 'source_type', 'source_id')
        indexes = [
            models.Index(fields=['product', 'attribute']),
            models.Index(fields=['source_type', 'source_id']),
        ]
    
    def __str__(self):
        return f"{self.product.style_id} - {self.attribute.attribute_name}: {self.value} ({self.source_type})"
    
    @property
    def source_name(self):
        """Get name of the source"""
        if self.source_type == 'ai':
            try:
                return AIProvider.objects.get(id=self.source_id).name
            except AIProvider.DoesNotExist:
                return f"AI Provider {self.source_id}"
        else:
            try:
                return HumanAnnotator.objects.get(id=self.source_id).user.username
            except HumanAnnotator.DoesNotExist:
                return f"Annotator {self.source_id}"


class MissingValueFlag(models.Model):
    """tbl_missing_value_flags table"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('reviewed', 'Reviewed'),
        ('resolved', 'Resolved'),
        ('rejected', 'Rejected'),
    ]
    
    id = models.BigAutoField(primary_key=True)
    product = models.ForeignKey(BaseProduct, on_delete=models.CASCADE, db_column='product_id')
    attribute = models.ForeignKey(AttributeMaster, on_delete=models.CASCADE, db_column='attribute_id')
    annotator = models.ForeignKey(HumanAnnotator, on_delete=models.CASCADE, db_column='annotator_id')
    batch_item = models.ForeignKey(BatchItem, on_delete=models.SET_NULL, null=True, blank=True, db_column='batch_item_id')
    requested_value = models.TextField()
    reason = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, db_column='reviewed_by')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_missing_value_flags'
        managed = False
        verbose_name = 'Missing Value Flag'
        verbose_name_plural = 'Missing Value Flags'
        unique_together = ('product', 'attribute', 'annotator', 'batch_item')
    
    def __str__(self):
        return f"Flag: {self.product.style_id} - {self.attribute.attribute_name} - {self.status}"


class AIProcessingControl(models.Model):
    """tbl_ai_processing_control table"""
    id = models.BigAutoField(primary_key=True)
    is_paused = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, db_column='paused_by')
    last_updated = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'tbl_ai_processing_control'
        managed = False
        verbose_name = 'AI Processing Control'
        verbose_name_plural = 'AI Processing Controls'
    
    def __str__(self):
        return f"AI Processing: {'Paused' if self.is_paused else 'Running'}"
    
    @classmethod
    def get_control(cls):
        """Get or create the singleton control instance"""
        try:
            return cls.objects.get(id=1)
        except cls.DoesNotExist:
            control = cls(id=1)
            control.save()
            return control
    
    def save(self, *args, **kwargs):
        if not self.pk:
            self.pk = 1
        super().save(*args, **kwargs)


class AIProcessingRun(models.Model):
    """Managed table to track real AI processing attempts without touching source tables."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.BigAutoField(primary_key=True)
    assignment_item = models.ForeignKey(
        BatchAssignmentItem,
        on_delete=models.CASCADE,
        db_column='assignment_item_id',
        related_name='processing_runs',
        null=True,
        blank=True,
    )
    provider = models.ForeignKey(
        AIProvider,
        on_delete=models.CASCADE,
        db_column='provider_id',
        related_name='processing_runs',
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    attempt = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    last_error = models.TextField(null=True, blank=True)
    last_response = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tbl_ai_processing_run'
        managed = True
        verbose_name = 'AI Processing Run'
        verbose_name_plural = 'AI Processing Runs'
        indexes = [
            models.Index(fields=['provider', 'status']),
            models.Index(fields=['assignment_item', 'status']),
        ]

    def __str__(self):
        return f"Run {self.id} for assignment item {self.assignment_item_id}"


class AIProviderFailureLog(models.Model):
    """Managed table to capture provider failures and keep legacy tables untouched."""

    id = models.BigAutoField(primary_key=True)
    provider = models.ForeignKey(
        AIProvider,
        on_delete=models.CASCADE,
        db_column='provider_id',
        related_name='failure_logs',
    )
    assignment_item = models.ForeignKey(
        BatchAssignmentItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column='assignment_item_id',
        related_name='failure_logs',
    )
    error_type = models.CharField(max_length=100)
    error_message = models.TextField()
    http_status = models.IntegerField(null=True, blank=True)
    request_payload = models.JSONField(null=True, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tbl_ai_provider_failure_log'
        managed = True
        verbose_name = 'AI Provider Failure Log'
        verbose_name_plural = 'AI Provider Failure Logs'
        indexes = [
            models.Index(fields=['provider', 'is_resolved']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"{self.provider.name} failure ({self.error_type})"
