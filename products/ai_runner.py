"""
AI batch processing orchestrator with proper queue behavior.
Ensures batches run sequentially, not in parallel.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from django.db import close_old_connections, transaction
from django.utils import timezone

from .ai_service import get_ai_service
from .attribute_utils import get_active_subclass_attribute_maps
from .models import (
    AIProcessingControl,
    AIProcessingRun,
    AIProvider,
    AIProviderFailureLog,
    AttributeOption,
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
        assignment_list = list(assignments)
        pause_event = threading.Event()

        with ThreadPoolExecutor(max_workers=max(len(assignment_list), 1)) as executor:
            futures = {
                executor.submit(self._process_assignment, assignment, pause_event): assignment
                for assignment in assignment_list
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    assignment = futures[future]
                    logger.exception(f"Provider worker failed for assignment {assignment.id}")

        if pause_event.is_set() or AIProcessingControl.get_control().is_paused:
            logger.info(f"Batch {batch_id} paused before completion")
            return

        # After all providers finish, update product statuses
        self._finalize_products(batch_id)
        logger.info(f"Batch {batch_id} processing complete")

    def _process_assignment(self, assignment: BatchAssignment, pause_event: threading.Event) -> None:
        """Process a single provider assignment using a thread pool."""
        close_old_connections()

        if AIProcessingControl.get_control().is_paused:
            logger.info(f"Processing paused for assignment {assignment.id}")
            assignment.status = 'pending'
            assignment.save(update_fields=["status", "updated_at"])
            pause_event.set()
            return

        provider = self._find_provider(assignment.assignment_id)
        if not provider:
            self._mark_assignment_failed(assignment, "Provider inactive or missing")
            return

        assignment_items = list(
            BatchAssignmentItem.objects.filter(assignment=assignment)
            .select_related("batch_item__product")
            .exclude(status='ai_done')  # Skip already completed
            .order_by("id")
        )

        max_threads = self._get_provider_config_int(provider, "max_threads", 50, min_value=1)
        request_delay_seconds = self._get_provider_request_delay(provider, max_threads)

        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = []
            for item in assignment_items:
                if AIProcessingControl.get_control().is_paused:
                    logger.info(f"Paused mid-batch {assignment.batch_id}")
                    assignment.status = 'pending'
                    assignment.save(update_fields=["status", "updated_at"])
                    pause_event.set()
                    break
                futures.append(
                    executor.submit(self._process_assignment_item, item, provider, request_delay_seconds)
                )

            if pause_event.is_set():
                for future in futures:
                    future.cancel()

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    logger.exception(f"AI provider {provider.name} item worker failed")
                if AIProcessingControl.get_control().is_paused:
                    assignment.status = 'pending'
                    assignment.save(update_fields=["status", "updated_at"])
                    pause_event.set()
                    break

        self._update_assignment_progress(assignment)

    def _process_assignment_item(
        self,
        assignment_item: BatchAssignmentItem,
        provider: AIProvider,
        request_delay_seconds: float = 0.0,
    ) -> None:
        """Process a single assignment item with retries."""
        close_old_connections()
        product = assignment_item.batch_item.product
        product_info = self._build_product_payload(product)
        attributes = self._get_attributes(product)

        max_retries = self._get_provider_config_int(provider, "max_retries", 3, min_value=1)
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
                if request_delay_seconds > 0:
                    time.sleep(request_delay_seconds)

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
        """Update assignment progress percentage and handle failures."""
        items = BatchAssignmentItem.objects.filter(assignment=assignment)
        total = items.count()
        completed = items.filter(status="ai_done").count()
        failed = items.filter(status="ai_failed").count()
        progress = (completed / total * 100) if total else 0

        assignment.progress = progress
        if failed > 0:
            assignment.status = "failed"
        elif completed == total and total > 0:
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
            "department": product.dept_name or "",
            "subdepartment": product.subdept_name or "",
            "class_name": product.class_name or "",
            "subclass_name": product.subclass_name or "",
            "subclass_id": product.subclass_id if product.subclass else None,
            "image_url": product.primary_image_url,
        }

    def _get_attributes(self, product: BaseProduct) -> List[AttributePayload]:
        """Get applicable attributes with allowed values."""
        attributes: List[AttributePayload] = []

        subclass_maps = list(get_active_subclass_attribute_maps(product.subclass))

        attribute_ids: List[int] = []
        seen_attribute_ids = set()
        for map_obj in subclass_maps:
            attribute_id = map_obj.attribute.id
            if attribute_id in seen_attribute_ids:
                continue
            seen_attribute_ids.add(attribute_id)
            attribute_ids.append(attribute_id)

        option_map = self._build_option_map(attribute_ids)

        added_attribute_ids = set()
        for map_obj in subclass_maps:
            attr = map_obj.attribute
            if attr.id in added_attribute_ids:
                continue
            added_attribute_ids.add(attr.id)
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

    def _get_provider_config_int(
        self,
        provider: AIProvider,
        key: str,
        default: int,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
    ) -> int:
        value = provider.config.get(key) if provider.config else None
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        if min_value is not None:
            value = max(value, min_value)
        if max_value is not None:
            value = min(value, max_value)
        return value

    def _get_provider_request_delay(self, provider: AIProvider, max_threads: int) -> float:
        config = provider.config or {}
        delay_from_rps = 0.0
        try:
            requests_per_second = float(config.get("requests_per_second") or 0)
        except (TypeError, ValueError):
            requests_per_second = 0.0
        if requests_per_second > 0:
            delay_from_rps = 1.0 / requests_per_second

        delay_from_cooldown = 0.0
        try:
            cooldown_ms = float(config.get("cooldown_ms") or 0)
        except (TypeError, ValueError):
            cooldown_ms = 0.0
        if cooldown_ms > 0 and max_threads > 0:
            delay_from_cooldown = (cooldown_ms / 1000.0) / max_threads

        return max(delay_from_rps, delay_from_cooldown)

    def _mark_assignment_failed(self, assignment: BatchAssignment, message: str) -> None:
        """Mark assignment as failed."""
        assignment.status = "failed"
        assignment.progress = 0
        assignment.save(update_fields=["status", "progress", "updated_at"])
        items = BatchAssignmentItem.objects.filter(assignment=assignment).exclude(status="ai_done")
        items.update(status="ai_failed", updated_at=timezone.now())
        product_ids = items.values_list("batch_item__product_id", flat=True).distinct()
        BaseProduct.objects.filter(
            id__in=product_ids,
            processing_status__in=["pending", "pending_ai", "ai_in_progress"],
        ).update(processing_status="ai_failed", updated_at=timezone.now())
        logger.warning(f"Assignment {assignment.id} failed: {message}")
