# products/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import *

router = DefaultRouter()
router.register(r'products', ProductViewSet, basename='product')
router.register(r'attributes', AttributeViewSet, basename='attribute')
router.register(r'ai-providers', AIProviderViewSet, basename='aiprovider')
router.register(r'ai-global-prompt', AIGlobalPromptViewSet, basename='aiglobalprompt')
router.register(r'ai-provider-subclass-prompts', AIProviderSubclassPromptViewSet, basename='aiprovidersubclassprompt')
router.register(r'human-annotators', HumanAnnotatorViewSet, basename='humanannotator')
router.register(r'batches', AnnotationBatchViewSet, basename='batch')
router.register(r'assignments', BatchAssignmentViewSet, basename='assignment')
router.register(r'batch-items', BatchAssignmentItemViewSet, basename='batch-item')
router.register(r'annotations', ProductAnnotationViewSet, basename='annotation')
router.register(r'missing-value-flags', MissingValueFlagViewSet, basename='missingvalueflag')
router.register(r'ai-processing-control', AIProcessingControlViewSet, basename='aiprocessingcontrol')
router.register(r'dashboard', DashboardViewSet, basename='dashboard')
router.register(r'auto-ai-processing', AutoAIProcessingViewSet, basename='autoai')
router.register(r'attribute-management', AttributeManagementViewSet, basename='attribute-management')

urlpatterns = [
    path('', include(router.urls)),
    path('ai-processing/status/', AIProcessingControlViewSet.as_view({'get': 'status'}), name='ai-processing-status'),
    # New endpoints for multi-batch creation with filters
    path('products/filtered_count/', ProductViewSet.as_view({'get': 'filtered_count'}), name='products-filtered-count'),
    path('products/create_multi_batch/', ProductViewSet.as_view({'post': 'create_multi_batch'}), name='products-create-multi-batch'),
    # Original endpoints for backward compatibility
    path('products/create_batch/', ProductViewSet.as_view({'post': 'create_batch'}), name='products-create-batch'),
]
