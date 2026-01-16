"""
AI batch processing orchestrator with proper queue behavior.
Ensures batches run sequentially, not in parallel.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone

from .ai_service import get_ai_service
from .models import (
    AIProcessingControl,
    AIProcessingRun,
    AIProvider,
    AIProviderFailureLog,
    AttributeGlobalMap,
    AttributeOption,
    AttributeSubclassMap,
    BatchAssignment,
    BatchAssignmentItem,
    BaseProduct,
    ProductAnnotation,
)

logger = logging.getLogger(__name__)

# CRITICAL: Global lock ensures only ONE batch processes at a time (queue behavior)
_BATCH_PROCESS_LOCK = threading.Lock()

# Track which batch IDs are currently queued/processing
_BATCH_QUEUE: List[int] = []
_QUEUE_LOCK = threading.Lock()


@dataclass
class AttributePayload:
    id: int
    name: str
    description: Optional[str]
    allowed_values: Optional[List[str]]


class AIBatchProcessor:
    """
    Executes AI processing for a batch using real providers.
    Uses global lock to ensure sequential processing (queue behavior).
    """

    def __init__(self, provider_ids: List[int]) -> None:
        self.providers = list(AIProvider.objects.filter(id__in=provider_ids, is_active=True))

    def process_batch(self, batch_id: int) -> None:
        """
        Process batch in queue - waits for lock, processes sequentially.
        """
        # Add to queue
        with _QUEUE_LOCK:
            if batch_id in _BATCH_QUEUE:
                logger.info(f"Batch {batch_id} already in queue, skipping duplicate")
                return
            _BATCH_QUEUE.append(batch_id)
            logger.info(f"Batch {batch_id} added to queue. Queue size: {len(_BATCH_QUEUE)}")
        
        try:
            # CRITICAL: Acquire lock - blocks until previous batch completes
            with _BATCH_PROCESS_LOCK:
                logger.info(f"Batch {batch_id} acquired processing lock, starting...")
                self._process_batch_internal(batch_id)
        finally:
            # Remove from queue
            with _QUEUE_LOCK:
                if batch_id in _BATCH_QUEUE:
                    _BATCH_QUEUE.remove(batch_id)
                logger.info(f"Batch {batch_id} removed from queue. Queue size: {len(_BATCH_QUEUE)}")

    def _process_batch_internal(self, batch_id: int) -> None:
        """Internal batch processing logic."""
        assignments = (
            BatchAssignment.objects.filter(batch_id=batch_id, assignment_type="ai")
            .select_related("batch")
        )
        
        # Only update to in_progress if not already processing
        assignments.filter(status='pending').update(status="in_progress")

        for assignment in assignments:
            # Check if paused
            if AIProcessingControl.get_control().is_paused:
                logger.info(f"Processing paused for batch {batch_id}")
                assignment.status = 'pending'
                assignment.save(update_fields=["status", "updated_at"])
                return
            
            provider = self._find_provider(assignment.assignment_id)
            if not provider:
                self._mark_assignment_failed(assignment, "Provider inactive or missing")
                continue

            assignment_items = (
                BatchAssignmentItem.objects.filter(assignment=assignment)
                .select_related("batch_item__product")
                .exclude(status='ai_done')  # Skip already completed
                .order_by("id")
            )

            for item in assignment_items:
                # Check pause before each item
                if AIProcessingControl.get_control().is_paused:
                    logger.info(f"Paused mid-batch {batch_id}")
                    item.status = "pending_ai"
                    item.save(update_fields=["status", "updated_at"])
                    return

                self._process_assignment_item(item, provider)

            self._update_assignment_progress(assignment)

        # After all providers finish, update product statuses
        self._finalize_products(batch_id)
        logger.info(f"Batch {batch_id} processing complete")

    def _process_assignment_item(self, assignment_item: BatchAssignmentItem, provider: AIProvider) -> None:
        """Process a single assignment item with retries."""
        product = assignment_item.batch_item.product
        product_info = self._build_product_payload(product)
        attributes = self._get_attributes(product)

        max_retries = provider.config.get("max_retries", 3) if provider.config else 3
        try:
            max_retries = int(max_retries)
        except (TypeError, ValueError):
            max_retries = 3
        if max_retries < 1:
            max_retries = 1
        attempt = 0

        assignment_item.status = "ai_in_progress"
        assignment_item.started_at = assignment_item.started_at or timezone.now()
        assignment_item.save(update_fields=["status", "started_at", "updated_at"])

        while attempt < max_retries:
            attempt += 1
            run = AIProcessingRun.objects.create(
                assignment_item=assignment_item,
                provider=provider,
                status="processing",
                attempt=attempt,
                max_retries=max_retries,
                started_at=timezone.now(),
            )

            # Check pause
            if AIProcessingControl.get_control().is_paused:
                assignment_item.status = "pending_ai"
                assignment_item.save(update_fields=["status", "updated_at"])
                run.status = "failed"
                run.last_error = "Paused by control flag"
                run.save(update_fields=["status", "last_error", "updated_at"])
                return

            try:
                service = get_ai_service(provider.id)
                annotations = service.annotate_product(
                    product_info=product_info,
                    attributes=[
                        {
                            "id": attr.id,
                            "name": attr.name,
                            "description": attr.description,
                            "allowed_values": attr.allowed_values,
                        }
                        for attr in attributes
                    ],
                )

                # Persist annotations
                for attr in attributes:
                    if attr.name not in annotations:
                        continue

                    value = annotations[attr.name]
                    
                    # Confidence scoring
                    confidence = None
                    if value is not None:
                        if str(value).strip().lower() == "unknown":
                            confidence = 0.0
                        else:
                            confidence = 1.0

                    ProductAnnotation.objects.update_or_create(
                        product=product,
                        attribute_id=attr.id,
                        source_type="ai",
                        source_id=provider.id,
                        defaults={
                            "value": value,
                            "confidence_score": confidence,
                            "batch_item": assignment_item.batch_item,
                            "updated_at": timezone.now(),
                        },
                    )

                # Mark complete
                assignment_item.status = "ai_done"
                assignment_item.completed_at = timezone.now()
                assignment_item.save(update_fields=["status", "completed_at", "updated_at"])

                run.status = "completed"
                run.completed_at = timezone.now()
                run.save(update_fields=["status", "completed_at", "updated_at"])
                
                logger.info(f"Completed item {assignment_item.id} for provider {provider.name}")
                return
                
            except Exception as exc:
                logger.exception(f"AI provider {provider.name} failed for item {assignment_item.id}")
                run.status = "failed"
                run.last_error = str(exc)
                run.save(update_fields=["status", "last_error", "updated_at"])

                self._log_failure(provider, assignment_item, exc)

                error_text = str(exc).lower()
                is_auth_error = (
                    "status 401" in error_text
                    or "status 403" in error_text
                    or "api key" in error_text
                    or "authentication" in error_text
                )
                if is_auth_error:
                    logger.error(
                        f"Auth error for provider {provider.name}; marking item {assignment_item.id} as failed"
                    )
                    assignment_item.status = "ai_failed"
                    assignment_item.save(update_fields=["status", "updated_at"])

                    product = assignment_item.batch_item.product
                    if product.processing_status in ["ai_in_progress", "pending_ai", "pending"]:
                        product.processing_status = "ai_failed"
                        product.save(update_fields=["processing_status", "updated_at"])
                    return

                if attempt >= max_retries:
                    logger.error(f"Max retries reached for item {assignment_item.id}")
                    assignment_item.status = "ai_failed"
                    assignment_item.save(update_fields=["status", "updated_at"])
                    
                    # Mark product as failed so the UI reflects provider issues.
                    product = assignment_item.batch_item.product
                    if product.processing_status in ["ai_in_progress", "pending_ai", "pending"]:
                        product.processing_status = "ai_failed"
                        product.save(update_fields=["processing_status", "updated_at"])
                else:
                    time.sleep(2 ** attempt)  # Exponential backoff

    def _update_assignment_progress(self, assignment: BatchAssignment) -> None:
        """Update assignment progress percentage."""
        items = BatchAssignmentItem.objects.filter(assignment=assignment)
        total = items.count()
        completed = items.filter(status="ai_done").count()
        progress = (completed / total * 100) if total else 0

        assignment.progress = progress
        if completed == total and total > 0:
            assignment.status = "completed"
        assignment.save(update_fields=["progress", "status", "updated_at"])

    def _finalize_products(self, batch_id: int) -> None:
        """Mark products as ai_done when all providers complete."""
        batch_items = BatchAssignmentItem.objects.filter(
            assignment__batch_id=batch_id,
            assignment__assignment_type="ai",
        ).select_related("batch_item__product")

        for item in batch_items:
            product = item.batch_item.product
            product_items = BatchAssignmentItem.objects.filter(batch_item=item.batch_item)

            if product_items.filter(status="ai_failed").exists():
                if product.processing_status != "ai_failed":
                    product.processing_status = "ai_failed"
                    product.updated_at = timezone.now()
                    product.save(update_fields=["processing_status", "updated_at"])
                continue
            
            # Only mark ai_done if ALL providers finished
            if product_items.exclude(status="ai_done").exists():
                continue

            if product.processing_status in ["ai_in_progress", "pending_ai", "pending", "ai_failed"]:
                product.processing_status = "ai_done"
                product.updated_at = timezone.now()
                product.save(update_fields=["processing_status", "updated_at"])

    def _build_product_payload(self, product: BaseProduct) -> Dict[str, Any]:
        """
        Build product payload for AI service.

        IMPORTANT:
        - We now have multi-color products with sizes/images per color.
        - For AI providers we **only** send:
            * style-level information (id, name, description, category)
            * the first image URL of the first color (`primary_image_url`)
        - We deliberately do **not** expose specific color/size fields to the AI
          provider to keep the prompt generic and avoid variant-level leakage.
        """
        return {
            "style_id": product.style_id,
            "name": product.style_desc or product.style_id,
            "description": product.style_description or "",
            "category": product.class_name or "",
            "subcategory": product.subclass_name or "",
            "image_url": product.primary_image_url,
        }

    def _get_attributes(self, product: BaseProduct) -> List[AttributePayload]:
        """Get applicable attributes with allowed values."""
        attributes: List[AttributePayload] = []

        global_maps = AttributeGlobalMap.objects.select_related("attribute")
        subclass_maps = AttributeSubclassMap.objects.filter(
            subclass=product.subclass
        ).select_related("attribute") if product.subclass else []

        attribute_ids: List[int] = []
        for map_obj in list(global_maps) + list(subclass_maps):
            attribute_ids.append(map_obj.attribute.id)

        option_map = self._build_option_map(attribute_ids)

        for map_obj in list(global_maps) + list(subclass_maps):
            attr = map_obj.attribute
            attributes.append(
                AttributePayload(
                    id=attr.id,
                    name=attr.attribute_name,
                    description=attr.description,
                    allowed_values=option_map.get(attr.id),
                )
            )
        return attributes

    def _build_option_map(self, attribute_ids: List[int]) -> Dict[int, List[str]]:
        """Build map of attribute IDs to allowed values."""
        options = AttributeOption.objects.filter(attribute_id__in=attribute_ids)
        option_map: Dict[int, List[str]] = {}
        for option in options:
            option_map.setdefault(option.attribute_id, []).append(option.option_value)
        return option_map

    def _log_failure(self, provider: AIProvider, assignment_item: BatchAssignmentItem, exc: Exception) -> None:
        """Log provider failure."""
        AIProviderFailureLog.objects.create(
            provider=provider,
            assignment_item=assignment_item,
            error_type=exc.__class__.__name__,
            error_message=str(exc),
            created_at=timezone.now(),
        )

    def _find_provider(self, provider_id: int) -> Optional[AIProvider]:
        """Find provider by ID."""
        for provider in self.providers:
            if provider.id == provider_id:
                return provider
        return None

    def _mark_assignment_failed(self, assignment: BatchAssignment, message: str) -> None:
        """Mark assignment as cancelled."""
        assignment.status = "cancelled"
        assignment.progress = 0
        assignment.save(update_fields=["status", "progress", "updated_at"])
        items = BatchAssignmentItem.objects.filter(assignment=assignment).exclude(status="ai_done")
        items.update(status="ai_failed", updated_at=timezone.now())
        product_ids = items.values_list("batch_item__product_id", flat=True).distinct()
        BaseProduct.objects.filter(
            id__in=product_ids,
            processing_status__in=["pending", "pending_ai", "ai_in_progress"],
        ).update(processing_status="ai_failed", updated_at=timezone.now())
        logger.warning(f"Assignment {assignment.id} cancelled: {message}")
