# -*- coding: utf-8 -*-
"""
通知工具模块，提供每日任务 Markdown 文案及 Bark / 企业微信应用消息推送。

Bark 使用环境变量 LYNKCO_BARK_KEY 或 env.json 的 notify.barkKey。
企业微信应用消息使用 LYNKCO_WECOM_CORP_ID、LYNKCO_WECOM_AGENT_ID、
LYNKCO_WECOM_SECRET、LYNKCO_WECOM_TO_USER，或对应的 env.json notify 字段。
未配置完整时跳过推送并打印提示。
"""
import re

import requests

from lynkco_common import env_value, load_env_data

BARK_DEFAULT_BASE = "https://api.day.app"
BARK_CONNECT_TIMEOUT = 5
BARK_READ_TIMEOUT = 15
WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_CONNECT_TIMEOUT = 5
WECOM_READ_TIMEOUT = 15


def _extract_point(energy_resp: dict) -> str:
    """从 myEnergy 响应中安全地取出 point 字段，取不到时返回 '?'。"""
    return str((energy_resp.get("data") or {}).get("point", "?"))


def build_markdown_report(result: dict) -> str:
    """
    把 lynkco_daily_tasks.run_daily_tasks() 返回的结果字典组装成一段
    Bark markdown 推送内容。
    """
    lines = []

    # --- 签到 ---
    if result.get("already_signed"):
        lines.append("### ℹ️ 签到")
        lines.append("- 今日已签到，无需重复签到")
    else:
        sign_result = result.get("sign_result") or {}
        sign_ok = bool(sign_result.get("success"))
        sign_data = sign_result.get("data") or {}
        if sign_ok:
            lines.append("### ✅ 签到成功")
            reward = sign_data.get("rewardEnergyNumber")
            if reward is not None:
                lines.append(f"- 本次奖励能量体：**+{reward}**")
        else:
            lines.append("### ❌ 签到失败")
            lines.append(f"- {sign_result.get('message', '未知错误')}")

    continue_data = (result.get("continue_info") or {}).get("data") or {}
    continue_days = continue_data.get("continueDays")
    sign_card = continue_data.get("signCardNumber")
    if continue_days is not None:
        lines.append(f"- 连续签到：**{continue_days} 天**")
    if sign_card is not None:
        lines.append(f"- 签到卡剩余：**{sign_card} 张**")

    # --- 分享 ---
    share_result = result.get("share_result")
    if share_result is not None:
        lines.append("\n### 🔗 分享任务")
        if share_result.get("ok"):
            lines.append("- 状态：**上报成功**")
            article_title = share_result.get("articleTitle")
            if article_title:
                lines.append(f"- 分享文章：{article_title}")
        else:
            detail = share_result.get("detail") or {}
            lines.append(f"- 状态：**失败**（{detail.get('message', '详情见日志')}）")

    # --- 积分变化 ---
    point_before = _extract_point(result.get("energy_before") or {})
    point_after = _extract_point(result.get("energy_after") or {})
    lines.append("\n### 💰 积分变化")
    try:
        delta = int(point_after) - int(point_before)
        delta_str = f"（+{delta}）" if delta > 0 else (f"（{delta}）" if delta < 0 else "（无变化）")
    except (ValueError, TypeError):
        delta_str = ""
    lines.append(f"- {point_before} → **{point_after}** {delta_str}".rstrip())

    return "\n".join(lines)


def send_bark_notification(title: str, markdown_body: str, group: str = "LynkCo签到",
                            icon: str = None, level: str = "active",
                            bark_key: str = None, open_url: str = None) -> dict:
    """
    通过 Bark 发送一条 Markdown 格式的推送通知。level 可选
    "critical"/"active"/"timeSensitive"/"passive"。bark_key 不传则读取
    环境变量 LYNKCO_BARK_KEY，未配置时返回 {"skipped": True} 且不抛异常。
    """
    bark_key = bark_key or env_value("LYNKCO_BARK_KEY") or load_env_data().get("notify", {}).get("barkKey", "").strip()
    if not bark_key:
        print("[提示] 未配置 LYNKCO_BARK_KEY，跳过 Bark 推送。")
        return {"skipped": True}

    url = f"{BARK_DEFAULT_BASE}/{bark_key}"

    payload = {
        "title": title,
        "markdown": markdown_body,
        "group": group,
        "level": level,
    }
    if icon:
        payload["icon"] = icon
    if open_url:
        payload["url"] = open_url

    # requests may inherit a desktop-wide proxy that can connect but never
    # return Bark's response. Bark is directly reachable, so use a clean
    # session and separate short connect/read timeouts for this notification.
    session = requests.Session()
    session.trust_env = False
    resp = session.post(
        url, json=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=(BARK_CONNECT_TIMEOUT, BARK_READ_TIMEOUT),
    )
    resp.raise_for_status()
    return resp.json()


def send_wecom_notification(title: str, markdown_body: str, corp_id: str = None,
                            agent_id: str = None, corp_secret: str = None,
                            to_user: str = None) -> dict:
    """向企业微信自建应用发送纯文本消息；to_user 可为成员 ID 或 @all。"""
    notify_config = load_env_data().get("notify") or {}
    corp_id = corp_id or env_value("LYNKCO_WECOM_CORP_ID") or str(notify_config.get("wecomCorpId") or "").strip()
    agent_id = agent_id or env_value("LYNKCO_WECOM_AGENT_ID") or str(notify_config.get("wecomAgentId") or "").strip()
    corp_secret = corp_secret or env_value("LYNKCO_WECOM_SECRET") or str(notify_config.get("wecomSecret") or "").strip()
    to_user = to_user or env_value("LYNKCO_WECOM_TO_USER") or str(notify_config.get("wecomToUser") or "").strip()

    if not all((corp_id, agent_id, corp_secret, to_user)):
        print("[提示] 企业微信推送未配置完整，需设置 CorpID、AgentID、Secret 和接收成员，跳过推送。")
        return {"skipped": True}

    token_resp = requests.get(
        f"{WECOM_API_BASE}/gettoken",
        params={"corpid": corp_id, "corpsecret": corp_secret},
        timeout=(WECOM_CONNECT_TIMEOUT, WECOM_READ_TIMEOUT),
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()
    if token_data.get("errcode") != 0:
        raise RuntimeError(f"企业微信获取 access_token 失败: {token_data.get('errmsg', '未知错误')}")

    plain_body = re.sub(r"(?m)^#{1,6}\s*", "", markdown_body)
    plain_body = re.sub(r"(?m)^\s*[-*]\s+", "· ", plain_body)
    plain_body = re.sub(r"\*\*(.*?)\*\*", r"\1", plain_body)
    plain_body = re.sub(r"`([^`]*)`", r"\1", plain_body)
    content = f"{title}\n\n{plain_body}"
    message_resp = requests.post(
        f"{WECOM_API_BASE}/message/send",
        params={"access_token": token_data["access_token"]},
        json={
            "touser": to_user,
            "msgtype": "text",
            "agentid": int(agent_id),
            "text": {"content": content},
        },
        timeout=(WECOM_CONNECT_TIMEOUT, WECOM_READ_TIMEOUT),
    )
    message_resp.raise_for_status()
    result = message_resp.json()
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信消息发送失败: {result.get('errmsg', '未知错误')}")
    return result
