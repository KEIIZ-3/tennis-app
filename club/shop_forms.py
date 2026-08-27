from django import forms
from django.forms import formset_factory

from .models import ShopInquiry, User


def customer_queryset():
    return User.objects.filter(is_active=True, role__in=User.LESSON_PARTICIPANT_ROLE_VALUES).order_by("full_name", "username")


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

    def clean(self):
        data = super().clean()
        if data.get("list_price") is not None and data.get("sale_price") is not None and data["list_price"] > 0 and data["sale_price"] > data["list_price"]:
            self.add_error("sale_price", "販売価格は定価以下にしてください。")
        return data


ShopQuoteItemFormSet = formset_factory(ShopQuoteItemForm, extra=3, min_num=1, validate_min=True)


class DirectPurchaseForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=User.objects.none(), label="顧客")
    description = forms.CharField(label="商品内容", widget=forms.Textarea(attrs={"rows": 2}))
    quantity = forms.IntegerField(label="数量", min_value=1, initial=1)
    amount = forms.IntegerField(label="販売価格（合計）", min_value=0)
    note = forms.CharField(label="備考", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = customer_queryset()
