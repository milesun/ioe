"""退换货的报价和一次性结算。金额在服务端按分计算。"""
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from inventory.models import (
    AfterSale, AfterSaleItem, Sale, SaleItem, Product, Member, Inventory,
    MemberTransaction, OperationLog, update_inventory,
)
from django.contrib.contenttypes.models import ContentType

ZERO = Decimal('0.00')
OFFLINE_METHODS = {'cash', 'wechat', 'alipay', 'card', 'other'}


def money(value):
    return Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def cents(value):
    return int(money(value) * 100)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def _allocate(total, weights):
    """最大余数法，结果之和严格等于 total（单位：分）。"""
    weight_sum = sum(weights)
    if not weight_sum:
        if total:
            raise ValidationError('原订单明细金额异常，请先核对原销售记录。')
        return [0] * len(weights)
    allocated = [total * weight // weight_sum for weight in weights]
    remainder_order = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % weight_sum), i))
    for i in remainder_order[:total - sum(allocated)]:
        allocated[i] += 1
    return allocated


def return_rows(sale):
    returned = {
        row['sale_item_id']: row['quantity']
        for row in AfterSaleItem.objects.filter(sale_item__sale=sale)
        .values('sale_item_id').annotate(quantity=Sum('quantity'))
    }
    rows = list(sale.items.select_related('product').order_by('pk'))
    for item in rows:
        item.returned_quantity = returned.get(item.pk, 0)
        item.returnable_quantity = item.quantity - item.returned_quantity
    return rows


def quote_aftersale(sale, payload):
    if sale.status != 'COMPLETED':
        raise ValidationError('只有已完成的销售单可以办理售后。')
    kind = payload.get('kind')
    if kind not in {'RETURN', 'EXCHANGE'}:
        raise ValidationError('售后类型无效。')
    if payload.get('offline_method') not in OFFLINE_METHODS or payload.get('topup_method') not in {'offline', 'balance'}:
        raise ValidationError('收退款方式无效。')
    reason = payload.get('reason', '').strip()
    if not reason or len(reason) > 2000:
        raise ValidationError('请填写售后原因（最多 2000 字）。')
    requested = payload.get('returns', {})
    if not requested or not isinstance(requested, dict):
        raise ValidationError('请至少选择一件退回商品。')
    rows = return_rows(sale)
    by_id = {str(item.pk): item for item in rows}
    if set(requested) - set(by_id):
        raise ValidationError('退回明细不属于原销售单。')
    if any(type(q) is not int or q <= 0 for q in requested.values()):
        raise ValidationError('退回数量必须为正整数。')
    if sale.final_amount < 0 or sale.balance_paid < 0 or sale.balance_paid > sale.final_amount or sale.points_earned < 0:
        raise ValidationError('原订单支付金额异常，请先核对。')
    if sale.payment_method == 'balance' and sale.balance_paid != sale.final_amount:
        raise ValidationError('原订单缺少准确的余额支付记录，请先核对。')
    if sale.balance_paid and not sale.member_id:
        raise ValidationError('原订单的余额支付会员已缺失，无法自动退款。')
    if any(item.quantity <= 0 or item.subtotal < 0 for item in rows):
        raise ValidationError('原订单明细数量或金额异常，请先核对。')
    budgets = _allocate(cents(sale.final_amount), [cents(item.subtotal) for item in rows])
    lines = []
    for item, budget in zip(rows, budgets):
        quantity = requested.get(str(item.pk), 0)
        if quantity > item.returnable_quantity:
            raise ValidationError(f'{item.product.name} 最多还可退 {item.returnable_quantity} 件。')
        if quantity:
            if not item.product.is_active:
                raise ValidationError(f'{item.product.name}（条码：{item.product.barcode}）已停用，请先到商品管理恢复该商品')
            # 对累计数量取整再相减，分次退货与一次性退货得到相同金额。
            amount = budget * (item.returned_quantity + quantity) // item.quantity - budget * item.returned_quantity // item.quantity
            lines.append({
                'sale_item_id': item.pk, 'product_id': item.product_id, 'quantity': quantity,
                'refund_amount': str(Decimal(amount) / 100), 'product_name': item.product.name,
                'barcode': item.product.barcode, 'specification': item.product.specification or '',
                'original_price': str(item.actual_price),
            })
    refund = sum((money(row['refund_amount']) for row in lines), ZERO)
    previous = sale.aftersales.aggregate(refund=Sum('refund_amount'), balance=Sum('balance_refund'), points=Sum('points_returned'))
    cumulative = (previous['refund'] or ZERO) + refund
    total = cents(sale.final_amount)
    if cumulative > sale.final_amount:
        raise ValidationError('累计退款超过原订单实付金额。')
    balance_refund = Decimal(cents(cumulative) * cents(sale.balance_paid) // total) / 100 - (previous['balance'] or ZERO) if total else ZERO
    points = cents(cumulative) * sale.points_earned // total - (previous['points'] or 0) if total else 0

    replacement_lines = []
    replacements = payload.get('replacements', [])
    if (kind == 'RETURN' and replacements) or (kind == 'EXCHANGE' and not replacements):
        raise ValidationError('退货不能包含换出商品，换货必须选择换出商品。')
    discount = sale.member.level.discount if sale.member_id else Decimal('1')
    product_ids = [row.get('product_id') for row in replacements]
    if len(product_ids) != len(set(product_ids)) or len(product_ids) > 50:
        raise ValidationError('换出商品重复或超过 50 种，请合并数量。')
    products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids, is_active=True)}
    for row in replacements:
        product = products.get(row.get('product_id'))
        quantity = row.get('quantity')
        if product is None or type(quantity) is not int or not 0 < quantity <= 1000000 or product.price < 0:
            raise ValidationError('换出商品已停用、价格异常或数量无效。')
        replacement_lines.append({
            'product_id': product.pk, 'quantity': quantity, 'product_name': product.name,
            'barcode': product.barcode, 'specification': product.specification or '',
            'price': str(product.price), 'subtotal': str(money(product.price * quantity)),
        })
    returned_stock = {}
    for line in lines:
        returned_stock[line['product_id']] = returned_stock.get(line['product_id'], 0) + line['quantity']
    available_stock = dict(Inventory.objects.filter(product_id__in=product_ids).values_list('product_id', 'quantity'))
    for row in replacement_lines:
        if available_stock.get(row['product_id'], 0) <= 0:
            raise ValidationError(f"{row['product_name']} 无库存，请选择其他换出商品。")
        if row['quantity'] > available_stock.get(row['product_id'], 0) + returned_stock.get(row['product_id'], 0):
            raise ValidationError(f"{row['product_name']} 库存不足，请调整换出数量。")
    new_total = sum((money(row['subtotal']) for row in replacement_lines), ZERO)
    new_amount = money(new_total * discount)
    if not ZERO <= new_amount <= new_total <= Decimal('99999999.99'):
        raise ValidationError('换出金额或会员折扣异常，单笔金额最多 99999999.99 元。')
    credit = min(refund, new_amount)
    balance_credit = min(balance_refund, credit)
    extra_balance = max(new_amount - refund, ZERO) if payload['topup_method'] == 'balance' else ZERO
    new_balance_paid = balance_credit + extra_balance
    if extra_balance and not sale.member_id:
        raise ValidationError('散客不能使用会员余额补款。')
    balance_delta = balance_refund - new_balance_paid
    if sale.member_id and sale.member.balance + balance_delta < 0:
        raise ValidationError('会员余额不足以支付补款。')
    # 线下实际变动 + 会员余额实际支出 = 新单金额 - 退回价值。
    offline_delta = new_amount - refund + balance_delta
    return {
        'version': sale.aftersales_version, 'return_lines': lines, 'replacement_lines': replacement_lines,
        'refund_amount': refund, 'balance_refund': money(balance_refund), 'points_returned': points,
        'replacement_total': new_total, 'replacement_amount': new_amount,
        'credit_amount': credit, 'balance_credit': balance_credit, 'extra_balance_paid': extra_balance,
        'new_balance_paid': new_balance_paid, 'balance_delta': balance_delta,
        'offline_delta': offline_delta, 'new_points': int(new_amount),
    }


def complete_aftersale(*, sale_id, payload, request_key, expected_quote, operator):
    if not operator.is_active or not operator.has_perm('inventory.add_aftersale'):
        raise PermissionDenied('您没有办理退换货的权限。')
    payload_hash = digest({'sale_id': sale_id, 'payload': payload})
    with transaction.atomic():
        # 第一个数据库操作必须是写入；SQLite 也能据此串行化同单售后。
        updated = Sale.objects.filter(pk=sale_id, status='COMPLETED', aftersales_version=expected_quote['version']).update(
            aftersales_version=F('aftersales_version') + 1,
        )
        existing = AfterSale.objects.filter(request_key=request_key).first()
        if existing:
            if existing.payload_hash != payload_hash or existing.operator_id != operator.pk:
                raise ValidationError('提交标识与原请求不一致。')
            return existing
        if not updated:
            raise ValidationError('原订单售后记录已变化，请重新预览。')
        sale = Sale.objects.select_for_update().select_related('member__level').get(pk=sale_id)
        if sale.member_id:
            sale.member = Member.objects.select_for_update().select_related('level').get(pk=sale.member_id)
        quote = quote_aftersale(sale, payload)
        quote['version'] -= 1
        if digest(quote) != digest(expected_quote):
            raise ValidationError('价格、会员或可退数量发生变化，请重新预览。')
        from inventory.models.inventory import lock_products_for_inventory
        lock_products_for_inventory([
            line['product_id'] for line in quote['return_lines'] + quote['replacement_lines']
        ])
        new_sale = None
        if payload['kind'] == 'EXCHANGE':
            paid = quote['new_balance_paid']
            method = 'balance' if paid == quote['replacement_amount'] and paid else ('mixed' if paid else payload['offline_method'])
            new_sale = Sale.objects.create(
                member=sale.member, total_amount=0, final_amount=0, operator=operator, status='COMPLETED',
                payment_method=method, balance_paid=paid, exchange_credit_amount=quote['credit_amount'],
                remark=f'原销售单 #{sale.pk} 换货',
            )
        fields = {name: quote[name] for name in (
            'refund_amount', 'balance_refund', 'replacement_amount', 'credit_amount',
            'balance_credit', 'extra_balance_paid', 'offline_delta', 'points_returned',
        )}
        aftersale = AfterSale.objects.create(
            sale=sale, replacement_sale=new_sale, kind=payload['kind'], request_key=request_key,
            payload_hash=payload_hash, reason=payload['reason'].strip(), operator=operator,
            offline_method=payload['offline_method'], replacement_snapshot=quote['replacement_lines'], **fields,
        )
        for line in quote['return_lines']:
            product = Product.objects.get(pk=line['product_id'])
            item_fields = {key: value for key, value in line.items() if key != 'product_id'}
            AfterSaleItem.objects.create(aftersale=aftersale, **item_fields)
            success, _, error = update_inventory(
                product, line['quantity'], 'IN', operator, notes=f'{aftersale.number} 退回商品', aftersale=aftersale,
            )
            if not success:
                raise ValidationError(error)
        if new_sale:
            # SaleItem.save 自带库存扣减，只走这一个路径。
            for line in quote['replacement_lines']:
                SaleItem.objects.create(
                    sale=new_sale, product_id=line['product_id'], quantity=line['quantity'],
                    price=money(line['price']), actual_price=money(line['price']),
                )
            new_sale.refresh_from_db()
            new_sale.discount_amount = quote['replacement_total'] - quote['replacement_amount']
            new_sale.points_earned = quote['new_points']
            new_sale.save()
        if sale.member_id:
            member = sale.member
            if quote['balance_refund'] or quote['points_returned']:
                MemberTransaction.objects.create(
                    member=member, transaction_type='REFUND', balance_change=quote['balance_refund'],
                    points_change=-quote['points_returned'], description=f'{aftersale.number} 退款及积分扣回',
                    created_by=operator, related_object_type='AfterSale', related_object_id=aftersale.pk,
                )
            if new_sale:
                MemberTransaction.objects.create(
                    member=member, transaction_type='PURCHASE', balance_change=-quote['new_balance_paid'],
                    points_change=quote['new_points'], description=f'{aftersale.number} 换货新单 #{new_sale.pk}',
                    created_by=operator, related_object_type='Sale', related_object_id=new_sale.pk,
                )
            all_returned = all(item.returnable_quantity == 0 for item in return_rows(sale))
            Member.objects.filter(pk=member.pk).update(
                balance=F('balance') + quote['balance_delta'],
                points=F('points') - quote['points_returned'] + quote['new_points'],
                total_spend=F('total_spend') - quote['refund_amount'] + quote['replacement_amount'],
                purchase_count=F('purchase_count') - int(all_returned) + int(new_sale is not None),
                updated_at=timezone.now(),
            )
        OperationLog.objects.create(
            operator=operator, operation_type='SALE', details=f'{aftersale.get_kind_display()} {aftersale.number}，原销售单 #{sale.pk}，退回价值 {aftersale.refund_amount}，线下实收 {aftersale.offline_delta}',
            related_object_id=aftersale.pk, related_content_type=ContentType.objects.get_for_model(AfterSale),
        )
        return aftersale
