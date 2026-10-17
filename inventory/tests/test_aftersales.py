import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User, Permission, Group
from django.core.exceptions import ValidationError
from django.test import TestCase, Client, TransactionTestCase
from django.db import connections, close_old_connections, OperationalError
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from inventory.models import (
    Sale, SaleItem, Inventory, InventoryTransaction, Product, Category,
    Member, MemberLevel, MemberTransaction, AfterSale, AfterSaleItem, OperationLog,
)
from inventory.services.aftersales_service import quote_aftersale, complete_aftersale
from inventory.services.sales_ledger import sales_summary, product_totals, sales_periods
from inventory.services.report_service import ReportService


class AfterSalesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='seller', password='secret')
        self.user.groups.add(Group.objects.get(name='销售员'))
        self.client.force_login(self.user)
        self.category = Category.objects.create(name='售后测试')
        self.product = Product.objects.create(name='旧商品', barcode='old', price=50, cost=10, category=self.category)
        self.new_product = Product.objects.create(name='新商品', barcode='new', price=120, cost=20, category=self.category)
        Inventory.objects.create(product=self.product, quantity=100)
        Inventory.objects.create(product=self.new_product, quantity=100)
        self.level = MemberLevel.objects.create(name='普通', discount=1, points_threshold=0)
        self.member = Member.objects.create(name='会员', phone='13800000000', level=self.level, balance=10, points=5, total_spend=100, purchase_count=1)
        self.sale = Sale.objects.create(member=self.member, total_amount=0, final_amount=0, status='COMPLETED', operator=self.user, points_earned=100, payment_method='mixed', balance_paid=60)
        self.item = SaleItem.objects.create(sale=self.sale, product=self.product, quantity=2, price=50, actual_price=50)
        self.sale.refresh_from_db()

    def payload(self, quantity=1, kind='RETURN', new_quantity=1, topup='offline'):
        return {
            'kind': kind, 'reason': '顾客申请', 'offline_method': 'cash', 'topup_method': topup,
            'returns': {str(self.item.pk): quantity},
            'replacements': [{'product_id': self.new_product.pk, 'quantity': new_quantity}] if kind == 'EXCHANGE' else [],
        }

    def complete(self, payload=None, sale=None, quote=None, key=None):
        sale = sale or Sale.objects.select_related('member__level').get(pk=self.sale.pk)
        payload = payload or self.payload()
        quote = quote or quote_aftersale(sale, payload)
        # 与经过签名的浏览器确认数据保持一致。
        quote = json.loads(json.dumps(quote, default=str))
        return complete_aftersale(sale_id=sale.pk, payload=payload, request_key=key or uuid.uuid4(), expected_quote=quote, operator=self.user)

    def test_partial_then_full_return_money_points_and_stock(self):
        record = self.complete()
        self.assertEqual(record.refund_amount, 50)
        self.assertEqual(record.balance_refund, 30)
        self.assertEqual(record.offline_delta, -20)
        self.member.refresh_from_db()
        self.assertEqual((self.member.balance, self.member.points, self.member.total_spend, self.member.purchase_count), (40, -45, 50, 1))
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 99)
        self.complete()
        self.member.refresh_from_db()
        self.assertEqual((self.member.balance, self.member.points, self.member.total_spend, self.member.purchase_count), (70, -95, 0, 0))
        self.item.refresh_from_db()
        self.sale.refresh_from_db()
        self.assertEqual(self.item.quantity, 2)
        self.assertEqual(self.sale.final_amount, 100)
        self.assertEqual(self.sale.aftersales_label, '全部退回')
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 100)
        with self.assertRaises(ValidationError):
            self.complete()

    def test_exchange_topup_then_return_replacement_preserves_payment_sources(self):
        record = self.complete(self.payload(quantity=2, kind='EXCHANGE'))
        new_sale = record.replacement_sale
        self.assertEqual((record.credit_amount, record.balance_delta, record.offline_delta), (100, 0, 20))
        self.assertEqual((new_sale.final_amount, new_sale.balance_paid, new_sale.exchange_credit_amount), (120, 60, 100))
        self.assertEqual(record.inventory_transactions.count(), 2)
        self.assertEqual(Inventory.objects.get(product=self.new_product).quantity, 99)
        self.member.refresh_from_db()
        self.assertEqual((self.member.balance, self.member.total_spend, self.member.purchase_count), (10, 120, 1))
        payload = self.payload()
        payload['returns'] = {str(new_sale.items.get().pk): 1}
        refunded = self.complete(payload, sale=Sale.objects.select_related('member__level').get(pk=new_sale.pk))
        self.assertEqual((refunded.balance_refund, refunded.offline_delta), (60, -60))

    def test_cheaper_exchange_returns_unused_sources_and_balance_topup(self):
        self.new_product.price = 20
        self.new_product.save()
        record = self.complete(self.payload(quantity=2, kind='EXCHANGE'))
        self.assertEqual((record.balance_credit, record.balance_delta, record.offline_delta), (20, 40, -40))
        self.assertEqual(record.replacement_sale.balance_paid, 20)

    def test_balance_topup_requires_sufficient_balance(self):
        payload = self.payload(quantity=2, kind='EXCHANGE', topup='balance')
        with self.assertRaises(ValidationError):
            self.complete(payload)
        self.assertFalse(AfterSale.objects.exists())
        self.member.balance = 30
        self.member.save()
        record = self.complete(payload)
        self.assertEqual((record.balance_delta, record.offline_delta), (-20, 0))
        self.assertEqual(record.replacement_sale.balance_paid, 80)

    def test_duplicate_confirmation_and_stale_quote(self):
        payload = self.payload()
        quote = quote_aftersale(self.sale, payload)
        key = uuid.uuid4()
        first = self.complete(payload, quote=quote, key=key)
        self.assertEqual(self.complete(payload, quote=quote, key=key).pk, first.pk)
        with self.assertRaises(ValidationError):
            self.complete(payload, quote=quote)
        self.assertEqual(AfterSale.objects.count(), 1)
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 99)

    def test_stock_failure_and_log_failure_roll_back_all_changes(self):
        for failure in ['stock', 'log']:
            with self.subTest(failure=failure):
                if failure == 'stock':
                    Inventory.objects.filter(product=self.new_product).update(quantity=0)
                    context = mock.patch('inventory.services.aftersales_service.OperationLog.objects.create', wraps=OperationLog.objects.create)
                else:
                    Inventory.objects.filter(product=self.new_product).update(quantity=100)
                    context = mock.patch('inventory.services.aftersales_service.OperationLog.objects.create', side_effect=RuntimeError('audit failed'))
                with context, self.assertRaises((ValidationError, RuntimeError)):
                    self.complete(self.payload(quantity=2, kind='EXCHANGE'))
                self.assertFalse(AfterSale.objects.exists())
                self.assertEqual(Sale.objects.count(), 1)
                self.assertEqual(Inventory.objects.get(product=self.product).quantity, 98)
                self.member.refresh_from_db()
                self.assertEqual((self.member.balance, self.member.points), (10, 5))
                self.sale.refresh_from_db()
                self.assertEqual(self.sale.aftersales_version, 0)

    def test_rounding_repeated_returns_equal_full_paid(self):
        Sale.objects.filter(pk=self.sale.pk).update(total_amount=100, discount_amount=Decimal('99.99'), final_amount=Decimal('.01'), balance_paid=0, payment_method='cash', points_earned=0)
        first = self.complete()
        second = self.complete()
        self.assertEqual((first.refund_amount, second.refund_amount), (0, Decimal('.01')))

    def test_new_price_change_rejects_confirmation(self):
        payload = self.payload(kind='EXCHANGE')
        quote = quote_aftersale(self.sale, payload)
        Product.objects.filter(pk=self.new_product.pk).update(price=121)
        with self.assertRaises(ValidationError):
            self.complete(payload, quote=quote)
        self.assertFalse(AfterSale.objects.exists())

    def test_cross_order_lines_and_invalid_quantities_rejected(self):
        for returned in ({'999999': 1}, {str(self.item.pk): -1}, {str(self.item.pk): 3}, {str(self.item.pk): 1.5}):
            with self.subTest(returned=returned), self.assertRaises(ValidationError):
                payload = self.payload()
                payload['returns'] = returned
                self.complete(payload)

    def test_history_views_and_snapshot(self):
        record = self.complete()
        Product.objects.filter(pk=self.product.pk).update(name='已改名', price=999)
        for route, args in [('aftersale_list', []), ('aftersale_detail', [record.pk]), ('sale_detail', [self.sale.pk]), ('inventory_transaction_list', []), ('member_detail', [self.member.pk])]:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=args))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, record.number)
        self.assertContains(self.client.get(reverse('aftersale_detail', args=[record.pk])), '旧商品')
        response = self.client.get(reverse('inventory_transaction_list'), {'aftersale': record.pk})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)

    def test_permission_get_and_signed_browser_confirmation(self):
        url = reverse('aftersale_create', args=[self.sale.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AfterSale.objects.exists())
        data = {'kind': 'RETURN', 'reason': '尺寸不合', 'offline_method': 'cash', 'topup_method': 'offline', f'return_{self.item.pk}': '1', 'replacement-TOTAL_FORMS': '0', 'replacement-INITIAL_FORMS': '0'}
        preview = self.client.post(url, data)
        self.assertEqual(preview.status_code, 200)
        self.assertIsNotNone(preview.context['preview'])
        token = preview.context['confirmation']
        response = self.client.post(url, {'confirmation': token, 'acknowledged': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AfterSale.objects.count(), 1)
        self.client.post(url, {'confirmation': token, 'acknowledged': 'on'})
        self.assertEqual(AfterSale.objects.count(), 1)
        ordinary = User.objects.create_user('ordinary', password='secret')
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse('aftersale_list')).status_code, 403)
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.client.force_login(self.user)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(url, data).status_code, 403)

    def test_zero_value_return_restores_stock(self):
        Sale.objects.filter(pk=self.sale.pk).update(total_amount=0, final_amount=0, balance_paid=0, points_earned=0, payment_method='cash')
        record = self.complete()
        self.assertEqual((record.refund_amount, record.balance_refund, record.offline_delta), (0, 0, 0))
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 99)

    def test_same_product_exchange_does_not_deduct_twice(self):
        payload = self.payload(kind='EXCHANGE')
        payload['replacements'] = [{'product_id': self.product.pk, 'quantity': 1}]
        Inventory.objects.filter(product=self.product).update(quantity=1)
        record = self.complete(payload)
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 1)
        self.assertEqual(record.inventory_transactions.count(), 2)
        self.assertEqual(record.offline_delta, 0)

    def test_replacement_options_disable_zero_and_missing_stock(self):
        from inventory.forms.aftersales_forms import ReplacementForm, ReplacementFormSet
        missing = Product.objects.create(name='尚无库存记录', barcode='no-stock-record', price=10, cost=1, category=self.category)
        Inventory.objects.filter(product=self.new_product).update(quantity=0)
        for form in [ReplacementForm(), ReplacementFormSet().empty_form]:
            options = {str(option['value']): option for _, group, _ in form.fields['product'].widget.optgroups('product', ['']) for option in group}
            for product in [self.new_product, missing]:
                self.assertTrue(options[str(product.pk)]['attrs']['disabled'])
                self.assertIn('无库存', options[str(product.pk)]['label'])
            self.assertFalse(options[str(self.product.pk)]['attrs'].get('disabled', False))
        forged = ReplacementForm({'product': self.new_product.pk, 'quantity': 1})
        self.assertFalse(forged.is_valid())
        self.assertIn('无库存', str(forged.errors))

    def test_stock_depleted_after_preview_rejects_exchange_confirmation(self):
        payload = self.payload(kind='EXCHANGE')
        quote = quote_aftersale(self.sale, payload)
        Inventory.objects.filter(product=self.new_product).update(quantity=0)
        with self.assertRaisesMessage(ValidationError, '无库存'):
            self.complete(payload, quote=quote)
        self.assertFalse(AfterSale.objects.exists())
        self.assertEqual(Inventory.objects.get(product=self.product).quantity, 98)

    def test_non_member_return_and_exchange(self):
        Sale.objects.filter(pk=self.sale.pk).update(member=None, balance_paid=0, payment_method='cash')
        record = self.complete(self.payload(quantity=2, kind='EXCHANGE'))
        self.assertEqual((record.offline_delta, record.balance_delta), (20, 0))
        self.assertIsNone(record.replacement_sale.member_id)

    def test_discount_allocation_has_exact_total_and_deterministic_remainder(self):
        second = SaleItem.objects.create(sale=self.sale, product=self.new_product, quantity=1, price=100, actual_price=100)
        Sale.objects.filter(pk=self.sale.pk).update(discount_amount=Decimal('199.99'), final_amount=Decimal('.01'), balance_paid=0, payment_method='cash', points_earned=0)
        payload = self.payload(quantity=2)
        first = self.complete(payload)
        payload['returns'] = {str(second.pk): 1}
        last = self.complete(payload)
        self.assertEqual(first.refund_amount + last.refund_amount, Decimal('.01'))
        self.assertEqual(first.refund_amount, Decimal('.01'))

    def test_reports_use_aftersale_day_and_do_not_count_exchange_credit_twice(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        old_time = timezone.now() - timedelta(days=1)
        Sale.objects.filter(pk=self.sale.pk).update(created_at=old_time)
        record = self.complete(self.payload(quantity=2, kind='EXCHANGE'))
        today = timezone.localdate()
        before = sales_summary(yesterday, yesterday)
        current = sales_summary(today, today)
        self.assertEqual((before['gross_sales'], before['refund_amount'], before['total_sales']), (100, 0, 100))
        self.assertEqual((current['gross_sales'], current['refund_amount'], current['total_sales']), (120, 100, 20))
        self.assertEqual(sum(row['total_sales'] for row in sales_periods(yesterday, today)), 120)
        all_products = {row['product__id']: row for row in product_totals(yesterday, today)}
        self.assertEqual(all_products[self.product.pk]['total_quantity'], 0)
        profit = ReportService.get_profit_report(yesterday, today)
        self.assertEqual((profit['total_sales'], profit['total_cost'], profit['gross_profit']), (120, 20, 100))
        member_data = ReportService.get_member_analysis(yesterday, today)
        self.assertEqual(member_data['member_sales']['total_amount'], 120)
        from inventory.services.export_service import ExportService
        exported = ExportService.format_member_data_for_export(member_data, yesterday, today)
        self.assertIn('120.00', exported['top_members'][0]['消费金额'])
        response = self.client.get(reverse('index'))
        self.assertEqual(response.context['today_sales_amount'], 20)
        self.assertEqual(self.client.get(reverse('sale_list')).context['today_sales'], 20)
        self.user.is_superuser = True
        self.user.save()
        for route in ['profit_report', 'sales_trend_report', 'member_analysis_report']:
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 200)
        self.assertContains(self.client.get(reverse('aftersale_list'), {'q': record.number, 'kind': 'EXCHANGE'}), record.number)

    def test_tampered_confirmation_and_unacknowledged_request_do_not_write(self):
        url = reverse('aftersale_create', args=[self.sale.pk])
        response = self.client.post(url, {'confirmation': 'invalid.signature', 'acknowledged': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AfterSale.objects.exists())
        response = self.client.post(url, {'confirmation': 'invalid.signature'})
        self.assertEqual(response.status_code, 302)

    def test_stale_browser_confirmation_preserves_choices_and_requotes(self):
        url = reverse('aftersale_create', args=[self.sale.pk])
        data = {'kind': 'EXCHANGE', 'reason': '换颜色', 'offline_method': 'cash', 'topup_method': 'offline', f'return_{self.item.pk}': 1,
                'replacement-TOTAL_FORMS': 1, 'replacement-INITIAL_FORMS': 0,
                'replacement-0-product': self.new_product.pk, 'replacement-0-quantity': 1}
        response = self.client.post(url, data)
        token = response.context['confirmation']
        Product.objects.filter(pk=self.new_product.pk).update(price=121)
        response = self.client.post(url, {'confirmation': token, 'acknowledged': 'on'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['preview']['replacement_amount'], 121)
        self.assertEqual(response.context['form']['reason'].value(), '换颜色')
        self.assertFalse(AfterSale.objects.exists())


class AfterSalesConcurrencyTests(TransactionTestCase):
    def test_two_confirmations_cannot_over_return(self):
        user = User.objects.create_superuser('admin', 'admin@example.com', 'secret')
        category = Category.objects.create(name='并发售后')
        product = Product.objects.create(name='并发商品', barcode='concurrent', price=10, cost=1, category=category)
        Inventory.objects.create(product=product, quantity=1)
        sale = Sale.objects.create(total_amount=0, final_amount=0, status='COMPLETED', operator=user)
        item = SaleItem.objects.create(sale=sale, product=product, quantity=1, price=10, actual_price=10)
        sale.refresh_from_db()
        payload = {'kind': 'RETURN', 'returns': {str(item.pk): 1}, 'replacements': [], 'reason': '并发退货', 'topup_method': 'offline', 'offline_method': 'cash'}
        quote = quote_aftersale(sale, payload)
        import threading
        gate = threading.Barrier(2)
        def submit():
            close_old_connections()
            try:
                operator = User.objects.get(pk=user.pk)
                gate.wait(timeout=10)
                complete_aftersale(sale_id=sale.pk, payload=payload, request_key=uuid.uuid4(), expected_quote=quote, operator=operator)
                return 'completed'
            except (ValidationError, OperationalError):
                return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(results.count('completed'), 1)
        self.assertEqual(AfterSale.objects.count(), 1)
        self.assertEqual(AfterSaleItem.objects.aggregate(total=Sum('quantity'))['total'], 1)
        self.assertEqual(Inventory.objects.get(product=product).quantity, 1)
