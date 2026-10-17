"""
Report generation and data analysis services.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from django.db.models import Sum, Count, F, Q, Avg, ExpressionWrapper, FloatField, DecimalField
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth
from django.utils import timezone

from inventory.models import Product, Inventory, Sale, SaleItem, InventoryTransaction, Member, MemberLevel, RechargeRecord, OperationLog
from inventory.utils.date_utils import get_period_boundaries

class ReportService:
    """Service for generating reports and analyzing data."""
    
    @staticmethod
    def get_sales_by_period(start_date=None, end_date=None, period='day'):
        """
        Get sales data grouped by the specified period.
        
        Args:
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering
            period: Grouping period - 'day', 'week', or 'month'
            
        Returns:
            QuerySet: Sales data grouped by period
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
            
        from .sales_ledger import sales_periods
        return sales_periods(start_date, end_date, period)

    @staticmethod
    def get_top_selling_products(start_date=None, end_date=None, limit=10):
        """
        Get top selling products for the given period.
        
        Args:
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering
            limit: Number of products to return
            
        Returns:
            QuerySet: Top selling products
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
            
        from .sales_ledger import product_totals
        return product_totals(start_date, end_date)[:limit]

    @staticmethod
    def get_inventory_turnover_rate(start_date=None, end_date=None, category=None):
        """
        Calculate inventory turnover rate for products.
        
        Args:
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering
            category: Optional category for filtering products
            
        Returns:
            list: Inventory turnover rates for products
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
        
        # Time period in days
        days = (end_date - start_date).days or 1  # Avoid division by zero
        
        # Get current inventory levels
        inventory_query = Inventory.objects.select_related('product').all()
        
        # Filter by category if specified
        if category:
            inventory_query = inventory_query.filter(product__category=category)
            
        inventory_data = inventory_query
        
        from .sales_ledger import product_totals
        sales_map = {row['product__id']: row['total_quantity'] for row in product_totals(start_date, end_date)}

        # Calculate turnover for each product
        product_turnover = []
        for inv in inventory_data:
            sold_quantity = sales_map.get(inv.product.id, 0)
            current_quantity = inv.quantity
            
            # Calculate average inventory (simple approximation)
            # For better accuracy, we would need historical inventory records
            average_inventory = (current_quantity + sold_quantity) / 2
            
            # Calculate turnover rate (annualized)
            if average_inventory > 0:
                turnover_rate = (sold_quantity / average_inventory) * (365 / days)
                turnover_days = 365 / turnover_rate if turnover_rate > 0 else float('inf')
            else:
                turnover_rate = 0
                turnover_days = float('inf')
                
            product_turnover.append({
                'product_id': inv.product.id,
                'product_name': inv.product.name,
                'product_code': inv.product.barcode,
                'category': inv.product.category.name,
                'current_stock': current_quantity,
                'sold_quantity': sold_quantity,
                'avg_stock': average_inventory,
                'turnover_rate': turnover_rate,
                'turnover_days': turnover_days
            })
            
        # Sort by turnover rate (descending)
        product_turnover.sort(key=lambda x: x['turnover_rate'], reverse=True)
            
        return product_turnover
    
    @staticmethod
    def get_profit_report(start_date=None, end_date=None):
        """
        Generate a profit report for the given period.
        
        Args:
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering
            
        Returns:
            dict: Profit report data
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
            
        from .sales_ledger import profit_summary
        return profit_summary(start_date, end_date)

    @staticmethod
    def get_member_analysis(start_date=None, end_date=None):
        """
        获取会员分析数据
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            dict: 会员分析数据
        """
        if not start_date:
            start_date = timezone.now().date() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now().date()
            
        from .sales_ledger import order_query, return_query, period_queryset
        orders = order_query(start_date, end_date)
        returns = return_query(start_date, end_date)
        member_totals = {}
        for row in orders.values('member_id').annotate(amount=Sum('final_amount'), count=Count('id')):
            member_totals[row['member_id']] = [row['amount'] or Decimal(0), row['count']]
        for row in returns.values('sale__member_id').annotate(amount=Sum('refund_amount')):
            values = member_totals.setdefault(row['sale__member_id'], [Decimal(0), 0])
            values[0] -= row['amount'] or Decimal(0)
        top_members = []
        for member in Member.objects.filter(pk__in=[pk for pk in member_totals if pk is not None]):
            member.period_spend, member.period_purchase_count = member_totals[member.pk]
            if member.period_spend > 0:
                top_members.append(member)
        top_members.sort(key=lambda member: (-member.period_spend, member.pk))
        member_sales = {'total_amount': sum((v[0] for pk, v in member_totals.items() if pk is not None), Decimal(0)),
                        'total_count': sum(v[1] for pk, v in member_totals.items() if pk is not None)}
        non_member = member_totals.get(None, [Decimal(0), 0])
        non_member_sales = {'total_amount': non_member[0], 'total_count': non_member[1]}
        total_members = Member.objects.count()
        active_members = orders.filter(member__isnull=False).values('member_id').distinct().count()
        return {
            'level_distribution': Member.objects.values('level__name').annotate(count=Count('id')).order_by('level__points_threshold'),
            'top_members': top_members[:10],
            'new_members': period_queryset(Member.objects.all(), 'created_at', start_date, end_date).count(),
            'active_members': active_members, 'total_members': total_members,
            'activity_rate': active_members / total_members * 100 if total_members else 0,
            'member_sales': member_sales, 'non_member_sales': non_member_sales,
            'member_avg_order': member_sales['total_amount'] / member_sales['total_count'] if member_sales['total_count'] else 0,
            'non_member_avg_order': non_member_sales['total_amount'] / non_member_sales['total_count'] if non_member_sales['total_count'] else 0,
        }

    @staticmethod
    def get_recharge_report(start_date=None, end_date=None):
        """
        Generate a recharge report for the given period.
        
        Args:
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering
            
        Returns:
            dict: Recharge report data
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
            
        # 总体统计
        summary = {
            'total_recharge_amount': RechargeRecord.objects.filter(
                created_at__range=(start_date, end_date)
            ).aggregate(total=Sum('amount'))['total'] or 0,
            
            'total_recharge_count': RechargeRecord.objects.filter(
                created_at__range=(start_date, end_date)
            ).count(),
            
            'total_actual_amount': RechargeRecord.objects.filter(
                created_at__range=(start_date, end_date)
            ).aggregate(total=Sum('actual_amount'))['total'] or 0,
            
            'avg_recharge_amount': RechargeRecord.objects.filter(
                created_at__range=(start_date, end_date)
            ).aggregate(avg=Avg('amount'))['avg'] or 0,
            
            'recharged_member_count': RechargeRecord.objects.filter(
                created_at__range=(start_date, end_date)
            ).values('member').distinct().count()
        }
        
        # 按日期统计充值
        daily_recharge = RechargeRecord.objects.filter(
            created_at__range=(start_date, end_date)
        ).annotate(
            day=TruncDay('created_at')
        ).values('day').annotate(
            total_amount=Sum('amount'),
            actual_amount=Sum('actual_amount'),
            count=Count('id'),
            member_count=Count('member', distinct=True)
        ).order_by('day')
        
        # 按支付方式统计
        payment_stats = RechargeRecord.objects.filter(
            created_at__range=(start_date, end_date)
        ).values('payment_method').annotate(
            count=Count('id'),
            total_amount=Sum('amount'),
            actual_amount=Sum('actual_amount')
        ).order_by('-count')
        
        # 会员充值排行
        top_members = Member.objects.filter(
            recharge_records__created_at__range=(start_date, end_date)
        ).annotate(
            total_recharge=Sum('recharge_records__amount', 
                              filter=Q(recharge_records__created_at__range=(start_date, end_date))),
            recharge_count=Count('recharge_records', 
                                filter=Q(recharge_records__created_at__range=(start_date, end_date)))
        ).order_by('-total_recharge')[:10]
        
        return {
            'summary': summary,
            'daily_recharge': daily_recharge,
            'payment_stats': payment_stats,
            'top_members': top_members
        }
    
    @staticmethod
    def get_operation_logs(start_date=None, end_date=None):
        """
        Get operation logs for the given period.
        
        Args:
            start_date: Optional start date for filtering
            end_date: Optional end date for filtering
            
        Returns:
            QuerySet: Operation logs
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=7)
        if not end_date:
            end_date = timezone.now()
        
        # 结束日期+1天以包含当天的记录
        end_date_inclusive = end_date + timedelta(days=1)
        
        # 获取日志记录
        logs = OperationLog.objects.filter(
            timestamp__range=(start_date, end_date_inclusive)
        ).select_related('operator', 'related_content_type').order_by('-timestamp')
        
        # 按操作类型统计
        operation_type_stats = OperationLog.objects.filter(
            timestamp__range=(start_date, end_date_inclusive)
        ).values('operation_type').annotate(
            count=Count('id')
        ).order_by('-count')
        
        # 按操作员统计
        operator_stats = OperationLog.objects.filter(
            timestamp__range=(start_date, end_date_inclusive)
        ).values('operator__username').annotate(
            count=Count('id')
        ).order_by('-count')
        
        return {
            'logs': logs,
            'operation_type_stats': operation_type_stats,
            'operator_stats': operator_stats
        } 