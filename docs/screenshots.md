# 界面预览

> **以下均为离线演示截图：Team 名、邮箱、金额、卡号都是虚构数据，不代表真实工作区、实际价格或生产验证结果。**

[返回首页](../README.md) · [运行离线演示](development.md)

用户管理：列出所有 Team 的成员和待接受邀请，一行看完所属 Team、状态、到期、席位类型和 Telegram 绑定。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/users-dark.png">
  <img alt="用户管理：成员表，每行显示成员、所属 Team / Owner、状态、加入时间、到期、按颜色区分的席位类型和 TG 绑定" src="images/users.png">
</picture>

查看账单：Team 卡片付款卡那一行的「查看账单」图标，打开这个 Team 已同步的 Stripe 账单，只读。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/team-billing-dark.png">
  <img alt="账单对话框：顶部是累计实付、近 30 天实付、最新一期三个汇总，下方是逐期账单表，列出账期、状态、应付、实付、说明和跳到 Stripe 发票的链接" src="images/team-billing.png">
</picture>

财务总览：各队订阅、折扣、余额按基准币种汇总，带支出趋势；月付和年付分别按月均展示，并列出年付全年金额及整期续费金额。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/finance-dark.png">
  <img alt="财务总览：顶部是月预计支出、折扣共省、30 天内续费、预警四个指标卡，下方是可切换时间范围的支出趋势折线图" src="images/finance.png">
</picture>

一次性兑换码：发给成员自助加入 / 续期，分 ChatGPT 码和 Premium 码，完整兑换码只在生成时显示一次。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/access-tokens-dark.png">
  <img alt="兑换码页面：兑换码列表，每行显示兑换码前缀、席位类型、授予时长、状态（未使用 / 已使用 / 已过期 / 已停用）、备注、兑换截止和兑换时间" src="images/access-tokens.png">
</picture>

巡逻管理：可先演练查看待移除名单，再决定是否激活自动移除。完整规则见[上手指南](getting-started.md#42-巡逻自动踢人--它会真的把人移出你的工作区)。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/patrol-dark.png">
  <img alt="TG 与巡逻页面的「巡逻」标签页：自动踢人开关和状态、「演练空跑」按钮，以及按 Team 排列、可点击切换豁免的芯片，芯片用颜色区分已豁免、观察和超员风险" src="images/patrol.png">
</picture>

成员自助页（部署地址的根路径）：填邮箱 + 兑换码即可加入或续期，也能只填邮箱查自己的到期。

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/self-service-dark.png">
  <img alt="成员自助页的查询标签页：填入邮箱后显示加入状态、所属 Team 和到期时间" src="images/self-service.png">
</picture>
