"""售后记录保留原销售单，已完成的单据仅供查询。"""
import uuid
import django

from django.db import models


def check_constraint(name, condition):
    # 项目允许 Django 4.2+；5.1 起将 check 参数更名为 condition。
    keyword = 'condition' if django.VERSION >= (5, 1) else 'check'
    return models.CheckConstraint(name=name, **{keyword: condition})


class AfterSale(models.Model):
    TYPES = [('RETURN', '退货'), ('EXCHANGE', '换货')]
    kind = models.CharField('类型', max_length=10, choices=TYPES)
    sale = models.ForeignKey('Sale', on_delete=models.PROTECT, related_name='aftersales')
    replacement_sale = models.OneToOneField(
        'Sale', on_delete=models.PROTECT, related_name='exchange_source', null=True, blank=True,
    )
    request_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payload_hash = models.CharField(max_length=64, editable=False)
    refund_amount = models.DecimalField('退回价值', max_digits=10, decimal_places=2)
    balance_refund = models.DecimalField('退回价值中的余额', max_digits=10, decimal_places=2, default=0)
    replacement_amount = models.DecimalField('换出金额', max_digits=10, decimal_places=2, default=0)
    credit_amount = models.DecimalField('换货抵扣', max_digits=10, decimal_places=2, default=0)
    balance_credit = models.DecimalField('抵扣中的余额', max_digits=10, decimal_places=2, default=0)
    extra_balance_paid = models.DecimalField('余额补款', max_digits=10, decimal_places=2, default=0)
    offline_delta = models.DecimalField('线下实收（负数为退款）', max_digits=10, decimal_places=2, default=0)
    offline_method = models.CharField(max_length=20)
    points_returned = models.PositiveIntegerField(default=0)
    reason = models.TextField('原因')
    operator = models.ForeignKey('auth.User', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    replacement_snapshot = models.JSONField(default=list)

    class Meta:
        ordering = ['-created_at', '-pk']
        default_permissions = ('add', 'view')
        verbose_name = '退换货单'
        verbose_name_plural = '退换货单'
        constraints = [
            check_constraint('aftersale_refund_nonnegative', models.Q(refund_amount__gte=0)),
            check_constraint('aftersale_balance_refund_valid', models.Q(balance_refund__gte=0, balance_refund__lte=models.F('refund_amount'))),
        ]

    @property
    def number(self):
        return f'AS{self.pk:08d}'

    @property
    def balance_delta(self):
        return self.balance_refund - self.balance_credit - self.extra_balance_paid

    @property
    def offline_method_label(self):
        return dict(self.sale.PAYMENT_METHODS).get(self.offline_method, self.offline_method)

    def __str__(self):
        return self.number


class AfterSaleItem(models.Model):
    aftersale = models.ForeignKey(AfterSale, on_delete=models.PROTECT, related_name='items')
    sale_item = models.ForeignKey('SaleItem', on_delete=models.PROTECT, related_name='returns')
    quantity = models.PositiveIntegerField()
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2)
    product_name = models.CharField(max_length=255)
    barcode = models.CharField(max_length=255)
    specification = models.CharField(max_length=255, blank=True)
    original_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=['aftersale', 'sale_item'], name='unique_aftersale_line'),
            check_constraint('aftersale_line_positive', models.Q(quantity__gt=0, refund_amount__gte=0)),
        ]
