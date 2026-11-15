#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qoder 每日签到 · 国际版 + 国内版 · 盒子入口（main.py）
════════════════════════════════════════════════════════════
盒子启动本脚本时，把参数 JSON 的路径塞进命令行第一个位置参数：

    python main.py "C:/Users/xxx/AppData/Local/Temp/xxxx.json"

按 params.mode 分派：
  auto    （默认）本机存在 Qoder 客户端时先「提取」设备标识 + 账号 token，再「签到」；
                  不存在则直接用已有 accounts.json 签到。
  extract        仅提取并写回 config.json / accounts.json。
  checkin        仅签到。

params.edition 控制范围：auto＝国际版+国内版都签；intl＝仅国际版；cn＝仅国内版。
每个账号带自己的接口地址与设备标识，互不串线。

结果信封写到 environment.output_json，格式为 {code, msg, ...业务键}：
  code 0＝全部成功，1＝没有账号，2＝有账号失败（需重新登录 / 查询失败）。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MODES = ("auto", "checkin", "extract")
EDITIONS = ("auto", "intl", "cn")


def _load_payload():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python main.py <参数JSON路径>")
    with open(sys.argv[1], "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _apply_params(params):
    import qoder_core as Q
    base = str(params.get("base_url", "") or "").strip()
    if base:
        # 只是「没有 baseUrl 的账号」的兜底；自动提取的账号会带上各自正确的端点
        Q.CONFIG["baseUrls"] = [u.strip() for u in base.split(",") if u.strip()]
    try:
        Q.CONFIG["refreshMarginH"] = int(params.get("refresh_margin_h", 72))
    except (TypeError, ValueError):
        pass


def _pause_before_exit(env, seconds=5):
    """仅手动运行（manual）时倒计时，方便看结果；定时（scheduled）与节点（node）直接退出。"""
    if str(env.get("invoke_mode", "")).strip().lower() != "manual":
        return
    try:
        for i in range(seconds, 0, -1):
            sys.stdout.write("\r窗口将在 %d 秒后自动关闭..." % i)
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()
    except Exception:
        pass


def _write_envelope(env, code, msg, extra=None):
    out = env.get("output_json")
    if not out:
        return
    data = {"code": int(code), "msg": str(msg)}
    if isinstance(extra, dict):
        data.update(extra)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception as e:
        print("⚠️ 结果信封写入失败: %s" % e)


def _extract(mode):
    """返回 (stats, 错误信息)。auto 模式检测不到客户端时静默跳过。"""
    try:
        import qoder_extract as E
        return E.run(only_device=False, only_token=False, no_write=False), ""
    except Exception as e:
        if mode == "extract":
            print("⚠️ 提取失败：%s" % e)
        return {"device_ok": False, "token_ok": False, "accounts": 0}, str(e)


def main():
    payload = _load_payload()
    env = payload.get("environment", {}) or {}
    params = payload.get("params", {}) or {}

    mode = str(params.get("mode", "auto") or "auto").strip().lower()
    if mode not in MODES:
        mode = "auto"
    edition = str(params.get("edition", "auto") or "auto").strip().lower()
    if edition not in EDITIONS:
        edition = "auto"

    # 1) 先「提取」——务必在 import qoder_core 之前，否则 core 会读到旧 config.json
    extract_stats = None
    if mode in ("auto", "extract"):
        if mode == "auto":
            try:
                import qoder_extract as E
                if not E.client_present():
                    print("ℹ️ 未检测到 Qoder 客户端，跳过自动提取，直接用已有 accounts.json 签到。")
                    extract_stats = {"device_ok": False, "token_ok": False,
                                     "accounts": 0, "skipped": True}
                else:
                    extract_stats = E.run()
            except Exception as e:
                print("⚠️ 提取失败：%s" % e)
                extract_stats = {"device_ok": False, "token_ok": False, "accounts": 0}
        else:
            extract_stats, err = _extract(mode)

    # 仅提取模式：到此结束
    if mode == "extract":
        dev_ok = bool(extract_stats and extract_stats.get("device_ok"))
        tok_ok = bool(extract_stats and extract_stats.get("token_ok"))
        n = int((extract_stats or {}).get("accounts") or 0)
        regions = (extract_stats or {}).get("regions") or []
        if not (dev_ok or tok_ok):
            msg = "提取未获得有效数据（请确认已安装并登录 Qoder 客户端）"
            code = 1
        else:
            msg = "提取完成：设备%s · 账号%d%s" % (
                "✓" if dev_ok else "×", n,
                ("（%s）" % "、".join(regions)) if regions else "")
            code = 0
        _write_envelope(env, code, msg, {
            "device_ok": dev_ok, "token_ok": tok_ok, "accounts": n,
            "regions": regions})
        _pause_before_exit(env)
        return code

    # 2) 签到（auto / checkin）：此时才 import qoder_core，读到的才是最新 config
    import qoder_core as Q
    _apply_params(params)
    summary = Q.run_all(edition=edition)

    _write_envelope(env, summary["code"], summary["msg"], {
        "total": summary["total"], "claimed": summary["claimed"],
        "already": summary["already"], "pending": summary["pending"],
        "bad": summary["bad"], "credits": summary["credits"],
        "details": summary["details"]})
    _pause_before_exit(env)
    return summary["code"]


if __name__ == "__main__":
    sys.exit(main())
