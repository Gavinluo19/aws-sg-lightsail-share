# 新加坡 AWS 自用网络公开分享包

版本 2026-10-25-public-v1。

这是 Gavin 的 AWS 新加坡部署经验、X 发文稿和可交给 Agent 执行的参考实现。公开文件不包含原服务器地址、AWS 账号、真实订阅、密码或私钥。

## 最简单的用法

1. 分享经验时阅读 `X_ARTICLE.md`，普通 X 帖子可以用 `X_THREAD.md`。
2. 想复刻时，把 **`AI_DEPLOY.md` 整个文件**发给有终端和浏览器能力的 Codex、WorkBuddy 或 Claude Code，再复制 `START_PROMPT.md` 的启动指令。
3. `AI_DEPLOY.md` 已包含完整的四份代码。单独分享这个 Markdown 就够了。旁边的 `.py` 文件是同样代码的独立副本，方便直接运行和审阅。

AWS Lightsail 新加坡入口：[lightsail.aws.amazon.com](https://lightsail.aws.amazon.com/)。登录后选择“亚太地区（新加坡）”，区域代码为 `ap-southeast-1`。参见 [AWS 官方区域说明](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html)。

普通聊天模型没有操作系统或 AWS 工具时，只能给出命令和解释。注册、付款验证、MFA、证书条款确认和首次系统 VPN 授权需要账号所有者参与。

## 本次公开实现

- 新加坡 Lightsail，Linux 公网 IPv4 的 $7/月基础档，2 vCPU、1 GB 内存、40 GB 磁盘、2 TB/月传输。
- 主通道 Hysteria2，公网 UDP 51821。
- 备用通道 Trojan + WebSocket + TLS，公网 TCP 443，内部后端只监听回环 TCP 10000。
- Let's Encrypt 公网 IP 证书，自动续期，保持证书验证。
- 每台设备独立凭据和订阅，最多六台有效设备。
- 新部署默认自动速率，不承诺固定 Mbps。实测后可设置 320 Mbps 下载参数。
- 客户端先用独立非 TUN 进程测试；Mac 需要试用实际内核时，使用 120 秒自动回退脚本。

历史方案中的 WireGuard / UDP 51820 尚有握手问题，没有纳入可默认交付的通道。不要把它宣传成已验收稳定备用。

## 实测和宣传口径

现有记录包含约 131 至 171 Mbps 的较快短时下载样本，也包含明显更慢的时段。320 Mbps 是设置值。尚未收到用户补充的 300 Mbps 以上实测证据，不把它写成已实现结果。

符合条件的 AWS 新客户可领取 $100 抵扣金，并可能通过指定活动再获得最高 $100。Free plan 最长六个月，抵扣金通常自开户十二个月到期。$7 是实例基础套餐费用，税费、超额外发流量、客户端购买和 AI 订阅另算。

公开脚本是重新参数化后的参考实现，已安排本地静态和配置校验。原服务器的验收记录不等于此公开实现已在第二台 AWS 实例完成端到端测试。每位部署者都必须完成 `AI_DEPLOY.md` 的验收，才能把自己的服务状态标为可用。

## 文件

| 文件 | 用途 |
|---|---|
| X_ARTICLE.md | 可编辑的 X 长文稿，含两位作者的鸣谢 |
| X_THREAD.md | 普通帖子串文稿 |
| AI_DEPLOY.md | 面向 Agent 的完整、自包含部署说明 |
| START_PROMPT.md | 复制到 Agent 的最短启动指令 |
| SECURITY_RECORD.md | 安全、测速和跨设备验收记录模板 |
| cloud_prepare.py | 默认只查看计划，明确批准后创建一台新 Lightsail |
| server_bootstrap.py | 仅适用全新 Ubuntu 24.04，生成服务和独立设备凭据 |
| client_probe.py | 独立进程测速和有限的认证/网络隔离检查 |
| client_guard_macos.py | Mac Clash Verge Rev 的临时内核试用和到期恢复 |
| SOURCES.md | 核实过的官方价格、软件和证书资料 |

原帖可用公开仓库或文件下载页分享这份包。这里没有伪造一个尚未发布的下载地址，也没有把你的原服务器作为公共服务开放。
