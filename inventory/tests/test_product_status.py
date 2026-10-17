import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections, IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from inventory.forms.product_forms import ProductForm, ProductVariantRowForm
from inventory.models import (
    AfterSale, Category, Inventory, InventoryCheck, InventoryCheckItem,
    InventoryTransaction, Member, MemberLevel, MemberTransaction, OperationLog, Product, Sale, SaleItem,
)
from inventory.models.inventory import update_inventory
from inventory.services.aftersales_service import quote_aftersale, complete_aftersale
from inventory.services.inventory_check_service import InventoryCheckService
from inventory.services.inventory_service import InventoryService
from inventory.services.product_service import set_product_active


@override_settings(ALLOWED_HOSTS=['testserver'])
class ProductStatusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(username='product-manager')
        cls.manager.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='inventory', codename__in=['change_product', 'change_inventory', 'add_aftersale'],
        ))
        cls.viewer = User.objects.create_user(username='product-viewer')
        cls.category = Category.objects.create(name='状态分类')
        cls.zero = Product.objects.create(name='零库存-A', barcode='STATUS-A', category=cls.category, price=10, cost=5, color='black', size='M')
        cls.stocked = Product.objects.create(name='现货-B', barcode='STATUS-B', category=cls.category, price=10, cost=5)
        cls.inactive = Product.objects.create(name='停用-C', barcode='STATUS-C', category=cls.category, price=10, cost=5, is_active=False)
        Inventory.objects.create(product=cls.zero, quantity=0)
        Inventory.objects.create(product=cls.stocked, quantity=3)
        Inventory.objects.create(product=cls.inactive, quantity=7)

    def setUp(self):
        self.client.force_login(self.manager)

    def test_stop_checks_current_stock_and_repeat_is_idempotent(self):
        url = reverse('product_delete', args=[self.zero.pk])
        self.assertContains(self.client.get(url), '确认停用')
        Inventory.objects.filter(product=self.zero).update(quantity=1)
        self.assertContains(self.client.post(url), '只有库存为 0')
        self.zero.refresh_from_db()
        self.assertTrue(self.zero.is_active)
        Inventory.objects.filter(product=self.zero).update(quantity=0)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.zero.refresh_from_db()
        self.assertFalse(self.zero.is_active)
        self.assertEqual(OperationLog.objects.count(), 1)
        self.assertEqual(OperationLog.objects.get().operator_id, self.manager.pk)
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_nonzero_and_missing_stock(self):
        for quantity in (3, -1):
            Inventory.objects.filter(product=self.stocked).update(quantity=quantity)
            with self.assertRaises(ValidationError):
                set_product_active(self.stocked.pk, False, self.manager)
            self.stocked.refresh_from_db()
            self.assertTrue(self.stocked.is_active)
        product = Product.objects.create(name='无库存记录', barcode='STATUS-MISSING', category=self.category, price=10, cost=5)
        set_product_active(product.pk, False, self.manager)
        self.assertFalse(Product.objects.get(pk=product.pk).is_active)
        self.assertFalse(Inventory.objects.filter(product=product).exists())

    def test_restore_keeps_original_identity_stock_and_filter_context(self):
        self.assertEqual(self.client.get(reverse('product_restore', args=[self.inactive.pk])).status_code, 405)
        url = reverse('product_restore', args=[self.inactive.pk]) + '?search=STATUS&category=' + str(self.category.pk) + '&sort=name&page=3'
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('status=active', response.url)
        self.assertIn('search=STATUS', response.url)
        self.assertIn('sort=name', response.url)
        self.assertNotIn('page=', response.url)
        self.inactive.refresh_from_db()
        self.assertTrue(self.inactive.is_active)
        self.assertEqual(self.inactive.barcode, 'STATUS-C')
        self.assertEqual(Inventory.objects.get(product=self.inactive).quantity, 7)
        self.client.post(url)
        self.assertEqual(OperationLog.objects.count(), 1)
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_permission_required_at_service_view_and_buttons(self):
        self.client.force_login(self.viewer)
        for route in ('product_delete', 'product_restore'):
            self.assertEqual(self.client.post(reverse(route, args=[self.zero.pk])).status_code, 403)
        with self.assertRaises(PermissionDenied):
            set_product_active(self.zero.pk, False, self.viewer)
        response = self.client.get(reverse('product_list'), {'status': 'all'})
        self.assertNotContains(response, reverse('product_restore', args=[self.inactive.pk]))
        self.assertNotContains(response, reverse('product_delete', args=[self.zero.pk]))

    def test_failed_audit_rolls_back_status(self):
        with patch('inventory.services.product_service.OperationLog.objects.create', side_effect=IntegrityError('audit failed')):
            with self.assertRaises(IntegrityError):
                set_product_active(self.zero.pk, False, self.manager)
        self.zero.refresh_from_db()
        self.assertTrue(self.zero.is_active)
        self.assertFalse(OperationLog.objects.exists())

    def test_lists_dashboard_and_historical_stock_visibility(self):
        response = self.client.get(reverse('product_list'))
        self.assertContains(response, '零库存-A')
        self.assertNotContains(response, '停用-C')
        response = self.client.get(reverse('product_list'), {'status': 'inactive', 'search': 'STATUS-C', 'category': self.category.pk, 'sort': 'name'})
        self.assertEqual(list(response.context['products']), [self.inactive])
        self.assertContains(response, reverse('product_restore', args=[self.inactive.pk]))
        self.assertEqual(self.client.get(reverse('product_list'), {'status': 'all'}).context['page_obj'].paginator.count, 3)
        self.assertEqual(self.client.get(reverse('product_list'), {'status': 'bad'}).context['selected_status'], 'active')
        response = self.client.get(reverse('inventory_list'))
        self.assertNotContains(response, '停用-C')
        response = self.client.get(reverse('index'))
        self.assertEqual(response.context['total_products'], 2)
        self.assertEqual(response.context['total_inventory_quantity'], 3)
        self.assertEqual(response.context['low_stock_products'], 2)
        self.assertEqual(response.context['out_of_stock_products'], 1)
        record = InventoryTransaction.objects.create(product=self.inactive, quantity=1, transaction_type='IN', operator=self.manager)
        response = self.client.get(reverse('inventory_transaction_list'))
        self.assertContains(response, record.product.name)
        set_product_active(self.zero.pk, False, self.manager)
        response = self.client.get(reverse('index'))
        self.assertEqual((response.context['total_products'], response.context['total_inventory_quantity']), (1, 3))
        set_product_active(self.zero.pk, True, self.manager)
        self.assertEqual(self.client.get(reverse('index')).context['total_products'], 2)

    def test_edit_cannot_toggle_status_or_restore_with_stale_instance(self):
        for product, forged_state in ((self.inactive, 'on'), (self.stocked, '')):
            response = self.client.post(reverse('product_edit', args=[product.pk]), {
                'name': product.name, 'barcode': product.barcode, 'category': self.category.pk,
                'price': 12, 'cost': 5, 'is_active': forged_state,
            })
            self.assertEqual(response.status_code, 302)
            product.refresh_from_db()
        self.assertFalse(self.inactive.is_active)
        self.assertTrue(self.stocked.is_active)
        self.assertNotIn('is_active', ProductForm(instance=self.zero).fields)

    def test_duplicate_inactive_barcode_provides_restore_instructions(self):
        data = {'barcode': self.inactive.barcode, 'name': '重复', 'category': self.category.pk, 'price': 10, 'cost': 5}
        for route in ('product_create', 'barcode_product_create'):
            self.assertContains(self.client.post(reverse(route), data), '已停用中恢复')
        row = ProductVariantRowForm({'size': 'M', 'barcode': self.inactive.barcode, 'initial_stock': 0})
        self.assertFalse(row.is_valid())
        self.assertIn('已停用中恢复', str(row.errors))
        response = self.client.get(reverse('barcode_product_create'), {'barcode': self.inactive.barcode})
        self.assertIn('status=inactive', response.url)
        self.assertIn('search=STATUS-C', response.url)
        self.assertEqual(Product.objects.count(), 3)

    def test_inactive_stock_cannot_be_changed_with_forged_id_or_stale_instance(self):
        for route, data in (
            ('inventory_in', {'quantity': 1}), ('inventory_out', {'quantity': 1}),
            ('inventory_adjust', {'quantity': 11, 'adjustment_action': 'set'}),
        ):
            self.assertContains(self.client.post(reverse(route), data | {'product': self.inactive.pk}), '已停用')
        self.assertFalse(update_inventory(self.inactive, 1, 'IN', self.manager)[0])
        with self.assertRaises(ValidationError):
            InventoryService.update_stock(self.inactive, 1, 'IN', self.manager)
        set_product_active(self.zero.pk, False, self.manager)
        # self.zero 的内存对象仍为启用，必须以数据库状态为准。
        self.assertTrue(self.zero.is_active)
        self.assertFalse(update_inventory(self.zero, 1, 'IN', self.manager)[0])
        self.assertEqual(Inventory.objects.get(product=self.inactive).quantity, 7)
        self.assertFalse(InventoryTransaction.objects.exists())

    def make_sold_out_sale(self, status='DRAFT'):
        sale = Sale.objects.create(operator=self.manager, total_amount=0, final_amount=0, status=status)
        item = SaleItem.objects.create(sale=sale, product=self.stocked, quantity=3, price=10, actual_price=10)
        sale.refresh_from_db()
        return sale, item

    def test_cancel_or_remove_sale_item_requires_manual_restore(self):
        sale, item = self.make_sold_out_sale()
        set_product_active(self.stocked.pk, False, self.manager)
        before = InventoryTransaction.objects.count()
        for route, args in (('sale_cancel', [sale.pk]), ('sale_item_delete', [sale.pk, item.pk])):
            response = self.client.post(reverse(route, args=args), {'reason': '取消'})
            self.assertEqual(response.status_code, 302)
            self.assertIn('已停用', ' '.join(str(message) for message in response.wsgi_request._messages))
            sale.refresh_from_db()
            self.assertEqual(sale.status, 'DRAFT')
            self.assertTrue(SaleItem.objects.filter(pk=item.pk).exists())
            self.assertEqual(Inventory.objects.get(product=self.stocked).quantity, 0)
            self.assertEqual(InventoryTransaction.objects.count(), before)
        set_product_active(self.stocked.pk, True, self.manager)
        self.client.post(reverse('sale_cancel', args=[sale.pk]), {'reason': '恢复后取消'})
        sale.refresh_from_db()
        self.assertEqual(sale.status, 'CANCELLED')
        self.assertEqual(Inventory.objects.get(product=self.stocked).quantity, 3)

    def test_return_preview_and_submission_block_inactive_without_refunds(self):
        sale, item = self.make_sold_out_sale('COMPLETED')
        level = MemberLevel.objects.create(name='普通等级', discount=1, points_threshold=0)
        member = Member.objects.create(name='退货会员', phone='13800138001', level=level, balance=5, points=10)
        Sale.objects.filter(pk=sale.pk).update(member=member, balance_paid=10, payment_method='mixed')
        sale.refresh_from_db()
        payload = {'kind': 'RETURN', 'reason': '申请退货', 'offline_method': 'cash', 'topup_method': 'offline', 'returns': {str(item.pk): 1}, 'replacements': []}
        quote = quote_aftersale(sale, payload)
        set_product_active(self.stocked.pk, False, self.manager)
        with self.assertRaisesMessage(ValidationError, '已停用'):
            quote_aftersale(sale, payload)
        with self.assertRaisesMessage(ValidationError, '已停用'):
            complete_aftersale(sale_id=sale.pk, payload=payload, expected_quote=quote, request_key=uuid.uuid4(), operator=self.manager)
        sale.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(sale.aftersales_version, 0)
        self.assertEqual((member.balance, member.points), (5, 10))
        self.assertFalse(MemberTransaction.objects.exists())
        self.assertFalse(AfterSale.objects.exists())
        self.assertEqual(Inventory.objects.get(product=self.stocked).quantity, 0)
        set_product_active(self.stocked.pk, True, self.manager)
        complete_aftersale(sale_id=sale.pk, payload=payload, expected_quote=quote, request_key=uuid.uuid4(), operator=self.manager)
        self.assertEqual(Inventory.objects.get(product=self.stocked).quantity, 1)

    def test_new_check_excludes_inactive_and_old_adjustment_rolls_back(self):
        check = InventoryCheckService.create_inventory_check('新盘点', '', self.manager)
        self.assertSetEqual(set(check.items.values_list('product_id', flat=True)), {self.zero.pk, self.stocked.pk})
        old = InventoryCheck.objects.create(name='老盘点', status='completed', created_by=self.manager)
        InventoryCheckItem.objects.create(inventory_check=old, product=self.stocked, system_quantity=3, actual_quantity=12)
        InventoryCheckItem.objects.create(inventory_check=old, product=self.inactive, system_quantity=7, actual_quantity=11)
        with self.assertRaisesMessage(ValidationError, '已停用'):
            InventoryCheckService.approve_inventory_check(old, self.manager, adjust_inventory=True)
        old.refresh_from_db()
        self.assertEqual(old.status, 'completed')
        self.assertEqual(Inventory.objects.get(product=self.stocked).quantity, 3)
        self.assertFalse(InventoryTransaction.objects.exists())


class ProductStatusConcurrencyTests(TransactionTestCase):
    def test_stop_and_receipt_cannot_create_hidden_positive_stock(self):
        if connection.vendor == 'sqlite' and ('mode=memory' in str(connection.settings_dict['NAME']) or str(connection.settings_dict['NAME']) == ':memory:'):
            self.skipTest('并发检查使用文件型 SQLite 测试库')
        user = User.objects.create_superuser(username='status-race')
        category = Category.objects.create(name='并发状态')
        product = Product.objects.create(name='并发商品', barcode='STATUS-RACE', category=category, price=10, cost=5)
        Inventory.objects.create(product=product, quantity=0)
        start = Barrier(2)
        def worker(stop):
            try:
                start.wait(timeout=10)
                if stop:
                    try:
                        return set_product_active(product.pk, False, user)[1]
                    except ValidationError:
                        return False
                return update_inventory(product, 1, 'IN', user)[0]
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, [True, False]))
        product.refresh_from_db()
        quantity = Inventory.objects.get(product=product).quantity
        self.assertEqual(sorted(results), [False, True])
        self.assertIn((product.is_active, quantity), [(False, 0), (True, 1)])
        self.assertEqual(InventoryTransaction.objects.count(), quantity)
