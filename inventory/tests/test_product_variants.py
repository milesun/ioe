from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse

from inventory.models import Category, Inventory, InventoryTransaction, OperationLog, Product
from inventory.services.product_service import create_products_with_stock


GIF = (
    b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff'
    b'!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00'
    b'\x00\x02\x02D\x01\x00;'
)


@override_settings(ALLOWED_HOSTS=['testserver'])
class ProductVariantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='variant-clerk', password='test-password')
        cls.user.user_permissions.add(Permission.objects.get(
            content_type__app_label='inventory', codename='change_inventory',
        ))
        cls.viewer = User.objects.create_user(username='variant-viewer')
        cls.category = Category.objects.create(name='上衣')

    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse('product_create')

    def bulk_data(self):
        return {
            'mode': 'bulk', 'name': '秋季外套', 'category': self.category.pk,
            'color': 'black', 'price': '100.00', 'cost': '50.00', 'warning_level': '0',
            'selected_sizes': ['M', 'L'],
            'variants-TOTAL_FORMS': '2', 'variants-INITIAL_FORMS': '0',
            'variants-0-size': 'M', 'variants-0-barcode': 'COAT-M', 'variants-0-initial_stock': '3',
            'variants-1-size': 'L', 'variants-1-barcode': 'COAT-L', 'variants-1-initial_stock': '0',
        }

    def assert_no_creation(self):
        for model in (Product, Inventory, InventoryTransaction, OperationLog):
            self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_create_pages_render_and_edit_has_no_initial_stock(self):
        self.assertContains(self.client.get(self.url), 'name="initial_stock"')
        self.assertContains(self.client.get(self.url + '?mode=bulk'), 'name="selected_sizes"')
        product = Product.objects.create(name='原商品', barcode='ORIGINAL', category=self.category, price=10, cost=5)
        response = self.client.get(reverse('product_edit', args=[product.pk]))
        self.assertNotContains(response, 'name="initial_stock"')
        self.assertNotContains(response, '批量新增')

    def test_batch_creates_independent_products_stock_and_operator_logs(self):
        response = self.client.post(self.url, self.bulk_data())
        self.assertRedirects(response, reverse('product_list'))
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(list(Inventory.objects.order_by('product__size').values_list('quantity', flat=True)), [0, 3])
        self.assertEqual(list(Inventory.objects.values_list('warning_level', flat=True)), [0, 0])
        record = InventoryTransaction.objects.get()
        self.assertEqual((record.product.size, record.quantity, record.transaction_type, record.operator_id), ('M', 3, 'IN', self.user.pk))
        self.assertEqual(OperationLog.objects.filter(operator=self.user, operation_type='OTHER').count(), 2)
        self.assertEqual(OperationLog.objects.filter(operator=self.user, operation_type='INVENTORY').count(), 1)
        self.assertTrue(all(product.color == 'black' and product.name == '秋季外套' for product in Product.objects.all()))
        # 重复点击或刷新重发不能重复创建或增加库存。
        response = self.client.post(self.url, self.bulk_data())
        self.assertContains(response, '该条码已存在')
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(InventoryTransaction.objects.count(), 1)

    def test_single_creation_also_records_initial_stock(self):
        data = {key: value for key, value in self.bulk_data().items() if not key.startswith('variants-')}
        data.update(mode='single', barcode='SINGLE', size='S', initial_stock='2')
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Inventory.objects.get().quantity, 2)
        self.assertEqual(InventoryTransaction.objects.get().quantity, 2)

    def test_no_stock_permission_cannot_forge_single_or_bulk_quantity(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url + '?mode=bulk')
        self.assertContains(response, 'readonly')
        response = self.client.post(self.url, self.bulk_data())
        self.assertContains(response, '您没有初始入库权限')
        self.assert_no_creation()
        data = self.bulk_data()
        data.update(mode='single', barcode='SINGLE', initial_stock='1')
        self.assertContains(self.client.post(self.url, data), '您没有初始入库权限')
        self.assert_no_creation()
        data['initial_stock'] = '0'
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Inventory.objects.get().quantity, 0)
        self.assertFalse(InventoryTransaction.objects.exists())
        data = self.bulk_data() | {'variants-0-initial_stock': '0'}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Product.objects.count(), 3)
        self.assertTrue(all(quantity == 0 for quantity in Inventory.objects.values_list('quantity', flat=True)))
        self.assertFalse(InventoryTransaction.objects.exists())

    def test_invalid_batch_is_not_partially_saved_and_keeps_input(self):
        changes = [
            {'variants-1-barcode': ' COAT-M '},
            {'variants-1-initial_stock': '-1'},
            {'variants-1-initial_stock': '1.5'},
            {'variants-1-size': 'M'},
            {'selected_sizes': ['S']},
            {'price': ''},
            {'cost': 'bad-number'},
            {'variants-1-barcode': ''},
            {'selected_sizes': []},
            {'variants-TOTAL_FORMS': '0'},
        ]
        for change in changes:
            with self.subTest(change=change):
                response = self.client.post(self.url, self.bulk_data() | change)
                self.assertContains(response, '保存失败')
                self.assertContains(response, 'value="秋季外套"')
                self.assertEqual(response.context['form']['color'].value(), 'black')
                self.assert_no_creation()

    def test_existing_barcode_blocks_entire_batch(self):
        Product.objects.create(name='已存在', barcode='COAT-L', category=self.category, price=1, cost=0)
        self.assertContains(self.client.post(self.url, self.bulk_data()), '该条码已存在')
        self.assertEqual(Product.objects.count(), 1)
        self.assertFalse(Inventory.objects.exists())

    def test_database_unique_conflict_rolls_back_first_product(self):
        original = Product.save
        def save_with_conflict(product, *args, **kwargs):
            if product.barcode == 'COAT-L':
                raise IntegrityError('simulated concurrent barcode conflict')
            return original(product, *args, **kwargs)
        with patch.object(Product, 'save', save_with_conflict):
            self.assertContains(self.client.post(self.url, self.bulk_data()), '条码已存在或已被其他人使用')
        self.assert_no_creation()

    def test_failed_initial_receipt_rolls_back_all_records(self):
        from inventory.models.inventory import update_inventory
        def receive(product, *args, **kwargs):
            if product.size == 'L':
                return False, None, 'simulated failure'
            return update_inventory(product, *args, **kwargs)
        data = self.bulk_data() | {'variants-1-initial_stock': '2'}
        with patch('inventory.services.product_service.update_inventory', side_effect=receive):
            self.assertContains(self.client.post(self.url, data), '初始入库失败')
        self.assert_no_creation()

    def test_image_shared_once_and_failed_batch_removes_only_new_upload(self):
        Path('temp').mkdir(exist_ok=True)
        with TemporaryDirectory(dir='temp') as directory, override_settings(MEDIA_ROOT=directory):
            existing = Path(directory) / 'existing.txt'
            existing.write_text('preserve', encoding='utf-8')
            data = self.bulk_data() | {'image': SimpleUploadedFile('coat.gif', GIF, content_type='image/gif')}
            with patch('inventory.services.product_service.OperationLog.objects.create', side_effect=IntegrityError('failed audit')):
                self.assertContains(self.client.post(self.url, data), '本次均未保存')
            self.assert_no_creation()
            self.assertEqual([path.name for path in Path(directory).rglob('*') if path.is_file()], ['existing.txt'])
            data['image'] = SimpleUploadedFile('coat.gif', GIF, content_type='image/gif')
            self.assertEqual(self.client.post(self.url, data).status_code, 302)
            self.assertEqual(len(set(Product.objects.values_list('image', flat=True))), 1)
            self.assertEqual(len(list(Path(directory).rglob('*.gif'))), 1)

    def test_service_checks_permission_even_without_forms(self):
        with self.assertRaises(ValidationError):
            create_products_with_stock({}, [{'barcode': 'BYPASS', 'size': 'M', 'initial_stock': 1}], self.viewer)
        self.assert_no_creation()

    def test_no_size_variant_is_supported(self):
        data = self.bulk_data() | {
            'selected_sizes': [''], 'variants-TOTAL_FORMS': '1', 'variants-0-size': '',
        }
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Product.objects.get().size, '')
