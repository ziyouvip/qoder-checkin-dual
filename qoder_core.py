#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qoder 签到核心 · 国际版 + 国内版（qoder_core.py）
════════════════════════════════════════════════════════════
零第三方依赖（纯标准库）。读 config.json / accounts.json，逐账号：

    「续期 token → 查活动 → claim」

与旧版的区别只有一条：**端点与设备标识跟着账号走**。
每个账号用自己的 baseUrl（国际版 openapi.qoder.sh / 国内版
openapi.qoder.com.cn）和自己的设备标识，绝不串线——串线会直接
401 TOKEN_EXPIRE，看起来像 token 过期，其实是打错了端点。

claim 接口幂等，重复运行不会重复发币。
"""
import base64
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("QODER_CONFIG_FILE", "").strip() or os.path.join(HERE, "config.json")
ACCOUNTS_FILE = os.getenv("QODER_ACCOUNTS_FILE", "").strip() or os.path.join(HERE, "accounts.json")
CACHE_FILE = os.path.join(HERE, ".qoder_token_cache.json")

REGION_INTL = "国际版"
REGION_CN = "国内版"
DEFAULT_BASES = ["https://openapi.qoder.com.cn"]
UA = "Qoder/claim"

PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
PATH_CLAIM = "/sash/api/v1/me/campaigns/{cid}/claim"
PATH_REFRESH = "/api/v1/deviceToken/refresh"

# 区域别名：accounts.json 里写 region = "intl" / "cn" 也认
REGION_ALIAS = {
    "intl": REGION_INTL, "international": REGION_INTL, REGION_INTL: REGION_INTL,
    "cn": REGION_CN, "china": REGION_CN, REGION_CN: REGION_CN,
}

# 设备标识：config.json 的键 -> Cosy-* 请求头 -> 环境变量 -> 默认值
DEVICE_KEYS = [
    ("clientType", "Cosy-ClientType", "QODER_COSY_CLIENT_TYPE", "10"),
    ("machineOS", "Cosy-MachineOS", "QODER_COSY_MACHINE_OS", ""),
    ("machineHostname", "Cosy-MachineHostname", "QODER_COSY_MACHINE_HOSTNAME", ""),
    ("machineId", "Cosy-MachineId", "QODER_COSY_MACHINE_ID", ""),
    ("machineToken", "Cosy-MachineToken", "QODER_COSY_MACHINE_TOKEN", ""),
    ("machineCode", "Cosy-MachineCode", "QODER_COSY_MACHINE_CODE", ""),
    ("machineType", "Cosy-MachineType", "QODER_COSY_MACHINE_TYPE", ""),
    ("version", "Cosy-Version", "QODER_COSY_VERSION", ""),
]


# ── 配置读取 ──────────────────────────────────────────────
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read().lstrip("\ufeff").strip()
        return json.loads(text) if text else default
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


CONFIG = read_json(CONFIG_FILE, {})
if not isinstance(CONFIG, dict):
    CONFIG = {}


def base_urls():
    env = os.getenv("QODER_BASE_URL", "").strip()
    if env:
        return [u.strip() for u in env.split(",") if u.strip()]
    v = CONFIG.get("baseUrls")
    if isinstance(v, list) and v:
        return [str(u).strip() for u in v if str(u).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return DEFAULT_BASES


def refresh_margin():
    try:
        return int(CONFIG.get("refreshMarginH", 72))
    except (TypeError, ValueError):
        return 72


def region_of_account(acc):
    """账号归属的区域：先看 region，再看 baseUrl 域名，认不出返回 ''。"""
    raw = str(acc.get("region") or "").strip()
    if raw:
        hit = REGION_ALIAS.get(raw) or REGION_ALIAS.get(raw.lower())
        if hit:
            return hit
    base = str(acc.get("baseUrl") or "").lower()
    if "qoder.sh" in base:
        return REGION_INTL
    if "qoder.com.cn" in base:
        return REGION_CN
    return ""


def device_headers(acc):
    """该账号的 Cosy-* 请求头：按区域取它自己那套设备标识，没有则退回共享的一套。"""
    region = region_of_account(acc)
    devices = CONFIG.get("devices") if isinstance(CONFIG.get("devices"), dict) else {}
    dev = devices.get(region) if region and isinstance(devices.get(region), dict) else None
    if not isinstance(dev, dict):
        dev = CONFIG.get("device") if isinstance(CONFIG.get("device"), dict) else {}
    h = {}
    for cfg_key, header, env, default in DEVICE_KEYS:
        v = os.getenv(env, "").strip()
        if not v:
            v = str(dev.get(cfg_key) or "").strip() or str(default or "")
        if v:
            h[header] = v
    h.setdefault("Cosy-ClientType", "10")
    return h


def bj(fmt="%Y-%m-%d %H:%M:%S"):
    return time.strftime(fmt, time.gmtime(time.time() + 8 * 3600))


# ── HTTP ─────────────────────────────────────────────────
_opener = urllib.request.build_opener()


def _raw(base, path, method, token="", body=None, device=None):
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json", "User-Agent": UA}
    headers.update(device or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        status = e.code
    except Exception as e:
        return 0, str(e), base
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = text
    return status, parsed, base


_last_base = ""


def api_call(path, method="GET", token="", body=None, bases=None, device=None):
    """按账号的端点候选依次尝试。网络错误(0)/404 视为端点不对换下一个；
    其余（含 401）视为该端点已给出结论。

    bases 为该账号自己的候选（国际版 / 国内版必须隔离），不传才退回全局配置。
    """
    global _last_base
    cand = [b.rstrip("/") for b in (bases or base_urls()) if str(b).strip()]
    if _last_base in cand:
        cand = [_last_base] + [b for b in cand if b != _last_base]
    last = (0, "no base", "")
    for base in cand:
        status, data, _ = _raw(base, path, method, token, body, device)
        if status in (0, 404):
            last = (status, data, base)
            continue
        _last_base = base
        return status, data, base
    return last


# ── JWT / 时间 ───────────────────────────────────────────
def _b64(seg):
    seg += "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def jwt_exp(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return 0
    try:
        return int(_b64(parts[1]).get("exp") or 0)
    except (TypeError, ValueError):
        return 0


def jwt_uid(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return ""
    data = _b64(parts[1])
    for k in ("sub", "uid", "user_id", "userId", "id"):
        if data.get(k):
            return str(data[k])
    return ""


def to_epoch(v):
    """expiresAt 可能是 epoch 秒/毫秒，也可能是 ISO 字符串；失败返回 0。"""
    if v in (None, ""):
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n // 1000 if n > 10 ** 11 else n
    try:
        return int(datetime.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def fmt_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "-"


# ── token 续期 ───────────────────────────────────────────
def account_bases(acc):
    """该账号专属端点候选；没有 baseUrl 时返回 None（退回全局配置）。"""
    base = str(acc.get("baseUrl") or "").strip()
    return [base.rstrip("/")] if base else None


def do_refresh(refresh_token, device=None, bases=None):
    if not refresh_token:
        return None
    status, data, _ = api_call(PATH_REFRESH, "POST", body={"refresh_token": refresh_token},
                               device=device, bases=bases)
    if status != 200 or not isinstance(data, dict):
        return None
    tok = data.get("token") or data.get("accessToken") or data.get("access_token")
    if not tok:
        return None
    return {"accessToken": tok,
            "refreshToken": data.get("refreshToken") or data.get("refresh_token") or refresh_token}


# ── 账号持久化（环境变量是静态的，靠缓存持久化轮换后的 token）──
def _acct_key(acc):
    return (str(acc.get("uid") or "").strip()
            or str(acc.get("refreshToken") or "")[:24]
            or str(acc.get("accessToken") or "")[:24]
            or "default")


def persist(acc):
    key = _acct_key(acc)
    cache = read_json(CACHE_FILE, {})
    if not isinstance(cache, dict):
        cache = {}
    cache[key] = {"accessToken": acc.get("accessToken", ""),
                  "refreshToken": acc.get("refreshToken", ""),
                  "expiresAt": acc.get("expiresAt") or 0,
                  "uid": acc.get("uid", ""),
                  "region": acc.get("region", ""),
                  "baseUrl": acc.get("baseUrl", ""),
                  "updatedAt": int(time.time())}
    try:
        atomic_write_json(CACHE_FILE, cache)
    except Exception as e:
        print("  ⚠️ [缓存] 写入失败: %s" % e)
    if os.path.isfile(ACCOUNTS_FILE):
        _write_back_accounts(acc)


def _write_back_accounts(acc):
    try:
        doc = read_json(ACCOUNTS_FILE, None)
        rows = doc.get("accounts") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            return
        hit = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            same = (acc.get("uid") and str(row.get("uid")) == str(acc["uid"])) or \
                   (acc.get("refreshToken") and row.get("refreshToken") == acc.get("refreshToken"))
            if same:
                row["accessToken"] = acc.get("accessToken", "")
                row["refreshToken"] = acc.get("refreshToken", "")
                row["expiresAt"] = acc.get("expiresAt") or 0
                row["uid"] = acc.get("uid", "")
                if acc.get("baseUrl"):
                    row["baseUrl"] = acc["baseUrl"]
                if acc.get("region"):
                    row["region"] = acc["region"]
                hit = True
                break
        if hit:
            atomic_write_json(ACCOUNTS_FILE, doc if isinstance(doc, dict) else {"accounts": rows})
    except Exception as e:
        print("  ⚠️ [回写] accounts.json 失败: %s" % e)


def apply_cache(accounts):
    cache = read_json(CACHE_FILE, {})
    if not isinstance(cache, dict) or not cache:
        return accounts
    for acc in accounts:
        rec = cache.get(_acct_key(acc))
        if isinstance(rec, dict) and rec.get("accessToken"):
            acc["accessToken"] = rec["accessToken"]
            acc["refreshToken"] = rec.get("refreshToken") or acc.get("refreshToken", "")
            acc["expiresAt"] = rec.get("expiresAt") or acc.get("expiresAt") or 0
            acc["baseUrl"] = acc.get("baseUrl") or rec.get("baseUrl", "")
            acc["region"] = acc.get("region") or rec.get("region", "")
    return accounts


# ── 账号加载 ─────────────────────────────────────────────
def _pick(d, *names):
    if not isinstance(d, dict):
        return ""
    for n in names:
        for k, v in d.items():
            if str(k).lower() == str(n).lower() and v not in (None, ""):
                return v
    return ""


def normalize(a, i):
    return {"accessToken": str(_pick(a, "accessToken", "access_token", "token") or ""),
            "refreshToken": str(_pick(a, "refreshToken", "refresh_token") or ""),
            "uid": str(_pick(a, "uid", "userId", "user_id", "sub", "id") or ""),
            "baseUrl": str(_pick(a, "baseUrl", "base_url", "endpoint") or "").rstrip("/"),
            "region": str(_pick(a, "region", "edition") or ""),
            "expiresAt": _pick(a, "expiresAt", "expires_at") or 0,
            "name": str(_pick(a, "name", "nickname") or "账号%d" % (i + 1))}


def _from_list(items):
    out = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            out.append(normalize(item, i))
    return out


def load_accounts():
    accounts = []
    env = os.getenv("QODER_ACCOUNTS", "").strip()
    if env:
        try:
            data = json.loads(env)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("accounts", [data])
        if isinstance(data, list):
            accounts += _from_list(data)
    doc = read_json(ACCOUNTS_FILE, None)
    if isinstance(doc, dict):
        accounts += _from_list(doc.get("accounts", []))
    elif isinstance(doc, list):
        accounts += _from_list(doc)

    uniq, seen = [], set()
    for acc in accounts:
        if not (acc.get("refreshToken") or acc.get("accessToken")):
            continue
        key = acc.get("uid") or acc.get("refreshToken", "")[:24] or acc.get("accessToken", "")[:24]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(acc)
    return apply_cache(uniq)


def filter_accounts(accounts, edition="auto"):
    """edition: auto=全部 / intl=仅国际版 / cn=仅国内版。认不出区域的账号在 auto 下保留。"""
    want = REGION_ALIAS.get(str(edition or "").strip().lower(), "")
    if not want:
        return accounts
    out = []
    for acc in accounts:
        if region_of_account(acc) == want:
            out.append(acc)
    return out


# ── 签到流程 ─────────────────────────────────────────────
def ensure_token(acc, force=False):
    margin = refresh_margin() * 3600
    device = device_headers(acc)
    bases = account_bases(acc)
    at = acc.get("accessToken") or ""
    exp = to_epoch(acc.get("expiresAt")) or (jwt_exp(at) if at else 0)
    if at and not force and (not exp or exp - time.time() > margin):
        return True, ""
    rt = acc.get("refreshToken") or ""
    if not rt:
        return False, "没有 refreshToken，且 accessToken 不可用"
    r = do_refresh(rt, device=device, bases=bases)
    if not r:
        if at and (not exp or exp > time.time()):
            return True, "续期失败，沿用未过期 accessToken"
        return False, "token 已失效且续期失败，请重新提取"
    acc["accessToken"] = r["accessToken"]
    acc["refreshToken"] = r["refreshToken"]
    acc["expiresAt"] = jwt_exp(r["accessToken"]) or acc.get("expiresAt") or 0
    if not acc.get("uid"):
        acc["uid"] = jwt_uid(r["accessToken"])
    persist(acc)
    return True, "已续期"


def _fmt(v):
    try:
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _login_hint(region):
    if region == REGION_INTL:
        return "请确认 Qoder 国际版客户端处于登录状态后重跑（自动模式会重新提取）"
    if region == REGION_CN:
        return "请确认 Qoder 国内版客户端处于登录状态后重跑（自动模式会重新提取）"
    return "请确认 Qoder 客户端处于登录状态后重跑"


def run_account(acc):
    region = region_of_account(acc)
    name = acc.get("name") or acc.get("uid") or _acct_key(acc)
    result = {"name": name, "region": region, "uid": acc.get("uid") or "-",
              "phase": "error", "msg": "", "credits": 0}
    device = device_headers(acc)
    bases = account_bases(acc)

    ok, msg = ensure_token(acc)
    if not ok:
        result["phase"] = "login_required"
        result["msg"] = msg + "；" + _login_hint(region)
        return result

    status, data, used = api_call(PATH_CAMPAIGNS, token=acc["accessToken"],
                                  bases=bases, device=device)
    if status == 401:
        ok2, _ = ensure_token(acc, force=True)
        if ok2:
            status, data, used = api_call(PATH_CAMPAIGNS, token=acc["accessToken"],
                                          bases=bases, device=device)
    if status == 401:
        result["phase"] = "login_required"
        result["msg"] = "token 无效（HTTP 401）；" + _login_hint(region)
        return result
    if status != 200 or not isinstance(data, dict):
        result["phase"] = "error"
        result["msg"] = "查询失败 HTTP %s: %s" % (status, str(data)[:120])
        return result

    result["endpoint"] = used
    campaigns = data.get("campaigns")
    if not isinstance(campaigns, list):
        campaigns = []
    benefits = [c for c in campaigns if isinstance(c, dict) and c.get("actionType") == "CLAIM_BENEFIT"]
    claimable = [c for c in benefits if c.get("claimStatus") == "CLAIMABLE"]

    if claimable:
        got, fail, detail = 0, 0, []
        for c in claimable:
            cid = str(c.get("campaignId") or "")
            s2, d2, _ = api_call(PATH_CLAIM.replace("{cid}", cid), "POST",
                                 token=acc["accessToken"], body={},
                                 bases=bases, device=device)
            dd = d2.get("data") if isinstance(d2, dict) and isinstance(d2.get("data"), dict) else d2
            done = s2 == 200 and isinstance(dd, dict) and dd.get("status") == "CLAIMED"
            if done:
                amt = (dd.get("benefit") or {}).get("amount") if isinstance(dd, dict) else 0
                amt = amt or (c.get("benefit") or {}).get("amount") or 0
                try:
                    got += float(amt)
                except (TypeError, ValueError):
                    pass
                detail.append(str(cid))
            else:
                fail += 1
        if fail:
            result["phase"] = "error"
            result["msg"] = "%d/%d 个活动领取失败" % (fail, len(claimable))
        else:
            result["phase"] = "claimed"
            result["credits"] = got
            result["msg"] = "签到成功 +%s Credits" % _fmt(got)
        return result

    now = time.time()
    claimed = [c for c in benefits if c.get("claimStatus") == "CLAIMED"
               and (c.get("startAt") or 0) <= now < (c.get("endAt") or 0)]
    if claimed:
        result["phase"] = "already"
        result["msg"] = "今日已领取"
        return result
    result["phase"] = "pending"
    result["msg"] = "活动未下发（或已结束），等下轮"
    return result


# ── 主流程 ───────────────────────────────────────────────
PHASE_LABEL = {"claimed": "✅ 签到成功", "already": "☑️ 已领取", "pending": "⏳ 待下发",
               "login_required": "🔑 需重新登录", "error": "❌ 失败"}


def run_all(edition="auto"):
    """执行签到，打印结果并返回汇总 dict（供 main.py 写回信封）。"""
    all_accounts = load_accounts()
    accounts = filter_accounts(all_accounts, edition)

    if not all_accounts:
        print("❌ 没有可用账号：")
        print("   请确认本机已安装并登录 Qoder 客户端（「自动」模式会全自动提取设备标识与 token）；")
        print("   或在本脚本安装目录放置 accounts.json（含 accessToken / refreshToken）。")
        return {"code": 1, "total": 0, "claimed": 0, "already": 0, "pending": 0,
                "bad": 0, "credits": 0, "details": [],
                "msg": "没有可用账号（请安装并登录 Qoder 客户端）"}

    if not accounts:
        print("⚠️ 按当前「签到范围」过滤后没有账号（共 %d 个），本次跳过。" % len(all_accounts))
        return {"code": 0, "total": 0, "claimed": 0, "already": 0, "pending": 0,
                "bad": 0, "credits": 0, "details": [],
                "msg": "当前签到范围下没有账号（共 %d 个）" % len(all_accounts)}

    counts = {}
    for acc in accounts:
        region = region_of_account(acc) or "未知版本"
        counts[region] = counts.get(region, 0) + 1
    print()
    print("=" * 58)
    print("  🤖 Qoder 每日签到  %s" % bj())
    print("  账号 %d 个（%s）" % (len(accounts), " · ".join("%s %d" % kv for kv in counts.items())))
    print("=" * 58)

    results = []
    for acc in accounts:
        region = region_of_account(acc)
        label_name = acc.get("name") or acc.get("uid") or _acct_key(acc)
        try:
            r = run_account(acc)
        except Exception as e:
            r = {"name": label_name, "region": region, "uid": acc.get("uid") or "-",
                 "phase": "error", "msg": str(e), "credits": 0}
        tag = "（%s）" % (region or "未知版本")
        label = PHASE_LABEL.get(r["phase"], r["phase"])
        credits = "  +%s" % _fmt(r["credits"]) if r.get("credits") else ""
        print("  %s %s%s: %s%s" % (label, r["name"], tag, r["msg"], credits))
        results.append(r)

    claimed = sum(1 for r in results if r["phase"] == "claimed")
    already = sum(1 for r in results if r["phase"] == "already")
    pending = sum(1 for r in results if r["phase"] == "pending")
    bad = sum(1 for r in results if r["phase"] in ("login_required", "error"))
    credits = sum(float(r.get("credits") or 0) for r in results)

    summary = "成功 %d · 已领 %d · 待下发 %d · 失败 %d" % (claimed, already, pending, bad)
    if credits:
        summary += " · 本次 +%s Credits" % _fmt(credits)
    print("-" * 58)
    print("  " + summary)
    if bad:
        print("  提示：失败账号请先在该版本客户端重新登录，再跑「自动」模式。")

    return {"code": 0 if bad == 0 else 2, "total": len(results), "claimed": claimed,
            "already": already, "pending": pending, "bad": bad,
            "credits": credits, "msg": summary,
            "details": [{"name": r["name"], "region": r.get("region") or "",
                         "uid": r.get("uid") or "", "phase": r["phase"],
                         "msg": r["msg"], "credits": r.get("credits") or 0} for r in results]}


def main():
    return run_all()["code"]


if __name__ == "__main__":
    sys.exit(main())
