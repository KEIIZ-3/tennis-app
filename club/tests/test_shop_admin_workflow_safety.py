import inspect
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase

from club.admin import ShopEstimateRequestAdmin, ShopProductMasterAdmin, ShopProductMasterImportForm
from club.models import ShopEstimateRequest, ShopProductMaster, User
from club.shop_admin_service import (
    import_shop_product_master_rows,
    update_shop_estimate_request_status,
)


class ShopEstimateRequestAdminSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="shop-customer")
        self.admin_user = User.objects.create_superuser(username="shop-admin")
        self.estimate = ShopEstimateRequest.objects.create(user=self.user)
        self.model_admin = ShopEstimateRequestAdmin(ShopEstimateRequest, AdminSite())
        self.model_admin.message_user = Mock()
        self.request = RequestFactory().post("/admin/")
        self.request.user = self.admin_user

    def test_admin_detail_status_change_uses_canonical_service(self):
        self.estimate.handling_status = ShopEstimateRequest.HANDLING_STATUS_CHECKED
        with patch("club.admin.update_shop_estimate_request_status") as service:
            self.model_admin.save_model(self.request, self.estimate, Mock(), change=True)
        service.assert_called_once_with(
            estimate_request_id=self.estimate.pk,
            handling_status=ShopEstimateRequest.HANDLING_STATUS_CHECKED,
            actor=self.admin_user,
        )

    def test_each_bulk_status_action_uses_canonical_service(self):
        actions = (
            ("mark_as_checked", ShopEstimateRequest.HANDLING_STATUS_CHECKED),
            ("mark_as_ordered", ShopEstimateRequest.HANDLING_STATUS_ORDERED),
            ("mark_as_completed", ShopEstimateRequest.HANDLING_STATUS_COMPLETED),
            ("mark_as_canceled", ShopEstimateRequest.HANDLING_STATUS_CANCELED),
        )
        for action_name, status in actions:
            with self.subTest(action=action_name):
                with patch(
                    "club.admin.update_shop_estimate_request_status",
                    wraps=update_shop_estimate_request_status,
                ) as service:
                    getattr(self.model_admin, action_name)(
                        self.request, ShopEstimateRequest.objects.filter(pk=self.estimate.pk)
                    )
                service.assert_called_once_with(
                    estimate_request_id=self.estimate.pk,
                    handling_status=status,
                    actor=self.admin_user,
                )

    def test_noop_status_update_is_safe(self):
        result, changed = update_shop_estimate_request_status(
            estimate_request_id=self.estimate.pk,
            handling_status=ShopEstimateRequest.HANDLING_STATUS_NEW,
            actor=self.admin_user,
        )
        self.assertFalse(changed)
        self.assertEqual(result.pk, self.estimate.pk)

    def test_bulk_failure_rolls_back_every_status_change(self):
        second = ShopEstimateRequest.objects.create(user=self.user)
        real_service = update_shop_estimate_request_status

        def fail_second(**kwargs):
            if kwargs["estimate_request_id"] == second.pk:
                raise ValidationError("simulated failure")
            return real_service(**kwargs)

        with patch("club.admin.update_shop_estimate_request_status", side_effect=fail_second):
            self.model_admin.mark_as_checked(
                self.request, ShopEstimateRequest.objects.order_by("pk")
            )

        statuses = set(ShopEstimateRequest.objects.values_list("handling_status", flat=True))
        self.assertEqual(statuses, {ShopEstimateRequest.HANDLING_STATUS_NEW})

    def test_bulk_actions_do_not_use_queryset_update(self):
        for action_name in (
            "mark_as_checked",
            "mark_as_ordered",
            "mark_as_completed",
            "mark_as_canceled",
        ):
            self.assertNotIn(".update(", inspect.getsource(getattr(self.model_admin, action_name)))


class ShopProductMasterImportSafetyTests(TestCase):
    def setUp(self):
        self.model_admin = ShopProductMasterAdmin(ShopProductMaster, AdminSite())
        self.existing = ShopProductMaster.objects.create(
            brand=ShopProductMaster.BRAND_YONEX,
            category=ShopProductMaster.CATEGORY_RACKET,
            product_type=ShopProductMaster.PRODUCT_TYPE_MAIN,
            product_name="既存商品",
            product_code="OLD",
            official_price=10000,
        )

    def _row(self, *, name="新商品", code="NEW", price=12000):
        return {
            "product_type": ShopProductMaster.PRODUCT_TYPE_MAIN,
            "category": ShopProductMaster.CATEGORY_RACKET,
            "brand": ShopProductMaster.BRAND_YONEX,
            "product_name": name,
            "display_name": "",
            "product_code": code,
            "official_price": price,
            "image_url": "",
            "product_url": "",
            "description": "",
            "spec_weight_unstrung": "",
            "spec_string_pattern": "",
            "spec_head_size": "",
            "spec_balance": "",
            "spec_length": "",
            "spec_beam": "",
            "spec_gauge": "",
            "spec_set_length": "",
            "sort_order": 0,
            "is_active": True,
        }

    def _csv(self, body):
        return SimpleUploadedFile("products.csv", body.encode("utf-8"), content_type="text/csv")

    def test_update_csv_keeps_existing_partial_success_contract(self):
        upload = self._csv(
            "product_name,product_code,official_price\n正常商品,OK,15000\n不正商品,BAD,-1\n"
        )
        result = self.model_admin._import_uploaded_products(
            upload_file=upload,
            import_mode=ShopProductMasterImportForm.IMPORT_MODE_UPDATE,
            default_is_active=True,
        )
        self.assertEqual((result["created"], result["skipped"]), (1, 1))
        self.assertTrue(ShopProductMaster.objects.filter(product_code="OK").exists())
        self.assertTrue(ShopProductMaster.objects.filter(pk=self.existing.pk).exists())

    def test_valid_replace_completely_replaces_existing_products(self):
        result = import_shop_product_master_rows(normalized_rows=[self._row()], replace=True)
        self.assertEqual(result, {"created": 1, "updated": 0, "skipped": 0, "errors": []})
        self.assertEqual(list(ShopProductMaster.objects.values_list("product_code", flat=True)), ["NEW"])

    def test_invalid_replace_preserves_all_existing_products(self):
        with self.assertRaises(ValidationError):
            import_shop_product_master_rows(
                normalized_rows=[self._row(), self._row(name="不正", code="BAD", price=-1)],
                replace=True,
            )
        self.assertEqual(list(ShopProductMaster.objects.values_list("product_code", flat=True)), ["OLD"])

    def test_empty_and_header_only_replace_preserve_existing_products(self):
        for body in ("", "product_name,product_code,official_price\n"):
            with self.subTest(body=body):
                with self.assertRaises(ValidationError):
                    self.model_admin._import_uploaded_products(
                        upload_file=self._csv(body),
                        import_mode=ShopProductMasterImportForm.IMPORT_MODE_REPLACE,
                        default_is_active=True,
                    )
                self.assertTrue(ShopProductMaster.objects.filter(pk=self.existing.pk).exists())

    def test_duplicate_replace_preserves_existing_products(self):
        with self.assertRaises(ValidationError) as raised:
            import_shop_product_master_rows(
                normalized_rows=[self._row(name="A"), self._row(name="B")], replace=True
            )
        self.assertIn("重複", str(raised.exception))
        self.assertTrue(ShopProductMaster.objects.filter(pk=self.existing.pk).exists())

    def test_save_failure_rolls_back_replace_deletion(self):
        original_save = ShopProductMaster.save

        def fail_new_product(instance, *args, **kwargs):
            if instance.product_code == "NEW":
                raise RuntimeError("simulated save failure")
            return original_save(instance, *args, **kwargs)

        with patch.object(ShopProductMaster, "save", fail_new_product):
            with self.assertRaisesRegex(RuntimeError, "simulated save failure"):
                import_shop_product_master_rows(normalized_rows=[self._row()], replace=True)
        self.assertEqual(list(ShopProductMaster.objects.values_list("product_code", flat=True)), ["OLD"])
