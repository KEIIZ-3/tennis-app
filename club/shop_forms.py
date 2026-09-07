from django import forms
from django.forms import formset_factory
from decimal import Decimal

from .models import ShopInquiry, User
from .shop_service import discount_rate_from_prices, sale_price_from_discount


def customer_queryset():
    return User.objects.filter(is_active=True, role__in=User.LESSON_PARTICIPANT_ROLE_VALUES).order_by("full_name", "username")


class CustomerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, customer):
        return customer.display_name()


class ShopInquiryForm(forms.ModelForm):
    class Meta:
        model = ShopInquiry
        fields = ["wanted_item"]
        labels = {"wanted_item": "欲しいもの"}
        widgets = {"wanted_item": forms.Textarea(attrs={"rows": 3, "placeholder": "例：HEAD SPEED MP 2026"})}


class ShopQuoteForm(forms.Form):
    PURCHASER_MEMBER = "member"
    PURCHASER_GUEST = "guest"
    purchaser_type = forms.ChoiceField(
        choices=((PURCHASER_MEMBER, "会員"), (PURCHASER_GUEST, "ゲスト")),
        label="購入者種別", widget=forms.RadioSelect,
    )
    customer = CustomerChoiceField(queryset=User.objects.none(), required=False, label="会員")
    guest_name = forms.CharField(required=False, max_length=120, label="お名前")
    inquiry = forms.ModelChoiceField(queryset=ShopInquiry.objects.none(), required=False, widget=forms.HiddenInput())
    note = forms.CharField(required=False, label="備考", widget=forms.Textarea(attrs={"rows": 3}))
    accounting_sale_amount = forms.IntegerField(required=False, min_value=1, label="売上額")
    accounting_purchase_cost = forms.IntegerField(required=False, min_value=0, label="仕入額")
    procurement_coach = forms.ChoiceField(choices=(), required=False, label="仕入コーチ")

    def __init__(self, *args, can_edit_accounting=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = customer_queryset()
        self.fields["inquiry"].queryset = ShopInquiry.objects.exclude(status=ShopInquiry.STATUS_CANCELED)
        from .settlement_balance_policy import main_coaches
        self.fields["procurement_coach"].choices = [('', '---------')] + [
            (str(coach.pk), coach.display_name()) for coach in main_coaches()
        ]
        if not can_edit_accounting:
            for name in ("accounting_sale_amount", "accounting_purchase_cost", "procurement_coach"):
                self.fields.pop(name)

    def clean(self):
        data = super().clean()
        purchaser_type = data.get("purchaser_type")
        customer = data.get("customer")
        guest_name = (data.get("guest_name") or "").strip()
        data["guest_name"] = guest_name
        if purchaser_type == self.PURCHASER_MEMBER:
            if customer is None:
                self.add_error("customer", "会員を選択してください。")
            data["guest_name"] = ""
        elif purchaser_type == self.PURCHASER_GUEST:
            if not guest_name:
                self.add_error("guest_name", "お名前を入力してください。")
            data["customer"] = None
            data["inquiry"] = None
        inquiry = data.get("inquiry")
        if inquiry and customer and inquiry.customer_id != customer.pk:
            self.add_error("customer", "問い合わせを行った会員を選択してください。")
        sale, cost = data.get("accounting_sale_amount"), data.get("accounting_purchase_cost")
        if sale is not None and cost is not None and cost > sale:
            self.add_error("accounting_purchase_cost", "仕入額が売上額を超える販売は登録できません。")
        return data


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
