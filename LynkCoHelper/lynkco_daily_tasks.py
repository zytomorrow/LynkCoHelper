# -*- coding: utf-8 -*-
"""
领克App 每日任务编排入口（签到 + 分享 + 积分查询 + 结果通知）。

- run_daily_tasks()：编排"查积分 -> 签到 -> 分享 -> 再查积分对比"，返回结构化结果字典。
- run_and_notify()：加载 token -> 执行每日任务 -> 组装 Markdown -> 推送到企业微信。
  即使每日任务中途失败，也会推送一条"执行失败"通知（含已执行的部分结果），
  保证不会出现"静默失败、收不到任何消息"的情况。

签到/分享成功后 myEnergy 接口的积分有几秒异步延迟，故查询"之后积分"前会先 sleep。

用法:
    python3 lynkco_daily_tasks.py    # 执行每日任务（签到+分享）并推送企业微信通知
"""
import json
import os
import sys
import time

from lynkco_common import env_value, mask_sensitive
from lynkco_login import load_token
from lynkco_notify import build_markdown_report, send_wecom_notification
from lynkco_sign import LynkCoSignClient
from lynkco_share import LynkCoShareClient

# 签到/分享完成后，等待多久再查询"之后积分"，单位秒。经真机验证 3~5 秒足够
# 让服务端把能量体变化同步到 myEnergy 接口，可通过环境变量覆盖。
ENERGY_REFRESH_DELAY_SECONDS = float(env_value("LYNKCO_ENERGY_DELAY", "5"))

EP_MY_ENERGY = "/app/energy/myEnergy"


def get_my_energy(client: LynkCoSignClient) -> dict:
    """查询当前账号的能量体积分（GET /app/energy/myEnergy，与签到共用同一网关/签名体系）。"""
    resp = client._request("GET", EP_MY_ENERGY)
    return resp.json()


def run_daily_tasks(token: str, do_share: bool = True, result: dict = None) -> dict:
    """
    编排执行一次完整的"签到 + 分享"流程，返回汇总结果字典：
    {"energy_before", "day_info", "already_signed", "sign_result", "continue_info",
     "share_result", "energy_after"}（已签到/do_share=False 时对应字段为 None）。

    do_share 默认 True，供需要在测试/脚本中跳过分享的调用方使用；
    正式入口 run_and_notify() 始终以 do_share=True 调用。

    result 参数：允许调用方传入一个 dict，用于在异常发生时保留"已执行的部分结果"
    （例如签到已成功、但后续查询积分失败时，仍能把这些信息带进失败通知里）。
    """
    if result is None:
        result = {}

    sign_client = LynkCoSignClient(token)

    result["energy_before"] = get_my_energy(sign_client)

    day_info = sign_client.get_sign_day_info()
    result["day_info"] = day_info

    already_signed = (day_info.get("data") or {}).get("signStatus") == 1
    result["already_signed"] = already_signed

    if already_signed:
        result["sign_result"] = None
    else:
        result["sign_result"] = sign_client.do_sign()

    result["continue_info"] = sign_client.get_continue_days()

    if do_share:
        try:
            share_client = LynkCoShareClient(token)
            result["share_result"] = share_client.do_share()
        except Exception as e:
            result["share_result"] = {"ok": False, "detail": {"message": f"分享任务异常: {e}"}}
    else:
        result["share_result"] = None

    # 积分变化有异步延迟，等待片刻再查询，避免看到"没有变化"的假象。
    if not already_signed or (do_share and (result.get("share_result") or {}).get("ok")):
        time.sleep(ENERGY_REFRESH_DELAY_SECONDS)
    result["energy_after"] = get_my_energy(sign_client)

    return result


def _build_failure_markdown(error_message: str, partial_result: dict) -> str:
    """生成失败通知，尽量把已执行的部分结果也带上（企业微信 markdown 不支持代码块）。"""
    lines = [
        "## ❌ 领克App · 每日任务执行失败",
        f"> 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**错误信息**：{error_message}",
    ]
    shown = {k: v for k, v in partial_result.items()
             if k not in ("error", "notify_result")}
    if shown:
        partial_text = json.dumps(mask_sensitive(shown), ensure_ascii=False, indent=2)
        if len(partial_text) > 1500:
            partial_text = partial_text[:1500] + "\n... (已截断)"
        lines += ["", "**已执行的部分结果**：", partial_text]
    lines += ["", "请检查 token 是否过期、网络/代理是否正常，或查看运行日志。"]
    return "\n".join(lines)


def run_and_notify() -> dict:
    """
    完整入口：加载 token -> 执行每日任务(run_daily_tasks) -> 组装 Markdown
    -> 推送到企业微信应用（lynkco_notify 模块）。

    每日任务固定包含"签到+分享"两项（分享接口每日有加分次数上限，重复
    调用不会重复加分，详见 docs/分享任务接口说明.md）。

    与旧版本的区别：每日任务执行失败（token 加载失败、网络异常、接口报错等）
    时，仍会推送一条"执行失败"通知，并附带已执行的部分结果；推送完成后
    再以 RuntimeError 退出，保证 CI 能感知失败，但不会出现"静默失败"。

    返回值同 run_daily_tasks()，并额外附带 "notify_result" 字段（企业微信
    推送接口的响应，或 {"skipped": True}）。失败时额外附带 "error" 字段。
    """
    result = {}
    error_message = None

    try:
        token = load_token()
        print("=== 执行每日任务（签到+分享）===")
        run_daily_tasks(token, do_share=True, result=result)
        print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        print(f"[错误] 每日任务执行失败: {error_message}")

    # 组装推送内容：成功走原报告，失败走失败报告（报告本身生成失败也兜底）。
    if error_message:
        result["error"] = error_message
        markdown_body = _build_failure_markdown(error_message, result)
    else:
        try:
            markdown_body = build_markdown_report(result)
        except Exception as e:
            error_message = f"生成报告失败: {e}"
            result["error"] = error_message
            markdown_body = _build_failure_markdown(error_message, result)

    print("\n=== 推送内容预览 ===")
    print(markdown_body)

    try:
        notify_result = send_wecom_notification(
            title="领克App · 每日任务",
            markdown_body=markdown_body,
        )
    except Exception as e:
        # 推送失败（网络问题/代理超时等）不应影响签到/分享本身已经成功执行
        # 这一事实，只记录警告，不让整个流程以异常状态退出。
        print(f"[警告] 企业微信推送失败（不影响签到/分享结果）: {e}")
        notify_result = {"skipped": True, "error": str(e)}
    print("\n=== 企业微信推送结果 ===")
    print(json.dumps(mask_sensitive(notify_result), ensure_ascii=False, indent=2))

    result["notify_result"] = notify_result

    # 通知已发出，再以异常方式退出，保证 CI/调度器能感知本次执行失败。
    if error_message:
        raise RuntimeError(f"每日任务执行失败: {error_message}")
    return result


def main():
    try:
        run_and_notify()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[错误] 执行失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()