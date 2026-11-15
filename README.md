# Qoder 每日签到（国际版 + 国内版）

> 一键完成 Qoder「每天领 Credits」，**国际版与国内版同时支持**：自动识别本机所有 Qoder 客户端档案，每个账号带上自己的接口地址与设备标识，各自签到互不干扰。纯 Python 标准库零依赖，可交给不忙脚本盒子定时无人值守运行。

---

## 核心特性

- **双版本共存**：本机同时装国际版（`openapi.qoder.sh`）和国内版（`openapi.qoder.com.cn`）时，两边账号分别提取、各自打自己的端点，绝不会「拿国际版 token 打国内版接口」这种串线（串线会报 401 TOKEN_EXPIRE，看起来像 token 过期）
- **一键提取**：自动从客户端解出设备标识（`Cosy-*`）与 token（DPAPI + AES-GCM），写回本地配置，无需手动复制
- **多账号**：`accounts.json` 写几个签几个，按 uid / refreshToken 去重；手写账号也认
- **token 自动续期**：距过期不足设定小时数时主动刷新（新版客户端给的是 ISO 时间串，已兼容）
- **按版本分区**：`edition` 参数可选「两版都签 / 仅国际版 / 仅国内版」
- **幂等安全**：claim 接口幂等，重复运行不会重复发币
- **零第三方依赖**：`pyproject.toml` 的 `dependencies` 为空，盒子不会为它创建 venv

---

## 安装

三种方式任选（详见[打包与分发](https://www.bm-box.cn/help/api/packaging-distribution)）：

| 方式 | 操作 |
| --- | --- |
| 本地 ZIP | 盒子 → 本地安装 → 选 `qoder-checkin-dual.zip` |
| GitHub 仓库 | 盒子 → 仓库安装 → 填仓库地址 |
| 脚本社区 | 盒子 → 脚本市场搜索安装 |

> 升级不要改 `bm-scripts-box-rc.toml` 里的 `info.id`，盒子靠 UUID 识别「同一个脚本」做智能覆盖安装。

---

## 使用

### 首次使用

前提：本机**安装并登录** Qoder 客户端（国际版 / 国内版都行，都装了更好）。

在盒子里点脚本卡片运行即可：脚本自动检测客户端 → 提取设备标识与账号 token → 立即签到。

提取结果落在脚本安装目录：

| 文件 | 内容 |
| --- | --- |
| `config.json` | `device`（共享兜底设备标识）+ `devices`（按版本分开）+ 接口地址 |
| `accounts.json` | 账号数组，每项含 `region` / `baseUrl` / `accessToken` / `refreshToken` / `expiresAt` |
| `.qoder_token_cache.json` | 续期后的 token 缓存（自动维护） |

### 日常签到

点卡片 → 盒子弹出启动窗口 → 选「运行模式 / 签到范围」→ 执行。

默认「自动」模式：有客户端则先刷新设备标识与 token 再签到；没有就直接用已有 `accounts.json`。

### 定时任务

盒子 → 新建定时任务 → 选本脚本 → 填参数 → 保存。建议每天 **10:00 之后**（Qoder 每日活动不是零点准点下发）。

执行结果会写进任务记录，例如 `成功 1 · 已领 1 · 待下发 0 · 失败 0 · 本次 +100 Credits`。

---

## 参数

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `mode` | `auto` / `checkin` / `extract` | 自动（提取+签到）/ 仅签到 / 仅提取 |
| `edition` | `auto` / `intl` / `cn` | 两版都签 / 仅国际版 / 仅国内版 |
| `base_url` | `...qoder.com.cn` / `...qoder.sh` | **仅兜底**：账号自己没有 `baseUrl` 时（例如手写 accounts.json）才用 |
| `refresh_margin_h` | 整数，默认 72 | 距过期不足这么多小时就主动续期 |

`accounts.json` 里的 `region` 也可以写 `intl` / `cn`，脚本会归一化。

---

## 返回结果（信封）

```json
{
  "code": 0,
  "msg": "成功 1 · 已领 1 · 待下发 0 · 失败 0 · 本次 +100 Credits",
  "total": 2, "claimed": 1, "already": 1, "pending": 0, "bad": 0, "credits": 100,
  "details": [
    {"name": "示例账号A", "region": "国际版", "phase": "claimed", "msg": "签到成功 +100 Credits", "credits": 100},
    {"name": "示例账号B", "region": "国内版", "phase": "already", "msg": "今日已领取", "credits": 0}
  ]
}
```

`code`：`0` 全部成功 / `1` 没有可用账号 / `2` 有账号失败（需重新登录或查询失败）。

---

## 排查

| 现象 | 处理 |
| --- | --- |
| 提示「需重新登录 · token 无效（HTTP 401）」 | 该版本客户端登录态失效：打开对应客户端确认已登录，再跑「自动」模式 |
| 一直「待下发」/ 活动列表为空 | 多半是设备标识过期；「自动」模式每次都会重提，通常可自愈 |
| 只签了其中一个版本 | 看日志里账号的 `region`；`edition` 是不是被设成了 `intl`/`cn` |
| 手动加账号后报 401 | 给该账号补上正确的 `baseUrl`（国际版 `.sh` / 国内版 `.com.cn`） |

> 同一账号不要和其他签到工具同时运行，否则会互相轮换 refreshToken 导致失效。

---

## 安全提示

本脚本运行时会在**脚本安装目录**生成以下文件，它们含你的凭据，请勿提交到任何仓库或分享：

| 文件 | 含什么 |
| --- | --- |
| `accounts.json` | `accessToken` / `refreshToken` / 你的 uid |
| `config.json` | 设备标识（`machineToken` 等） |
| `.qoder_token_cache.json` | 续期后的 token |

仓库里已附带 `.gitignore` 覆盖这三个文件；发布/提 PR 前建议再 `git status` 确认一眼。


---

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10+（64 位） |
| 运行环境 | Python 3.8+（盒子自备，无需 pip 安装任何包） |
| 提取前提 | 本机安装并登录 Qoder 客户端（仅提取时需要） |

---

## 致谢

设备标识提取、DPAPI + AES-GCM 解密等核心思路参考了以下开源项目：

- [sunp-1/qoder-checkin](https://github.com/sunp-1/qoder-checkin)（MIT）
- qoder-cf-checkin（Cloudflare Worker 版）

---

## License

MIT

---

## 免责声明

本脚本仅供个人学习与研究使用，请遵守 Qoder（含 Qoder CN / 国际版）的服务条款。使用本工具产生的一切后果由使用者自行承担。
