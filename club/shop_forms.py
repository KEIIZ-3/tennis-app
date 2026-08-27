from django import forms
from django.forms import formset_factory
from decimal import Decimal

from .models import ShopInquiry, User
from .shop_service import discount_rate_from_prices, sale_price_from_discount


def customer_queryset():
    purchasable_roles = set(User.LESSON_PARTICIPANT_ROLE_VALUES) | set(User.COACH_ROLE_VALUES)
    return User.objects.filter(
        is_active=True, is_staff=False, is_superuser=False, role__in=purchasable_roles,
    ).order_by("full_name", "username")


class ShopInquiryForm(forms.ModelForm):
    class Meta:
        model = ShopInquiry
        fields = ["wanted_item"]
        labels = {"wanted_item": "欲しいもの"}
        widgets = {"wanted_item": forms.Textarea(attrs={"rows": 3, "placeholder": "例：HEAD SPEED MP 2026"})}


class ShopQuoteForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=User.objects.none(), label="顧客名")
    inquiry = forms.ModelChoiceField(queryset=ShopInquiry.objects.none(), required=False, widget=forms.HiddenInput())
    note = forms.CharField(required=False, label="備考", widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = customer_queryset()
        self.fields["inquiry"].queryset = ShopInquiry.objects.exclude(status=ShopInquiry.STATUS_CANCELED)


class ShopQuoteItemForm(forms.Form):
    description = forms.CharField(label="商品名・内容", max_length=255)
    quantity = forms.IntegerField(label="数量", min_value=1, initial=1)
    list_price = forms.IntegerField(label="定価", min_value=0)
    sale_price = forms.IntegerField(label="販売価格", min_value=0)
    discount_rate = forms.DecimalField(label="値引率 (%)", required=False, min_value=Decimal("0"), max_value=Decimal("100"), decimal_places=1, max_digits=4)
    cost_price = forms.IntegerField(label="原価", required=False, min_value=0)
    pricing_source = forms.ChoiceField(required=False, choices=(("sale", "sale"), ("discount", "discount")), widget=forms.HiddenInput(), initial="sale")

    def clean(self):
        data = super().clean()
        list_price, sale_price = data.get("list_price"), data.get("sale_price")
        if data.get("pricing_source") == "discount" and list_price is not None and data.get("discount_rate") is not None:
            sale_price = data["sale_price"] = sale_price_from_discount(list_price, data["discount_rate"])
        elif list_price and sale_price is not None:
            data["discount_rate"] = discount_rate_from_prices(list_price, sale_price)
        elif sale_price not in (None, 0):
            self.add_error("sale_price", "定価が0円の場合、販売価格は0円にしてください。")
        if list_price is not None and sale_price is not None and sale_price > list_price:
            self.add_error("sale_price", "販売価格は定価以下にしてください。")
        return data


ShopQuoteItemFormSet = formset_factory(
    ShopQuoteItemForm, extra=3, min_num=1, validate_min=True, can_delete=True,
)


class DirectPurchaseForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=User.objects.none(), label="顧客")
    description = forms.CharField(label="商品内容", widget=forms.Textarea(attrs={"rows": 2}))
    quantity = forms.IntegerField(label="数量", min_value=1, initial=1)
    amount = forms.IntegerField(label="販売価格（合計）", min_value=0)
    note = forms.CharField(label="備考", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = customer_queryset()
