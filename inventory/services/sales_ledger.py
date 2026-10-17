"""销售为正、售后为负；按各自发生时间统计，换货抵扣不重复记收入。"""
from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from inventory.models import Sale, AfterSale, AfterSaleItem
from .aftersales_service import _allocate, cents

ZERO = Decimal('0.00')


def period_queryset(queryset, field, start=None, end=None):
    for value, lookup, boundary in [(start, 'gte', time.min), (end, 'lte', time.max)]:
        if value is None:
            continue
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime.combine(value, boundary)
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        queryset = queryset.filter(**{f'{field}__{lookup}': value})
    return queryset


def order_query(start=None, end=None, member_id=None):
    query = period_queryset(Sale.objects.filter(status='COMPLETED'), 'created_at', start, end)
    return query.filter(member_id=member_id) if member_id is not None else query


def return_query(start=None, end=None, member_id=None):
    query = period_queryset(AfterSale.objects.all(), 'created_at', start, end)
    return query.filter(sale__member_id=member_id) if member_id is not None else query


def sales_summary(start=None, end=None, member_id=None):
    orders = order_query(start, end, member_id)
    returns = return_query(start, end, member_id)
    amounts = orders.aggregate(gross_sales=Sum('final_amount'), original_total=Sum('total_amount'), discounts=Sum('discount_amount'))
    refund = returns.aggregate(total=Sum('refund_amount'))['total'] or ZERO
    gross = amounts['gross_sales'] or ZERO
    return {
        'gross_sales': gross, 'refund_amount': refund, 'total_sales': gross - refund,
        'order_count': orders.count(), 'return_count': returns.count(),
        'original_total': amounts['original_total'] or ZERO, 'discount_amount': amounts['discounts'] or ZERO,
    }


def sales_events(start=None, end=None, member_id=None):
    events = []
    for sale in order_query(start, end, member_id).prefetch_related('items__product__category'):
        items = sorted(sale.items.all(), key=lambda item: item.pk)
        if not items:
            continue
        weights = [max(cents(item.subtotal), 0) for item in items]
        amounts = _allocate(cents(sale.final_amount), weights) if sum(weights) else [0] * len(items)
        for item, amount in zip(items, amounts):
            events.append({
                'created_at': sale.created_at, 'sale_id': sale.pk, 'member_id': sale.member_id,
                'product': item.product, 'quantity': item.quantity,
                'amount': Decimal(amount) / 100, 'cost': item.product.cost * item.quantity,
                'refund': ZERO, 'is_return': False,
            })
    returns = return_query(start, end, member_id)
    for item in AfterSaleItem.objects.filter(aftersale__in=returns).select_related('aftersale__sale', 'sale_item__product__category'):
        product = item.sale_item.product
        events.append({
            'created_at': item.aftersale.created_at, 'sale_id': item.aftersale.sale_id,
            'member_id': item.aftersale.sale.member_id, 'product': product,
            'quantity': -item.quantity, 'amount': -item.refund_amount, 'cost': -product.cost * item.quantity,
            'refund': item.refund_amount, 'is_return': True,
        })
    return events


def product_totals(start=None, end=None):
    totals = {}
    for event in sales_events(start, end):
        product = event['product']
        row = totals.setdefault(product.pk, {
            'product__id': product.pk, 'product__name': product.name,
            'product__barcode': product.barcode, 'product__category__name': product.category.name if product.category_id else '',
            'total_quantity': 0, 'total_sales': ZERO, 'total_cost': ZERO, 'refund_amount': ZERO,
        })
        row['total_quantity'] += event['quantity']
        row['total_sales'] += event['amount']
        row['total_cost'] += event['cost']
        row['refund_amount'] += event['refund']
    for row in totals.values():
        row['profit'] = row['total_sales'] - row['total_cost']
        row['profit_margin'] = row['profit'] * 100 / row['total_cost'] if row['total_cost'] else ZERO
    return sorted(totals.values(), key=lambda row: (-row['total_quantity'], row['product__id']))


def sales_periods(start=None, end=None, period='day'):
    buckets = {}
    def bucket(moment):
        local = timezone.localtime(moment)
        if period == 'month':
            local = local.replace(day=1)
        elif period == 'week':
            from datetime import timedelta
            local -= timedelta(days=local.weekday())
        key = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return buckets.setdefault(key, {
            'period': key, 'total_sales': ZERO, 'gross_sales': ZERO, 'refund_amount': ZERO,
            'total_cost': ZERO, 'order_count': 0, 'return_count': 0, 'item_count': 0,
        })
    for sale in order_query(start, end):
        row = bucket(sale.created_at)
        row['order_count'] += 1
        row['gross_sales'] += sale.final_amount
        row['total_sales'] += sale.final_amount
    for record in return_query(start, end):
        row = bucket(record.created_at)
        row['return_count'] += 1
        row['refund_amount'] += record.refund_amount
        row['total_sales'] -= record.refund_amount
    for event in sales_events(start, end):
        row = bucket(event['created_at'])
        row['total_cost'] += event['cost']
        row['item_count'] += -1 if event['is_return'] else 1
    for row in buckets.values():
        row['profit'] = row['total_sales'] - row['total_cost']
        row['profit_margin'] = row['profit'] * 100 / row['total_cost'] if row['total_cost'] else ZERO
    return [buckets[key] for key in sorted(buckets)]


def profit_summary(start=None, end=None):
    summary = sales_summary(start, end)
    categories = {}
    events = sales_events(start, end)
    for event in events:
        product = event['product']
        name = product.category.name if product.category_id else ''
        row = categories.setdefault(name, {'product__category__name': name, 'sales': ZERO, 'cost': ZERO, 'quantity': 0})
        row['sales'] += event['amount']
        row['cost'] += event['cost']
        row['quantity'] += event['quantity']
    for row in categories.values():
        row['profit'] = row['sales'] - row['cost']
        row['profit_margin'] = row['profit'] * 100 / row['cost'] if row['cost'] else ZERO
    total_cost = sum((event['cost'] for event in events), ZERO)
    gross_profit = summary['total_sales'] - total_cost
    return dict(summary, start_date=start, end_date=end, total_cost=total_cost, gross_profit=gross_profit,
                profit_margin=gross_profit * 100 / total_cost if total_cost else ZERO,
                item_count=sum(-1 if event['is_return'] else 1 for event in events),
                category_data=sorted(categories.values(), key=lambda row: -row['profit']))
