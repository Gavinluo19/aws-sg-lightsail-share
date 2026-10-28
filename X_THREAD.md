# X 串文 · 已发布存档

## 已发布（稳定链接）

- Article：https://x.com/gavinluo19/status/2114674868096835661
- 1 主帖：https://x.com/gavinluo19/status/2114675668390642033
- 2 实测：https://x.com/gavinluo19/status/2114675858304844226
- 3 风险分：https://x.com/gavinluo19/status/2114675890508398831
- 4 开源收束：https://x.com/gavinluo19/status/2114675930756968775
- 实测图补发：https://x.com/gavinluo19/status/2114882145256935844

正式仓：https://github.com/Gavinluo19/aws-sg-lightsail-share  
（发稿时「方案」曾链到 ai-model-picker staging 路径，现已迁到本仓。）

---

# 下方为发稿前建议稿存档

# X 串文建议稿

每段单独发一条，顺序回复前一条。正式发布前再核对下载链接和最新测试成绩。本轮未代发。

## 1

我在新加坡 AWS 搭了一条自己管理的网络，用的是 Lightsail 每月 7 美元基础套餐。

一个 FAST 节点原来只有约 1.9 Mbps。检查后发现下载参数被设成 2 Mbps，提速后同一个 2 MiB 文件从 8.7 秒缩到约 0.3 秒，套餐没升级。

## 2

7 美元这档包括公网 IPv4、2 vCPU、1 GB 内存、40 GB SSD 和每月 2 TB 传输额度。

这是服务器基础费用。税费、超额外发流量、客户端购买和 AI 会员另算。停止实例仍计费，预算提醒也不会自动封顶。

## 3

符合条件的 AWS 新客户注册可获 100 美元抵扣金，指定活动还能再拿最高 100 美元。

免费计划最长六个月，抵扣金另有到期日。不要用 100÷7，宣传成“免费十四个月”。

## 4

我把方法写成了能交给 Codex、WorkBuddy、Claude Code 的 Markdown。

先核对账号和费用，再部署。密码在自己的服务器生成，每台设备独立领取。注册、MFA、付款验证和系统授权仍由本人完成。

## 5

公开方案先用 Hysteria2 主通道，加 TCP/TLS 备用。证书验证保持开启，代理不能访问云元数据和服务器私网。

WireGuard 目前还有未解决的握手问题，没有放进默认交付方案。

## 6

速度按记录说。现有较快短时样本约 131 至 171 Mbps，也出现过明显更慢的时段。

320 Mbps 是设置值。新部署先自动调速，再按自己的网络测试，不能保证人人同速。

## 7

测试前先保留原线路，用独立进程试新配置。Mac 试实际内核时，部署包提供 120 秒到期恢复。

IPPure 可辅助看出口、WebRTC 和 DNS。检测通过不等于账号永不受限，App 和手机也要单独验收。

## 8

感谢余温和逸尘的分享启发。我的补充集中在 AWS 新加坡费用、独立设备凭据、证书续期、测速与回退。

配套的 AI_DEPLOY.md 包含完整代码。AWS Lightsail 新加坡入口 https://lightsail.aws.amazon.com/，登录后选择 `ap-southeast-1`。

https://x.com/gkxspace/status/2101993381303820704
https://x.com/gengdaJ/status/2108363527971041650
