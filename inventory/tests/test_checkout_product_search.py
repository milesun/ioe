from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from inventory.models import Category, Inventory, Product


class CheckoutProductSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='variant-cashier')
        cls.category = Category.objects.create(name='收银测试分类')
        cls.black_shirt = Product.objects.create(
            name='同款衬衫', barcode='SHIRT-BLACK-2XL', category=cls.category,
            price=Decimal('99.00'), cost=Decimal('50.00'),
            size='2XL', color='black', specification='长袖', manufacturer='测试厂商',
        )
        cls.blue_shirt = Product.objects.create(
            name='同款衬衫', barcode='SHIRT-BLUE-M', category=cls.category,
            price=Decimal('89.00'), cost=Decimal('40.00'),
            size='M', color='blue', specification='短袖',
        )
        Inventory.objects.create(product=cls.black_shirt, quantity=12)
        Inventory.objects.create(product=cls.blue_shirt, quantity=5)
        cls.plain = Product.objects.create(
            name='空属性商品', barcode='PLAIN-001', category=cls.category,
            price=Decimal('10.00'), cost=Decimal('5.00'),
        )
        cls.legacy = Product.objects.create(
            name='旧尺码商品', barcode='LEGACY-001', category=cls.category,
            price=Decimal('20.00'), cost=Decimal('10.00'),
            size='XXL', color='red',
        )

    def setUp(self):
        self.client.force_login(self.user)

    def assert_shirt_attributes(self, data, product):
        self.assertEqual(data['name'], product.name)
        self.assertEqual(data['barcode'], product.barcode)
        self.assertEqual(data['size'], product.size)
        self.assertEqual(data['size_display'], product.size)
        self.assertEqual(data['color'], product.color)
        self.assertEqual(data['color_display'], '黑色' if product.color == 'black' else '蓝色')
        self.assertEqual(data['price'], float(product.price))
        self.assertEqual(data['stock'], 12 if product.color == 'black' else 5)
        self.assertEqual(data['category'], self.category.name)

    def test_exact_barcode_returns_attributes_on_both_routes(self):
        for route in ('product_by_barcode', 'product_search_by_barcode'):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[self.black_shirt.barcode]))
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertTrue(data['success'])
                self.assertFalse(data['multiple_matches'])
                self.assertEqual(data['product_id'], self.black_shirt.pk)
                self.assertEqual(data['specification'], '长袖')
                self.assertEqual(data['manufacturer'], '测试厂商')
                self.assert_shirt_attributes(data, self.black_shirt)

    def test_single_fuzzy_match_returns_attributes(self):
        response = self.client.get(reverse('product_search_by_barcode', args=['SHIRT-BLACK']))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['multiple_matches'])
        self.assertEqual(data['product_id'], self.black_shirt.pk)
        self.assertEqual(data['specification'], '长袖')
        self.assert_shirt_attributes(data, self.black_shirt)

    def test_multiple_matches_distinguish_same_name_variants(self):
        response = self.client.get(reverse('product_search_by_barcode', args=['同款衬衫']))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['multiple_matches'])
        products = {item['product_id']: item for item in data['products']}
        self.assertEqual(set(products), {self.black_shirt.pk, self.blue_shirt.pk})
        for product in (self.black_shirt, self.blue_shirt):
            with self.subTest(product=product.barcode):
                self.assert_shirt_attributes(products[product.pk], product)
                self.assertEqual(products[product.pk]['specification'], product.specification)

    def test_name_search_keeps_existing_fields_and_adds_attributes(self):
        for query in ('同款衬衫', 'SHIRT-', '长袖'):
            with self.subTest(query=query):
                response = self.client.get(reverse('product_search_api'), {'query': query})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertTrue(data['success'])
                products = {item['id']: item for item in data['products']}
                expected = (self.black_shirt,) if query == '长袖' else (self.black_shirt, self.blue_shirt)
                self.assertEqual(set(products), {product.pk for product in expected})
                self.assertEqual(data['count'], len(expected))
                for product in expected:
                    self.assert_shirt_attributes(products[product.pk], product)
                    self.assertEqual(products[product.pk]['spec'], product.specification)

    def test_empty_attributes_and_missing_inventory_do_not_create_stock(self):
        for route in ('product_search_by_barcode', 'product_search_api'):
            with self.subTest(route=route):
                if route == 'product_search_api':
                    response = self.client.get(reverse(route), {'query': self.plain.name})
                    data = response.json()['products'][0]
                else:
                    response = self.client.get(reverse(route, args=[self.plain.barcode]))
                    data = response.json()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(data['size'], '')
                self.assertEqual(data['size_display'], '无尺码')
                self.assertEqual(data['color'], '')
                self.assertEqual(data['color_display'], '无颜色')
                self.assertEqual(data['stock'], 0)
        self.assertFalse(Inventory.objects.filter(product=self.plain).exists())

    def test_old_size_remains_readable(self):
        response = self.client.get(reverse('product_search_by_barcode', args=[self.legacy.barcode]))
        data = response.json()
        self.assertEqual(data['size'], 'XXL')
        self.assertEqual(data['size_display'], 'XXL')
        self.assertEqual(data['color_display'], '红色')
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.size, 'XXL')

    def test_empty_and_not_found_search_responses_are_unchanged(self):
        response = self.client.get(reverse('product_search_api'), {'query': 'x'})
        self.assertFalse(response.json()['success'])
        response = self.client.get(reverse('product_search_by_barcode', args=['NOT-FOUND']))
        self.assertFalse(response.json()['success'])
