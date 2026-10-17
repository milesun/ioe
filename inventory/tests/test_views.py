import json
import os
import re
import tempfile
from html import unescape
from urllib.parse import parse_qs, urlsplit

from django.core import management
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User, Permission, Group
from decimal import Decimal

from inventory.models import (
    Category, 
    Product, 
    Inventory, 
    InventoryTransaction,
    Member,
    MemberLevel,
    Sale,
    SaleItem
)

class ViewTestCase(TestCase):
    """视图测试的基类"""
    
    def setUp(self):
        # 创建测试用户
        self.user = User.objects.create_user(
            username='testuser', 
            password='12345',
            email='test@example.com'
        )
        
        # 创建测试管理员
        self.admin = User.objects.create_user(
            username='admin', 
            password='admin123',
            email='admin@example.com',
            is_staff=True
        )
        
        # 创建客户端
        self.client = Client()
        
        # 创建测试分类
        self.category = Category.objects.create(
            name='测试分类',
            description='测试分类描述'
        )
        
        # 创建测试商品
        self.product = Product.objects.create(
            barcode='1234567890',
            name='测试商品',
            category=self.category,
            description='测试商品描述',
            price=Decimal('10.00'),
            cost=Decimal('5.00')
        )
        
        # 创建库存记录
        self.inventory = Inventory.objects.create(
            product=self.product,
            quantity=100,
            warning_level=10
        )
        
        # 创建会员等级
        self.member_level = MemberLevel.objects.create(
            name='普通会员',
            discount=Decimal('0.95'),  # 95%
            points_threshold=0,
            color='#FF5733'
        )
        
        # 创建会员
        self.member = Member.objects.create(
            name='测试会员',
            phone='13800138000',
            level=self.member_level,
            balance=Decimal('100.00'),
            points=0
        )

class ProductViewTest(ViewTestCase):
    """测试商品相关视图"""
    
    def test_product_list_view(self):
        """测试商品列表视图"""
        # 登录
        self.client.login(username='testuser', password='12345')
        
        # 访问商品列表页面
        response = self.client.get(reverse('product_list'))
        
        # 验证响应
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/product_list.html')
        self.assertContains(response, '测试商品')
        
    def test_product_create_view(self):
        """测试创建商品视图"""
        # 登录
        self.client.login(username='testuser', password='12345')
        
        # 访问创建商品页面
        response = self.client.get(reverse('product_create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/product/product_form.html')
        
        # 提交创建商品表单
        product_data = {
            'barcode': '9876543210',
            'name': '新测试商品',
            'category': self.category.id,
            'description': '新测试商品描述',
            'price': '15.00',
            'cost': '7.50'
        }
        
        response = self.client.post(reverse('product_create'), product_data)
        
        # 验证重定向
        self.assertRedirects(response, reverse('product_list'))
        
        # 验证商品创建
        self.assertTrue(Product.objects.filter(barcode='9876543210').exists())

class InventoryViewTest(ViewTestCase):
    """测试库存相关视图"""

    def setUp(self):
        super().setUp()
        self.user.user_permissions.add(Permission.objects.get(
            codename='change_inventory', content_type__app_label='inventory'
        ))

    def test_inventory_list_view(self):
        """测试库存列表视图"""
        # 登录
        self.client.login(username='testuser', password='12345')
        
        # 访问库存列表页面
        response = self.client.get(reverse('inventory_list'))
        
        # 验证响应
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/inventory_list.html')
        self.assertContains(response, '测试商品')

    def test_inventory_list_action_links_include_product(self):
        """库存操作按钮应连接到对应商品的操作页。"""
        self.client.login(username='testuser', password='12345')

        response = self.client.get(reverse('inventory_list'))

        for view_name in ('inventory_in', 'inventory_out', 'inventory_adjust'):
            with self.subTest(view_name=view_name):
                expected_url = f"{reverse(view_name)}?product_id={self.product.id}"
                self.assertContains(response, f'href="{expected_url}"')

    def _create_inventory_items(self, count, quantity=100, warning_level=10, **product_fields):
        items = []
        first_index = Product.objects.count()
        for index in range(first_index, first_index + count):
            fields = {
                'barcode': f'pagination-{index}',
                'name': f'分页商品 {index}',
                'category': self.category,
                'price': Decimal('10.00'),
                'cost': Decimal('5.00'),
            }
            fields.update(product_fields)
            product = Product.objects.create(**fields)
            items.append(Inventory.objects.create(
                product=product, quantity=quantity, warning_level=warning_level,
            ))
        return items

    def test_inventory_pagination_has_no_duplicates_or_missing_items(self):
        self.client.force_login(self.user)
        items = [self.inventory] + self._create_inventory_items(20)
        # 停用商品不应影响页数或总数。
        self._create_inventory_items(1, is_active=False)

        first = self.client.get(reverse('inventory_list'))
        second = self.client.get(reverse('inventory_list'), {'page': 2})

        for response, expected in ((first, items[:20]), (second, items[20:])):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['page_obj'].paginator.count, 21)
            self.assertEqual(response.context['page_obj'].paginator.num_pages, 2)
            self.assertEqual(list(response.context['inventory_items']), expected)
        self.assertContains(first, '显示 1 至 20 条，共 21 条记录')
        self.assertContains(second, '显示 21 至 21 条，共 21 条记录')
        self.assertContains(first, 'aria-disabled="true" aria-label="上一页"')
        self.assertContains(second, 'aria-disabled="true" aria-label="下一页"')
        self.assertContains(second, 'aria-current="page">2</span>')

    def test_inventory_pagination_preserves_combined_filters(self):
        self.client.force_login(self.user)
        name = '分页 & + # 商品'
        matching = self._create_inventory_items(
            21, quantity=10, name=name, color='blue', size='M',
        )
        other_category = Category.objects.create(name='其他分页分类')
        for override in (
            {'name': '不匹配的商品'}, {'category': other_category},
            {'color': 'red'}, {'size': 'L'}, {'quantity': 11}, {'is_active': False},
        ):
            fields = {'quantity': 10, 'name': name, 'color': 'blue', 'size': 'M'}
            fields.update(override)
            self._create_inventory_items(1, **fields)
        filters = {
            'search': name, 'category': str(self.category.pk),
            'color': 'blue', 'size': 'M', 'stock_status': 'low',
        }

        first = self.client.get(reverse('inventory_list'), filters)
        next_link = next(
            unescape(link)
            for link in re.findall(r'href="([^"]+)"', first.content.decode())
            if link.startswith('?page=2&amp;')
        )
        self.assertEqual(
            parse_qs(urlsplit(next_link).query),
            {key: [value] for key, value in {**filters, 'page': '2'}.items()},
        )
        second = self.client.get(reverse('inventory_list') + next_link)
        self.assertEqual(second.context['page_obj'].paginator.count, 21)
        self.assertEqual(list(second.context['inventory_items']), matching[20:])
        self.assertContains(second, '<input type="hidden" name="stock_status" value="low">', html=True)
        self.assertContains(second, 'btn-outline-danger active')

        status_links = [
            unescape(link)
            for link in re.findall(r'href="([^"]+)"', second.content.decode())
            if link.startswith('?stock_status=')
        ]
        self.assertEqual(len(status_links), 3)
        for link in status_links:
            params = parse_qs(urlsplit(link).query)
            self.assertNotIn('page', params)
            self.assertEqual(params['search'], [name])
            for key in ('category', 'color', 'size'):
                self.assertEqual(params[key], [filters[key]])

    def test_inventory_stock_filter_applies_before_pagination(self):
        self.client.force_login(self.user)
        normal = [self.inventory] + self._create_inventory_items(20)
        low = self._create_inventory_items(1, quantity=0, warning_level=1)
        boundary = self._create_inventory_items(1, quantity=5, warning_level=5)

        response = self.client.get(reverse('inventory_list'), {'stock_status': 'low'})
        self.assertEqual(list(response.context['inventory_items']), low + boundary)
        self.assertEqual(response.context['page_obj'].paginator.count, 2)

        response = self.client.get(reverse('inventory_list'), {'stock_status': 'normal', 'page': 2})
        self.assertEqual(response.context['page_obj'].paginator.count, 21)
        self.assertEqual(list(response.context['inventory_items']), normal[20:])

        response = self.client.get(reverse('inventory_list'), {'stock_status': 'unknown'})
        self.assertEqual(response.context['stock_status'], 'all')
        self.assertEqual(response.context['page_obj'].paginator.count, 23)

    def test_inventory_pagination_handles_invalid_pages(self):
        self.client.force_login(self.user)
        self._create_inventory_items(20)

        for value, expected_page in (('abc', 1), ('', 1), ('0', 2), ('-1', 2), ('999', 2)):
            with self.subTest(page=value):
                response = self.client.get(reverse('inventory_list'), {'page': value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['page_obj'].number, expected_page)

    def test_inventory_pagination_single_page_and_empty_results(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('inventory_list'))
        self.assertContains(response, '显示 1 至 1 条，共 1 条记录')
        self.assertNotContains(response, 'aria-label="库存列表分页"')

        response = self.client.get(reverse('inventory_list'), {'stock_status': 'low'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertContains(response, '没有找到符合筛选条件的库存')
        self.assertContains(response, '清除筛选')
        self.assertNotContains(response, 'aria-label="库存列表分页"')
        self.assertNotContains(response, '共 0 条记录')

    def test_inventory_pagination_supports_english(self):
        self.client.force_login(self.user)
        self._create_inventory_items(20)
        response = self.client.get(
            reverse('inventory_list'), {'page': 2}, HTTP_ACCEPT_LANGUAGE='en',
        )
        self.assertContains(response, 'Showing 21 to 21 of 21 records')
        self.assertContains(response, 'aria-label="Inventory pagination"')
        self.assertContains(response, 'aria-label="Previous"')
        self.assertContains(response, 'aria-disabled="true" aria-label="Next"')

    def test_inventory_pagination_elides_large_page_ranges(self):
        self.client.force_login(self.user)
        self._create_inventory_items(200)
        response = self.client.get(reverse('inventory_list'), {'page': 6})

        self.assertEqual(response.context['page_obj'].paginator.num_pages, 11)
        self.assertContains(response, '<span class="page-link">…</span>', count=2, html=True)
        self.assertContains(response, 'href="?page=1&amp;stock_status=all"')
        self.assertContains(response, 'href="?page=11&amp;stock_status=all"')
        self.assertNotContains(response, 'href="?page=2&amp;stock_status=all"')

    def test_inventory_action_forms_prefill_selected_product(self):
        """从库存列表进入操作页时应预选当前商品。"""
        self.client.login(username='testuser', password='12345')

        for view_name in ('inventory_in', 'inventory_out', 'inventory_adjust'):
            with self.subTest(view_name=view_name):
                response = self.client.get(
                    reverse(view_name),
                    {'product_id': self.product.id},
                )

                self.assertEqual(response.status_code, 200)
                expected_template = (
                    'inventory/inventory_adjust_form.html'
                    if view_name == 'inventory_adjust'
                    else 'inventory/inventory_transaction_form.html'
                )
                self.assertTemplateUsed(response, expected_template)
                self.assertEqual(
                    response.context['form'].fields['product'].initial,
                    self.product,
                )
                if view_name == 'inventory_adjust':
                    self.assertEqual(
                        response.context['current_quantity'],
                        self.inventory.quantity,
                    )
        
    def test_inventory_transaction_create_view(self):
        """测试创建库存交易视图"""
        # 登录
        self.client.login(username='testuser', password='12345')
        
        # 访问创建库存交易页面
        response = self.client.get(reverse('inventory_create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/inventory_form.html')
        
        # 提交创建库存交易表单
        transaction_data = {
            'product': self.product.id,
            'quantity': '50',
            'notes': '测试入库'
        }
        
        response = self.client.post(reverse('inventory_create'), transaction_data)
        
        # 验证重定向
        self.assertRedirects(response, reverse('inventory_list'))
        
        # 验证库存交易创建和库存更新
        self.assertTrue(InventoryTransaction.objects.filter(product=self.product, quantity=50).exists())
        
        # 刷新库存对象
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 150)  # 100 + 50


class MemberApiViewTest(ViewTestCase):
    """测试会员相关 API 视图"""

    def test_member_search_requires_login(self):
        """未登录用户不能通过手机号查询会员隐私信息"""
        url = reverse('member_search_by_phone', args=[self.member.phone])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])
        self.assertIn('next=', response['Location'])

    def test_member_search_returns_data_for_authenticated_user(self):
        """登录用户仍可使用会员搜索 API"""
        self.client.login(username='testuser', password='12345')
        url = reverse('member_search_by_phone', args=[self.member.phone])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['member_id'], self.member.id)
        self.assertEqual(data['member_phone'], self.member.phone)


class SaleViewTest(ViewTestCase):
    """测试销售相关视图"""
    
    def test_sale_list_view(self):
        """测试销售列表视图"""
        # 登录
        self.client.login(username='testuser', password='12345')
        
        # 访问销售列表页面
        response = self.client.get(reverse('sale_list'))
        
        # 验证响应
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/sale_list.html')
        
    def test_sale_create_view(self):
        """测试创建销售视图"""
        # 登录
        self.client.login(username='testuser', password='12345')
        
        # 访问创建销售页面
        response = self.client.get(reverse('sale_create'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'inventory/sale_form.html')
        
        response = self.client.post(reverse('sale_create'), {
            'payment_method': 'cash',
            'member': self.member.pk,
            'products[0][id]': self.product.pk,
            'products[0][quantity]': '2',
            'products[0][price]': '10.00',
        })
        sale = Sale.objects.get(member=self.member)
        self.assertRedirects(response, reverse('sale_detail', args=[sale.pk]))
        self.assertEqual(sale.final_amount, Decimal('19.00'))
        self.assertEqual(SaleItem.objects.get(sale=sale).quantity, 2)
        self.inventory.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 98)

    def test_empty_checkout_does_not_create_sale_or_change_stock(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('sale_create'), {
            'payment_method': 'cash', 'member': self.member.pk,
        })
        self.assertRedirects(response, reverse('sale_create'))
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleItem.objects.exists())
        self.assertFalse(InventoryTransaction.objects.exists())
        self.inventory.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(self.inventory.quantity, 100)
        self.assertEqual(self.member.balance, Decimal('100.00'))
        self.assertEqual(self.member.purchase_count, 0)


class BackupViewSecurityTest(TestCase):
    """备份管理视图的安全回归测试"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(
            username='backup-admin',
            password='backup-pass',
            email='backup@example.com'
        )
        self.client.force_login(self.user)

        self.temp_parent = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_parent.cleanup)
        self.backup_root = os.path.join(self.temp_parent.name, 'backups')
        self.temp_dir = os.path.join(self.temp_parent.name, 'temp')
        os.makedirs(self.backup_root, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

    def test_delete_backup_rejects_parent_directory_traversal(self):
        sentinel_path = os.path.join(self.temp_parent.name, 'keep.txt')
        with open(sentinel_path, 'w', encoding='utf-8') as sentinel:
            sentinel.write('do not delete')

        with self.settings(BACKUP_ROOT=self.backup_root, TEMP_DIR=self.temp_dir):
            response = self.client.post('/system/backup/delete/../', {'confirm': 'on'})

        self.assertRedirects(response, reverse('backup_list'))
        self.assertTrue(os.path.exists(sentinel_path))
        self.assertTrue(os.path.isdir(self.backup_root))

    def test_restore_backup_flushes_records_missing_from_snapshot(self):
        backup_name = 'snapshot'
        backup_dir = os.path.join(self.backup_root, backup_name)
        os.makedirs(backup_dir, exist_ok=True)
        db_file = os.path.join(backup_dir, 'db.json')

        with self.settings(BACKUP_ROOT=self.backup_root, TEMP_DIR=self.temp_dir):
            management.call_command(
                'dumpdata',
                '--exclude',
                'auth.permission',
                '--exclude',
                'contenttypes',
                '--exclude',
                'sessions.session',
                '--indent',
                '4',
                '--output',
                db_file,
                verbosity=0,
            )

        with open(os.path.join(backup_dir, 'backup_info.json'), 'w', encoding='utf-8') as backup_info:
            json.dump(
                {
                    'name': backup_name,
                    'created_at': '2026-05-30T11:00:00',
                    'created_by': self.user.username,
                    'includes_media': False,
                },
                backup_info,
            )

        category = Category.objects.create(name='备份后分类')
        product = Product.objects.create(
            barcode='post-backup-product',
            name='备份后商品',
            category=category,
            price=Decimal('10.00'),
            cost=Decimal('5.00'),
        )

        with self.settings(BACKUP_ROOT=self.backup_root, TEMP_DIR=self.temp_dir):
            response = self.client.post(
                reverse('restore_backup', args=[backup_name]),
                {'confirm': 'on'},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('system_settings'))
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
