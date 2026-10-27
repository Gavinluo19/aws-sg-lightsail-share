# 来源与版本

核对日期为 2026 年 10 月 25 日。价格、软件、赠金资格和服务条款可能变化，实际部署前重新读回。

## AWS

- [Lightsail 公网 IPv4 与 IPv6 套餐表](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)
- [Free plan 与 Paid plan](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html)
- [Free Tier 常见问题](https://aws.amazon.com/free/free-tier-faqs/)
- [Lightsail 计费与免费试用说明](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html)
- [Lightsail 传输额度和超额费用](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-data-transfer-allowance.html)
- [静态 IP](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-static-ip-addresses-in-amazon-lightsail.html)
- [Lightsail 防火墙](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html)
- [AWS CLI 安装](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- [AWS CLI 浏览器登录](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html)
- [AWS Lightsail 控制台](https://lightsail.aws.amazon.com/)，登录后选择 `ap-southeast-1`。
- [AWS Lightsail 区域与可用区说明](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html)

## 软件

- [Clash Verge Rev 官方发行页](https://github.com/clash-verge-rev/clash-verge-rev/releases)
- [Mihomo 官方文档](https://wiki.metacubex.one/)
- [Shadowrocket 正版 App Store 页面](https://apps.apple.com/app/id932747118)，价格与可用性以自己的商店区域为准。
- [sing-box 官方发布](https://github.com/SagerNet/sing-box/releases/tag/v1.14.4)
- [sing-box 安装说明](https://sing-box.sagernet.org/installation/package-manager/)
- [sing-box Hysteria2 服务端](https://sing-box.sagernet.org/configuration/inbound/hysteria2/)
- [sing-box Trojan 服务端](https://sing-box.sagernet.org/configuration/inbound/trojan/)
- [Certbot](https://certbot.eff.org/)
- [Let's Encrypt 的 IP 证书和 Certbot 说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)

参考实现锁定 sing-box 1.14.4、Certbot 5.8.0。Linux 下载包的 SHA256 已从官方 GitHub release 元数据核对并写入代码。协议配置在交付前使用对应版本的官方程序校验；升级版本时需要重新验证，不能只替换版本号。

## 启发来源

- [余温的海外基础设施经验](https://x.com/gkxspace/status/2101993381303820704)
- [逸尘的 Claude 使用环境教程](https://x.com/gengdaJ/status/2108363527971041650)

两篇内容的相关网络章节已通过已登录浏览器读取。这里鸣谢公开分享与方法启发，不表示两位参与了代码审计、为本部署包背书或认可了本文所有判断。

## 辅助检测

- [IPPure Claude 环境检测](https://ippure.com/claude)
- [Claude 官方支持地区](https://support.claude.com/en/articles/8461763-where-can-i-access-claude)

IPPure 的检测说明及分数属于第三方材料；中文、字体或时区信号与封号之间的具体因果和阈值不能仅凭该页面认定。应把可验证的网络泄漏与平台政策分开判断。
