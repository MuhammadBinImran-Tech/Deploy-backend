from typing import Iterable, List

from .models import AttributeSubclassMap


def get_active_subclass_attribute_maps(subclass):
    if not subclass:
        return AttributeSubclassMap.objects.none()
    return AttributeSubclassMap.objects.filter(
        subclass=subclass,
        attribute__is_active=True,
    ).select_related("attribute")


def get_active_subclass_attribute_ids(subclass) -> List[int]:
    if not subclass:
        return []
    return list(
        AttributeSubclassMap.objects.filter(
            subclass=subclass,
            attribute__is_active=True,
        ).values_list("attribute_id", flat=True)
    )


def filter_annotations_to_subclass(queryset, subclass):
    attribute_ids = get_active_subclass_attribute_ids(subclass)
    if not attribute_ids:
        return queryset.none()
    return queryset.filter(attribute_id__in=attribute_ids)
