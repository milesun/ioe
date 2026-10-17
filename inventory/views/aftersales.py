import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import OperationalError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from inventory.forms.aftersales_forms import AfterSaleForm, ReplacementFormSet
from inventory.models import AfterSale, Sale
from inventory.services.aftersales_service import quote_aftersale, complete_aftersale

QUOTE_SALT = 'inventory.aftersale.quote.v1'


@login_required
@permission_required('inventory.add_aftersale', raise_exception=True)
def aftersale_create(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related('member__level'), pk=sale_id)
    if sale.status != 'COMPLETED':
        messages.error(request, '只有已完成的销售单可以办理售后。')
        return redirect('sale_detail', sale_id=sale.pk)
    preview = token = None
    form = AfterSaleForm(request.POST or None, sale=sale)
    formset = ReplacementFormSet(request.POST or None, prefix='replacement')
    if request.method == 'POST' and request.POST.get('confirmation'):
        if request.POST.get('acknowledged') != 'on':
            messages.error(request, '请确认已完成线下收退款，并确认商品可以再次销售。')
            return redirect('aftersale_create', sale_id=sale.pk)
        try:
            signed = signing.loads(request.POST['confirmation'], salt=QUOTE_SALT, max_age=900)
            if signed['operator_id'] != request.user.pk or signed['sale_id'] != sale.pk:
                messages.error(request, '确认信息与当前账号或销售单不一致，请重新预览。')
                return redirect('aftersale_create', sale_id=sale.pk)
            result = complete_aftersale(
                sale_id=sale.pk, payload=signed['payload'], request_key=signed['request_key'],
                expected_quote=signed['quote'], operator=request.user,
            )
            messages.success(request, f'{result.number} 办理成功。')
            return redirect('aftersale_detail', pk=result.pk)
        except (signing.BadSignature, KeyError, ValueError):
            messages.error(request, '确认信息已过期或无效，请重新预览。')
            return redirect('aftersale_create', sale_id=sale.pk)
        except (ValidationError, OperationalError) as error:
            message = '数据库繁忙，请稍后重新预览。' if isinstance(error, OperationalError) else '；'.join(error.messages)
            messages.error(request, message)
            if isinstance(error, OperationalError):
                return redirect('aftersale_create', sale_id=sale.pk)
            # 失败事务已回滚。保留店员选择，并用最新数据重新报价。
            payload = signed['payload']
            data = {key: payload[key] for key in ('kind', 'reason', 'offline_method', 'topup_method')}
            data.update({f'return_{key}': value for key, value in payload['returns'].items()})
            data.update({'replacement-TOTAL_FORMS': len(payload['replacements']), 'replacement-INITIAL_FORMS': 0})
            for index, row in enumerate(payload['replacements']):
                data[f'replacement-{index}-product'] = row['product_id']
                data[f'replacement-{index}-quantity'] = row['quantity']
            sale = Sale.objects.select_related('member__level').get(pk=sale.pk)
            form = AfterSaleForm(data, sale=sale)
            formset = ReplacementFormSet(data, prefix='replacement')
    if request.method == 'POST' and form.is_valid() and formset.is_valid():
        replacements = [
            {'product_id': row['product'].pk, 'quantity': row['quantity']}
            for row in formset.cleaned_data if row
        ]
        payload = form.payload(replacements)
        try:
            preview = quote_aftersale(sale, payload)
            signed = json.loads(json.dumps({
                'operator_id': request.user.pk, 'sale_id': sale.pk, 'payload': payload,
                'request_key': str(uuid.uuid4()), 'quote': preview,
            }, default=str))
            token = signing.dumps(signed, salt=QUOTE_SALT, compress=True)
        except ValidationError as error:
            form.add_error(None, error)
    return render(request, 'inventory/aftersales/form.html', {
        'sale': sale, 'form': form, 'formset': formset, 'preview': preview, 'confirmation': token,
    })


@login_required
@permission_required('inventory.view_aftersale', raise_exception=True)
def aftersale_list(request):
    records = AfterSale.objects.select_related('sale__member', 'operator', 'replacement_sale')
    query = request.GET.get('q', '').strip()
    if query:
        condition = Q(sale__member__name__icontains=query) | Q(sale__member__phone__icontains=query) | Q(items__product_name__icontains=query) | Q(items__barcode__icontains=query)
        numeric = query.upper().removeprefix('AS').lstrip('0') or '0'
        if numeric.isdigit() and len(numeric) <= 10:
            condition |= Q(pk=int(numeric)) | Q(sale_id=int(numeric)) | Q(replacement_sale_id=int(numeric))
        records = records.filter(condition).distinct()
    kind = request.GET.get('kind', '')
    if kind in dict(AfterSale.TYPES):
        records = records.filter(kind=kind)
    operator = request.GET.get('operator', '').strip()
    if operator:
        records = records.filter(operator__username__icontains=operator)
    from datetime import date
    for key, lookup in [('date_from', 'created_at__date__gte'), ('date_to', 'created_at__date__lte')]:
        value = request.GET.get(key, '')
        if value:
            try:
                records = records.filter(**{lookup: date.fromisoformat(value)})
            except ValueError:
                messages.error(request, '日期格式无效，请使用日期选择器。')
    params = request.GET.copy()
    params.pop('page', None)
    return render(request, 'inventory/aftersales/list.html', {
        'page_obj': Paginator(records, 20).get_page(request.GET.get('page')),
        'filters': request.GET, 'pagination_query': params.urlencode(),
    })


@login_required
@permission_required('inventory.view_aftersale', raise_exception=True)
def aftersale_detail(request, pk):
    record = get_object_or_404(AfterSale.objects.select_related('sale__member', 'replacement_sale', 'operator'), pk=pk)
    return render(request, 'inventory/aftersales/detail.html', {
        'record': record, 'items': record.items.all(), 'transactions': record.inventory_transactions.select_related('product'),
    })
