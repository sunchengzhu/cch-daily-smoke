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

## GitHub Actions Secret

Fiber RPC 开启 biscuit 鉴权时，需要在 repo 里配置 secret：

```bash
gh secret set CCH_SMOKE_FNN_AUTH_TOKEN --repo sunchengzhu/cch-daily-smoke
```

token 需要覆盖本 smoke 用到的 RPC 权限：

```text
read("info");
read("channel");
read("invoice");
write("invoice");
read("payment");
write("payment");
read("cch");
write("cch");
```

## 验证内容

- CCH `send_btc` order 达到 `Success`。
- `fiber2` Fiber payment 达到 `Success`。
- `lnd-b` invoice 变成 `SETTLED`。
- CCH `receive_btc` order 达到 `Success`。
- `fiber2` Fiber invoice 变成 `Paid`。
- `lnd-a` invoice 变成 `SETTLED`。
- Fiber channel 和 LND channel 的两端余额变化符合本金和 CCH fee。
