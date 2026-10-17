# 退换货功能说明

## 如何使用

销售员和超级管理员可以办理退换货。普通登录账号没有办理权限。

1. 打开“销售管理”，进入一张已完成的销售单。
2. 点击“办理退换货”，填写退回数量和原因。
3. 换货时选择换出商品，填写数量。退货时将换出商品留空。
4. 选择线下实际收退款渠道。需要补款时，可以选择线下支付或会员余额。
5. 点击“预览收退款”，核对金额、积分和会员余额变化。
6. 收回可再次销售的商品，完成线下收退款，勾选确认后完成售后。

现金、微信、支付宝和银行卡由店员在线下收退款，系统只记账。
会员余额由系统自动调整。首版退回商品全部恢复可售库存，不受理残次品入库。

## 金额和记录

退货按原销售单折后实付金额计算。系统将原实付分摊到商品行，分次退货按累计数量计算尾差。
全部退完时，累计退款等于原实付，积分扣回等于原赠送积分。积分可以扣成负数。

换出商品使用当前售价和会员当前折扣。退回价值先抵扣新销售单，顾客只补交或收回差价。
原余额来源先用于抵扣，未用完的余额退回会员账户；其余退款在线下完成。
换货新单保存余额来源，之后再退货时仍能正确分配退款。

原销售单和数量保留不变。退货另存售后单，换货还会生成关联新销售单。
已完成的售后单没有编辑、删除或撤销入口。

历史记录可以从四处查看：侧栏“退换货记录”、原销售单详情、库存流水和会员详情。
售后详情显示办理时的商品快照。商品改名或改价后，快照不会改变。
可以从售后单跳转到原单和换货新单，也可以沿新单继续查找后续售后。

销售按销售日期统计，退回金额按售后日期扣减。净销售额为销售额减退回金额。
换货新单正常计入销售，抵扣金额不重复计入收入。利润仍使用项目现有商品成本口径。

## 主要文件及职责

| 文件或目录 | 修改内容 |
| --- | --- |
| `inventory/models/aftersales.py` | 新增售后单和退回明细，保存金额、来源和商品快照。 |
| `inventory/models/sales.py` | 增加并发版本、换货抵扣金额和售后标记；换出库存关联售后单。 |
| `inventory/models/inventory.py` | 库存流水增加售后单关联，库存更新函数接受该关联。 |
| `inventory/models/__init__.py` | 导出新增模型。 |
| `inventory/services/aftersales_service.py` | 报价、分摊、库存、余额、积分、并发和重复提交校验。 |
| `inventory/services/sales_ledger.py` | 按销售和售后发生日期统一统计净销售额、数量和成本。 |
| `inventory/services/report_service.py` | 销售趋势、商品排行、周转、利润及会员统计计入售后。 |
| `inventory/services/export_service.py` | 会员导出使用相同净消费统计，修正会员对象字段读取。 |
| `inventory/forms/aftersales_forms.py` | 退回数量、原因、渠道和换出商品表单。 |
| `inventory/views/aftersales.py` | 办理、签名确认、历史筛选、分页和详情。 |
| `inventory/views/sales.py` | 原单售后历史、可退数量及销售列表净销售统计。 |
| `inventory/views/inventory.py` | 支持按售后单筛选库存流水。 |
| `inventory/views/member.py` | 会员售后记录和净消费统计。 |
| `inventory/views/core.py` | 首页销售趋势和商品排行扣减售后。 |
| `inventory/views_report.py` | 利润页面接入统一汇总及分期数据。 |
| `inventory/permissions/__init__.py` | 定义售后查看、办理权限。 |
| `inventory/views/system/user.py` | 新建销售员组时包含售后权限。 |
| `inventory/urls.py` | 新增办理、历史列表和详情路由。 |
| `inventory/templates/inventory/aftersales/` | 新增办理、确认、列表、详情及历史展示组件。 |
| `inventory/templates/inventory/base.html` | 增加侧栏“退换货记录”。 |
| `inventory/templates/inventory/sale_list.html`、`sale_detail.html` | 显示真实状态、售后标记、已退数量和关联单。 |
| `inventory/templates/inventory/inventory_transaction_list.html` | 售后流水筛选及单据链接。 |
| `inventory/templates/inventory/member/member_detail.html` | 会员售后记录及退款流水链接。 |
| `inventory/templates/inventory/index.html` | 展示销售额、退回金额和净销售额。 |
| `inventory/templates/inventory/reports/profit.html`、`sales_trend.html` | 利润汇总和销售趋势展示净额。 |
| `inventory/migrations/0012_…py` | 新增售后结构，并给销售员组授予查看和办理权限。 |
| `inventory/tests/test_aftersales.py` | 售后结算、权限、历史、统计、并发及回滚测试。 |
| `inventory/tests/test_business_integrity.py` | 日志下载测试关闭响应，避免 Windows 文件清理失败。 |

## 运行和迁移

本功能没有新增依赖。使用已经安装项目依赖的 Python 环境。
本机验证使用 `py312-common` Conda 环境；默认的 `python` 环境没有安装 Django。

```powershell
conda activate py312-common
$env:PYTHONUTF8 = "1"
python manage.py migrate inventory 0012 --plan
python manage.py migrate inventory 0012
python manage.py runserver
```

迁移前先备份当前环境的数据库。SQLite 应使用连接的 `backup()` 方法，避免运行中直接复制遗漏日志中的数据。
本地数据库和 Docker 数据卷是不同的数据源，在 Docker 部署时需要对容器实际使用的数据库执行迁移。
更新代码和迁移后，重启原有服务。

本次迁移不改写原销售金额、销售数量、库存和会员余额。
历史上通过手动入库或备注处理的退货不会自动生成售后单。
已有商品尺码选项与旧迁移之间存在差异，`makemigrations --check` 仍可能提示尺码变更；本次未包含这项无关变更。

## 如何验证

测试使用临时数据库，不操作业务数据。

```powershell
$env:PYTHONUTF8 = "1"
python manage.py test inventory.tests.test_aftersales --settings=inventory.test_settings --noinput
python manage.py test --settings=inventory.test_settings --noinput
```

页面验收可以使用测试商品：

1. 新建一张包含两件各 100 元商品的销售单。
2. 退回其中一件，核对库存增加一件、退款 100 元、原销售明细仍是两件。
3. 将剩下一件换成 120 元商品，核对实际补收 20 元，并生成关联新单。
4. 从退换货记录、原销售单、库存流水和会员详情查到售后。
5. 重复提交确认页面，核对不重复退款、入库或生成新单。
6. 更改商品名称和售价，核对售后详情仍显示办理时的信息。

出现 `no such table: inventory_aftersale` 或“没有售后权限”时，先检查是否对当前服务实际连接的数据库执行了迁移。
出现“原订单售后记录已变化”时，重新预览剩余可退数量。
出现“价格、会员或可退数量发生变化”时，核对最新报价再确认。
出现 `database is locked` 时，本次数据库事务会回滚；稍后重新预览，不要重复在线下退款。
