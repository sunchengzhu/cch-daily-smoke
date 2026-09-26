# 手动参数速查（中文备注）

在 Actions 选择 **cch daily smoke**（`cch-daily-smoke.yml`）。手动触发页面上能看到的参数只有 5 个，其余都是脚本内部的环境变量。本文把
「界面上选什么」「环境变量填什么」「默认值是多少」「什么时候需要动」写在一处，
避免记不住。

> 普通手动测试：日期留空，版本选 `release`，其余保持默认即可。
> 需要测试 Discord 通知时勾选通知选项；不勾选则只看 GitHub Summary。每日定时任务自动发送。
> 页面已写明各项用途，下文提供更详细的参数说明。

---

## 1. 手动触发时你能选的 5 个参数

普通手动运行主要使用 `fiber_source`；需要同时测试通知时，勾选 `send_discord_report`。
其余三个基本不用碰。

| 参数 | 默认值 | 中文备注 |
| --- | --- | --- |
| `scheduled_date` | 空 | **排期日期，手动跑一律留空。** 只给服务器定时器用（东八区 `YYYY-MM-DD`），作用是当天去重 + 在报告里显示「计划 10:00 / 实际开始 +延迟」的对照。填了会被当成定时任务。留空则报告显示 Manual run。 |
| `fiber_source` | `release` | **被测 FNN 包从哪来。** `release`＝GitHub Releases 里的最新**稳定版**（日常就用这个）；`develop`＝内测 develop 包（未发布代码，会变）；`pr`＝某个 PR 的构建包（要同时填 `fiber_pr_number`）。 |
| `fiber_allow_prerelease` | `false` | **是否允许自动选到 rc / 预发布版。** `false`＝只选稳定版（默认）。填 `true` 才会装上 rc；**预发布版可能没有数据库迁移**（v0.10.0-rc1 就是），需要人工处理，别在有数据的节点上随手打开。 |
| `fiber_pr_number` | 空 | **只有版本来源选 `pr` 时填写。** 例如测试 `nervosnetwork/fiber#1607`，只填 `1607`，不填 `#` 或完整链接；选 `release` / `develop` 时留空。本仓库的 PR 编号不填这里。脚本会校验构建包属于指定 Fiber PR。 |
| `send_discord_report` | `false` | 勾选后本次手动运行也发送 Discord 测试通知；不勾选则只保留 GitHub Summary。GitHub `schedule` 或携带 `scheduled_date` 的每日任务自动发送，不受此选项影响。 |

例如测试本仓库的 PR #12，只需在 **Use workflow from** 选它的分支；FNN 版本来源可保持 `release`，日期和 Fiber PR 编号留空。

### `fiber_source` 三个取值怎么选

| 想干什么 | 怎么选 |
| --- | --- |
| 日常回归 / 上线前确认 | `release`（默认，什么都不用改） |
| 提前验证下个稳定版里的改动 | `release` + `fiber_allow_prerelease=true` |
| 验证某个 PR 的修复 | `pr` + 填 `fiber_pr_number` |
| 跟最新开发进展 | `develop`（包里没有 `fnn-cli`，脚本会保留 node1 已装的 CLI） |

---

## 2. FNN 自动更新（`scripts/update_fnn.sh`）

日常定时任务会先跑这个脚本，把节点二进制升级到目标版本，再跑 smoke。

| 环境变量 | 默认值 | 中文备注 |
| --- | --- | --- |
| `CCH_SMOKE_FNN_SOURCE` | `release` | 对应界面上的 `fiber_source`。 |
| `CCH_SMOKE_FNN_ALLOW_PRERELEASE` | `0` | 对应界面上的 `fiber_allow_prerelease`，只接受 `0` / `1`，其它值直接报错退出。 |
| `CCH_SMOKE_FNN_PR_NUMBER` | 空 | 对应界面上的 `fiber_pr_number`，只能与 `source=pr` 搭配。 |
| `CCH_SMOKE_FNN_RELEASE_TAG` | 空 | **钉死某个 release 标签**（例：`v0.9.1`）。想固定跑某个版本、或专门跑某个 rc 时用。只能与 `source=release` 搭配。目前界面没有透传它，需要手工跑脚本时设。 |
| `CCH_SMOKE_FNN_REPOSITORY` | `nervosnetwork/fiber` | 换仓库时用，基本不动。 |
| `CCH_SMOKE_FNN_CONF_URL` | `https://github-test-logs.ckbapp.dev/fiber/fnn.conf` | `develop` / `pr` 包的清单地址，换内测源时才有用。 |
| `CCH_SMOKE_NODE1_DIR` / `CCH_SMOKE_NODE2_DIR` | `/home/ckb/fiber-test/testnet/node1`、`…/node2` | 两个节点目录。换机器才需要改。 |
| `CCH_SMOKE_FNN_BACKUP_ROOT` | `/home/ckb/fiber-test/testnet/.binary-backups` | 升级前旧二进制的备份位置（回滚靠它）。 |
| `CCH_SMOKE_FNN_AUTH_TOKEN` | 空 | Fiber RPC 的 biscuit token（真实值），脚本会把它落到私有临时文件。 |
| `CCH_SMOKE_FNN_AUTH_TOKEN_FILE` | 空 | 上面 token 的文件形式，优先级高于直接给 token。CI 里用 secret 写成文件后传这个。 |
| `CCH_SMOKE_FNN_VALIDATION_TIMEOUT` | `300` | 新二进制 `--check-validate` 扫库的超时秒数。超时会明确报 `timed out`，并按失败处理（回滚 + 恢复服务）。设这个是为了避免卡死时耗到 workflow 30 分钟上限——那样 step 被强杀，清理逻辑来不及跑，两个 Fiber 服务会停在关闭状态。 |

**升级时会发生什么**：版本没变就不重启；只有 `fnn-cli` 落后就只换 CLI、不扫数据库；
`fnn` 有新版本才停服 → 用新二进制 `--check-validate` 扫两个库 → 通过才替换，
不通过就**报错退出并恢复旧服务**（不会带着旧数据硬启）。数据库迁移必须人工按对应
版本的 migration guide 做。

---

## 3. 三个 smoke 场景的总开关

| 环境变量 | 默认值 | 中文备注 |
| --- | --- | --- |
| `CCH_SMOKE_ENABLED` | 空 | 设 `1` 才跑 **原有本地 CCH** 场景。 |
| `CCH_FIBER_SWAP_SMOKE_ENABLED` | 空 | 设 `1` 才跑 **FiberSwap direct**（走直连 Lightning 通道）。 |
| `CCH_FIBER_SWAP_RELAY_SMOKE_ENABLED` | 空 | 设 `1` 才跑 **FiberSwap via relay LND**（经公共中继）。**就是 09-24/09-25 失败的那个场景。** |

没开的场景会显示 `skipped`，报告里的「N/3 场景通过」按这三个算。

---

## 4. 原有本地 CCH 场景

| 环境变量 | 默认值 | 中文备注 |
| --- | --- | --- |
| `CCH_SMOKE_FNN_CLI` | 空 | `fnn-cli` 路径，CI 里是 `/home/ckb/fiber-test/testnet/node1/fnn-cli`。 |
| `CCH_SMOKE_F1_RPC` / `CCH_SMOKE_F2_RPC` | 空 | 两个 Fiber 节点的 RPC，CI 里是 `http://127.0.0.1:8227` / `:8229`。 |
| `CCH_SMOKE_LND_A_CONTAINER` / `CCH_SMOKE_LND_B_CONTAINER` | 空 | 两个 LND 的 docker 容器名（`lnd-a` / `lnd-b`）。 |
| `CCH_SMOKE_LND_DIR` | `/data/.lnd` | 容器内 LND 数据目录。 |
| `CCH_SMOKE_LND_NETWORK` | `testnet4` | 本地 CCH 用的网络。 |
| `CCH_SMOKE_AMOUNT_SATS` | `100` | 每笔本金（sats）。日常固定 100，**稳定性测试才需要改**。 |
| `CCH_SMOKE_CURRENCY` | `Fibt` | cWBTC 币种，换测试币种时才动。 |
| `CCH_SMOKE_UDT_SCRIPT_JSON` | 内置 cWBTC script | 直接给 UDT script（替代按币种查）。 |
| `CCH_SMOKE_FIBER_CHANNEL_ID` | 空 | 有多条 cWBTC channel 时，指定走哪条。 |
| `CCH_SMOKE_LND_CHANNEL_ID` | 空 | 有多条 LND channel 时，指定走哪条。 |
| `CCH_SMOKE_WAIT_TIMEOUT` | `180` | 等待通道/TLC 收敛的秒数。**注意：中继那条通道不活跃时，就是在这里等满 180s 才失败。** |
| `CCH_SMOKE_COMMAND_TIMEOUT` | `60` | 单条命令超时秒数。 |
| `CCH_SMOKE_DEBUG` | 空 | 设 `1` 在成功日志里多打 channel id / outpoint，排查时开。 |

---

## 5. FiberSwap（direct 与 relay 共用）

| 环境变量 | 默认值 | 中文备注 |
| --- | --- | --- |
| `CCH_FIBER_SWAP_FNN_CLI` | 空 | 同本地场景的 `fnn-cli` 路径。 |
| `CCH_FIBER_SWAP_F2_RPC` | 空 | 用哪个 Fiber 节点发起，CI 里是 `http://127.0.0.1:8229`。 |
| `CCH_FIBER_SWAP_API_BASE_URL` | 空 | FiberSwap 的 CCH API，CI 里是 `https://fiber-swap-api.retric.uk`。 |
| `CCH_FIBER_SWAP_AMOUNT_SATS` | `100` | 每笔本金（sats）。 |
| `CCH_FIBER_SWAP_MAX_CCH_FEE_SATS` | `100` | CCH 手续费上限（sats），超了就判定失败。 |
| `CCH_FIBER_SWAP_MAX_FIBER_FEE` | `100` | Fiber 侧手续费上限（raw cWBTC）。 |
| `CCH_FIBER_SWAP_LND_FEE_LIMIT_SATS` | `10` | 本机 LND 出账允许的 Lightning 路由费上限。 |
| `CCH_FIBER_SWAP_PAYMENT_TIMEOUT` | `120` | 单笔支付超时秒数（必须小于 `WAIT_TIMEOUT`）。 |
| `CCH_FIBER_SWAP_WAIT_TIMEOUT` | `180` | 轮询等待总超时秒数。 |
| `CCH_FIBER_SWAP_COMMAND_TIMEOUT` | `60` | 单条命令超时秒数。 |
| `CCH_FIBER_SWAP_CURRENCY` | `Fibt` | 换测试币种时才动。 |
| `CCH_FIBER_SWAP_UDT_SCRIPT_JSON` | 内置 cWBTC script | 直接给 UDT script。 |
| `CCH_FIBER_SWAP_DEBUG` | 空 | 设 `1` 打开调试输出。 |
| `CCH_FIBER_SWAP_FNN_AUTH_TOKEN` | 空 | FiberSwap 用的 token，兼容复用 `CCH_SMOKE_FNN_AUTH_TOKEN`。 |
| `CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE` | 空 | token 文件形式（CI 用的就是这个）。 |

### 5.1 direct 场景专用

| 环境变量 | CI 里的值 | 中文备注 |
| --- | --- | --- |
| `CCH_FIBER_SWAP_LND_D_CONTAINER` | `lnd-d` | 本机 LND 容器。 |
| `CCH_FIBER_SWAP_LND_DIR` | `/data/.lnd` | 容器内数据目录。 |
| `CCH_FIBER_SWAP_LND_NETWORK` | `testnet` | LND 网络。 |
| `CCH_FIBER_SWAP_LND_D_PUBKEY` | `027431fb…99f5` | 本机 LND 身份，用于确认没连错节点。 |
| `CCH_FIBER_SWAP_LND_REMOTE_PUBKEY` | `03c7796e…290d` | 对端（FiberSwap 的 LND）身份。 |
| `CCH_FIBER_SWAP_LND_CHANNEL_POINT` | `70fdfd0d…35:1` | 必须走的 Lightning 通道 outpoint。 |
| `CCH_FIBER_SWAP_BOTTLE_PUBKEY` | `02b6d4e3…be71` | 中间的 Bottle 节点身份。 |
| `CCH_FIBER_SWAP_FIBER_CHANNEL_ID` | `0x4b6513e2…5536` | Fiber 侧通道 id。 |
| `CCH_FIBER_SWAP_LND_D_LNCLI_PREFIX` | — | 换 `lncli` 调用方式时才用。 |

### 5.2 relay 场景专用（09-24/09-25 失败在这里）

| 环境变量 | CI 里的值 | 中文备注 |
| --- | --- | --- |
| `CCH_FIBER_SWAP_RELAY_LND_CONTAINER` | `lnd-c` | 中继 LND 容器。 |
| `CCH_FIBER_SWAP_RELAY_NODE_PUBKEY` | `03676dd4…0a41` | 中继 LND 身份。 |
| `CCH_FIBER_SWAP_RELAY_CHANNEL_POINT` | `0cedcc72…08e:1` | 中继通道 outpoint（**这个是权威标识**，脚本按它找通道）。 |
| `CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID` | `5637388530143199233` | **第二跳** `Rainbow Dash ↔ FiberSwap LND` 的 SCID，用于 graph 端点和路由检查。第一跳 `lnd-c ↔ Rainbow Dash` 的 SCID 从固定 outpoint 动态读取，当前是 `5636281321933307905`。两者属于不同通道，不能相互替代；第二跳重建后需核实新端点与 SCID 再修改此配置。 |
| `CCH_FIBER_SWAP_RELAY_LND_FEE_LIMIT_SATS` | `80` | 中继路径允许的路由费上限（中继比直连贵，所以给到 80）。 |
| `CCH_FIBER_SWAP_RELAY_GOSSIP_LND_CONTAINER` | direct LND 容器（默认 `lnd-d`） | 原通道 inactive 且 peer 断连时，从这个节点补查 Rainbow 最新公告；空值禁用辅助查询。按更新时间尝试最多 4 个公网 IP，总计最多 45 秒。自定义 lncli prefix 时不查询辅助容器。 |
| `CCH_FIBER_SWAP_RELAY_LOCAL_LND_PUBKEY` | 空 | 可选的身份钉死，不填就只钉通道和 peer。 |

---

## 6. 流动性检查（独立步骤）

| 环境变量 | 默认值 | 中文备注 |
| --- | --- | --- |
| `CCH_SMOKE_LND_MIN_SPENDABLE_SATS` | `1000000` | `lnd-b` 至少要有的可支付余额，低于它就从 `lnd-a` 补。 |
| `CCH_SMOKE_LND_TOPUP_SATS` | `3000000` | 一次补充的金额（还会额外覆盖 channel reserve 和本次支付缺口）。 |

---

## 7. 定时任务派发（服务器侧 `dispatch_daily_smoke.py`）

| 环境变量 | 默认值 | 中文备注 |
| --- | --- | --- |
| `CCH_SMOKE_DISPATCH_TOKEN_FILE` | — | 服务器上存放 GitHub token 的文件（权限必须是 `0600`）。由 systemd unit 指定，日常不动。 |

派发时会显式带上 `fiber_source=release` + `send_discord_report=true` + 当天日期，
所以**定时任务只跟稳定版、并且一定会发 Discord**。

---

## 8. 报告相关（由 workflow 自动填，不要手改）

`CCH_REPORT_*` 这一组（`..._OUTCOME`、`..._JSON`、`..._RUN_URL` 等）是 workflow
在 job 之间传递结果的内部变量，`scripts/send_daily_smoke_report.py` 读取后生成
GitHub 摘要和 Discord 消息。**不需要也不应该手工设置**，这里只说明它们存在，
以免在日志里看到时误以为是需要配置的参数。

---

## 9. 一句话记忆

- **日常**：什么都不用填，直接 Run workflow。
- **想测 rc**：`fiber_allow_prerelease` 勾上。
- **想测 Fiber PR #1607**：`fiber_source=pr` + 编号填 `1607`。
- **通知**：想测试 Discord 就勾选 `send_discord_report`；不勾选则只看 GitHub Summary。
- **`scheduled_date` 永远留空。**
