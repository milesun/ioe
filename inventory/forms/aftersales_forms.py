from django import forms
from django.forms import formset_factory

from inventory.models import Product, AfterSale
from inventory.services.aftersales_service import return_rows


OFFLINE_CHOICES = [('cash', '现金'), ('wechat', '微信'), ('alipay', '支付宝'), ('card', '银行卡'), ('other', '其他')]


def product_label(product):
    color = product.get_color_display() or '无颜色'
    size = product.get_size_display() or '无尺码'
    return f'{product.name}｜{color}｜{size}'


class AfterSaleForm(forms.Form):
    kind = forms.ChoiceField(label='售后类型', choices=AfterSale.TYPES)
    reason = forms.CharField(label='原因', max_length=2000, widget=forms.Textarea(attrs={'rows': 2}))
    offline_method = forms.ChoiceField(label='线下实际收退款渠道', choices=OFFLINE_CHOICES)
    topup_method = forms.ChoiceField(label='差价补款方式', choices=[('offline', '线下支付'), ('balance', '会员余额')])

    def __init__(self, *args, sale, **kwargs):
        super().__init__(*args, **kwargs)
        self.sale = sale
        self.return_items = return_rows(sale)
        for item in self.return_items:
            self.fields[f'return_{item.pk}'] = forms.IntegerField(
                label=f'{product_label(item.product)}（已退 {item.returned_quantity}，可退 {item.returnable_quantity}）',
                required=False, min_value=0, max_value=max(item.returnable_quantity, 0), initial=0,
            )
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'

    @property
    def quantity_fields(self):
        return [self[f'return_{item.pk}'] for item in self.return_items]

    def payload(self, replacements):
        return {
            'kind': self.cleaned_data['kind'], 'reason': self.cleaned_data['reason'],
            'offline_method': self.cleaned_data['offline_method'], 'topup_method': self.cleaned_data['topup_method'],
            'returns': {str(item.pk): self.cleaned_data[f'return_{item.pk}'] for item in self.return_items if self.cleaned_data[f'return_{item.pk}']},
            'replacements': replacements,
        }


def product_stock(product):
    inventory = getattr(product, 'inventory', None)
    return inventory.quantity if inventory else 0


class ReplacementProductSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        product = getattr(value, 'instance', None)
        if product is not None and product_stock(product) <= 0:
            option['attrs']['disabled'] = True
        return option


class ReplacementProductChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, product):
        label = product_label(product)
        return f'{label}｜无库存' if product_stock(product) <= 0 else label

    def clean(self, value):
        product = super().clean(value)
        if product is not None and product_stock(product) <= 0:
            raise forms.ValidationError('该商品无库存，请选择其他商品。', code='out_of_stock')
        return product


class ReplacementForm(forms.Form):
    product = ReplacementProductChoiceField(label='换出商品', empty_label='请选择换出商品', queryset=Product.objects.filter(is_active=True).select_related('inventory').order_by('name', 'color', 'size', 'barcode'), widget=ReplacementProductSelect(attrs={'class': 'form-select'}))
    quantity = forms.IntegerField(label='数量', min_value=1, max_value=1000000, widget=forms.NumberInput(attrs={'class': 'form-control'}))


ReplacementFormSet = formset_factory(ReplacementForm, extra=1, max_num=50, validate_max=True)
