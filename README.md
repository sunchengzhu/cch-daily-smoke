# CCH Daily Smoke

独立 smoke 项目，用于每日验证已部署 testnet 环境里的两条 CCH 主流程：

- `fiber2 -> (fiber1/CCH -> lnd-a) -> lnd-b`
- `lnd-b -> (lnd-a -> fiber1/CCH) -> fiber2`

## 推荐部署方式

推荐把 GitHub Actions self-hosted runner 装在 `test-new-02`，或者装在能访问同一套内网资源的机器上。

这样 CI 只需要访问本机/内网资源：

- Fiber RPC: `http://127.0.0.1:8227`, `http://127.0.0.1:8229`
- LND: `docker exec lnd-a lncli ...`, `docker exec lnd-b lncli ...`

所以重点不是“节点必须提供公网 RPC”，而是“CI runner 能访问 RPC”。如果 runner 就在 `test-new-02`，不需要把 Fiber RPC 或 LND admin RPC 暴露到公网。尤其不建议把 LND admin macaroon/RPC 暴露公网。

## 前置条件

- `fiber1/CCH` 已配置 CCH，并连接 `lnd-a`。
- `fiber2` 和 `fiber1/CCH` 之间已有 ready 的 mzBTC channel。
- `lnd-a` 和 `lnd-b` 之间已有 active channel。
- 两个 Fiber 节点都 whitelist mzBTC，且 CCH wrapped BTC 指向 mzBTC。

mzBTC:

- Explorer: https://testnet.explorer.nervos.org/xudt/0xb76a77e0807794af162716c450885092e29eaabce09be1cc30335ce9d906b590
- Type script:

```json
{
  "code_hash": "0x25c29dc317811a6f6f3985a7a9ebc4838bd388d19d0feeecf0bcd60f6c0975bb",
  "hash_type": "type",
  "args": "0x7275c8fb7feb81d22a47aa582c4f2487d771a1933957fe8fee9b363603487b1a00000000"
}
```

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
CCH_SMOKE_AMOUNT_SATS=100 \
python -m pytest -vv -s
```

也可以：

```bash
make smoke
```

## 可选环境变量

| Variable | Default | Purpose |
| --- | --- | --- |
| `CCH_SMOKE_FIBER_CHANNEL_ID` | unset | 多条 mzBTC channel 时指定目标 channel |
| `CCH_SMOKE_LND_CHANNEL_ID` | unset | 多条 LND channel 时指定目标 channel |
| `CCH_SMOKE_LND_NETWORK` | `testnet4` | LND bitcoin network |
| `CCH_SMOKE_LND_TOPUP_SATS` | `5000` | `lnd-b` outbound 不足时，从 `lnd-a` 自动补流动性 |
| `CCH_SMOKE_FNN_AUTH_TOKEN` | unset | Fiber RPC biscuit token |
| `CCH_SMOKE_FNN_AUTH_TOKEN_FILE` | unset | Fiber RPC biscuit token file |
| `CCH_SMOKE_DEBUG` | unset | 设为 `1` 时在成功日志中打印 channel id/outpoint |

## GitHub Actions Secret

Fiber RPC 开启 biscuit 鉴权时，需要在 repo 里配置 secret：

```bash
gh secret set CCH_SMOKE_FNN_AUTH_TOKEN --repo sunchengzhu/cch-daily-smoke
```

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

- 从 `nervosnetwork/fiber` 选择发布时间最新的 release，包括 prerelease。
- 下载当前 Linux 架构对应的 portable 包并校验 GitHub 提供的 SHA-256。
- `fnn` 版本无变化时不重启；只有 `fnn-cli` 落后时直接更新 CLI，不扫描数据库。
- `fnn` 有新版本时先停止 `fiber-testnet1.service` 和 `fiber-testnet2.service`。
- 使用新 `fnn --check-validate` 检查两个节点的数据库。只有确认不需要迁移时，
  才备份并替换两个节点的 `fnn`，同时更新 node1 的 `fnn-cli`。
- 启动服务并等待两个 RPC 返回版本、commit 和 pubkey，成功后才运行 smoke。
- 启动或健康检查失败时恢复旧二进制并重新启动服务。

如果新版本需要数据库迁移，自动更新会在替换前失败并恢复旧服务。数据库迁移必须
先备份数据，再按照对应版本的 migration guide 手动执行。`v0.9.x` 已内置统一迁移
系统；只有早于统一迁移 epoch 的数据库才需要使用 `v0.8.x` 的 `fnn-migrate`。

可通过 `CCH_SMOKE_FNN_RELEASE_TAG` 指定发布标签，默认自动选择最新发布版本。

## 验证内容

- CCH `send_btc` order 达到 `Success`。
- `fiber2` Fiber payment 达到 `Success`。
- `lnd-b` invoice 变成 `SETTLED`。
- CCH `receive_btc` order 达到 `Success`。
- `fiber2` Fiber invoice 变成 `Paid`。
- `lnd-a` invoice 变成 `SETTLED`。
- Fiber channel 和 LND channel 的两端余额变化符合本金和 CCH fee。
