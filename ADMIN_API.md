# 一次性兑换码 —— 管理接口与指令

面向管理员的兑换码管理方式有三种：Telegram 指令出码（日常主用）、后台页面查看与停用、以及直接调 HTTP 接口（写脚本时用）。

## 一次性兑换码管理（Admin API）

所有管理接口都要带管理密钥，两种写法等价：`X-API-Key: {admin_api_key}`，或 `Authorization: Bearer {admin_api_key}`（管理后台自己用的是后者）。

在后台轮换密钥后，**旧密钥还能继续用 10 分钟**——这样轮换请求万一断在半路，你还有时间拿到新密钥。

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
  "note": "optional note"
}
```

参数说明：
- `grant_expires_in`：成员使用此码后的有效期时长，格式如 `7d` / `30d` / `360d` / `never`，必填
- `token_ttl`：兑换码本身的有效期，默认 `7d`，可选。过期后无法使用
- `note`：可选备注，方便管理员标记码的用途

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

注意这个天数说的是**兑换之后给多长的会员时长**，不是码本身的有效期。Telegram 出的码固定 **7 天内必须用掉**（`token_ttl` 写死 `7d`），过期作废。要出一张长期有效的码，用后台「生成兑换码」按钮，那里可以单独设置码的有效期。

机器人会返回完整的兑换码（`atm_` 前缀的字符串），管理员可直接复制转发给成员使用。

## 后台页面

管理后台的「兑换码」页面（`/admin/access-tokens`）用于查看已生成的码、状态、谁在什么时候用了，以及停用某个码；页面右上角的「生成兑换码」按钮会直接调用上面的生成接口，生成后当场显示并可复制完整兑换码（这一刻是唯一能拿到完整码的机会，接口之后只保留前缀用于识别）。除此之外，也可以在 Telegram 中向机器人发送 `/token <天数>` 出码，二者等价，选哪个都行。
