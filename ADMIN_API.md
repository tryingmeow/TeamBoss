# 一次性兑换码 —— 管理接口与指令

面向管理员的兑换码管理方式有三种：Telegram 指令出码（日常主用）、后台页面查看与停用、以及直接调 HTTP 接口（写脚本时用）。

## 一次性兑换码管理（Admin API）

所有管理接口都要带管理密钥，两种写法等价：`X-API-Key: {admin_api_key}`，或 `Authorization: Bearer {admin_api_key}`（管理后台自己用的是后者）。

在后台轮换密钥后，**旧密钥还能继续用 10 分钟**——这样轮换请求万一断在半路，你还有时间拿到新密钥。

**修改管理员密码会同时换掉密钥，旧密钥立即失效**（没有 10 分钟宽限）。改完密码后，把脚本、外部系统里保存的密钥换成后台「设置」里显示的新值。

### 生成兑换码

```
POST /api/access-tokens
```

**请求头：**
```
X-API-Key: {admin_api_key}
Content-Type: application/json
```

**请求体：**
```json
{
  "grant_expires_in": "30d",
  "token_ttl": "7d",
  "note": "optional note",
  "seat_type": "default"
}
```

参数说明：
- `grant_expires_in`：成员使用此码后的有效期时长，格式如 `7d` / `30d` / `360d` / `never`，必填
- `token_ttl`：兑换码本身的有效期，默认 `7d`，可选。过期后无法使用
- `note`：可选备注，方便管理员标记码的用途
- `seat_type`：兑换后得到的席位类型，可选，默认 `default`。只接受两个值：`default`（ChatGPT 码）和 `prolite`（Premium 码，**Beta，验证范围见[生产测试范围](README.md#production-test-scope)**）；Codex 不出码，传其他值会被参数校验拒绝。两种码都只用已付费的空位，不管 Team 的超员策略是什么都不会加购席位；Premium 码没有空位时兑换失败、码不消耗。完整的兑换规则见 [上手指南第 5 节](docs/getting-started.md#5-让成员自己兑换)。

**响应（200 OK）：**
```json
{
  "id": 1,
  "token": "atm_xxxxxxxxxxxxxxxxxxxxx",
  "token_prefix": "atm_xxxxxxxxxx",
  "grant_expires_in": "30d",
  "token_expires_at": "2026-08-01T12:34:56.789Z",
  "max_uses": 1,
  "used_count": 0,
  "note": "optional note",
  "seat_type": "default",
  "created_at": "2026-07-25T12:34:56.789Z"
}
```

### 列出所有兑换码

```
GET /api/access-tokens
```

**请求头：**
```
X-API-Key: {admin_api_key}
```

**响应（200 OK）：**
```json
[
  {
    "id": 1,
    "token_prefix": "atm_xxxxxxxxxx",
    "grant_expires_in": "30d",
    "token_expires_at": "2026-08-01T12:34:56.789Z",
    "max_uses": 1,
    "used_count": 0,
    "note": "optional note",
    "seat_type": "default",
    "disabled": false,
    "created_at": "2026-07-25T12:34:56.789Z",
    "last_used_at": null
  }
]
```

字段说明：
- `disabled`：`true` 表示已被管理员停用，无法继续使用
- `used_count`：已使用次数，因为 `max_uses` 固定为 1，所以最多为 1
- `last_used_at`：最后一次使用的时间，若未使用则为 null
- `seat_type`：这张码的席位类型，`default`（ChatGPT）或 `prolite`（Premium）；早于这个字段生成的码都是 `default`

### 停用兑换码

```
DELETE /api/access-tokens/{token_id}
```

**请求头：**
```
X-API-Key: {admin_api_key}
```

**响应（200 OK）：**
```json
{
  "status": "ok"
}
```

停用后该码将无法再使用。已经使用过的码停用无实际效果。

## Telegram 指令（一次性兑换码）

管理员可在绑定的 Telegram 机器人中直接生成兑换码，无需进入后台。

### /token 指令

```
/token <天数>
```

示例：
- `/token 30` - 兑换后给 **30 天**会员时长
- `/token 7` - 兑换后给 **7 天**会员时长

注意这个天数说的是**兑换之后给多长的会员时长**，不是码本身的有效期。Telegram 出的码固定 **7 天内必须用掉**（`token_ttl` 写死 `7d`），过期作废，而且**一律是 ChatGPT 码**（不带 `seat_type`，按默认 `default`）。要出一张长期有效的码或 Premium 码，用后台「生成兑换码」按钮，那里可以单独设置码的有效期和席位类型。

机器人会返回完整的兑换码（`atm_` 前缀的字符串），管理员可直接复制转发给成员使用。

## 后台页面

管理后台的「兑换码」页面（`/admin/access-tokens`）用于查看已生成的码、状态、谁在什么时候用了，以及停用某个码；页面右上角的「生成兑换码」按钮会直接调用上面的生成接口，可以选席位类型（ChatGPT / Premium）、授予时长和兑换有效期，生成后当场显示并可复制完整兑换码（这一刻是唯一能拿到完整码的机会，接口之后只保留前缀用于识别）。列表里的「席位」列显示每张码的席位类型。除此之外，也可以在 Telegram 中向机器人发送 `/token <天数>` 出码，但只能出 7 天内有效的 ChatGPT 码。


## 成员自助接口

这些接口无需管理认证，供根路径的自助页或自建前端调用。兑换业务规则见[上手指南 §5](docs/getting-started.md#5-让成员自己兑换)。

| 接口 | 用途 |
|---|---|
| `POST /api/self-service/redeem` | 凭邮箱和兑换码加入或续期 |
| `POST /api/self-service/query` | 请求体的 `query` 可填邮箱或兑换码，返回成员状态；要查看兑换历史，还需通过 `token` 提交一张该邮箱使用过的码，否则历史为空数组 |
| `POST /api/self-service/status` | 凭邮箱查询当前是否还在 Team 中；内置自助页未调用，供自建前端使用 |

三个接口按来源 IP 限流，反代需按[部署说明](docs/deployment.md#让后端收到真实访客-ip)传递真实客户端 IP。有效兑换码的兑换尝试另有限额：每张码每小时 10 次、全站每 10 分钟 60 次，超限返回 429，码不消耗。「请选择 Team」及服务端 / 上游 5xx 故障不占该码的次数。

[返回项目介绍](README.md) · [部署与运维](docs/deployment.md) · [本地开发](docs/development.md)
