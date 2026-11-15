#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qoder 提取模块 · 国际版 + 国内版（qoder_extract.py）
════════════════════════════════════════════════════════════
一次扫描本机所有 Qoder 客户端档案，把「设备标识」和「账号 token」分别落盘：

  config.json  →  "device"（共享/兜底设备标识） + "devices"（按版本分开的设备标识）
  accounts.json →  accounts 数组，每个账号自带 region 与 baseUrl

为什么要区分版本：Qoder 国际版（openapi.qoder.sh）与国内版
（openapi.qoder.com.cn）是两套互不相通的账号体系，token 只有打到
自己版本的端点才有效，打错就是 401 TOKEN_EXPIRE。

零第三方依赖：优先 cryptography，缺失则退回 Windows CNG（bcrypt）。
"""
import base64
import ctypes
import ctypes.wintypes as wt
import datetime
import glob
import json
import os
import platform
import re
import socket
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REGION_INTL = "国际版"
REGION_CN = "国内版"

# 客户端数据目录前缀 -> (区域, 该区域的默认接口地址)
PROFILE_KINDS = (
    ("com.qoder.app.", REGION_INTL, "https://openapi.qoder.sh"),
    ("com.qodercn.app.", REGION_CN, "https://openapi.qoder.com.cn"),
)

UMID_REL = ("resources", "umid", "runtime-info.exe")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("QODER_CONFIG_FILE", "").strip() or os.path.join(HERE, "config.json")
ACCOUNTS_FILE = os.getenv("QODER_ACCOUNTS_FILE", "").strip() or os.path.join(HERE, "accounts.json")


# ── Windows 解密原语（DPAPI + AES-GCM）────────────────────
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _AeadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.ULONG), ("dwInfoVersion", wt.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)), ("cbNonce", wt.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)), ("cbAuthData", wt.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)), ("cbTag", wt.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)), ("cbMacContext", wt.ULONG),
        ("cbAAD", wt.ULONG), ("cbData", ctypes.c_ulonglong), ("dwFlags", wt.ULONG),
    ]


def _dpapi_unprotect(data):
    src = _DataBlob(len(data), ctypes.create_string_buffer(data, len(data)))
    dst = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)):
        raise OSError("DPAPI 解不开（%s）——需同一 Windows 用户且已登录桌面" % ctypes.WinError())
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _decrypt_gcm_cng(key, nonce, blob):
    bcrypt = ctypes.WinDLL("bcrypt.dll")

    def chk(res, what):
        if res != 0:
            raise OSError("CNG %s 失败 NTSTATUS=0x%08x" % (what, res & 0xFFFFFFFF))

    ct, tag = blob[:-16], blob[-16:]
    h_alg = ctypes.c_void_p()
    chk(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0), "OpenAlg")
    mode = "ChainingModeGCM"
    chk(bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0), "SetProperty")
    h_key = ctypes.c_void_p()
    kbuf = ctypes.create_string_buffer(key, len(key))
    chk(bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0, kbuf, len(key), 0), "GenKey")
    nbuf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
    tbuf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
    info = _AeadInfo()
    info.cbSize = ctypes.sizeof(_AeadInfo)
    info.dwInfoVersion = 1
    info.pbNonce = ctypes.cast(nbuf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbNonce = len(nonce)
    info.pbTag = ctypes.cast(tbuf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbTag = len(tag)
    ibuf = ctypes.create_string_buffer(ct, len(ct))
    obuf = ctypes.create_string_buffer(len(ct))
    done = ctypes.c_ulong()
    chk(bcrypt.BCryptDecrypt(h_key, ibuf, len(ct), ctypes.byref(info), None, 0,
                             obuf, len(ct), ctypes.byref(done), 0), "Decrypt")
    return obuf.raw[:done.value]


def _decrypt_gcm(key, nonce, blob):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return _decrypt_gcm_cng(key, nonce, blob)
    return AESGCM(key).decrypt(nonce, blob, None)


# ── 基础工具 ─────────────────────────────────────────────
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


def to_epoch(v):
    """客户端时间可能是 ISO 字符串（'2026-12-13T14:14:11Z'）、epoch 秒或毫秒；失败返回 0。"""
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


def region_of(root):
    base = os.path.basename(root).lower()
    for prefix, region, _base in PROFILE_KINDS:
        if base.startswith(prefix):
            return region
    return ""


def default_base_of(region):
    for _prefix, r, base in PROFILE_KINDS:
        if r == region:
            return base
    return ""


# ── 客户端档案定位 ────────────────────────────────────────
def appdata_profiles():
    """[(root, region, default_base)]，按目录修改时间倒序；同名只留一份。"""
    env = os.environ.get("QODER_AUTH_DIR", "").strip()
    if env:
        return [(env, region_of(env), default_base_of(region_of(env)))]
    appdata = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    roots = []
    for pattern, _region, _base in PROFILE_KINDS:
        roots += sorted(glob.glob(os.path.join(appdata, pattern + "*")),
                        key=os.path.getmtime, reverse=True)
    out, seen = [], set()
    for root in roots:
        key = root.lower()
        if key in seen:
            continue
        seen.add(key)
        region = region_of(root)
        out.append((root, region, default_base_of(region)))
    return out


def _read_store(root):
    raw_path = os.path.join(root, "auth.v1.dat")
    state_path = os.path.join(root, "Local State")
    if not (os.path.isfile(raw_path) and os.path.isfile(state_path)):
        return None
    with open(raw_path, "rb") as fh:
        raw = fh.read()
    if raw[:3] != b"v10":
        return None
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    key_blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    sess = json.loads(_decrypt_gcm(_dpapi_unprotect(key_blob[5:]), raw[3:15], raw[15:]).decode("utf-8"))
    if not sess.get("token"):
        return None
    return sess


def read_session(root):
    """解密该档案的登录会话；没有登录或解不开返回 None。"""
    try:
        return _read_store(root)
    except Exception:
        return None


def base_of(root, sess, region):
    """优先用会话里的 authenticationContext（[webOrigin, apiBase, ...]）拿真实端点。"""
    ac = sess.get("authenticationContext")
    if isinstance(ac, str):
        try:
            ac = json.loads(ac)
        except ValueError:
            ac = None
    if isinstance(ac, list):
        for item in ac:
            if isinstance(item, str) and item.startswith("https://openapi."):
                return item.rstrip("/")
    return default_base_of(region)


# ── 账号提取 ─────────────────────────────────────────────
def collect_accounts():
    """扫描所有档案，返回账号列表（每个账号带自己的 region / baseUrl）。"""
    out = []
    for root, region, _base in appdata_profiles():
        sess = read_session(root)
        if not sess:
            continue
        user = sess.get("user") if isinstance(sess.get("user"), dict) else {}
        out.append({
            "name": str(user.get("name") or "").strip() or (region + "账号"),
            "uid": str(user.get("id") or ""),
            "region": region,
            "baseUrl": base_of(root, sess, region),
            "accessToken": sess.get("token") or "",
            "refreshToken": sess.get("refreshToken") or "",
            "expiresAt": to_epoch(sess.get("expiresAt")),
            "refreshExpiresAt": to_epoch(sess.get("refreshTokenExpiresAt")),
            "profile": os.path.basename(root),
        })
    uniq, seen = [], set()
    for acc in out:
        key = acc.get("uid") or acc.get("refreshToken", "")[:24] or acc.get("accessToken", "")[:24]
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(acc)
    return uniq


def _jwt_uid(token):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return ""
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return ""
    for k in ("sub", "uid", "user_id", "userId", "id"):
        if data.get(k):
            return str(data[k])
    return ""


def write_accounts(accounts, account_name=""):
    """把账号合并进 accounts.json：按 uid / refreshToken 匹配，其余条目原样保留。"""
    if not accounts:
        return 0
    doc = read_json(ACCOUNTS_FILE, None)
    rows = doc.get("accounts") if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])
    rows = [a for a in rows if isinstance(a, dict)] if isinstance(rows, list) else []

    written = 0
    for tok in accounts:
        uid = tok.get("uid") or _jwt_uid(tok.get("accessToken") or "")
        region = tok.get("region") or ""
        name = (account_name or "").strip() or tok.get("name") or (region + "账号" if region else "我的账号")

        def fill(target):
            target["accessToken"] = tok["accessToken"]
            target["refreshToken"] = tok["refreshToken"]
            if tok.get("baseUrl"):
                target["baseUrl"] = tok["baseUrl"]
            if region:
                target["region"] = region
            if uid:
                target["uid"] = uid
            if tok.get("expiresAt"):
                target["expiresAt"] = tok["expiresAt"]
            if tok.get("refreshExpiresAt"):
                target["refreshExpiresAt"] = tok["refreshExpiresAt"]
            target["name"] = name or target.get("name") or "我的账号"

        hit = False
        for row in rows:
            same = (uid and str(row.get("uid")) == uid) or \
                   (tok.get("refreshToken") and row.get("refreshToken") == tok["refreshToken"])
            if same:
                fill(row)
                hit = True
                break
        if not hit:
            fresh = {}
            fill(fresh)
            rows.append(fresh)
        written += 1

    atomic_write_json(ACCOUNTS_FILE, {"accounts": rows})
    return written


# ── 设备标识提取 ─────────────────────────────────────────
def _dir_of(value):
    """从 DisplayIcon / UninstallString / InstallLocation 中解析安装目录。"""
    if not value:
        return ""
    v = str(value).strip()
    m = re.match(r'^"([^"]+)"', v)
    if m:
        v = m.group(1)
    else:
        m2 = re.match(r'^(.*?\.(?:exe|ico))', v, re.IGNORECASE)
        if m2:
            v = m2.group(1)
        else:
            v = v.split(",")[0].strip()
    v = v.strip().strip('"').strip()
    if v.lower().endswith((".exe", ".ico")):
        return os.path.dirname(v)
    return v


def _region_of_dir(path):
    low = str(path).lower()
    if "qoder cn" in low or "qodercn" in low:
        return REGION_CN
    return REGION_INTL


def install_roots():
    """[(install_dir, region)]，来自常见安装位置 + 注册表。"""
    local = os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    roots = []
    for base in (os.path.join(local, "Programs"), pf, pf86):
        for name in ("Qoder", "Qoder CN"):
            roots.append((os.path.join(base, name), _region_of_dir(name)))
    try:
        import winreg
        hives = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, path in hives:
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            try:
                total = winreg.QueryInfoKey(key)[0]
            except OSError:
                winreg.CloseKey(key)
                continue
            for i in range(total):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                        if "qoder" not in display.lower():
                            continue
                        region = REGION_CN if "cn" in display.lower() else REGION_INTL
                        for field in ("InstallLocation", "DisplayIcon", "UninstallString"):
                            try:
                                val = winreg.QueryValueEx(sub, field)[0]
                            except OSError:
                                val = ""
                            d = _dir_of(val)
                            if d:
                                roots.append((os.path.abspath(d), region))
                                break
                except OSError:
                    continue
            winreg.CloseKey(key)
    except Exception:
        pass
    out, seen = [], set()
    for d, region in roots:
        key = os.path.normcase(d)
        if key in seen:
            continue
        seen.add(key)
        out.append((os.path.abspath(d), region))
    return out


def umid_exes():
    """[(runtime-info.exe, region)]"""
    env = os.environ.get("QODER_UMID_EXE", "").strip()
    if env:
        return [(env, region_of(os.path.dirname(os.path.dirname(os.path.dirname(env)))) or REGION_INTL)]
    out = []
    for d, region in install_roots():
        exe = os.path.join(d, *UMID_REL)
        if os.path.isfile(exe):
            out.append((exe, region))
    return out


def run_umid(exe):
    try:
        p = subprocess.run([exe, "--account-stdin"], input=b"", capture_output=True,
                           timeout=40, cwd=os.path.dirname(exe))
        lines = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}


def arch():
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(m, "x86_64")


def _version_near(exe):
    mf = os.path.join(os.path.dirname(os.path.dirname(exe)), "build-manifest.json")
    try:
        with open(mf, encoding="utf-8") as fh:
            return str(json.load(fh).get("productVersion") or "")
    except Exception:
        return ""


def build_device(region):
    """为某个版本构造设备标识；该版本没装或跑不出 machineToken 时返回 None。"""
    exe = next((e for e, r in umid_exes() if r == region), None)
    if not exe:
        return None
    info = run_umid(exe)
    if not info.get("machineToken"):
        return None
    machine_id = ""
    for root, r, _base in appdata_profiles():
        if r != region:
            continue
        try:
            with open(os.path.join(root, "auth.machine-id"), encoding="utf-8") as fh:
                machine_id = fh.read().strip()
        except OSError:
            machine_id = ""
        if machine_id:
            break
    return {
        "clientType": "10",
        "machineOS": arch() + "_windows",
        "machineHostname": socket.gethostname(),
        "machineId": machine_id,
        "machineToken": info.get("machineToken") or "",
        "machineCode": info.get("machineCode") or "",
        "machineType": info.get("machineType") or "",
        "version": _version_near(exe),
    }


def build_all_devices():
    """{区域: 设备标识}；只包含能跑出 machineToken 的版本。"""
    out = {}
    for region in (REGION_INTL, REGION_CN):
        dev = build_device(region)
        if dev:
            out[region] = dev
    return out


def write_devices(devices, shared=None):
    """写入 config.json：device = 共享兜底，devices = 按版本分开。"""
    if not devices and not shared:
        return False
    doc = read_json(CONFIG_FILE, {})
    if not isinstance(doc, dict):
        doc = {}
    if devices:
        merged = doc.get("devices") if isinstance(doc.get("devices"), dict) else {}
        merged.update(devices)
        doc["devices"] = merged
        if not isinstance(doc.get("device"), dict) or not doc["device"].get("machineToken"):
            doc["device"] = dict(list(devices.values())[0])
    if shared:
        doc["device"] = shared
    atomic_write_json(CONFIG_FILE, doc)
    return True


# ── 对外入口 ─────────────────────────────────────────────
def client_present():
    """本机是否有 Qoder 客户端痕迹（档案目录或 runtime-info.exe）。"""
    if appdata_profiles():
        return True
    return bool(umid_exes())


def run(only_device=False, only_token=False, no_write=False, account_name=""):
    """执行提取并写回。返回统计 dict。"""
    stats = {"device_ok": False, "token_ok": False, "accounts": 0,
             "devices": [], "regions": []}

    devices = build_all_devices() if not only_token else {}
    accounts = collect_accounts() if not only_device else []
    stats["devices"] = sorted(devices.keys())
    stats["regions"] = sorted(set(a.get("region") for a in accounts if a.get("region")))

    print("=" * 58)
    print("  Qoder 一键提取 · 国际版 + 国内版")
    print("=" * 58)

    if not only_token:
        print("\n── 设备标识（config.json 的 device / devices）──")
        if devices:
            for region, dev in devices.items():
                # 只报“已获取”，不回显 machineCode / machineToken —— 避免截图或日志外泄设备指纹
                print("  [%s] 设备标识已获取 · 版本 %s" % (region, dev.get("version") or "-"))
            if no_write:
                print("  预览模式，未写回 config.json。")
            else:
                write_devices(devices)
                stats["device_ok"] = True
                print("  已写入 config.json")
        else:
            print("  未找到可用的 runtime-info.exe（未装客户端？）")
            print("  可用 QODER_UMID_EXE 指定其路径后重跑。")

    if not only_device:
        print("\n── 账号 token（accounts.json 的 accounts 数组）──")
        if accounts:
            for acc in accounts:
                print("  [%s] %s  %s" % (acc.get("region") or "未知",
                                         acc.get("name") or "-",
                                         acc.get("baseUrl") or "（无 baseUrl）"))
            if no_write:
                print("  预览模式，未写回 accounts.json。")
            else:
                n = write_accounts(accounts, account_name=account_name)
                stats["token_ok"] = True
                stats["accounts"] = n
                print("  已写入 accounts.json（%d 个账号，按 uid/refreshToken 去重）" % n)
        else:
            print("  未读到有效会话：确认客户端已登录，且脚本与客户端是同一 Windows 用户。")
    return stats


def main(argv):
    args = [a.lower() for a in argv]
    only_device = "--device" in args and "--token" not in args
    only_token = "--token" in args and "--device" not in args
    no_write = "--no-write" in args or "--dry-run" in args
    account_name = ""
    for i, a in enumerate(argv):
        if a.lower() == "--name" and i + 1 < len(argv):
            account_name = argv[i + 1]
            break
    if "--json" in args:
        out = {}
        if not only_token:
            out["devices"] = build_all_devices()
        if not only_device:
            out["accounts"] = collect_accounts()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    run(only_device=only_device, only_token=only_token,
        no_write=no_write, account_name=account_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
