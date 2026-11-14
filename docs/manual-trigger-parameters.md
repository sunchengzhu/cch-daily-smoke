# 手动参数速查（中文备注）

在 Actions 选择 **cch daily smoke**（`cch-daily-smoke.yml`）。手动触发只有日期和
通知两个参数。日常任务只校验已安装的 FNN，不会升级节点。

## 1. 手动触发参数

| 参数 | 默认值 | 中文备注 |
| --- | --- | --- |
| `scheduled_date` | 空 | 手动运行留空；仅服务器定时任务传入当天日期，用于去重和日报。 |
| `send_discord_report` | `false` | 手动运行需要通知时勾选；每日定时任务始终发送。 |

测试本仓库 PR 时，在 **Use workflow from** 选择对应分支；节点仍须运行该分支预期的 FNN 版本。

---

## 2. FNN 校验与人工升级

日常任务运行 `scripts/verify_fnn.sh`，只读取两台节点的二进制版本并检查两个 RPC。
预期版本写在工作流的 `CCH_SMOKE_FNN_EXPECTED_TAG` 和
`CCH_SMOKE_FNN_EXPECTED_COMMIT`。版本不符时停止 smoke，节点保持原状。
报告会保留两台节点及 CLI 的实际版本，并在 GitHub/Discord 标题标明版本不匹配。
报告阶段独立查询最新正式 release，仅供人工升级参考，不会自动升级或要求每日跟随最新包。

需要升级时，由人或 Codex 确定目标版本、检查迁移与备份，再单独运行
`scripts/update_fnn.sh`。release 升级必须显式设置 `CCH_SMOKE_FNN_RELEASE_TAG`；
脚本不会自行挑选最新发布包。升级完成并确认两个 RPC 后，再修改工作流中的预期版本。

| 人工升级变量 | 默认值 | 用途 |
| --- | --- | --- |
| `CCH_SMOKE_FNN_SOURCE` | `release` | 目标来源，可选 `release`、`develop`、`pr`。 |
| `CCH_SMOKE_FNN_RELEASE_TAG` | 无 | `release` 必填，指定准确标签，包括 RC。 |
| `CCH_SMOKE_FNN_PR_NUMBER` | 空 | 仅升级 Fiber PR 包时填写编号。 |
| `CCH_SMOKE_FNN_AUTH_TOKEN_FILE` | 空 | 两个 RPC 的认证文件；每日校验也使用它。 |
| `CCH_SMOKE_FNN_BACKUP_ROOT` | `/home/ckb/fiber-test/testnet/.binary-backups` | 停服数据快照和旧二进制的存放位置。 |
| `CCH_SMOKE_FNN_VALIDATION_TIMEOUT` | `300` | 停服数据校验的秒数上限；大库升级前需评估并调整。 |
| `CCH_SMOKE_FNN_DATA_BACKUP_TIMEOUT` | `600` | 每个节点停服备份的秒数上限。 |

人工升级仍会检查磁盘、停服务、校验数据、备份，再替换二进制并检查健康；
校验或备份失败会恢复旧服务。节点数据快照留给人工评估，不会自动回滚通道状态。

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

派发时带上 `send_discord_report=true` 和当天日期；定时任务不会指定或升级 FNN 版本。

---

## 8. 报告相关（由 workflow 自动填，不要手改）

`CCH_REPORT_*` 这一组（`..._OUTCOME`、`..._JSON`、`..._RUN_URL` 等）是 workflow
在 job 之间传递结果的内部变量，`scripts/send_daily_smoke_report.py` 读取后生成
GitHub 摘要和 Discord 消息。**不需要也不应该手工设置**，这里只说明它们存在，
以免在日志里看到时误以为是需要配置的参数。

---

## 9. 一句话记忆

- **日常**：什么都不用填，直接 Run workflow。
- **升级 Fiber**：单独发起人工升级，指定版本，验证成功后更新工作流中的预期版本。
- **通知**：想测试 Discord 就勾选 `send_discord_report`；不勾选则只看 GitHub Summary。
- **`scheduled_date` 永远留空。**
