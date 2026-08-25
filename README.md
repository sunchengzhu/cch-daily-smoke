# CCH Daily Smoke

独立 smoke 项目，用于每日验证已部署 testnet 环境里的 CCH 主流程。每日 workflow
会运行三组互不替代的 live test：

- 原有本地 CCH：`fiber1/CCH + lnd-a + lnd-b`。
- FiberSwap 直连：`fiber2 + Bottle (Fiber trampoline) + FiberSwap FNN
  (CCH node) + FiberSwap CCH LND + lnd-d`。
- FiberSwap 经中继 LND：Fiber 侧相同，Lightning 侧使用
  `lnd-c ↔ relay LND ↔ FiberSwap CCH LND`。

> 本 Demo 按 `1 sat BTC = 1 raw cWBTC` 计价（即 `1 BTC = 1 cWBTC`）。

原有本地 CCH 路径：

- `fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b`
- `lnd-b -> (lnd-a -> fiber1/CCH) -> fiber2`

## FiberSwap 双向 FLOW

`lnd-d` 和 `fiber2` 是我们自己的节点。外部资金路径包含 FiberSwap CCH LND、
FiberSwap FNN (CCH node) 和 Bottle (Fiber trampoline)。FiberSwap API 属于控制面：
CI 通过它创建和查询订单，它再调用 FiberSwap FNN；API 本身不托管资金，也不是资金
路径中的一跳。跨币种由 FiberSwap CCH 协调，CI 用同一个 payment hash 关联
Lightning 和 Fiber 两侧，防止看起来成功但实际付错订单。

```text
CONTROL PATH（只编排订单，不经手资金）
CI → FiberSwap API → FiberSwap FNN (CCH node)

FLOW 1（BTC → cWBTC）
BTC / Lightning : lnd-d → FiberSwap CCH LND
CCH action      : CCH 通过 LND 收到 BTC 后，通知它的 FNN 支付 cWBTC
cWBTC / Fiber   : FiberSwap FNN → Bottle (Fiber trampoline) → fiber2

FLOW 2（cWBTC → BTC）
cWBTC / Fiber   : fiber2 → Bottle (Fiber trampoline) → FiberSwap FNN
CCH action      : CCH 通过 FNN 收到 cWBTC 后，通知它的 LND 支付 BTC
BTC / Lightning : FiberSwap CCH LND → lnd-d
```

workflow 还会运行一组独立的 relay-LND 变体。CCH/Fiber 侧逻辑保持不变，
Lightning 资金路径改为：

```text
FLOW 1: lnd-c → relay LND → FiberSwap CCH LND
FLOW 2: FiberSwap CCH LND → relay LND → lnd-c
```

relay 测试会锁定 `lnd-c ↔ relay LND` channel point 和
`relay LND ↔ FiberSwap CCH LND` SCID，并校验实际两跳路径。FLOW 2 使用
`--private` 和 `86400` 秒 expiry 创建 `lnd-c` invoice；invoice route hint 必须指向
指定 relay LND 且包含非零转发费，从而覆盖 FNN v0.9+ 的 CCH Lightning 路由费流程。
日志会显示 route hint 中最后一跳要求的费用；FiberSwap CCH LND 实际支付的整条
Lightning 路由总费用未由外部 API 暴露。

这里的 `CCH action` 是两笔支付的协调边界，不是 LND 与 FNN 之间的一条资金通道；
BTC 不会直接变成 FNN 数据，cWBTC 也不会直接进入 LND。

两条 FLOW 谁付款、谁收款、手续费给谁：

| FLOW | 谁花钱 | 谁收到本金 | 手续费归属 |
| --- | --- | --- | --- |
| FLOW 1（BTC → cWBTC） | `lnd-d` 支付 BTC 本金和 CCH fee | `fiber2` 收到 cWBTC 本金 | FiberSwap CCH 收取 CCH fee；FiberSwap 服务支付其 FNN 发出 cWBTC 时产生的 Fiber 路由费，但外部 API 不提供该费用的具体金额；`lnd-d` 与 FiberSwap CCH LND 是直连通道，所以 Lightning 路由费应为 0 |
| FLOW 2（cWBTC → BTC） | `fiber2` 支付 cWBTC 本金、CCH fee 和 Fiber 路由费 | `lnd-d` 收到 BTC 本金 | FiberSwap CCH 收取 CCH fee；Bottle (Fiber trampoline) 是首个 Fiber trampoline hop，FNN 只返回整条 Fiber 路由的聚合费用，不提供各节点最终分配；直连 Lightning 路由费应为 0 |

当前测试从“FLOW 1 让 `fiber2` 收到 100 raw cWBTC”开始，Demo 中
`1 raw cWBTC = 1 sat`。按当前 CCH 配置，一轮的典型金额如下；CI 以 API 和 FNN
的实际返回值为准，不把手续费写死：

```text
FLOW 1: lnd-d 支出 200 sats
        = 100 sats 本金 + 100 sats CCH fee
        fiber2 收到 100 raw cWBTC

FLOW 2: fiber2 支出 300 + F raw cWBTC
        = 200 raw cWBTC 本金 + 100 raw cWBTC CCH fee
          + F raw cWBTC Fiber 路由费
        lnd-d 收回 200 sats

F 是 FNN 返回的本次实际聚合路由费，CI 当前允许的上限为 100 raw cWBTC。
```

FLOW 2 的 BTC 本金不是再次固定为 100 sats，而是取 FLOW 1 中 `lnd-d` 的实际
总支出。这样 FLOW 1 从 `lnd-d` 通道花出去多少，FLOW 2 就向同一通道还回多少，
一整轮结束后 `lnd-d ↔ FiberSwap CCH LND` 的本金变化为 0。这个“净归零”只针对稀缺的
Lightning 通道流动性；CCH fee 和 Fiber 路由费仍然是每轮真实消耗。日志会打印
两条通道的 Before、After 和 Change，让不熟悉通道余额的读者也能直接看到钱从哪来、
去了哪里。

## 推荐部署方式

推荐把 GitHub Actions self-hosted runner 装在 `test-new-02`，或者装在能访问同一套内网资源的机器上。

这样 CI 只需要访问本机/内网资源：

- Fiber RPC: `http://127.0.0.1:8227`, `http://127.0.0.1:8229`
- LND: `docker exec lnd-a lncli ...`, `docker exec lnd-b lncli ...`,
  `docker exec lnd-d lncli ...`

所以重点不是“节点必须提供公网 RPC”，而是“CI runner 能访问 RPC”。如果 runner 就在 `test-new-02`，不需要把 Fiber RPC 或 LND admin RPC 暴露到公网。尤其不建议把 LND admin macaroon/RPC 暴露公网。

FiberSwap live test 还要求 runner 能通过 HTTPS 访问
`https://fiber-swap-api.retric.uk`。FiberSwap CCH LND 通过 Lightning P2P 与
`lnd-d` 连接，不需要向 FiberSwap 服务端暴露 `lnd-d` 的 admin RPC。

## 前置条件

### 原有本地 CCH

- `fiber1/CCH` 已配置 CCH，并连接 `lnd-a`。
- `fiber2` 和 `fiber1/CCH` 之间已有 ready 的 cWBTC channel。
- `lnd-a` 和 `lnd-b` 之间已有 active channel。
- 两个 Fiber 节点都 whitelist cWBTC，且 CCH wrapped BTC 指向 cWBTC。

### FiberSwap live test

- `lnd-d` 容器已解锁、完成 Bitcoin `testnet` 同步，并能执行 `lncli`。
- `lnd-d` 已连接 FiberSwap CCH LND；两者之间的新私有通道必须为 `active=true`，通道
  outpoint 固定为
  `70fdfd0dd7960b1b7ca1f562ae031b3a37925fa957eb97c97536f82ed5f37c35:1`。
- 该 LND 通道两端都必须有可用余额：FLOW 1 使用 `lnd-d` 本地余额，FLOW 2 使用
  FiberSwap CCH LND 一侧余额。CI 会用该通道的 SCID 强制 FLOW 1 首跳，不会自动
  改用其他通道。
- `fiber2` 与 Bottle (Fiber trampoline) 之间的 cWBTC 通道为 `ChannelReady`，且
  `fiber2` 有足够的 cWBTC 支付本金、CCH fee 和经过 Bottle 首跳的 Fiber 路由费。
- FiberSwap API 和 FiberSwap FNN (CCH node) 在线，且 CCH 已连接对应的
  FiberSwap CCH LND。
- self-hosted runner 可以访问本机 Fiber RPC、Docker 中的 `lnd-d`，以及外部
  FiberSwap HTTPS API。
- relay-LND 变体还要求 runner 能执行 Docker 中的 `lnd-c`；
  `lnd-c ↔ relay LND` 通道必须 active 且两端都有可用余额，relay LND 与
  FiberSwap CCH LND 的公有通道必须在图中启用。

cWBTC:

- Type hash: `0x569e6b14fa5f3aef02dd83ce4b10ce5b5d691e39244a5d3a0d2a1624e9a73a72`
- Type script:

```json
{
  "code_hash": "0x25c29dc317811a6f6f3985a7a9ebc4838bd388d19d0feeecf0bcd60f6c0975bb",
  "hash_type": "type",
  "args": "0x9a1086531ed6dc69e0bd44cef5278e03faf3015b31aff60b08fb87663ce8507100000000"
}
```

## FiberSwap CI 环境变量

workflow 会固定下面这些值，以保证测试命中刚刚验证过的节点和通道，而不是在存在
多条通道时随机选中另一条：

| Variable | CI value | Purpose |
| --- | --- | --- |
| `CCH_FIBER_SWAP_SMOKE_ENABLED` | `1` | 明确启用外部 FiberSwap live test；普通 unit test 不会误跑它 |
| `CCH_FIBER_SWAP_FNN_CLI` | `/home/ckb/fiber-test/testnet/node1/fnn-cli` | runner 上的 `fnn-cli` 路径 |
| `CCH_FIBER_SWAP_F2_RPC` | `http://127.0.0.1:8229` | 自建 `fiber2` RPC |
| `CCH_FIBER_SWAP_FNN_AUTH_TOKEN` | unset | 手动运行时兼容使用的 biscuit token；测试会立即转换成权限为 `0600` 的临时文件，优先推荐使用文件变量 |
| `CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE` | workflow 生成的临时文件 | 调用 `fiber2` RPC 的 biscuit token 文件；CI 从 GitHub secret 安全生成，不把 token 放进 `fnn-cli` argv |
| `CCH_FIBER_SWAP_API_BASE_URL` | `https://fiber-swap-api.retric.uk` | FiberSwap API |
| `CCH_FIBER_SWAP_AMOUNT_SATS` | `100` | 每轮 FLOW 1 的 BTC/cWBTC 本金 |
| `CCH_FIBER_SWAP_MAX_CCH_FEE_SATS` | `100` | 单笔允许的 CCH service fee 上限；超出时在真实支付前失败 |
| `CCH_FIBER_SWAP_MAX_FIBER_FEE` | `100` | FLOW 2 允许的 Fiber 聚合路由费上限（raw cWBTC） |
| `CCH_FIBER_SWAP_LND_FEE_LIMIT_SATS` | `10` | FLOW 1 允许的 Lightning 路由费上限；直连通道实际应为 0 |
| `CCH_FIBER_SWAP_PAYMENT_TIMEOUT` | `120` | Lightning/Fiber 真实支付的超时时间（秒） |
| `CCH_FIBER_SWAP_LND_D_CONTAINER` | `lnd-d` | 自建付款/收款 LND 容器 |
| `CCH_FIBER_SWAP_LND_DIR` | `/data/.lnd` | 容器内 LND 数据目录 |
| `CCH_FIBER_SWAP_LND_NETWORK` | `testnet` | `lnd-d` 使用 Bitcoin testnet3；LND/lncli 将该网络命名为 `testnet`，不要写成 `testnet3`，也不要改成现有本地流程使用的 `testnet4` |
| `CCH_FIBER_SWAP_LND_D_PUBKEY` | `027431fb…3799f5` | 自建 `lnd-d` 身份公钥 |
| `CCH_FIBER_SWAP_LND_REMOTE_PUBKEY` | `03c7796e…6290d` | FiberSwap CCH LND 身份公钥 |
| `CCH_FIBER_SWAP_LND_CHANNEL_POINT` | `70fdfd0d…f37c35:1` | 唯一允许使用的 `lnd-d ↔ FiberSwap CCH LND` 私有通道 outpoint |
| `CCH_FIBER_SWAP_BOTTLE_PUBKEY` | `02b6d4e3…02be71` | Bottle (Fiber trampoline) 的公钥 |
| `CCH_FIBER_SWAP_FIBER_CHANNEL_ID` | `0x4b6513e2…ed5536` | `fiber2 ↔ Bottle (Fiber trampoline)` cWBTC 通道 |
| `CCH_FIBER_SWAP_RELAY_SMOKE_ENABLED` | relay workflow step 中为 `1` | 明确启用 relay-LND live test |
| `CCH_FIBER_SWAP_RELAY_LND_CONTAINER` | `lnd-c` | relay 测试使用的本地付款/收款 LND 容器 |
| `CCH_FIBER_SWAP_RELAY_LOCAL_LND_PUBKEY` | unset | 可选的 `lnd-c` 身份公钥固定值；未设置时使用 `getinfo` 返回值 |
| `CCH_FIBER_SWAP_RELAY_NODE_PUBKEY` | `03676dd4…70a41` | 公共 relay LND 身份公钥 |
| `CCH_FIBER_SWAP_RELAY_CHANNEL_POINT` | `0cedcc72…5408e:1` | 唯一允许使用的 `lnd-c ↔ relay LND` 通道 outpoint |
| `CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID` | `5637388530143199233` | 唯一允许使用的 `relay LND ↔ FiberSwap CCH LND` 公有通道 SCID |
| `CCH_FIBER_SWAP_RELAY_LND_FEE_LIMIT_SATS` | `80` | FLOW 1 两跳 Lightning 支付允许的路由费上限 |

公钥和 channel id 在实际 workflow 中保存完整值；上表为了可读性只缩写了较长的值。
测试会从 `CCH_FIBER_SWAP_LND_CHANNEL_POINT` 对应的 active 通道读取真实 `scid`，并用
它强制 FLOW 1 的首跳，同时检查 FLOW 2 invoice 中的私有 route hint。

## 手动运行

```bash
cd /Users/sunchengzhu/project/cch-daily-smoke
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

CCH_SMOKE_ENABLED=1 \
CCH_SMOKE_FNN_CLI=/home/ckb/fiber-test/testnet/node1/fnn-cli \
CCH_SMOKE_F1_RPC=http://127.0.0.1:8227 \
CCH_SMOKE_F2_RPC=http://127.0.0.1:8229 \
CCH_SMOKE_LND_A_CONTAINER=lnd-a \
CCH_SMOKE_LND_B_CONTAINER=lnd-b \
python -m pytest -vv -s
```

只手动运行 FiberSwap 双向 FLOW 时，先设置
`CCH_FIBER_SWAP_FNN_AUTH_TOKEN`（也兼容复用 `CCH_SMOKE_FNN_AUTH_TOKEN`），再把
token 安全写入临时文件并执行：

```bash
CCH_FNN_AUTH_VALUE="${CCH_FIBER_SWAP_FNN_AUTH_TOKEN:-${CCH_SMOKE_FNN_AUTH_TOKEN:-}}"
: "${CCH_FNN_AUTH_VALUE:?set CCH_FIBER_SWAP_FNN_AUTH_TOKEN first}"
unset CCH_FIBER_SWAP_FNN_AUTH_TOKEN CCH_SMOKE_FNN_AUTH_TOKEN
CCH_FNN_AUTH_FILE="$(mktemp)"
chmod 600 "$CCH_FNN_AUTH_FILE"
printf '%s' "$CCH_FNN_AUTH_VALUE" > "$CCH_FNN_AUTH_FILE"
unset CCH_FNN_AUTH_VALUE

CCH_FIBER_SWAP_SMOKE_ENABLED=1 \
CCH_FIBER_SWAP_FNN_CLI=/home/ckb/fiber-test/testnet/node1/fnn-cli \
CCH_FIBER_SWAP_F2_RPC=http://127.0.0.1:8229 \
CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE="$CCH_FNN_AUTH_FILE" \
CCH_FIBER_SWAP_API_BASE_URL=https://fiber-swap-api.retric.uk \
CCH_FIBER_SWAP_LND_D_CONTAINER=lnd-d \
CCH_FIBER_SWAP_LND_DIR=/data/.lnd \
CCH_FIBER_SWAP_LND_NETWORK=testnet \
CCH_FIBER_SWAP_LND_D_PUBKEY=027431fbdbbc67df1ed0cf30568fe8ab04ef2fbff296ebcc0c826dc01e923799f5 \
CCH_FIBER_SWAP_LND_REMOTE_PUBKEY=03c7796e27b079b25ea5c9f02dbb289ab1720248ecdd25dceed7cd489f2fe6290d \
CCH_FIBER_SWAP_LND_CHANNEL_POINT=70fdfd0dd7960b1b7ca1f562ae031b3a37925fa957eb97c97536f82ed5f37c35:1 \
CCH_FIBER_SWAP_BOTTLE_PUBKEY=02b6d4e3ab86a2ca2fad6fae0ecb2e1e559e0b911939872a90abdda6d20302be71 \
CCH_FIBER_SWAP_FIBER_CHANNEL_ID=0x4b6513e2d1ad8341366d3b0733f00306aee7e2db21e669a30aba5d33a1ed5536 \
python -m pytest -vv -s \
  tests/test_fiber_swap_daily_smoke.py::test_fiber_swap_bidirectional

rm -f -- "$CCH_FNN_AUTH_FILE"
```

在同一台 `test-new-02` runner 上只运行 relay-LND 变体时，先执行上面的 auth 文件
准备部分（测试结束后再删除），然后运行：

```bash
CCH_FIBER_SWAP_RELAY_SMOKE_ENABLED=1 \
CCH_FIBER_SWAP_FNN_CLI=/home/ckb/fiber-test/testnet/node1/fnn-cli \
CCH_FIBER_SWAP_F2_RPC=http://127.0.0.1:8229 \
CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE="$CCH_FNN_AUTH_FILE" \
CCH_FIBER_SWAP_API_BASE_URL=https://fiber-swap-api.retric.uk \
CCH_FIBER_SWAP_LND_DIR=/data/.lnd \
CCH_FIBER_SWAP_LND_NETWORK=testnet \
CCH_FIBER_SWAP_LND_REMOTE_PUBKEY=03c7796e27b079b25ea5c9f02dbb289ab1720248ecdd25dceed7cd489f2fe6290d \
CCH_FIBER_SWAP_BOTTLE_PUBKEY=02b6d4e3ab86a2ca2fad6fae0ecb2e1e559e0b911939872a90abdda6d20302be71 \
CCH_FIBER_SWAP_FIBER_CHANNEL_ID=0x4b6513e2d1ad8341366d3b0733f00306aee7e2db21e669a30aba5d33a1ed5536 \
CCH_FIBER_SWAP_RELAY_LND_CONTAINER=lnd-c \
CCH_FIBER_SWAP_RELAY_NODE_PUBKEY=03676dd479c6c5422bdcec4691f42f1a6c6b386519b431542e03f31e773d570a41 \
CCH_FIBER_SWAP_RELAY_CHANNEL_POINT=0cedcc728ba1a51582c6650fc14f1a862912ee4ea400a94049c83326b7e5408e:1 \
CCH_FIBER_SWAP_RELAY_TO_FIBER_SWAP_SCID=5637388530143199233 \
CCH_FIBER_SWAP_RELAY_LND_FEE_LIMIT_SATS=80 \
python -m pytest -vv -s \
  tests/test_fiber_swap_relay_daily_smoke.py::test_fiber_swap_via_relay_lnd_bidirectional

rm -f -- "$CCH_FNN_AUTH_FILE"
```

Daily smoke 固定使用 `100 sats ↔ 100 raw cWBTC` 作为每条路径的本金，
CCH fee 另计。这句话描述的是原有本地 CCH 流程；FiberSwap FLOW 2 会按
FLOW 1 的实际 LND 支出动态设置回补金额。稳定性测试仍可通过 `amount_sats`
单独设置每笔本金。

也可以：

```bash
make smoke
```

## 可选环境变量

| Variable | Default | Purpose |
| --- | --- | --- |
| `CCH_SMOKE_FIBER_CHANNEL_ID` | unset | 多条 cWBTC channel 时指定目标 channel |
| `CCH_SMOKE_LND_CHANNEL_ID` | unset | 多条 LND channel 时指定目标 channel |
| `CCH_SMOKE_LND_NETWORK` | `testnet4` | 原有 local CCH smoke 使用的 Bitcoin/Lightning testnet4 网络 |
| `CCH_SMOKE_LND_MIN_SPENDABLE_SATS` | `1000000` | 独立 CI 流动性步骤要求 `lnd-b` 至少保有的可支付余额 |
| `CCH_SMOKE_LND_TOPUP_SATS` | `3000000` | `lnd-b` outbound 不足时，从 `lnd-a` 一次补充的最小流动性缓冲；实际补充量至少覆盖 channel reserve 与本次支付缺口 |
| `CCH_SMOKE_FNN_AUTH_TOKEN` | unset | Fiber RPC biscuit token |
| `CCH_SMOKE_FNN_AUTH_TOKEN_FILE` | unset | Fiber RPC biscuit token file |
| `CCH_SMOKE_DEBUG` | unset | 设为 `1` 时在成功日志中打印 channel id/outpoint |

## GitHub Actions Secret

Fiber RPC 开启 biscuit 鉴权时，需要在 repo 里配置 secret：

```bash
gh secret set CCH_SMOKE_FNN_AUTH_TOKEN --repo sunchengzhu/cch-daily-smoke
```

CI 在 smoke 前通过独立的 `Ensure LND outbound liquidity` 步骤检查流动性：

```bash
python scripts/ensure_lnd_liquidity.py
```

当 `lnd-b` 可支付余额低于 `CCH_SMOKE_LND_MIN_SPENDABLE_SATS` 时，该步骤从
`lnd-a` 一次转入至少 `CCH_SMOKE_LND_TOPUP_SATS`；业务 smoke 本身不会再补充或
改变额外流动性，因此两个步骤的失败和日志彼此独立。

FiberSwap live test 使用同一个 GitHub secret。workflow 会先把它写入权限为
`0600` 的共享临时文件，通过 `CCH_FIBER_SWAP_FNN_AUTH_TOKEN_FILE` 传给测试，并在 job
末尾通过 `always()` 清理；token 不会出现在 `fnn-cli` argv 中。手动运行时直接设置
兼容 token 环境变量也会由测试代码转换成临时文件。它不会运行上面的 `lnd-a → lnd-b` 自动补充
脚本，也不会自动开关 `lnd-d ↔ FiberSwap CCH LND` 通道；指定通道的任一侧余额不足时会
直接失败并打印 Before 状态，避免 CI 在后台改动真实通道结构。

token 需要覆盖本 smoke 用到的 RPC 权限：

```text
read("node");
read("channels");
read("invoices");
write("invoices");
read("payments");
write("payments");
read("cch");
write("cch");
```

## FNN 自动更新

每日 smoke 前会运行 `scripts/update_fnn.sh`：

- 定时任务和手动任务默认从 `nervosnetwork/fiber` 选择发布时间最新的 release，
  包括 prerelease。
- 手动触发参数 `fiber_source` 可选择 `release`、`develop` 或 `pr`。`release`
  表示 GitHub Releases 列表中发布时间最新的发布包；`develop` 会读取
  `https://github-test-logs.ckbapp.dev/fiber/fnn.conf` 中的 `TARBALL_develop`。
  选择 `pr` 时还需填写 `fiber_pr_number`，例如 `1607`，脚本会读取
  `TARBALL_pr1607` 并校验下载包确实属于该 PR。
- release 包会校验 GitHub 提供的 SHA-256；develop 和 PR 包通过 HTTPS 下载并
  记录 SHA-256，但上游未提供摘要，因此无法做外部摘要比对。
- `fnn` 版本无变化时不重启；只有 `fnn-cli` 落后时直接更新 CLI，不扫描数据库。
- `fnn` 有新版本时先停止 `fiber-testnet1.service` 和 `fiber-testnet2.service`。
- 使用新 `fnn --check-validate` 并行检查两个节点的数据库。只有确认不需要迁移时，
  才备份并替换两个节点的 `fnn`，同时更新 node1 的 `fnn-cli`。
- 当前 develop 和 PR 包不包含 `fnn-cli`，选择这两类包时会保留 node1 已安装的
  CLI。
- 启动服务并等待两个 RPC 返回版本、commit 和 pubkey，成功后才运行 smoke。
- 启动或健康检查失败时恢复旧二进制并重新启动服务。

如果新版本需要数据库迁移，自动更新会在替换前失败并恢复旧服务。数据库迁移必须
先备份数据，再按照对应版本的 migration guide 手动执行。`v0.9.x` 已内置统一迁移
系统；只有早于统一迁移 epoch 的数据库才需要使用 `v0.8.x` 的 `fnn-migrate`。

`release` 模式可通过 `CCH_SMOKE_FNN_RELEASE_TAG` 指定发布标签；未指定时自动选择
最新发布版本。

## 验证内容

### 原有本地 CCH

- CCH `send_btc` order 达到 `Success`。
- `fiber2` Fiber payment 达到 `Success`。
- `lnd-b` invoice 变成 `SETTLED`。
- CCH `receive_btc` order 达到 `Success`。
- `fiber2` Fiber invoice 变成 `Paid`。
- `lnd-a` invoice 变成 `SETTLED`。
- Fiber channel 和 LND channel 的两端余额变化符合本金和 CCH fee。

### FiberSwap

- **FLOW 1（BTC → cWBTC）**：订单、Lightning payment 和 Fiber invoice 使用同一
  payment hash；`lnd-d` 支付成功，`fiber2` 收到 100 raw cWBTC。
- **FLOW 2（cWBTC → BTC）**：订单、Fiber payment 和 `lnd-d` invoice 使用同一
  payment hash；`fiber2` 支付成功，`lnd-d` invoice 变为 `SETTLED`。
- 两条 FLOW 都命中配置的 LND channel point 和 Fiber channel；FLOW 2 的私有
  Lightning route hint 指向 FiberSwap CCH LND。
- 日志分别打印本金、CCH fee、Fiber 聚合路由费、Lightning 路由费，以及
  `lnd-d ↔ FiberSwap CCH LND`、`fiber2 ↔ Bottle (Fiber trampoline)` 的
  Before/After/Change。
- FLOW 2 向 `lnd-d` 回补 FLOW 1 的实际总支出；整轮结束后指定 LND 通道的
  local/remote 本金回到测试前水平。
- relay-LND 变体要求 FLOW 1 的实际成功路径严格为
  `lnd-c → relay LND → FiberSwap CCH LND`，FLOW 2 的 private invoice hint 和
  settled HTLC 严格命中 `lnd-c ↔ relay LND` 通道；整轮同样恢复该通道的本金。

## 稳定性测试

稳定性测试把一个完整业务 Flow 计为一笔交易，两个方向独立运行：

```bash
# Flow 1: fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b
python scripts/run_stability.py \
  --flow fiber-to-lnd \
  --mode fixed-tps \
  --tps 5 \
  --duration 300 \
  --amount-sats 100

# 恢复或重新平衡流动性后，再运行 Flow 2
python scripts/run_stability.py \
  --flow lnd-to-fiber \
  --mode fixed-tps \
  --tps 5 \
  --duration 300 \
  --amount-sats 100
```

`--duration` 单位为秒，4 小时为 `14400`。`fixed-tps` 指每秒启动的完整
Flow 数；完成 TPS、成功率和延迟是测试结果。默认最大在途数为 100，超过上限的
交易会记录成 `MaxInflightExceeded`，不会排队后伪装成按时启动。

对照测试可以使用严格串行模式：

```bash
python scripts/run_stability.py \
  --flow lnd-to-fiber \
  --mode sequential \
  --duration 300 \
  --amount-sats 100
```

`sequential` 会在一笔完整 Flow 成功或失败后才启动下一笔，并发始终为 1；
`--tps` 和 `--max-inflight` 在该模式下不参与调度。报告中的实际启动 TPS 是串行
执行自然达到的速率，可以直接和 `fixed-tps` 结果对照。

控制台每 10 秒输出一次进度，失败交易实时输出错误。运行时交易明细先写入
JSONL；GitHub Actions 会在后置步骤生成页面 Markdown 汇总，不上传 Artifact，
避免占用 Actions 存储额度。Runner 本地仍会生成 `summary.json`、
`transactions.csv` 和 `failures.csv`，用于当次任务的汇总处理；任务结束后不保留。
即使任务中途取消，也会根据已经完成的交易生成部分汇总。最终报告包含成功数、
失败数、拒绝数、实际启动 TPS、完成 TPS、p50/p95/p99 延迟和错误分类。默认任意
失败都会让命令以非零状态退出，也可以通过
`--max-failure-rate` 设置允许的失败比例。测试过程中不会自动补充流动性；启动前
会根据目标交易数检查两个资金来源的本金容量，手续费和通道 reserve 仍需额外预留。

GitHub Actions 中可以手动运行 `cch stability`，通过 `load_mode` 选择固定 TPS 或
严格串行。正式计时前，CI 会先使用相同方向执行一笔 100 sats 的真实端到端探测交易；
探测必须在 90 秒内成功，否则停止运行，且探测结果不计入稳定性统计。默认参数是
单方向 5 TPS、5 分钟。探测日志会逐步显示 invoice、CCH order、支付和最终状态检查；
失败后还会显示失败阶段、清理结果以及两端 pending HTLC 数；
稳定性工作流同时将单条 FNN/LND 命令超时设置为 90 秒，避免探测提前结束。
两个方向需要分别触发，并在两次运行之间检查或恢复通道流动性。长稳测试把
`duration_seconds` 改为 `14400`。
