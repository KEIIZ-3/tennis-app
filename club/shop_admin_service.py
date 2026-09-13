from django.core.exceptions import ValidationError
from django.db import transaction

from .models import ShopEstimateRequest, ShopProductMaster


PRODUCT_IMPORT_FIELDS = (
    "product_type",
    "category",
    "brand",
    "product_name",
    "display_name",
    "product_code",
    "official_price",
    "image_url",
    "product_url",
    "description",
    "spec_weight_unstrung",
    "spec_string_pattern",
    "spec_head_size",
    "spec_balance",
    "spec_length",
    "spec_beam",
    "spec_gauge",
    "spec_set_length",
    "sort_order",
    "is_active",
)


def update_shop_estimate_request_status(*, estimate_request_id, handling_status, actor=None):
    """Canonical status update entry point for legacy shop estimate requests."""
    del actor  # Reserved for a future audit record without changing this contract.
    with transaction.atomic():
        estimate_request = ShopEstimateRequest.objects.select_for_update().get(
            pk=estimate_request_id
        )
        if estimate_request.handling_status == handling_status:
            return estimate_request, False

        estimate_request.handling_status = handling_status
        estimate_request.full_clean()
        estimate_request.save(update_fields=("handling_status", "updated_at"))
        return estimate_request, True


def import_shop_product_master_rows(*, normalized_rows, replace):
    """Import normalized rows, making replacement strictly all-or-nothing."""
    if replace:
        candidates, errors = _validate_replacement_rows(normalized_rows)
        if errors:
            raise ValidationError(errors)
        with transaction.atomic():
            ShopProductMaster.objects.all().delete()
            for candidate in candidates:
                candidate.save()
        return {"created": len(candidates), "updated": 0, "skipped": 0, "errors": []}

    created_count = 0
    updated_count = 0
    errors = []
    for index, row in enumerate(normalized_rows, start=2):
        try:
            with transaction.atomic():
                instance = _find_existing_product(row) or ShopProductMaster()
                _apply_product_row(instance, row)
                instance.full_clean()
                is_update = bool(instance.pk)
                instance.save()
            if is_update:
                updated_count += 1
            else:
                created_count += 1
        except Exception as exc:
            messages = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
            errors.append(f"{index}行目をスキップしました: {' / '.join(messages)}")

    return {
        "created": created_count,
        "updated": updated_count,
        "skipped": len(errors),
        "errors": errors,
    }


def _validate_replacement_rows(rows):
    if not rows:
        return [], ["置換対象の商品が0件のため、既存データを保持しました。"]

    candidates = []
    errors = []
    identities = {}
    for index, row in enumerate(rows, start=2):
        identity = _product_identity(row)
        if identity in identities:
            errors.append(
                f"{index}行目は{identities[identity]}行目と重複しています。"
            )
            continue
        identities[identity] = index

        candidate = ShopProductMaster()
        _apply_product_row(candidate, row)
        try:
            # Replacement removes the current set first, so current-DB uniqueness
            # must not make an otherwise valid replacement fail validation.
            candidate.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            errors.append(f"{index}行目が不正です: {' / '.join(exc.messages)}")
        else:
            candidates.append(candidate)
    return candidates, errors


def _product_identity(row):
    product_code = (row.get("product_code") or "").strip()
    if product_code:
        return ("product_code", product_code)
    return (
        "product",
        row["brand"],
        row["category"],
        row["product_type"],
        row["product_name"],
    )


def _find_existing_product(row):
    product_code = (row.get("product_code") or "").strip()
    if product_code:
        return ShopProductMaster.objects.filter(product_code=product_code).first()
    return ShopProductMaster.objects.filter(
        brand=row["brand"],
        category=row["category"],
        product_type=row["product_type"],
        product_name=row["product_name"],
    ).first()


def _apply_product_row(instance, row):
    for field_name in PRODUCT_IMPORT_FIELDS:
        setattr(instance, field_name, row[field_name])
