"""
商品相关业务服务
提供商品管理相关的业务逻辑处理
"""
import csv
import io
import logging
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db import transaction
from django.db.models import Q

from inventory.models import Product, Category, ProductImage, ProductBatch, Inventory, OperationLog
from inventory.models.inventory import update_inventory


logger = logging.getLogger(__name__)


def create_products_with_stock(common_data, variants, operator, additional_images=()):
    """一次提交的商品、初始入库和日志全部成功后才提交。图片只保存一份。"""
    if not operator.is_authenticated or not operator.is_active:
        raise ValidationError('请使用有效账户创建商品')
    if not variants:
        raise ValidationError('请至少填写一个商品')
    quantities = [row['initial_stock'] for row in variants]
    if any(type(quantity) is not int or not 0 <= quantity <= 2147483647 for quantity in quantities):
        raise ValidationError('初始库存必须是非负整数')
    if any(quantities) and not operator.has_perm('inventory.change_inventory'):
        raise ValidationError('您没有初始入库权限，请将初始库存设为 0')
    for field, label in [('barcode', '条码'), ('size', '尺码')]:
        if len({row[field] for row in variants}) != len(variants):
            raise ValidationError(f'本次提交的{label}不能重复')

    fields = ('name', 'category', 'color', 'description', 'price', 'cost', 'specification', 'manufacturer')
    shared = {field: common_data[field] for field in fields if field in common_data}
    products = [Product(**shared, barcode=row['barcode'], size=row['size'], is_active=True) for row in variants]
    warning_level = common_data.get('warning_level')
    if warning_level is None:
        warning_level = 10
    for product in products:
        product.full_clean()
        Inventory(product=product, quantity=0, warning_level=warning_level).full_clean(exclude=['product'])

    # 文件存储不属于数据库事务，失败时单独清理本次新上传的文件。
    saved_files = []
    try:
        with transaction.atomic():
            upload = common_data.get('image')
            if upload:
                image_field = Product._meta.get_field('image')
                image_path = image_field.storage.save(
                    image_field.generate_filename(products[0], upload.name), upload,
                    max_length=image_field.max_length,
                )
                saved_files.append((image_field.storage, image_path))
                for product in products:
                    product.image = image_path

            product_type = ContentType.objects.get_for_model(Product)
            stock_type = ContentType.objects.get_for_model(Inventory)
            for product, quantity in zip(products, quantities):
                product.save()
                stock = Inventory.objects.create(product=product, quantity=0, warning_level=warning_level)
                identity = f'{product.name} | {product.get_color_display()} | {product.get_size_display()} | {product.barcode}'
                OperationLog.objects.create(
                    operator=operator, operation_type='OTHER', details=f'新增商品：{identity}',
                    related_object_id=product.pk, related_content_type=product_type,
                )
                if quantity:
                    success, _, _ = update_inventory(product, quantity, 'IN', operator, notes='新增商品：初始入库')
                    if not success:
                        raise ValidationError('初始入库失败，本次商品均未保存，请重试')
                    OperationLog.objects.create(
                        operator=operator, operation_type='INVENTORY',
                        details=f'新增商品初始入库：{identity}，数量：{quantity}',
                        related_object_id=stock.pk, related_content_type=stock_type,
                    )

            for image in additional_images:
                image.product = products[0]
                if image.image and not image.image._committed:
                    image.image.save(image.image.name, image.image.file, save=False)
                    saved_files.append((image.image.storage, image.image.name))
                image.save()
        return products
    except Exception:
        for storage, path in saved_files:
            try:
                storage.delete(path)
            except Exception:
                logger.exception('清理新增商品图片失败：%s', path)
        raise


def import_products_from_csv(csv_file, user):
    """从CSV文件导入商品"""
    result = {
        'success': 0,
        'skipped': 0,
        'failed': 0,
        'failed_rows': []
    }
    
    # 读取CSV文件
    decoded_file = csv_file.read().decode('utf-8')
    csv_data = csv.reader(io.StringIO(decoded_file))
    headers = next(csv_data)  # 获取表头
    
    # 验证必要的表头
    required_headers = ['name', 'retail_price']
    missing_headers = [h for h in required_headers if h not in headers]
    if missing_headers:
        raise ValueError(f"CSV文件缺少必要的表头: {', '.join(missing_headers)}")
    
    # 获取各列索引
    headers_lower = [h.lower() for h in headers]
    name_idx = headers_lower.index('name')
    retail_price_idx = headers_lower.index('retail_price')
    
    # 可选列的索引
    category_idx = headers_lower.index('category') if 'category' in headers_lower else -1
    wholesale_price_idx = headers_lower.index('wholesale_price') if 'wholesale_price' in headers_lower else -1
    cost_price_idx = headers_lower.index('cost_price') if 'cost_price' in headers_lower else -1
    barcode_idx = headers_lower.index('barcode') if 'barcode' in headers_lower else -1
    sku_idx = headers_lower.index('sku') if 'sku' in headers_lower else -1
    specification_idx = headers_lower.index('specification') if 'specification' in headers_lower else -1
    
    # 处理每一行数据
    for row_num, row in enumerate(csv_data, start=2):  # 从2开始，因为1是表头
        try:
            if not row or len(row) < len(required_headers):
                result['skipped'] += 1
                continue
            
            # 解析基本信息
            name = row[name_idx].strip()
            if not name:
                result['failed'] += 1
                result['failed_rows'].append((row_num, "商品名称不能为空"))
                continue
            
            # 解析价格
            try:
                retail_price = float(row[retail_price_idx].replace(',', ''))
                if retail_price < 0:
                    result['failed'] += 1
                    result['failed_rows'].append((row_num, "零售价不能为负数"))
                    continue
            except (ValueError, IndexError):
                result['failed'] += 1
                result['failed_rows'].append((row_num, "零售价格式不正确"))
                continue
            
            # 检查商品是否已存在
            existing_product = None
            if barcode_idx >= 0 and row[barcode_idx]:
                barcode = row[barcode_idx].strip()
                existing_product = Product.objects.filter(barcode=barcode).first()
                if existing_product:
                    result['skipped'] += 1
                    result['failed_rows'].append((row_num, f"条码 {barcode} 已存在"))
                    continue
            
            # 解析分类
            category = None
            if category_idx >= 0 and row[category_idx]:
                category_name = row[category_idx].strip()
                category, _ = Category.objects.get_or_create(name=category_name)
            
            # 创建商品
            with transaction.atomic():
                product = Product.objects.create(
                    name=name,
                    category=category,
                    price=retail_price,
                    cost=float(row[cost_price_idx]) if cost_price_idx >= 0 and row[cost_price_idx] else retail_price * 0.7,
                    barcode=row[barcode_idx].strip() if barcode_idx >= 0 and row[barcode_idx] else None,
                    specification=row[specification_idx].strip() if specification_idx >= 0 and row[specification_idx] else "",
                    created_by=user
                )
                
                # 创建初始库存记录
                Inventory.objects.create(
                    product=product,
                    quantity=0,
                    warning_level=5
                )
                
                result['success'] += 1
                
        except Exception as e:
            result['failed'] += 1
            result['failed_rows'].append((row_num, str(e)))
            
    return result


def search_products(query, category_id=None, active_only=True):
    """搜索商品"""
    products = Product.objects.select_related('category').all()
    
    if query:
        products = products.filter(
            Q(name__icontains=query) | 
            Q(barcode__icontains=query) |
            Q(specification__icontains=query)
        )
    
    if category_id:
        products = products.filter(category_id=category_id)
    
    if active_only:
        products = products.filter(is_active=True)
    
    return products.order_by('name')


def get_product_with_inventory(product_id):
    """获取商品及其库存信息"""
    try:
        product = Product.objects.get(id=product_id)
        inventory = Inventory.objects.get(product=product)
        return {
            'product': product,
            'inventory': inventory
        }
    except (Product.DoesNotExist, Inventory.DoesNotExist):
        return None
