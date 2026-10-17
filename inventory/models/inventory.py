from django.db import models, transaction
from django.utils import timezone
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from .product import Product


class Inventory(models.Model):
    product = models.OneToOneField(Product, on_delete=models.PROTECT, verbose_name='商品')
    quantity = models.IntegerField(default=0, verbose_name='库存数量')
    warning_level = models.IntegerField(default=10, verbose_name='预警数量')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    def clean(self):
        if self.quantity < 0:
            raise ValidationError('库存数量不能为负数')
        if self.warning_level < 0:
            raise ValidationError('预警数量不能为负数')
    
    @property
    def is_low_stock(self):
        return self.quantity <= self.warning_level
    
    class Meta:
        verbose_name = '库存'
        verbose_name_plural = '库存'
        permissions = (
            ("can_view_item", "可以查看物料"),
            ("can_add_item", "可以添加物料"),
            ("can_change_item", "可以修改物料"),
            ("can_delete_item", "可以删除物料"),
            ("can_export_item", "可以导出物料"),
            ("can_import_item", "可以导入物料"),
            ("can_allocate_item", "可以分配物料"),
            ("can_checkin_item", "可以入库物料"),
            ("can_checkout_item", "可以出库物料"),
            ("can_adjust_item", "可以调整物料库存"),
            ("can_return_item", "可以归还物料"),
            ("can_move_item", "可以移动物料"),
            ("can_manage_backup", "可以管理备份"),
        )
    
    def __str__(self):
        return f'{self.product.name} - {self.quantity}'


class InventoryTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('IN', '入库'),
        ('OUT', '出库'),
        ('ADJUST', '调整'),
    ]

    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name='商品')
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES, verbose_name='交易类型')
    quantity = models.IntegerField(verbose_name='数量')
    operator = models.ForeignKey(User, on_delete=models.PROTECT, verbose_name='操作员')
    notes = models.TextField(blank=True, verbose_name='备注')
    aftersale = models.ForeignKey(
        'AfterSale', on_delete=models.PROTECT, null=True, blank=True,
        related_name='inventory_transactions', verbose_name='售后单',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    
    class Meta:
        verbose_name = '库存交易记录'
        verbose_name_plural = '库存交易记录'
    
    def __str__(self):
        return f'{self.product.name} - {self.get_transaction_type_display()} - {self.quantity}'


def check_inventory(product, quantity):
    """检查库存是否足够"""
    try:
        inventory = Inventory.objects.get(product=product)
        return inventory.quantity >= quantity
    except Inventory.DoesNotExist:
        return False


def lock_products_for_inventory(product_ids, *, require_active=True):
    """在调用方事务内锁定商品；实际 UPDATE 也能在 SQLite 下阻止并发停用。"""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('商品库存锁必须在 transaction.atomic() 内使用')
    products = []
    for product_id in sorted(set(product_ids)):
        if not Product.objects.filter(pk=product_id).update(is_active=models.F('is_active')):
            raise ValidationError('商品不存在')
        product = Product.objects.get(pk=product_id)
        if require_active and not product.is_active:
            raise ValidationError(
                f'{product.name}｜{product.get_color_display()}｜{product.get_size_display()}'
                f'（条码：{product.barcode}）已停用，请先到商品管理恢复该商品'
            )
        products.append(product)
    return products


def update_inventory(product, quantity, transaction_type, operator, notes='', *, aftersale=None):
    """更新库存并记录交易"""
    try:
        with transaction.atomic():
            lock_products_for_inventory([product.pk])
            stock = Inventory.objects.filter(product=product)
            if quantity < 0:
                stock = stock.filter(quantity__gte=-quantity)

            # 用同一条 UPDATE 校验并修改库存；SQLite 下也不会覆盖并发变更。
            changes = {
                'quantity': models.F('quantity') + quantity,
                'updated_at': timezone.now(),
            }
            if not stock.update(**changes):
                inventory, _ = Inventory.objects.get_or_create(
                    product=product, defaults={'quantity': 0}
                )
                if not stock.update(**changes):
                    raise ValidationError(
                        f"库存不足: {product.name}, 当前库存: {inventory.quantity}, "
                        f"请求数量: {abs(quantity)}"
                    )

            inventory = Inventory.objects.get(product=product)
            record = InventoryTransaction.objects.create(
                product=product,
                transaction_type=transaction_type,
                quantity=abs(quantity),
                operator=operator,
                notes=notes,
                aftersale=aftersale,
            )
        return True, inventory, record
    except Exception as e:
        # Catch outside atomic so both quantity and ledger are rolled back.
        return False, None, '；'.join(e.messages) if isinstance(e, ValidationError) else str(e)


class StockAlert(models.Model):
    """库存预警模型"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name='商品')
    alert_type = models.CharField(
        max_length=20, 
        choices=[
            ('low_stock', '低库存'),
            ('expiring', '即将过期'),
            ('overstock', '库存过量')
        ],
        verbose_name='预警类型'
    )
    is_active = models.BooleanField(default=True, verbose_name='是否激活')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name='解决时间')
    
    class Meta:
        verbose_name = '库存预警'
        verbose_name_plural = '库存预警'
        
    def __str__(self):
        return f'{self.product.name} - {self.get_alert_type_display()}'
