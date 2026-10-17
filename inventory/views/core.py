"""
核心视图模块
包含首页和仪表盘等核心功能
"""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Avg
from django.utils import timezone
from datetime import timedelta

from inventory.models import (
    Product, Inventory, Sale, SaleItem, 
    Member, InventoryTransaction, OperationLog
)


@login_required
def index(request):
    """系统首页/仪表盘视图"""
    # 获取系统概览统计
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    
    # 商品统计
    total_products = Product.objects.filter(is_active=True).count()
    active_inventory = Inventory.objects.filter(product__is_active=True)
    total_inventory_quantity = active_inventory.aggregate(total=Sum('quantity', default=0))['total']
    active_products = total_products
    low_stock_products = active_inventory.filter(quantity__lte=10).count()
    out_of_stock_products = active_inventory.filter(quantity=0).count()
    
    # 销售统计
    completed_sales = Sale.objects.filter(status='COMPLETED')
    total_sales = completed_sales.count()
    today_sales = completed_sales.filter(created_at__date=today).count()
    from inventory.services.sales_ledger import sales_summary, product_totals
    today_summary = sales_summary(today, today)
    today_sales_amount = today_summary['total_sales']
    yesterday_sales = completed_sales.filter(created_at__date=yesterday).count()
    yesterday_sales_amount = sales_summary(yesterday, yesterday)['total_sales']

    # 会员统计
    total_members = Member.objects.count()
    active_members = total_members
    new_members_month = Member.objects.filter(created_at__gte=month_ago).count()
    
    # 近期销售走势
    sales_trend = []
    for i in range(7):
        date = today - timedelta(days=i)
        daily_sales = sales_summary(date, date)['total_sales']
        sales_trend.append({
            'date': date.strftime('%m-%d'),
            'amount': float(daily_sales)
        })
    sales_trend.reverse()
    
    # 热销商品
    top_products = [dict(row, total_qty=row['total_quantity'], total_amount=row['total_sales'])
                    for row in product_totals(week_ago, today)[:5]]

    # 最近操作日志
    recent_logs = OperationLog.objects.all().order_by('-timestamp')[:10]
    
    # 获取当月生日会员
    current_month = today.month
    birthday_members = Member.objects.filter(
        birthday__isnull=False,  # 确保生日字段不为空
        birthday__month=current_month,
        is_active=True
    ).order_by('birthday__day')[:10]
    
    context = {
        'total_products': total_products,
        'total_inventory_quantity': total_inventory_quantity,
        'active_products': active_products,
        'low_stock_products': low_stock_products,
        'out_of_stock_products': out_of_stock_products,
        'total_sales': total_sales,
        'today_sales': today_sales,
        'today_sales_amount': today_sales_amount,
        'sales_summary': today_summary,
        'yesterday_sales': yesterday_sales,
        'yesterday_sales_amount': yesterday_sales_amount,
        'total_members': total_members,
        'active_members': active_members,
        'new_members_month': new_members_month,
        'sales_trend': sales_trend,
        'top_products': top_products,
        'recent_logs': recent_logs,
        'birthday_members': birthday_members,
        'current_month': current_month,
    }
    
    return render(request, 'inventory/index.html', context)


@login_required
def reports_index(request):
    """报表首页视图"""
    return render(request, 'inventory/reports/index.html')
